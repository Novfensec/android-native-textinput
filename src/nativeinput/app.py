"""
Native TextInput Test App
=========================

Self-contained test for the KeyboardProxy Android IME fix.

What this tests
---------------
- Plain text typing (autocorrect, autocomplete, space bar swipe)
- Gesture / swipe typing (SwiftKey, Gboard glide)
- CJK / Indic composing text
- input_type variants: text, number, url, mail, tel, address
- Done / Search / Go button → on_text_validate
- Backspace, arrow keys, physical keyboard
- Focus/unfocus keyboard show / hide

"""

from kivy.utils import platform as _platform

if _platform == 'android':
    def _patch_window_sdl():
        try:
            from kivy.core.window.window_sdl2 import WindowSDL
            _orig_request = WindowSDL.request_keyboard
            _orig_release = WindowSDL.release_keyboard

            def _patched_request(self, callback, target,
                                 input_type='text',
                                 keyboard_suggestions=True):
                # Call the WindowBase layer (builds the Keyboard object)
                # but NOT the SDL show_keyboard call.
                from kivy.core.window import WindowBase
                kb = WindowBase.request_keyboard(
                    self, callback, target, input_type, keyboard_suggestions
                )
                self._sdl_keyboard = kb
                return kb

            def _patched_release(self, *largs):
                from kivy.core.window import WindowBase
                WindowBase.release_keyboard(self, *largs)
                self._sdl_keyboard = None
                return True

            WindowSDL.request_keyboard = _patched_request
            WindowSDL.release_keyboard = _patched_release
        except Exception as e:
            import logging
            logging.warning('NativeTextInput: SDL patch failed: %s', e)

    _patch_window_sdl()

from kivy.app import App
from kivy.clock import Clock
from kivy.logger import Logger
from kivy.metrics import dp
from kivy.uix.textinput import TextInput
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.widget import Widget
from kivy.uix.spinner import Spinner
from kivy.properties import StringProperty, BooleanProperty


