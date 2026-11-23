"""Windows 全局快捷键监听。"""

import ctypes
from ctypes import wintypes as w
import threading

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
    """监听实体鼠标 5 和 F10，并转交 Windows 正常处理。"""

    def __init__(self, events):
        self.events = events
        self.thread_id = None
        self.pressed = False
        self.mouse_pressed = False
        self.modified = False
        self.closing = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="Hotkeys")
        self.thread.start()

    def on_toggle(self):
        self.events.put(("toggle", None))

    def keyboard_event(self, message, event, modifiers_down):
        if event.flags & 0x10:  # LLKHF_INJECTED
            return
        down = message in (0x0100, 0x0104)
        if self.pressed and down and event.virtual_key in MODIFIER_EVENTS:
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

    def run(self):
        api = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
        api.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, w.HMODULE, w.DWORD]
        api.SetWindowsHookExW.restype = w.HANDLE
        api.CallNextHookEx.argtypes = [w.HANDLE, ctypes.c_int, w.WPARAM, w.LPARAM]
        api.CallNextHookEx.restype = ctypes.c_ssize_t
        api.UnhookWindowsHookEx.argtypes = [w.HANDLE]
        api.GetAsyncKeyState.argtypes = [ctypes.c_int]
        api.GetAsyncKeyState.restype = w.SHORT
        kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
        kernel.GetModuleHandleW.restype = w.HMODULE

        def modifiers_down():
            return any(api.GetAsyncKeyState(key) & 0x8000 for key in MODIFIER_KEYS)

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

        msg = w.MSG()
        api.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self.thread_id = kernel.GetCurrentThreadId()
        keyboard_hook = mouse_hook = None
        try:
            module = kernel.GetModuleHandleW(None)
            keyboard_hook = api.SetWindowsHookExW(13, keyboard_callback, module, 0)  # WH_KEYBOARD_LL
            if not keyboard_hook:
                raise OSError(f"F10 监听失败（Windows 错误 {ctypes.get_last_error()}）")
            mouse_hook = api.SetWindowsHookExW(14, mouse_callback, module, 0)  # WH_MOUSE_LL
            if not mouse_hook:
                raise OSError(f"鼠标 5 监听失败（Windows 错误 {ctypes.get_last_error()}）")
            self.events.put(("hotkeys", f"{AUTOMATION_HOTKEY}自动总开关已就绪"))
            while not self.closing.is_set():
                result = api.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0 or self.closing.is_set():
                    break
        except Exception as exc:
            self.events.put(("hotkey_error", str(exc)))
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
