import runpy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from input_output import UsbHidKeyboard, WindowsInput


KeyboardBridge = runpy.run_path(str(Path(__file__).with_name("usb_keyboard") / "code.py"))["KeyboardBridge"]


class AutomationOutputTests(unittest.TestCase):
    def test_automation_keeps_ch9329_even_when_a_pico_port_is_inherited(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "input_config.json"
            config.write_text('{"ch9329_port":"COM3","baudrate":9600,"address":0}', encoding="utf-8")
            with (patch.dict("os.environ", {"CH9329_PORT": "", "KEYBOARD_HID_PORT": "COM99"}),
                  patch("input_output.INPUT_CONFIG", config)):
                native = WindowsInput(require_ch9329=True)
                self.assertEqual(native.keyboard_name, "CH9329 USB HID (COM3 / 9600)")
                self.assertEqual(native.mouse_name, native.keyboard_name)
                with self.assertRaisesRegex(ValueError, "CH9329"):
                    WindowsInput(hid_port="", require_ch9329=True)


class BridgePort:
    def __init__(self):
        self.connected = True
        self.reports = []
        self.commands = bytearray()
        self.responses = bytearray()
        link = SimpleNamespace(connected=True, read=lambda size: self.read_bytes(self.commands, size),
                               write=self.responses.extend)
        self.bridge = KeyboardBridge(link, SimpleNamespace(send_report=lambda report: self.reports.append(bytes(report))))

    @staticmethod
    def read_bytes(buffer, size):
        result = bytes(buffer[:size])
        del buffer[:size]
        return result

    def reset_input_buffer(self):
        self.responses.clear()

    def write(self, data):
        self.commands.extend(data)
        self.bridge.tick(0)
        return len(data)

    def read(self, size):
        return self.read_bytes(self.responses, size)

    def close(self):
        self.connected = self.bridge.link.connected = False
        self.bridge.tick(0)


class UsbHidTests(unittest.TestCase):
    def test_windows_uses_usb_for_combat_keys_and_q_probe(self):
        port = BridgePort()
        native = WindowsInput.__new__(WindowsInput)
        native._hid_keyboard = UsbHidKeyboard("COM7")
        native.input_lock = threading.Lock()
        native.foreground = lambda: 123
        native.api = SimpleNamespace(SendInput=Mock(), GetAsyncKeyState=lambda vk: 0)
        wait = Mock()
        cancel = SimpleNamespace(is_set=lambda: False, wait=wait)

        with patch.dict("sys.modules", {"serial": SimpleNamespace(Serial=Mock(return_value=port))}):
            for key, options in (("Q", {"virtual_key": True, "hold_seconds": 0.150}), ("E", {}), ("V", {})):
                self.assertTrue(native.send(key, 123, cancel, **options))
        self.assertEqual([report[2] for report in port.reports], [20, 0, 8, 0, 25, 0])
        self.assertEqual([call.args[0] for call in wait.call_args_list], [0.150, 0.035, 0.035])
        native.api.SendInput.assert_not_called()
        native.close()
        self.assertFalse(port.connected)

    def test_focus_change_during_handshake_blocks_the_press(self):
        native = WindowsInput.__new__(WindowsInput)
        native.input_lock = threading.Lock()
        native.foreground = Mock(side_effect=[123, 456])
        native.api = SimpleNamespace(GetAsyncKeyState=lambda vk: 0)
        native._hid_keyboard = Mock()
        self.assertFalse(native.send("Q", 123, threading.Event()))
        native._hid_keyboard.prepare.assert_called_once()
        native._hid_keyboard.tap.assert_not_called()

    def test_wrong_serial_device_fails_without_pressing_a_key(self):
        port = Mock()
        port.write.return_value = 1
        port.read.return_value = b""
        keyboard = UsbHidKeyboard("COM7")
        with patch.dict("sys.modules", {"serial": SimpleNamespace(Serial=lambda *args, **kwargs: port)}):
            with self.assertRaisesRegex(OSError, "COM7 连接失败"):
                keyboard.prepare()
        port.write.assert_called_once_with(b"?")
        port.close.assert_called_once()
        self.assertIsNone(keyboard.link)

    def test_firmware_releases_on_host_disconnect_or_missing_key_up(self):
        for disconnected in (False, True):
            with self.subTest(disconnected=disconnected):
                port = BridgePort()
                port.write(bytes((20,)))
                if disconnected:
                    port.close()
                else:
                    port.bridge.tick(0.31)
                self.assertEqual([report[2] for report in port.reports], [20, 0])
