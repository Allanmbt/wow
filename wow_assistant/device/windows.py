"""Keyboard and mouse backends with shared Windows input guards."""

import ctypes
from ctypes import wintypes as w
import json
import os
import threading

from .ch9329 import CH9329Keyboard, DEFAULT_BAUD

from ..paths import config_path

INPUT_CONFIG = config_path("input_config.json")
WIN_MOUSE_FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


class UsbHidKeyboard:
    """Send press/release commands to the CircuitPython USB keyboard bridge."""

    def __init__(self, port):
        self.port = port
        self.link = None

    @property
    def name(self):
        return f"USB HID ({self.port})"

    def _exchange(self, command, expected):
        if self.link.write(command) != len(command) or self.link.read(len(expected)) != expected:
            raise OSError("设备未返回预期确认，请检查是否选中了键盘固件的数据端口")

    def prepare(self):
        if self.link is not None:
            return
        try:
            import serial
        except ImportError as exc:
            raise OSError("USB HID 模式需要 pyserial，请先安装该依赖") from exc
        try:
            self.link = serial.Serial(self.port, timeout=0.2, write_timeout=0.2)
            self.link.reset_input_buffer()
            self._exchange(b"?", b"HID1")
        except (OSError, ValueError) as exc:
            self.close()
            raise OSError(f"USB HID {self.port} 连接失败：{exc}") from exc

    def tap(self, key, cancel, hold_seconds):
        if len(key) != 1 or not "A" <= key <= "Z":
            raise ValueError("USB HID 后端目前支持 A–Z 键")
        if not 0 <= hold_seconds < 0.25:
            raise ValueError("USB HID 按住时间须小于 250 毫秒")
        try:
            self._exchange(bytes((ord(key) - ord("A") + 4,)), b"+")
            try:
                cancel.wait(hold_seconds)
            finally:
                self._exchange(b"\x00", b"+")
        except OSError as exc:
            self.close()
            raise OSError(f"USB HID {self.port} 发键失败：{exc}") from exc
        return True

    def close(self):
        link, self.link = self.link, None
        if link is not None:
            try:
                link.close()
            except OSError:
                pass


def configured_ch9329(port=None, baudrate=None, address=None, *, trace=None):
    config = json.loads(INPUT_CONFIG.read_text(encoding="utf-8")) if INPUT_CONFIG.exists() else {}
    port = port or os.environ.get("CH9329_PORT", "").strip() or config.get("ch9329_port")
    if not port:
        return None
    if baudrate is None:
        baudrate = int(os.environ.get("CH9329_BAUD") or config.get("baudrate", DEFAULT_BAUD))
    if address is None:
        env_address = os.environ.get("CH9329_ADDRESS")
        address = int(env_address, 0) if env_address else config.get("address", 0)
    return CH9329Keyboard(port, baudrate, address, trace=trace)


def _configured_keyboard(hid_port, require_ch9329=False):
    if hid_port is not None:
        return UsbHidKeyboard(hid_port.strip()) if hid_port.strip() else None
    pico_port = os.environ.get("KEYBOARD_HID_PORT", "").strip()
    if pico_port and not require_ch9329 and not os.environ.get("CH9329_PORT", "").strip():
        return UsbHidKeyboard(pico_port)
    return configured_ch9329()


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class INPUTDATA(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("data", INPUTDATA)]