if _platform == 'android':
    _jnius_available = False
    try:
        from jnius import autoclass, java_method, PythonJavaClass
        _jnius_available = True
    except ImportError:
        Logger.warning('NativeTextInput: jnius not available; '
                       'falling back to SDL keyboard.')

    if _jnius_available:
        _PythonActivity = autoclass('org.kivy.android.PythonActivity')
        _KeyboardProxy = autoclass('org.novfensec.android.KeyboardProxy')

        _proxy_instance = None

        def _get_proxy():
            global _proxy_instance
            if _proxy_instance is None:
                activity = _PythonActivity.mActivity
                _proxy_instance = _KeyboardProxy(activity)
            return _proxy_instance

        class _PyListener(PythonJavaClass):
            __javainterfaces__ = [
                'org/novfensec/android/KeyboardProxy$TextInputListener'
            ]
            __javacontext__ = 'app'

            def __init__(self, widget):
                super().__init__()
                self._widget = widget

            @java_method('(Ljava/lang/String;)V')
            def onText(self, text):
                w = self._widget
                if w:
                    Clock.schedule_once(lambda dt: w._proxy_on_text(text), 0)

            @java_method('(Ljava/lang/String;I)V')
            def onComposing(self, composing, delete_back):
                w = self._widget
                if w:
                    Clock.schedule_once(
                        lambda dt: w._proxy_on_composing(composing, delete_back), 0)

            @java_method('(Ljava/lang/String;)V')
            def onAction(self, action):
                w = self._widget
                if w:
                    Clock.schedule_once(
                        lambda dt: w._proxy_on_action(action), 0)

            @java_method('(II)V')
            def onDeleteSurrounding(self, before_length, after_length):
                w = self._widget
                if w is None:
                    return
                Clock.schedule_once(
                    lambda dt: w._proxy_on_delete_surrounding(before_length, after_length), 0)

            @java_method('()V')
            def onHide(self):
                w = self._widget
                if w is None:
                    return
                Clock.schedule_once(lambda dt: w._proxy_on_hide(), 0)


        class AndroidKeyboardProxy:
            """
            Mixin that replaces SDL keyboard handling with the native Android
            IME proxy for any FocusBehavior/TextInput-derived widget.

            Mix in *before* TextInput in the MRO::

                class MyInput(AndroidKeyboardProxy, TextInput):
                    pass

            Or use NativeTextInput which already does this for you.
            """
            _proxy_keyboard_active = False

            def _bind_keyboard(self):
                """Replace SDL request_keyboard with our Java proxy."""
                super()._bind_keyboard()
                self._start_proxy_keyboard()
                self.bind(cursor=self._proxy_update_selection,
                          selection_text=self._proxy_update_selection,
                          text=self._proxy_update_selection)

            def _unbind_keyboard(self):
                """Stop the proxy and let FocusBehavior clean up."""
                self.unbind(cursor=self._proxy_update_selection,
                            selection_text=self._proxy_update_selection,
                            text=self._proxy_update_selection)
                self._stop_proxy_keyboard()
                super()._unbind_keyboard()

            def _proxy_update_selection(self, *largs):
                if not getattr(self, '_proxy_keyboard_active', False):
                    return
                if self._selection:
                    sel_start = min(self._selection_from, self._selection_to)
                    sel_end = max(self._selection_from, self._selection_to)
                else:
                    sel_start = self.cursor_index()
                    sel_end = sel_start
                
                notify_imm = not getattr(self, '_proxy_ime_updating', False)
                _get_proxy().updateState(sel_start, sel_end, self.text, notify_imm)

            def _start_proxy_keyboard(self):
                if not _jnius_available:
                    return
                proxy = _get_proxy()
                listener = _PyListener(self)
                self._proxy_listener = listener
                proxy.setListener(listener)
                input_type = getattr(self, 'input_type', 'text') or 'text'
                suggestions = getattr(self, 'keyboard_suggestions', True)
                multiline = getattr(self, 'multiline', False)
                proxy.show(input_type, suggestions, multiline)
                self._proxy_keyboard_active = True
                Logger.debug(
                    'KeyboardProxy: started for %r (input_type=%s)',
                    self, input_type
                )

            def _stop_proxy_keyboard(self):
                if not _jnius_available:
                    return
                if self._proxy_keyboard_active:
                    _get_proxy().hide()
                    self._proxy_keyboard_active = False
                    self._proxy_listener = None
                    Logger.debug('KeyboardProxy: stopped for %r', self)

            def _proxy_on_text(self, text):
                """Plain committed text from the IME."""
                self._proxy_ime_updating = True
                if self._selection:
                    self.delete_selection()
                self.insert_text(text, False)
                self._proxy_ime_updating = False

            def _proxy_on_composing(self, composing, delete_back):
                """
                Composing text update: first delete delete_back chars
                then insert composing (which may be empty to just delete).
                """
                self._proxy_ime_updating = True
                if delete_back > 0:
                    ci = self.cursor_index()
                    self._selection_from = max(0, ci - delete_back)
                    self._selection_to = ci
                    self._selection = True
                    self.delete_selection()
                elif self._selection:
                    self.delete_selection()

                if composing:
                    self.insert_text(composing, False)
                self._proxy_ime_updating = False

            def _proxy_on_action(self, action):
                """Special-key action from the IME or physical keyboard."""
                _action_map = {
                    'backspace': 'backspace',
                    'del':       'del',
                    'enter':     'enter',
                    'escape':    'escape',
                    'left':      'cursor_left',
                    'right':     'cursor_right',
                    'up':        'cursor_up',
                    'down':      'cursor_down',
                    'home':      'cursor_home',
                    'end':       'cursor_end',
                    'pageup':    'cursor_pgup',
                    'pagedown':  'cursor_pgdn',
                }
                kivy_action = _action_map.get(action)
                if kivy_action is None:
                    return

                key_tuple = (None, None, kivy_action, 1)
                self._key_down(key_tuple)

            def _proxy_on_delete_surrounding(self, before_length, after_length):
                """Batch deletion from the IME (e.g. long-press delete / one-tap clear)."""
                self._proxy_ime_updating = True
                
                if self._selection:
                    sel_start = min(self._selection_from, self._selection_to)
                    sel_end = max(self._selection_from, self._selection_to)
                else:
                    sel_start = sel_end = self.cursor_index()

                keep_start = max(0, sel_start - before_length)
                keep_end = min(len(self.text), sel_end + after_length)

                if after_length > 0 and keep_end > sel_end:
                    self._selection_from = sel_end
                    self._selection_to = keep_end
                    self._selection = True
                    self.delete_selection()

                if before_length > 0 and sel_start > keep_start:
                    self._selection_from = keep_start
                    self._selection_to = sel_start
                    self._selection = True
                    self.delete_selection()

                self._proxy_ime_updating = False

            def _proxy_on_hide(self):
                """IME dismissed (back button or Done)."""
                if self.focus:
                    self.focus = False

    else:
        class AndroidKeyboardProxy:
            pass

