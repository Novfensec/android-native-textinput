package org.novfensec.android;

/**
 * KeyboardProxy - Android IME bridge for Kivy TextInput.
 *
 * This is an invisible 1x1 View that owns the IME focus on Android.
 * When Kivy TextInput gets Python-level focus it hands control to this
 * view instead of going through SDL.  The view implements a real Android
 * InputConnection (BaseInputConnection) so the soft keyboard, autocomplete,
 * SwiftKey-style gesture typing and composing text all work correctly.
 *
 * Events flow:
 *   [Android IME] -> [KeyboardProxy (Java)] -> [TextInputListener]
 *                                                       |
 *                                               Implemented in Python
 *                                               via pyjnius
 *
 * Python usage (see app.py):
 *   from jnius import autoclass
 *   KeyboardProxy = autoclass('org.novfensec.android.KeyboardProxy')
 *   proxy = KeyboardProxy(activity)
 *   proxy.setListener(my_python_listener)
 *   proxy.show(input_type_str, show_suggestions)
 *   proxy.hide()
 *
 * Build: // ksproject automatically parses `.java` into AGP.
 *   uv sync
 *   uv run ksproject android build
 */

import android.app.Activity;
import android.content.Context;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.BaseInputConnection;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.ExtractedText;
import android.view.inputmethod.ExtractedTextRequest;
import android.view.inputmethod.InputConnection;
import android.view.inputmethod.InputMethodManager;
import android.util.Log;

public class KeyboardProxy {

    private static final String TAG = "KeyboardProxy";

    // -------------------------------------------------------------------------
    //  Listener interface - implemented on the Python side
    // -------------------------------------------------------------------------

    /** All callbacks are invoked on the UI thread. */
    public interface TextInputListener {
        /**
         * A string of one or more characters was committed by the IME.
         * Replaces the SDL on_textinput event.
         */
        void onText(String text);

        /**
         * Composing text changed (mid-gesture or mid-IME session).
         * @param composing  Current composing string (may be empty to signal end).
         * @param deleteBack How many previously-committed composing chars
         *                   to delete before inserting composing.
         */
        void onComposing(String composing, int deleteBack);

        /**
         * A special-key action was performed.
         * @param action  One of: "backspace", "enter", "del", "escape",
         *                "left", "right", "up", "down",
         *                "home", "end", "pageup", "pagedown"
         */
        void onAction(String action);

        /**
         * Delete committed text around the cursor.
         * @param beforeLength Number of characters to delete before the cursor.
         * @param afterLength Number of characters to delete after the cursor.
         */
        void onDeleteSurrounding(int beforeLength, int afterLength);

        /** The IME was dismissed (back button or Done). */
        void onHide();
    }

    // -------------------------------------------------------------------------
    //  The invisible proxy view
    // -------------------------------------------------------------------------

    private final class ProxyView extends View {

        ProxyView(Context ctx) {
            super(ctx);
            setFocusable(true);
            setFocusableInTouchMode(true);
        }

        @Override
        public boolean onCheckIsTextEditor() {
            return true;
        }

