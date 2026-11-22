import threading
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ch9329_output import CH9329Keyboard
from input_output import WindowsInput


GET_INFO = bytes.fromhex("57 AB 00 01 00 03")
INFO = bytes.fromhex("57 AB 00 81 08 30 01 03 00 00 00 00 00 BF")
NOT_ENUMERATED = bytes.fromhex("57 AB 00 81 08 30 00 03 00 00 00 00 00 BE")
PRESS_Q = bytes.fromhex("57 AB 00 02 08 00 00 14 00 00 00 00 00 20")
RELEASE = bytes.fromhex("57 AB 00 02 08 00 00 00 00 00 00 00 00 0C")
ACK = bytes.fromhex("57 AB 00 82 01 00 85")
MOUSE_ACK = bytes.fromhex("57 AB 00 85 01 00 88")
MOUSE_UP = bytes.fromhex("57 AB 00 05 05 01 00 00 00 00 0D")


class FakeSerial:
    def __init__(self, *replies):
        self.replies = deque(replies)
        self.pending = b""
        self.requests = []
        self.closed = False

    def reset_input_buffer(self):
        self.pending = b""

    def write(self, data):
        self.requests.append(data)
        self.pending += self.replies.popleft()
        return len(data)

    def read(self, size):
        size = min(size, 2)
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk

    def close(self):
        self.closed = True


class CH9329Tests(unittest.TestCase):
    def connect(self, *replies):
        port = FakeSerial(*replies)
        factory = Mock(return_value=port)
        serial = patch.dict("sys.modules", {"serial": SimpleNamespace(Serial=factory)})
        serial.start()
        self.addCleanup(serial.stop)
        return port, factory

    def test_q_via_shared_output_has_exact_frames_and_no_software_input(self):
        port, factory = self.connect(INFO, ACK, ACK)
        logs = []
        native = WindowsInput.__new__(WindowsInput)
        native._hid_keyboard = CH9329Keyboard("COM3", trace=logs.append)
        native.input_lock = threading.Lock()
        native.foreground = lambda: 123
        native.api = SimpleNamespace(SendInput=Mock(), GetAsyncKeyState=lambda key: 0)
        wait = Mock()
        cancel = SimpleNamespace(is_set=lambda: False, wait=wait)

        self.assertTrue(native.send("Q", 123, cancel, virtual_key=True, hold_seconds=0.15))
        self.assertEqual(port.requests, [GET_INFO, PRESS_Q, RELEASE])
        native.api.SendInput.assert_not_called()
        factory.assert_called_once_with("COM3", 9600, timeout=0.05, write_timeout=0.2)
        wait.assert_called_once_with(0.15)
        self.assertIn("Q 松开 RX 57 AB 00 82 01 00 85", logs)
        native.close()
        self.assertTrue(port.closed)

    def test_probe_reports_usb_disconnected_without_sending_a_key(self):
        port, _ = self.connect(NOT_ENUMERATED)
        keyboard = CH9329Keyboard("COM3")
        info = keyboard.probe()
        self.assertEqual(info.version, 0x30)
        self.assertTrue(info.caps_lock)
        self.assertFalse(info.usb_ready)
        with self.assertRaisesRegex(OSError, "未枚举"):
            keyboard.tap("q", threading.Event(), 0.15)
        self.assertEqual(port.requests, [GET_INFO])
        self.assertTrue(port.closed)

    def test_press_checksum_failure_still_attempts_release_and_closes(self):
        port, _ = self.connect(INFO, ACK[:-1] + b"\x00", ACK)
        keyboard = CH9329Keyboard("COM3")
        with self.assertRaisesRegex(OSError, "Q 按下失败.*校验和错误"):
            keyboard.tap("q", threading.Event(), 0.15)
        self.assertEqual(port.requests, [GET_INFO, PRESS_Q, RELEASE])
        self.assertTrue(port.closed)

    def test_device_error_on_release_is_reported(self):
        port, _ = self.connect(INFO, ACK, bytes.fromhex("57 AB 00 C2 01 E5 AA"))
        keyboard = CH9329Keyboard("COM3")
        cancel = SimpleNamespace(is_set=lambda: False, wait=Mock())
        with self.assertRaisesRegex(OSError, "Q 松开失败.*E5"):
            keyboard.tap("Q", cancel, 0.15)
        self.assertTrue(port.closed)

    def test_stop_during_hold_still_releases(self):
        port, _ = self.connect(INFO, ACK, ACK)
        cancel = threading.Event()
        cancel.wait = lambda seconds: cancel.set()
        keyboard = CH9329Keyboard("COM3")
        self.assertTrue(keyboard.tap("Q", cancel, 0.15))
        self.assertEqual(port.requests, [GET_INFO, PRESS_Q, RELEASE])
        self.assertFalse(keyboard.tap("Q", cancel, 0.15))

    def test_configured_address_and_baud_used_in_readonly_probe(self):
        port, factory = self.connect(bytes.fromhex("57 AB 03 81 08 30 01 03 00 00 00 00 00 C2"))
        keyboard = CH9329Keyboard("COM3", baudrate=115200, address=3)
        self.assertTrue(keyboard.probe().usb_ready)
        self.assertEqual(port.requests, [bytes.fromhex("57 AB 03 01 00 06")])
        factory.assert_called_once_with("COM3", 115200, timeout=0.05, write_timeout=0.2)

    def test_left_right_and_middle_use_serial_without_moving_the_mouse(self):
        port, _ = self.connect(INFO, *(MOUSE_ACK,) * 6)
        native = WindowsInput.__new__(WindowsInput)
        native._hid_keyboard = CH9329Keyboard("COM3")
        native.input_lock = threading.Lock()
        native.foreground = lambda: 123
        native.cursor_in_window = lambda hwnd: True
        native.api = SimpleNamespace(SendInput=Mock(), GetAsyncKeyState=lambda key: 0)
        cancel = SimpleNamespace(is_set=lambda: False, wait=lambda seconds: None)

        self.assertFalse(native.right_click(456, cancel, lambda: True))
        self.assertTrue(native.mouse_click("left", 123, cancel, lambda: True))
        self.assertTrue(native.right_click(123, cancel, lambda: True))
        self.assertTrue(native.mouse_click("middle", 123, cancel, lambda: True))
        self.assertEqual(port.requests, [
            GET_INFO, bytes.fromhex("57 AB 00 05 05 01 01 00 00 00 0E"), MOUSE_UP,
            bytes.fromhex("57 AB 00 05 05 01 02 00 00 00 0F"), MOUSE_UP,
            bytes.fromhex("57 AB 00 05 05 01 04 00 00 00 11"), MOUSE_UP,
        ])
        native.api.SendInput.assert_not_called()

    def test_read_config_checks_checksum_on_ch9329f_extended_reply(self):
        payload = bytes.fromhex("80 80 00 00 00 25 80 08 00 00 03 86 1A 2A E1 00 00 00 01 00 0D")
        frame = bytes.fromhex("57 AB 00 88 32") + payload.ljust(65, b"\x00")
        reply = frame + bytes((sum(frame) & 0xFF,))
        _, _ = self.connect(reply)
        config = CH9329Keyboard("COM3").read_config()
        self.assertEqual((config.mode, config.serial_mode, config.address, config.baudrate), (0x80, 0x80, 0, 9600))
