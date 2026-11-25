import ctypes
import queue
import unittest
from ctypes import wintypes as w
from unittest.mock import Mock, patch

from wow_assistant.device.hotkeys import F10_VK, Hotkeys
from wow_assistant.device.raw_input import (
    RIM_TYPEKEYBOARD, RIM_TYPEMOUSE, RI_KEY_BREAK, RI_MOUSE_BUTTON_5_DOWN, RI_MOUSE_BUTTON_5_UP, WM_INPUT,
    RawInput, RawInputPacket, RawKeyboard, RawMouse,
)


class RawHotkeyTests(unittest.TestCase):
    def setUp(self):
        with patch.object(Hotkeys, "run"):
            self.hotkeys = Hotkeys(queue.Queue())
            self.hotkeys.thread.join()
        self.modifiers_down = Mock(return_value=False)

    def keyboard(self, key, *, down=True, device=10, scan=0):
        packet = RawInputPacket()
        packet.header.type, packet.header.device = RIM_TYPEKEYBOARD, device
        packet.data.keyboard = RawKeyboard(key=key, scan=scan, flags=0 if down else RI_KEY_BREAK)
        self.hotkeys.raw_event(packet, self.modifiers_down)

    def mouse(self, buttons, device=20):
        packet = RawInputPacket()
        packet.header.type, packet.header.device = RIM_TYPEMOUSE, device
        packet.data.mouse = RawMouse(buttons=buttons)
        self.hotkeys.raw_event(packet, self.modifiers_down)

    def test_retired_f8_no_longer_starts_any_action(self):
        self.keyboard(0x77)
        self.keyboard(0x77, down=False)
        self.assertTrue(self.hotkeys.events.empty())

    def test_raw_keyboard_modifier_and_device_removal_cancel_pending_hotkey(self):
        self.keyboard(F10_VK)
        self.keyboard(0x11, scan=0x1D, device=11)
        self.keyboard(0x11, scan=0x1D, device=11, down=False)
        self.keyboard(F10_VK, down=False)
        self.keyboard(F10_VK)
        self.hotkeys.raw_device_removed(10)
        self.keyboard(F10_VK, down=False)
        self.assertTrue(self.hotkeys.events.empty())
        self.keyboard(F10_VK)
        self.keyboard(F10_VK, down=False)
        self.assertEqual(list(self.hotkeys.events.queue), [("toggle", None)])

    def test_mouse_5_toggles_with_its_own_device_release(self):
        self.mouse(RI_MOUSE_BUTTON_5_DOWN)
        self.mouse(RI_MOUSE_BUTTON_5_UP, device=21)
        self.mouse(0x0080)  # Mouse 4 release
        self.assertTrue(self.hotkeys.events.empty())
        self.mouse(RI_MOUSE_BUTTON_5_UP)
        self.mouse(RI_MOUSE_BUTTON_5_UP)
        self.assertEqual(list(self.hotkeys.events.queue), [("toggle", None)])

    def test_raw_data_reader_rejects_event_without_hardware_device(self):
        raw = RawInput.__new__(RawInput)
        raw.api, raw.packet = Mock(), RawInputPacket()
        message = w.MSG(hWnd=111, message=WM_INPUT, wParam=1, lParam=456)

        def fill_packet(_, __, destination, *_args):
            packet = RawInputPacket()
            packet.header.type = RIM_TYPEKEYBOARD
            packet.header.size = ctypes.sizeof(packet)
            packet.data.keyboard.key = F10_VK
            ctypes.memmove(destination, ctypes.byref(packet), packet.header.size)
            return packet.header.size

        raw.api.GetRawInputData.side_effect = fill_packet
        self.assertIsNone(raw.read(message))
        raw.api.DefWindowProcW.assert_called_once_with(111, WM_INPUT, 1, 456)

    def test_win32_message_dispatches_complete_f10_press(self):
        raw = RawInput.__new__(RawInput)
        raw.api, raw.packet, raw.window = Mock(), RawInputPacket(), 111
        message = w.MSG(hWnd=raw.window, message=WM_INPUT, wParam=1, lParam=456)

        def fill_packet(_, __, destination, *_args):
            ctypes.memmove(destination, ctypes.byref(packet), packet.header.size)
            return packet.header.size

        raw.api.GetRawInputData.side_effect = fill_packet
        packet = RawInputPacket()
        packet.header.type = RIM_TYPEKEYBOARD
        packet.header.size = ctypes.sizeof(packet)
        packet.header.device = 10
        for flags in (0, RI_KEY_BREAK):
            packet.data.keyboard = RawKeyboard(key=F10_VK, flags=flags)
            self.hotkeys._read_raw(raw, message, self.modifiers_down)
        self.assertEqual(list(self.hotkeys.events.queue), [("toggle", None)])
        self.assertEqual(raw.api.DefWindowProcW.call_count, 2)


if __name__ == "__main__":
    unittest.main()