        @Override
        public InputConnection onCreateInputConnection(EditorInfo outAttrs) {
            outAttrs.inputType = _currentInputType;
            outAttrs.imeOptions = EditorInfo.IME_FLAG_NO_EXTRACT_UI;
            
            if (!_multiline) {
                outAttrs.imeOptions |= EditorInfo.IME_ACTION_DONE;
            } else {
                outAttrs.imeOptions |= EditorInfo.IME_ACTION_NONE;
            }

            if ((outAttrs.inputType & InputType.TYPE_MASK_CLASS) == InputType.TYPE_CLASS_TEXT) {
                if (!_showSuggestions) {
                    outAttrs.inputType |= InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS;
                    outAttrs.inputType |= InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD;
                }
            }

            return new BaseInputConnection(this, false) {

                // How many chars are currently in-flight as composing text
                private int composingLength = 0;

                // -- Composing lifecycle -------------------------------------

                @Override
                public boolean finishComposingText() {
                    composingLength = 0;
                    return super.finishComposingText();
                }

                @Override
                public boolean setComposingRegion(int start, int end) {
                    composingLength = Math.abs(end - start);
                    return super.setComposingRegion(start, end);
                }

                @Override
                public boolean setComposingText(CharSequence text,
                                                int newCursorPosition) {
                    String s = text.toString();
                    
                    int start = Math.min(_selStart, _selEnd);
                    int end = Math.max(_selStart, _selEnd);
                    if (composingLength > 0) {
                        start = Math.max(0, start - composingLength);
                    }
                    try {
                        if (start >= 0 && end <= _currentText.length() && start <= end) {
                            _currentText = _currentText.substring(0, start) + s + _currentText.substring(end);
                            _selStart = _selEnd = start + s.length();
                        }
                    } catch (Exception e) {}

                    if (_listener != null) {
                        _listener.onComposing(s, composingLength);
                    }
                    composingLength = s.length();
                    return true;
                }

                // -- Committed text ------------------------------------------

                @Override
                public boolean commitText(CharSequence text,
                                          int newCursorPosition) {
                    String s = text.toString();
                    
                    int start = Math.min(_selStart, _selEnd);
                    int end = Math.max(_selStart, _selEnd);
                    if (composingLength > 0) {
                        start = Math.max(0, start - composingLength);
                    }
                    try {
                        if (start >= 0 && end <= _currentText.length() && start <= end) {
                            _currentText = _currentText.substring(0, start) + s + _currentText.substring(end);
                            _selStart = _selEnd = start + s.length();
                        }
                    } catch (Exception e) {}

                    if (_listener != null) {
                        if (composingLength > 0) {
                            // Flush any pending composing chars first
                            _listener.onComposing("", composingLength);
                        }
                        if (!s.isEmpty()) {
                            _listener.onText(s);
                        }
                    }
                    composingLength = 0;
                    return true;
                }

                // -- Context Tracking ----------------------------------------

                @Override
                public CharSequence getTextBeforeCursor(int n, int flags) {
                    int start = Math.min(_selStart, _selEnd);
                    if (start <= 0 || _currentText.isEmpty()) return "";
                    start = Math.min(start, _currentText.length());
                    int begin = Math.max(0, start - n);
                    return _currentText.substring(begin, start);
                }

                @Override
                public CharSequence getTextAfterCursor(int n, int flags) {
                    int end = Math.max(_selStart, _selEnd);
                    if (end < 0 || _currentText.isEmpty()) return "";
                    end = Math.min(end, _currentText.length());
                    int finish = Math.min(_currentText.length(), end + n);
                    return _currentText.substring(end, finish);
                }

                @Override
                public CharSequence getSelectedText(int flags) {
                    int start = Math.min(_selStart, _selEnd);
                    int end = Math.max(_selStart, _selEnd);
                    if (start == end || start < 0 || end > _currentText.length()) return null;
                    return _currentText.substring(start, end);
                }

                @Override
                public ExtractedText getExtractedText(ExtractedTextRequest request, int flags) {
                    ExtractedText et = new ExtractedText();
                    et.text = _currentText;
                    et.selectionStart = _selStart;
                    et.selectionEnd = _selEnd;
                    return et;
                }

                // -- Deletion ------------------------------------------------

                @Override
                public boolean deleteSurroundingText(int beforeLength,
                                                     int afterLength) {
                    if ((beforeLength > 0 || afterLength > 0) && _listener != null) {
                        int start = Math.min(_selStart, _selEnd);
                        int end = Math.max(_selStart, _selEnd);
                        int keepStart = Math.max(0, start - beforeLength);
                        int keepEnd = Math.min(_currentText.length(), end + afterLength);
                        
                        try {
                            if (keepStart >= 0 && keepEnd <= _currentText.length() && keepStart <= keepEnd) {
                                _currentText = _currentText.substring(0, keepStart) + _currentText.substring(keepEnd);
                                _selStart = _selEnd = keepStart;
                            }
                        } catch (Exception e) {}

                        _listener.onDeleteSurrounding(beforeLength, afterLength);
                        return true;
                    }
                    return super.deleteSurroundingText(beforeLength, afterLength);
                }

                // -- Hard key events (physical keyboard or key injection) -----

                @Override
                public boolean sendKeyEvent(KeyEvent event) {
                    if (event.getAction() == KeyEvent.ACTION_DOWN) {
                        String action = keyCodeToAction(event.getKeyCode());
                        if (action != null && _listener != null) {
                            if (action.equals("backspace") && composingLength > 0) {
                                composingLength--;
                            }
                            _listener.onAction(action);
                            return true;
                        }

                        // Fallback: Some keyboards (especially numeric keypads) send digits as KeyEvents
                        int unicode = event.getUnicodeChar();
                        if (unicode != 0 && _listener != null) {
                            _listener.onText(String.valueOf((char) unicode));
                            return true;
                        }
                    }
                    return super.sendKeyEvent(event);
                }

                // -- IME action button (Done / Search / Go ...) --------------

                @Override
                public boolean performEditorAction(int editorAction) {
                    if (_listener != null) {
                        _listener.onAction("enter");
                    }
                    if (!_multiline || editorAction == EditorInfo.IME_ACTION_DONE) {
                        hide();
                    }
                    return true;
                }
            };
        }
    }

    // -------------------------------------------------------------------------
    //  Public API
    // -------------------------------------------------------------------------

    private final Activity  _activity;
    private final ProxyView _proxyView;
    private       int       _currentInputType = InputType.TYPE_CLASS_TEXT;
    private       boolean   _showSuggestions  = true;
    private       boolean   _multiline        = false;
    private       TextInputListener _listener = null;

    private String _currentText = "";
    private int _selStart = 0;
    private int _selEnd = 0;

    private Runnable hideRunnable;

    public KeyboardProxy(Activity activity) {
        _activity  = activity;
        _proxyView = new ProxyView(activity);

        hideRunnable = () -> {
            InputMethodManager imm = getIMM();
            if (imm != null) {
                imm.hideSoftInputFromWindow(_proxyView.getWindowToken(), 0);
            }
            if (_listener != null) {
                _listener.onHide();
            }
        };

        // Add the tiny invisible view so IMM can attach to it
        activity.runOnUiThread(() ->
            activity.addContentView(
                _proxyView,
                new ViewGroup.LayoutParams(1, 1)
            )
        );
    }

