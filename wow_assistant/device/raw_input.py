"""Receive background Win32 Raw Input from the keyboard and mouse drivers."""

import ctypes
from ctypes import wintypes as w


WM_INPUT = 0x00FF
WM_INPUT_DEVICE_CHANGE = 0x00FE
GIDC_REMOVAL = 2
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RI_KEY_BREAK = 1
RI_MOUSE_BUTTON_5_DOWN = 0x0100
RI_MOUSE_BUTTON_5_UP = 0x0200
RID_INPUT = 0x10000003
RIDEV_REMOVE = 1
RIDEV_INPUTSINK = 0x100
RIDEV_DEVNOTIFY = 0x2000
HWND_MESSAGE = w.HWND(-3)


class RawInputDevice(ctypes.Structure):
    _fields_ = [("usage_page", w.USHORT), ("usage", w.USHORT), ("flags", w.DWORD), ("target", w.HWND)]


class RawInputHeader(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("size", w.DWORD), ("device", w.HANDLE), ("input_code", w.WPARAM)]


class RawKeyboard(ctypes.Structure):
    _fields_ = [("scan", w.USHORT), ("flags", w.USHORT), ("reserved", w.USHORT), ("key", w.USHORT),
                ("message", w.UINT), ("extra", w.ULONG)]


class RawMouse(ctypes.Structure):
    _fields_ = [("flags", w.USHORT), ("padding", w.USHORT), ("buttons", w.USHORT), ("button_data", w.USHORT),
                ("raw_buttons", w.ULONG), ("x", w.LONG), ("y", w.LONG), ("extra", w.ULONG)]


class RawInputData(ctypes.Union):
    _fields_ = [("keyboard", RawKeyboard), ("mouse", RawMouse)]


class RawInputPacket(ctypes.Structure):
    _fields_ = [("header", RawInputHeader), ("data", RawInputData)]


class RawInput:
    def __init__(self, api):
        self.api = api
        api.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
        api.CreateWindowExW.restype = w.HWND
        api.DestroyWindow.argtypes = [w.HWND]
        api.DestroyWindow.restype = w.BOOL
        api.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RawInputDevice), w.UINT, w.UINT]
        api.RegisterRawInputDevices.restype = w.BOOL
        api.GetRawInputData.argtypes = [w.HANDLE, w.UINT, w.LPVOID, ctypes.POINTER(w.UINT), w.UINT]
        api.GetRawInputData.restype = w.UINT
        api.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        api.DefWindowProcW.restype = ctypes.c_ssize_t
        self.window = api.CreateWindowExW(0, "STATIC", "DugeHotkeys", 0, 0, 0, 0, 0,
                                           HWND_MESSAGE, None, None, None)
        if not self.window:
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.register(RIDEV_INPUTSINK | RIDEV_DEVNOTIFY, self.window):
            error = ctypes.get_last_error()
            api.DestroyWindow(self.window)
            raise ctypes.WinError(error)
        self.packet = RawInputPacket()

    def register(self, flags, window):
        # Generic Desktop usage 2 is mouse and usage 6 is keyboard. Keep ordinary messages enabled.
        devices = (RawInputDevice * 2)(RawInputDevice(1, 2, flags, window), RawInputDevice(1, 6, flags, window))
        return self.api.RegisterRawInputDevices(devices, len(devices), ctypes.sizeof(RawInputDevice))

    def read(self, message):
        size = w.UINT(ctypes.sizeof(self.packet))
        try:
            received = self.api.GetRawInputData(message.lParam, RID_INPUT, ctypes.byref(self.packet),
                                                ctypes.byref(size), ctypes.sizeof(RawInputHeader))
            header = self.packet.header
            if received != header.size or not header.device:
                return None
            if header.type == RIM_TYPEKEYBOARD and received >= RawInputPacket.data.offset + ctypes.sizeof(RawKeyboard):
                return self.packet
            if header.type == RIM_TYPEMOUSE and received >= RawInputPacket.data.offset + ctypes.sizeof(RawMouse):
                return self.packet
            return None
        finally:
            self.api.DefWindowProcW(message.hWnd, message.message, message.wParam, message.lParam)

    def close(self):
        self.register(RIDEV_REMOVE, None)
        self.api.DestroyWindow(self.window)
