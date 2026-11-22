import json
from pathlib import Path
import queue
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from auto_combat import ATTACK_SKILLS
from diagnose_ctrl import (CONFIG, TEMPLATE, DiagnosticHotkeys,
                           DiagnosticInput, print_summary, probe_icons)


class DiagnosticTests(unittest.TestCase):
    def test_input_methods_are_disabled(self):
        native = DiagnosticInput.__new__(DiagnosticInput)
        with self.assertRaises(AssertionError):
            native.send("Q", 123, None)
        with self.assertRaises(AssertionError):
            native.right_click(123, None, lambda: True)

    def test_hotkey_queues_diagnostic_and_toggle_without_doing_work_in_hook(self):
        hotkeys = DiagnosticHotkeys.__new__(DiagnosticHotkeys)
        hotkeys.events = queue.Queue()
        hotkeys.on_toggle()
        self.assertEqual(list(hotkeys.events.queue), [("raw_hotkey", None), ("toggle", None)])

    def test_summary_reports_no_hotkey_or_listener_failure(self):
        lines = []
        print_summary([], 0, [], ["实体总开关监听失败"], Path("diagnostics"), lines.append)
        self.assertTrue(any("实体总开关通知：0 次" in line for line in lines))
        self.assertTrue(any("实体总开关监听失败" in line for line in lines))

    def test_icon_probe_saves_evidence_without_sending_input(self):
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        images = {}
        for key, path in [("seal", TEMPLATE), *((key, path) for key, (_, _, path) in ATTACK_SKILLS.items())]:
            if not config.get(key + "_region") or not path.exists():
                continue
            images[tuple(sorted(config[key+"_region"].items()))] = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), 1)
        class Capture:
            monitors = [{}, dict(left=0, top=0, width=8000, height=4000)]
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def grab(self, region):
                image = images[tuple(sorted(region.items()))]
                return np.dstack((image, np.full(image.shape[:2], 255, np.uint8)))
        native = SimpleNamespace(api=SimpleNamespace(GetAsyncKeyState=lambda key: 0),
            client_bounds=lambda hwnd: dict(left=0, top=0, width=8000, height=4000),
            send=Mock(side_effect=AssertionError("不能发送输入")), right_click=Mock())
        snapshot = dict(foreground=dict(hwnd=123, title="Game", pid=456, process="wow.exe", own=False),
                        mouse_window=dict(hwnd=123, title="Game"))
        with tempfile.TemporaryDirectory() as directory, patch("mss.MSS", Capture):
            result = probe_icons(native, snapshot, Path(directory), 1, "sample", lambda line: None)
            self.assertIs(result["q_ready"], True)
            self.assertEqual(len(list(Path(directory).glob("*.png"))), len(images))
        native.send.assert_not_called()
        native.right_click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