else:
    class AndroidKeyboardProxy:
        pass


class NativeTextInput(AndroidKeyboardProxy, TextInput):
    """
    Drop-in for TextInput.
    On Android uses KeyboardProxy.java for IME; everywhere else is
    identical to TextInput.
    """
    pass


# ============================================================================
#  Test app UI
# ============================================================================

LOG_HISTORY = []

KV_STR = """
#: import dp kivy.metrics.dp

<LogPanel>:
    orientation: 'vertical'
    canvas.before:
        Color:
            rgba: 0.08, 0.08, 0.12, 1
        Rectangle:
            pos: self.pos
            size: self.size

<RoundedInput@NativeTextInput>:
    size_hint_y: None
    height: dp(48)
    padding: [dp(14), dp(12)]
    font_size: dp(15)
    background_color: 0.13, 0.13, 0.20, 1
    foreground_color: 0.95, 0.95, 1, 1
    hint_text_color: 0.45, 0.45, 0.65, 1
    cursor_color: 0.4, 0.6, 1, 1
    selection_color: 0.2, 0.4, 0.9, 0.4

<SectionLabel>:
    size_hint_y: None
    height: dp(30)
    font_size: dp(11)
    color: 0.4, 0.55, 0.9, 1
    bold: True
    halign: 'left'
    valign: 'middle'
    text_size: self.size
"""

from kivy.lang import Builder