class WindowsInput:
    _hid_keyboard = None
    _mouse_state = None

    def __init__(self, hid_port=None, *, keyboard=None, require_ch9329=False):
        self._hid_keyboard = keyboard if keyboard is not None else _configured_keyboard(hid_port, require_ch9329)
        if require_ch9329 and not isinstance(self._hid_keyboard, CH9329Keyboard):
            raise ValueError("自动功能需要 CH9329 USB HID，请检查 input_config.json 或 CH9329_PORT")
        self.input_lock = threading.Lock()
        self._mouse_state = threading.local()
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.GetForegroundWindow.restype = w.HWND
        self.api.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.api.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.api.SendInput.restype = w.UINT
        self.api.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
        self.api.ClientToScreen.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
        self.api.WindowFromPoint.argtypes, self.api.WindowFromPoint.restype = [w.POINT], w.HWND
        self.api.GetAncestor.argtypes, self.api.GetAncestor.restype = [w.HWND, w.UINT], w.HWND
        self.api.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        self.api.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]

    def cursor_in_window(self, hwnd):
        point = w.POINT()
        if not self.api.GetCursorPos(ctypes.byref(point)):
            return False
        target = self.window_at(point.x, point.y)
        return target is not None and target == hwnd

    def window_at(self, x, y):
        target = self.api.WindowFromPoint(w.POINT(x, y))
        return (self.api.GetAncestor(target, 2) or target) if target else None

    def right_click(self, hwnd, cancel, recheck):
        return self.mouse_click("right", hwnd, cancel, recheck)

    def _mouse_context_issue(self, hwnd, cancel):
        if cancel.is_set():
            return "自动功能已停止"
        if self.foreground() != hwnd:
            return "绑定窗口不在前台"
        if self.modifiers_down():
            return "修饰键仍按住"
        if any(self.api.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4)):
            return "鼠标键仍按住"
        if not self.cursor_in_window(hwnd):
            return "鼠标不在绑定窗口内"
        return ""

    @property
    def mouse_block_reason(self):
        return getattr(self._mouse_state, "reason", "")

    @property
    def mouse_name(self):
        if isinstance(self._hid_keyboard, CH9329Keyboard):
            return self._hid_keyboard.name
        return "Windows SendInput"

    def mouse_click(self, button, hwnd, cancel, recheck):
        if button not in WIN_MOUSE_FLAGS:
            raise ValueError("鼠标点击支持左键、右键和中键")
        with self.input_lock:
            reason = self._mouse_context_issue(hwnd, cancel)
            device = self._hid_keyboard
            if not reason and isinstance(device, CH9329Keyboard):
                device.prepare()
                reason = self._mouse_context_issue(hwnd, cancel)
            if not reason:
                reason = self._mouse_context_issue(hwnd, cancel) if recheck() else "点击前指针复核未通过"
            if self._mouse_state is not None:
                self._mouse_state.reason = reason
            if reason:
                return False
            if isinstance(device, CH9329Keyboard):
                return device.click(button, cancel)
            down_flag, up_flag = WIN_MOUSE_FLAGS[button]
            down = INPUT(type=0, data=INPUTDATA(mi=MOUSEINPUT(0, 0, 0, down_flag, 0, 0)))
            up = INPUT(type=0, data=INPUTDATA(mi=MOUSEINPUT(0, 0, 0, up_flag, 0, 0)))
            if self.api.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT)) != 1:
                raise OSError(f"SendInput 鼠标按下失败 · {button}")
            try:
                cancel.wait(0.035)
            finally:
                if self.api.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT)) != 1:
                    raise OSError(f"SendInput 鼠标松开失败 · {button}")
            return True

    def client_bounds(self, hwnd):
        rect, origin = w.RECT(), w.POINT()
        if not self.api.GetClientRect(hwnd, ctypes.byref(rect)) or not self.api.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise OSError("无法读取游戏窗口范围")
        if rect.right < 100 or rect.bottom < 100:
            raise ValueError("游戏窗口不可见或太小")
        return dict(left=origin.x, top=origin.y, width=rect.right, height=rect.bottom)

    def foreground(self):
        return self.api.GetForegroundWindow()

    def is_own_window(self, hwnd):
        return self.window_pid(hwnd) == os.getpid()

    def window_pid(self, hwnd):
        pid = w.DWORD()
        self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def is_game_window(self, hwnd):
        if not hwnd:
            return False
        kind, title = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(512)
        self.api.GetClassNameW(hwnd, kind, len(kind))
        self.api.GetWindowTextW(hwnd, title, len(title))
        return (kind.value.startswith("GxWindowClass") or "魔兽世界" in title.value
                or "world of warcraft" in title.value.casefold())

    def modifiers_down(self):
        return any(self.api.GetAsyncKeyState(key) & 0x8000
                   for key in (0x10, 0x11, 0x12, 0x5B, 0x5C))

    @property
    def keyboard_name(self):
        if self._hid_keyboard is None:
            return "Windows SendInput"
        return self._hid_keyboard.name

    def ch9329_ready(self, *, probe=False):
        device = self._hid_keyboard
        if not isinstance(device, CH9329Keyboard):
            return False
        if not probe:
            info = device.info
            return None if info is None else info.usb_ready
        if not self.input_lock.acquire(blocking=False):
            return None
        try:
            already_open = device.link is not None
            try:
                return device.probe().usb_ready
            except OSError:
                return False
            finally:
                if not already_open:
                    device.close()
        finally:
            self.input_lock.release()

    def close(self):
        if self._hid_keyboard is not None:
            with self.input_lock:
                self._hid_keyboard.close()

    def _can_send(self, vk, hwnd, cancel):
        return (not cancel.is_set() and self.foreground() == hwnd
                and not self.modifiers_down() and not (self.api.GetAsyncKeyState(vk) & 0x8000))

    def send(self, key, hwnd, cancel, *, virtual_key=False, hold_seconds=0.035, recheck=None):
        with self.input_lock:
            vk = ord(key)
            if not self._can_send(vk, hwnd, cancel):
                return False
            if self._hid_keyboard is not None:
                self._hid_keyboard.prepare()
                # 串口首次握手可能等待，发报告前重新确认目标。
                if not self._can_send(vk, hwnd, cancel):
                    return False
            if recheck is not None and (not recheck() or not self._can_send(vk, hwnd, cancel)):
                return False
            if self._hid_keyboard is None:
                self._send_windows_key(vk, cancel, virtual_key, hold_seconds)
            else:
                return self._hid_keyboard.tap(key, cancel, hold_seconds)
            return True

    def _send_windows_key(self, vk, cancel, virtual_key, hold_seconds):
        if virtual_key:
            key_code, scan, flags = vk, 0, 0
        else:
            key_code, scan, flags = 0, self.api.MapVirtualKeyW(vk, 0), 0x0008
        down = INPUT(type=1, data=INPUTDATA(ki=KEYBDINPUT(key_code, scan, flags, 0, 0)))
        up = INPUT(type=1, data=INPUTDATA(ki=KEYBDINPUT(key_code, scan, flags | 0x0002, 0, 0)))
        if self.api.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT)) != 1:
            raise OSError("SendInput 按下失败，请检查目标应用和工具的权限级别")
        try:
            cancel.wait(hold_seconds)
        finally:
            if self.api.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT)) != 1:
                raise OSError("SendInput 松开失败，输出已停止")
