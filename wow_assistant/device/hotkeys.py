"""Windows 全局快捷键监听。"""

import ctypes
from ctypes import wintypes as w
import threading

from .raw_input import (
    GIDC_REMOVAL, RIM_TYPEKEYBOARD, RI_KEY_BREAK, RI_MOUSE_BUTTON_5_DOWN, RI_MOUSE_BUTTON_5_UP,
    WM_INPUT, WM_INPUT_DEVICE_CHANGE, RawInput,
)

AUTOMATION_HOTKEY = "鼠标 5 或 F10"
F10_VK = 0x79
MODIFIER_KEYS = (0x10, 0x11, 0x12, 0x5B, 0x5C)
MODIFIER_EVENTS = (*MODIFIER_KEYS, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)


class LowLevelKeyboardEvent(ctypes.Structure):
    _fields_ = [("virtual_key", w.DWORD), ("scan_code", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra_info", ctypes.c_size_t)]


class LowLevelMouseEvent(ctypes.Structure):
    _fields_ = [("point", w.POINT), ("mouse_data", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra_info", ctypes.c_size_t)]


class Hotkeys:
    """接收实体键鼠快捷键，同时保留游戏的正常输入。"""

    def __init__(self, events):
        self.events = events
        self.thread_id = None
        self.pressed = False
        self.mouse_pressed = False
        self.modified = False
        self.raw_keys = {}
        self.raw_modifiers = set()
        self.raw_mouse = set()
        self.closing = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="Hotkeys")
        self.thread.start()

    def on_toggle(self):
        self.events.put(("toggle", None))

    def keyboard_event(self, message, event, modifiers_down):
        if event.flags & 0x10:  # LLKHF_INJECTED
            return
        down = message in (0x0100, 0x0104)
        if down and event.virtual_key in MODIFIER_EVENTS and self.pressed:
            self.modified = True
        if event.virtual_key != F10_VK:
            return
        if down:
            if not self.pressed:
                self.pressed = True
                self.modified = bool(event.flags & 0x20) or modifiers_down()
        elif self.pressed:
            self.pressed = False
            if not self.modified and not (event.flags & 0x20) and not modifiers_down():
                self.on_toggle()

    def mouse_event(self, message, event):
        if event.mouse_data >> 16 != 2 or event.flags & 0x01:  # XBUTTON2, LLMHF_INJECTED
            return
        if message == 0x020B:  # WM_XBUTTONDOWN
            self.mouse_pressed = True
        elif self.mouse_pressed:  # WM_XBUTTONUP
            self.mouse_pressed = False
            self.on_toggle()

    def raw_event(self, packet, modifiers_down):
        device = packet.header.device
        if packet.header.type == RIM_TYPEKEYBOARD:
            keyboard = packet.data.keyboard
            key = keyboard.key
            down = not (keyboard.flags & RI_KEY_BREAK)
            if key in MODIFIER_EVENTS:
                identity = device, key, keyboard.scan, keyboard.flags & 0x06
                if down:
                    self.raw_modifiers.add(identity)
                    for pressed in self.raw_keys:
                        self.raw_keys[pressed] = False
                else:
                    self.raw_modifiers.discard(identity)
            elif key == F10_VK:
                identity = device, key
                if down:
                    if identity not in self.raw_keys:
                        self.raw_keys[identity] = not self.raw_modifiers and not modifiers_down()
                elif self.raw_keys.pop(identity, False) and not self.raw_modifiers and not modifiers_down():
                    self.on_toggle()
        else:
            buttons = packet.data.mouse.buttons
            if buttons & RI_MOUSE_BUTTON_5_DOWN:
                self.raw_mouse.add(device)
            if buttons & RI_MOUSE_BUTTON_5_UP and device in self.raw_mouse:
                self.raw_mouse.discard(device)
                self.on_toggle()

    def raw_device_removed(self, device):
        self.raw_mouse.discard(device)
        self.raw_keys = {identity: allowed for identity, allowed in self.raw_keys.items() if identity[0] != device}
        self.raw_modifiers = {identity for identity in self.raw_modifiers if identity[0] != device}

    def run(self):
        api = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        api.GetMessageW.restype = ctypes.c_int
        api.GetAsyncKeyState.argtypes = [ctypes.c_int]
        api.GetAsyncKeyState.restype = w.SHORT

        def modifiers_down():
            return any(api.GetAsyncKeyState(key) & 0x8000 for key in MODIFIER_KEYS)

        msg = w.MSG()
        api.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self.thread_id = kernel.GetCurrentThreadId()
        try:
            try:
                raw = RawInput(api)
            except OSError as exc:
                self._run_hooks(api, kernel, msg, modifiers_down, exc)
            else:
                try:
                    self.events.put(("hotkeys", f"Windows 实体键鼠监听就绪 · {AUTOMATION_HOTKEY}启停"))
                    self._message_loop(api, msg, lambda: self._read_raw(raw, msg, modifiers_down))
                finally:
                    raw.close()
        except Exception as exc:
            self.events.put(("hotkey_error", str(exc)))

    def _read_raw(self, raw, msg, modifiers_down):
        if msg.hWnd != raw.window:
            return
        if msg.message == WM_INPUT:
            packet = raw.read(msg)
            if packet is not None:
                self.raw_event(packet, modifiers_down)
        elif msg.message == WM_INPUT_DEVICE_CHANGE and msg.wParam == GIDC_REMOVAL:
            self.raw_device_removed(msg.lParam)

    def _message_loop(self, api, msg, on_message=None):
        while not self.closing.is_set():
            result = api.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == -1:
                raise ctypes.WinError(ctypes.get_last_error())
            if result == 0 or self.closing.is_set():
                break
            if on_message is not None:
                on_message()

    def _run_hooks(self, api, kernel, msg, modifiers_down, raw_error):
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
        api.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, w.HMODULE, w.DWORD]
        api.SetWindowsHookExW.restype = w.HANDLE
        api.CallNextHookEx.argtypes = [w.HANDLE, ctypes.c_int, w.WPARAM, w.LPARAM]
        api.CallNextHookEx.restype = ctypes.c_ssize_t
        api.UnhookWindowsHookEx.argtypes = [w.HANDLE]
        kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
        kernel.GetModuleHandleW.restype = w.HMODULE

        @callback_type
        def keyboard_callback(code, message, data):
            if code == 0 and message in (0x0100, 0x0101, 0x0104, 0x0105) and not self.closing.is_set():
                event = ctypes.cast(data, ctypes.POINTER(LowLevelKeyboardEvent)).contents
                self.keyboard_event(message, event, modifiers_down)
            return api.CallNextHookEx(None, code, message, data)

        @callback_type
        def mouse_callback(code, message, data):
            if code == 0 and message in (0x020B, 0x020C) and not self.closing.is_set():
                event = ctypes.cast(data, ctypes.POINTER(LowLevelMouseEvent)).contents
                self.mouse_event(message, event)
            return api.CallNextHookEx(None, code, message, data)

        keyboard_hook = mouse_hook = None
        try:
            module = kernel.GetModuleHandleW(None)
            keyboard_hook = api.SetWindowsHookExW(13, keyboard_callback, module, 0)  # WH_KEYBOARD_LL
            if not keyboard_hook:
                raise OSError(f"F10 监听失败（Windows 错误 {ctypes.get_last_error()}）")
            mouse_hook = api.SetWindowsHookExW(14, mouse_callback, module, 0)  # WH_MOUSE_LL
            if not mouse_hook:
                raise OSError(f"鼠标 5 监听失败（Windows 错误 {ctypes.get_last_error()}）")
            self.events.put(("hotkeys", f"Windows 键鼠兼容监听就绪 · 原始输入注册失败 · {raw_error}"))
            self._message_loop(api, msg)
        finally:
            if mouse_hook:
                api.UnhookWindowsHookEx(mouse_hook)
            if keyboard_hook:
                api.UnhookWindowsHookEx(keyboard_hook)

    def close(self):
        self.closing.set()
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        self.thread.join(timeout=0.5)