    /** Register the Python-side event listener. */
    public void setListener(TextInputListener listener) {
        _listener = listener;
    }

    /**
     * Show the soft keyboard.
     *
     * @param inputTypeStr  One of: "text", "number", "url", "mail",
     *                               "datetime", "tel", "address", "null"
     * @param suggestions   Whether to show IME suggestions / autocorrect.
     */
    public void show(String inputTypeStr, boolean suggestions, boolean multiline) {
        _currentInputType = resolveInputType(inputTypeStr, multiline);
        _showSuggestions  = suggestions;
        _multiline        = multiline;

        _activity.runOnUiThread(() -> {
            _proxyView.removeCallbacks(hideRunnable);
            _proxyView.requestFocus();
            InputMethodManager imm = getIMM();
            if (imm != null) {
                imm.restartInput(_proxyView);   // force InputConnection refresh
                imm.showSoftInput(_proxyView, InputMethodManager.SHOW_IMPLICIT);
            }
        });
    }

    /** Hide the soft keyboard and remove IME focus. */
    public void hide() {
        _proxyView.postDelayed(hideRunnable, 100);
    }

    /** Sync Kivy's cursor position and text back to the IME so it can track where the user touched. */
    public void updateState(int selStart, int selEnd, String text, boolean notifyIMM) {
        _selStart = selStart;
        _selEnd = selEnd;
        if (text != null) {
            _currentText = text;
        }
        if (notifyIMM) {
            InputMethodManager imm = getIMM();
            if (imm != null) {
                imm.updateSelection(_proxyView, selStart, selEnd, -1, -1);
            }
        }
    }

    /** Returns true if the soft keyboard is currently shown (best-effort). */
    public boolean isShown() {
        InputMethodManager imm = getIMM();
        return imm != null && imm.isActive(_proxyView);
    }

    // -------------------------------------------------------------------------
    //  Helpers
    // -------------------------------------------------------------------------

    private InputMethodManager getIMM() {
        return (InputMethodManager)
            _activity.getSystemService(Context.INPUT_METHOD_SERVICE);
    }

    /**
     * Map Kivy's input_type string to an Android InputType flag combination.
     * Mirrors the mapping in Kivy's window SDL2 provider.
     */
    private static int resolveInputType(String type, boolean multiline) {
        if (type == null) return InputType.TYPE_CLASS_TEXT;
        switch (type) {
            case "number":
                return InputType.TYPE_CLASS_NUMBER
                     | InputType.TYPE_NUMBER_FLAG_DECIMAL
                     | InputType.TYPE_NUMBER_FLAG_SIGNED;
            case "url":
                return InputType.TYPE_CLASS_TEXT
                     | InputType.TYPE_TEXT_VARIATION_URI;
            case "mail":
                return InputType.TYPE_CLASS_TEXT
                     | InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS;
            case "datetime":
                return InputType.TYPE_CLASS_DATETIME
                     | InputType.TYPE_DATETIME_VARIATION_NORMAL;
            case "tel":
                return InputType.TYPE_CLASS_PHONE;
            case "address":
                return InputType.TYPE_CLASS_TEXT
                     | InputType.TYPE_TEXT_VARIATION_POSTAL_ADDRESS;
            case "null":
                return InputType.TYPE_NULL;
            default:  // "text" and anything unrecognised
                int flags = InputType.TYPE_CLASS_TEXT
                          | InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
                          | InputType.TYPE_TEXT_FLAG_AUTO_CORRECT;
                if (multiline) {
                    flags |= InputType.TYPE_TEXT_FLAG_MULTI_LINE;
                }
                return flags;
        }
    }

    /**
     * Translate an Android KeyEvent keycode into a Kivy action name.
     * Returns null for keys that should not generate an action event.
     */
    private static String keyCodeToAction(int keyCode) {
        switch (keyCode) {
            case KeyEvent.KEYCODE_DEL:          return "backspace";
            case KeyEvent.KEYCODE_FORWARD_DEL:  return "del";
            case KeyEvent.KEYCODE_ENTER:
            case KeyEvent.KEYCODE_NUMPAD_ENTER: return "enter";
            case KeyEvent.KEYCODE_ESCAPE:       return "escape";
            case KeyEvent.KEYCODE_DPAD_LEFT:    return "left";
            case KeyEvent.KEYCODE_DPAD_RIGHT:   return "right";
            case KeyEvent.KEYCODE_DPAD_UP:      return "up";
            case KeyEvent.KEYCODE_DPAD_DOWN:    return "down";
            case KeyEvent.KEYCODE_MOVE_HOME:    return "home";
            case KeyEvent.KEYCODE_MOVE_END:     return "end";
            case KeyEvent.KEYCODE_PAGE_UP:      return "pageup";
            case KeyEvent.KEYCODE_PAGE_DOWN:    return "pagedown";
            default:                            return null;
        }
    }
}
