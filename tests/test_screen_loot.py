"""使用离线画面和假输入验证备用拾取路径，不向游戏发送操作。"""

import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from wow_assistant.gathering.cursor import BAG_PATH, LOOT_TEMPLATE, CursorMatcher, LootMonitor
from wow_assistant.gathering.screen import ScreenLootProbe


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.reference = cv2.imdecode(np.frombuffer(BAG_PATH.read_bytes(), np.uint8), 1)
        self.probe = ScreenLootProbe(None, None, self.reference)

    def scene(self, scale, pointer=(40, 45)):
        scene = np.random.default_rng(17).integers(0, 255, (240, 240, 3), dtype=np.uint8)
        overlay = self.reference.copy()
        hsv = cv2.cvtColor(overlay, cv2.COLOR_BGR2HSV)
        # 换掉样本原来的绿色地面，验证袋子出现在不同背景上也能匹配。
        overlay[(hsv[:, :, 0] > 35) & (hsv[:, :, 0] < 170)] = (120, 5, 150)
        overlay = cv2.resize(overlay, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        height, width = overlay.shape[:2]
        x, y = pointer
        scene[y:y+height, x:x+width] = overlay
        return scene

    def test_bag_on_different_background_at_multiple_sizes(self):
        for scale in (0.10, 0.15, 0.20, 0.30, 0.50, 0.60):
            with self.subTest(scale=scale):
                hit = self.probe.match(self.scene(scale), (40, 45), (0, 0))
                self.assertIsNotNone(hit)
                self.assertGreaterEqual(hit.score, 0.90)

    def test_random_background_and_bag_far_from_pointer_do_not_click(self):
        self.assertIsNone(self.probe.match(self.scene(0.3), (160, 160), (0, 0)))
        random = np.random.default_rng(29).integers(0, 255, (220, 220, 3), dtype=np.uint8)
        self.assertIsNone(self.probe.match(random, (30, 30), (0, 0)))

    def test_disappeared_bag_fails_final_visual_check(self):
        scene = self.scene(0.3)
        hit = self.probe.match(scene, (40, 45), (0, 0))
        self.assertIsNotNone(hit)
        self.probe.same_position = lambda pos: True
        self.probe.grab = lambda region: np.zeros_like(hit.template)
        self.assertFalse(self.probe.verify(hit))

    def test_screen_detection_and_recheck_use_fifty_percent(self):
        template, mask = self.probe.templates[0]
        self.probe.templates = [(template, mask)]
        self.probe.same_position = lambda position: True
        self.probe.grab = lambda region: template.copy()
        for score in (0.49, 0.50, 0.56):
            with self.subTest(score=score), patch("cv2.matchTemplate", return_value=np.array([[score]], np.float32)):
                hit = self.probe.match(template, (0, 0), (0, 0))
                if score < 0.5:
                    self.assertIsNone(hit)
                else:
                    self.assertIsNotNone(hit)
                    self.assertTrue(self.probe.verify(hit))

    def test_transparent_template_accepts_sixty_seven_percent_in_both_checks(self):
        reference = cv2.imdecode(np.frombuffer(LOOT_TEMPLATE.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
        probe = ScreenLootProbe(None, None, reference)
        template, mask = probe.templates[0]
        probe.templates = [(template, mask)]
        probe.same_position = lambda position: True
        probe.grab = lambda region: template.copy()
        with patch("cv2.matchTemplate", return_value=np.array([[0.67]], np.float32)):
            hit = probe.match(template, (0, 0), (0, 0))
            self.assertIsNotNone(hit)
            self.assertTrue(probe.verify(hit))

    def test_software_cursor_can_click_when_windows_cursor_is_hidden(self):
        hit = self.probe.match(self.scene(0.3), (40, 45), (0, 0))
        screen = Mock()
        screen.find.return_value = hit
        screen.verify.return_value = True
        screen.same_position.return_value = True
        monitor = LootMonitor.__new__(LootMonitor)
        monitor.hwnd, monitor.events = 123, queue.Queue()
        monitor.screen_reference = self.reference
        monitor.matcher = CursorMatcher(self.reference)
        monitor.cancel = threading.Event()
        clock, clicks = [0.0], []
        monitor.cancel.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)

        def click(hwnd, cancel, recheck):
            # 截图/识别已经结束；锁内只检查位置和结果是否足够新鲜。
            with patch.object(screen, "verify", side_effect=AssertionError("锁内不能截图")):
                if recheck():
                    clicks.append(True)
            cancel.set()
            return True

        monitor.native = SimpleNamespace(foreground=lambda: 123, right_click=click)
        reader = SimpleNamespace(capture=lambda: None)
        with patch("wow_assistant.gathering.cursor.CursorReader", return_value=reader), patch("wow_assistant.gathering.cursor.ScreenLootProbe", return_value=screen), patch("wow_assistant.gathering.cursor.time.monotonic", lambda: clock[0]):
            monitor.run()
        self.assertEqual(clicks, [True])
        screen.close.assert_called_once()
        self.assertFalse(any(kind == "loot_error" for kind, _ in monitor.events.queue))


if __name__ == "__main__":
    unittest.main()