class LogPanel(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._log_label = None

    def setup(self, log_label):
        self._log_label = log_label

    def log(self, msg):
        LOG_HISTORY.append(msg)
        if len(LOG_HISTORY) > 60:
            LOG_HISTORY.pop(0)
        if self._log_label:
            self._log_label.text = '\n'.join(reversed(LOG_HISTORY[-20:]))


class NativeTextInputTestApp(App):

    def build(self):
        Builder.load_string(KV_STR)
        self._log_panel = None
        return self._build_ui()

    def _build_ui(self):
        root = BoxLayout(orientation='vertical', padding=dp(8), spacing=dp(6))

        title = Label(
            text='Native TextInput Test',
            size_hint_y=None,
            height=dp(40),
            font_size=dp(17),
            bold=True,
            color=(0.55, 0.75, 1, 1),
        )
        root.add_widget(title)

        scroll = ScrollView(size_hint=(1, 0.55))
        grid = GridLayout(
            cols=1,
            spacing=dp(8),
            padding=[dp(4), dp(4)],
            size_hint_y=None,
        )
        grid.bind(minimum_height=grid.setter('height'))

        def section(text):
            lbl = Label(
                text=text,
                size_hint_y=None, height=dp(26),
                font_size=dp(11), bold=True,
                color=(0.4, 0.55, 0.9, 1),
                halign='left', valign='middle',
            )
            lbl.bind(size=lambda w, s: setattr(w, 'text_size', s))
            grid.add_widget(lbl)

        def make_input(hint, input_type='text', multiline=False,
                       suggestions=True, validate_msg=None):
            ti = NativeTextInput(
                hint_text=hint,
                input_type=input_type,
                multiline=multiline,
                keyboard_suggestions=suggestions,
                size_hint_y=None,
                height=dp(52) if multiline else dp(48),
                padding=[dp(14), dp(12)],
                font_size=dp(15),
                background_color=(0.13, 0.13, 0.20, 1),
                foreground_color=(0.95, 0.95, 1, 1),
                hint_text_color=(0.45, 0.45, 0.65, 1),
                cursor_color=(0.4, 0.6, 1, 1),
                selection_color=(0.2, 0.4, 0.9, 0.4),
            )
            msg = validate_msg or f'[{input_type}] validated: '

            def _on_text(inst, val):
                self._log(f'text({input_type}): {repr(val[-40:])}')

            def _on_validate(inst):
                self._log(f'✓ {msg}{repr(inst.text)}')

            def _on_focus(inst, val):
                state = 'focused' if val else 'unfocused'
                self._log(f'focus({input_type}): {state}')

            ti.bind(text=_on_text,
                    on_text_validate=_on_validate,
                    focus=_on_focus)
            grid.add_widget(ti)
            return ti

        section('TEXT — autocorrect, autocomplete, gesture typing')
        make_input('Type anything here… try swipe/gesture typing',
                   input_type='text', validate_msg='text → ')

        section('TEXT — no suggestions (raw input)')
        make_input('Raw text, no autocorrect', input_type='text',
                   suggestions=False, validate_msg='raw → ')

        section('NUMBER — numeric keypad')
        make_input('Enter a number…', input_type='number',
                   validate_msg='number → ')

        section('URL — .com key, no autocorrect')
        make_input('https://…', input_type='url', validate_msg='url → ')

        section('EMAIL — @ key visible')
        make_input('user@example.com', input_type='mail',
                   validate_msg='mail → ')

        section('TELEPHONE — phone dial pad')
        make_input('+1 (555) 000-0000', input_type='tel',
                   validate_msg='tel → ')

        section('ADDRESS')
        make_input('123 Main Street…', input_type='address',
                   validate_msg='addr → ')

        section('MULTILINE — Enter adds newline')
        ml = NativeTextInput(
            hint_text='Multiline field — Enter = new line, Done to dismiss…',
            multiline=True,
            input_type='text',
            size_hint_y=None,
            height=dp(100),
            padding=[dp(14), dp(12)],
            font_size=dp(15),
            background_color=(0.13, 0.13, 0.20, 1),
            foreground_color=(0.95, 0.95, 1, 1),
            hint_text_color=(0.45, 0.45, 0.65, 1),
            cursor_color=(0.4, 0.6, 1, 1),
            selection_color=(0.2, 0.4, 0.9, 0.4),
        )
        ml.bind(text=lambda i, v: self._log(f'multiline: {len(v)} chars'))
        grid.add_widget(ml)

        scroll.add_widget(grid)
        root.add_widget(scroll)

        # -- Controls row ---------------------------------------------------──
        ctrl_row = BoxLayout(
            size_hint_y=None, height=dp(42), spacing=dp(8)
        )

        clear_btn = Button(
            text='Clear log',
            background_color=(0.2, 0.2, 0.35, 1),
            font_size=dp(13),
        )
        clear_btn.bind(on_release=self._clear_log)
        ctrl_row.add_widget(clear_btn)

        platform_lbl = Label(
            text=f'Platform: {_platform}',
            font_size=dp(12),
            color=(0.5, 0.7, 0.5, 1),
        )
        ctrl_row.add_widget(platform_lbl)

        root.add_widget(ctrl_row)

        # -- Event log ------------------------------------------------------──
        log_scroll = ScrollView(size_hint=(1, 0.35))
        log_bg = BoxLayout(
            orientation='vertical',
            size_hint_y=None,
            padding=[dp(8), dp(6)],
            spacing=dp(2),
        )
        log_bg.bind(minimum_height=log_bg.setter('height'))

        with log_bg.canvas.before:
            from kivy.graphics import Color, Rectangle
            Color(0.05, 0.05, 0.09, 1)
            self._log_rect = Rectangle(pos=log_bg.pos, size=log_bg.size)
        log_bg.bind(pos=lambda w, v: setattr(self._log_rect, 'pos', v),
                    size=lambda w, v: setattr(self._log_rect, 'size', v))

        self._log_label = Label(
            text='Events will appear here…',
            font_size=dp(12),
            color=(0.6, 0.85, 0.6, 1),
            size_hint_y=None,
            halign='left',
            valign='top',
            markup=True,
        )
        self._log_label.bind(
            texture_size=lambda w, ts: setattr(w, 'height', ts[1])
        )
        self._log_label.bind(
            width=lambda w, wd: setattr(w, 'text_size', (wd, None))
        )

        log_bg.add_widget(self._log_label)
        log_scroll.add_widget(log_bg)
        root.add_widget(log_scroll)

        self._log(f'NativeTextInput test app started on {_platform}')
        if _platform == 'android':
            self._log('Android: KeyboardProxy.java IME bridge active')
        else:
            self._log('Non-Android: proxy is no-op, using SDL keyboard')

        return root

    def _log(self, msg):
        Logger.info('NativeTextInput: %s', msg)
        LOG_HISTORY.append(msg)
        if len(LOG_HISTORY) > 60:
            LOG_HISTORY.pop(0)
        if self._log_label:
            self._log_label.text = '\n'.join(reversed(LOG_HISTORY[-25:]))

    def _clear_log(self, *_):
        LOG_HISTORY.clear()
        if self._log_label:
            self._log_label.text = '— log cleared —'

def main():
    NativeTextInputTestApp().run()

if __name__ == '__main__':
    main()