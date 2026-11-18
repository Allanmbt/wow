"""指针匹配、单次点击与独立运行回归测试；不会发送真实鼠标事件。"""

import ctypes
from ctypes import wintypes as w
import queue
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from auto_combat import CombatApp, WindowsInput
from cursor_loot import BAG_PATH, CursorFrame, CursorMatcher, CursorReader, HoverLatch, LootMonitor


def bag_frame():
    reference = cv2.imdecode(np.frombuffer(BAG_PATH.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    mask = np.zeros(reference.shape[:2], np.uint8)
    cv2.ellipse(mask, (60, 55), (23, 27), 0, 0, 360, 255, -1)
    cv2.fillPoly(mask, [np.array([[29, 23], [50, 23], [29, 45]])], 255)
    image = np.zeros_like(reference)
    image[mask > 0] = reference[mask > 0]
    return reference, CursorFrame(image, mask, (100, 100), 123)


def png_frame():
    reference = cv2.imdecode(np.frombuffer(BAG_PATH.with_name("bag.png").read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
    mask = (reference[:, :, 3] >= 176).astype(np.uint8) * 255
    image = (reference[:, :, :3].astype(np.float32) * (reference[:, :, 3:4] / 255.0)).astype(np.uint8)
    image[mask == 0] = 0
    return reference, CursorFrame(image, mask, (100, 100), 123)


class MatcherTests(unittest.TestCase):
    def test_transparent_template_rgb_is_ignored(self):
        reference, frame = png_frame()
        reference[reference[:, :, 3] == 0, :3] = (10, 255, 180)
        self.assertGreater(CursorMatcher(reference).score(frame), 0.98)

    def test_invalid_or_empty_template_is_rejected(self):
        for reference in (None, np.zeros((32, 32, 3), np.uint8), np.zeros((32, 32, 4), np.uint8)):
            with self.subTest(reference=None if reference is None else reference.shape):
                with self.assertRaises(ValueError):
                    CursorMatcher(reference)

    @unittest.skipUnless(sys.platform == "win32", "Windows 原生指针资源")
    def test_real_windows_cursor_rendering_at_common_sizes(self):
        # 只在内存中创建/绘制资源；不改变当前指针，也不发送任何点击。
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.CreateIconFromResourceEx.argtypes = [ctypes.c_void_p, w.DWORD, w.BOOL, w.DWORD,
                                                  ctypes.c_int, ctypes.c_int, w.UINT]
        user.CreateIconFromResourceEx.restype = w.HANDLE
        user.DestroyIcon.argtypes = [w.HANDLE]
        data = BAG_PATH.with_name("bag.png").read_bytes()
        buffer = ctypes.create_string_buffer(data)
        reference, _ = png_frame()
        matcher = CursorMatcher(reference)
        for size in (32, 48, 64, 96):
            with self.subTest(size=size):
                icon = user.CreateIconFromResourceEx(buffer, len(data), True, 0x30000, size, size, 0)
                self.assertTrue(icon)
                try:
                    reader = CursorReader()
                    def current(pointer):
                        pointer._obj.flags, pointer._obj.hCursor = 1, icon
                        return 1
                    reader.user.GetCursorInfo = current
                    self.assertGreaterEqual(matcher.score(reader.capture()), 0.88)
                finally:
                    user.DestroyIcon(icon)

    def test_supplied_bag_with_transparent_background_matches(self):
        reference, frame = bag_frame()
        self.assertGreater(CursorMatcher(reference).score(frame), 0.98)

    def test_background_outside_cursor_does_not_affect_match(self):
        reference, frame = bag_frame()
        frame.image[frame.mask == 0] = (220, 10, 180)
        self.assertGreater(CursorMatcher(reference).score(frame), 0.98)

    def test_scaled_cursor_matches(self):
        reference, frame = bag_frame()
        frame.image = cv2.resize(frame.image, None, fx=1.5, fy=1.5)
        frame.mask = cv2.resize(frame.mask, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_NEAREST)
        self.assertGreater(CursorMatcher(reference).score(frame), 0.88)

    def test_normal_arrow_and_hidden_cursor_do_not_match(self):
        reference, _ = bag_frame()
        arrow = np.zeros((48, 48, 3), np.uint8)
        cv2.fillPoly(arrow, [np.array([[2, 2], [2, 40], [15, 25], [35, 25]])], (255, 255, 255))
        frame = CursorFrame(arrow, (arrow.max(axis=2) > 0).astype(np.uint8) * 255, (0, 0))
        matcher = CursorMatcher(reference)
        self.assertEqual(matcher.score(frame), 0)
        self.assertEqual(matcher.score(None), 0)

    def test_unchanged_cursor_uses_cache_even_after_mouse_moves(self):
        reference, frame = bag_frame()
        matcher = CursorMatcher(reference)
        score = matcher.score(frame)
        frame.position = (200, 200)
        with patch.object(matcher, "_match", side_effect=AssertionError("不应重复做全尺寸匹配")):
            self.assertEqual(matcher.score(frame), score)


class LatchTests(unittest.TestCase):
    def test_stable_hover_clicks_once_then_rearms_after_leaving(self):
        latch = HoverLatch()
        self.assertFalse(latch.observe(0, True, (10, 10)))
        self.assertTrue(latch.observe(0.2, True, (10, 10)))
        latch.clicked(0.2)
        self.assertFalse(latch.observe(2, True, (10, 10)))
        latch.observe(2.1, False, (10, 10))
        latch.observe(2.6, False, (10, 10))
        self.assertFalse(latch.observe(2.7, True, (10, 10)))
        self.assertTrue(latch.observe(2.9, True, (10, 10)))

    def test_brief_flicker_and_mouse_motion_do_not_repeatedly_click(self):
        latch = HoverLatch()
        latch.observe(0, True, (0, 0))
        self.assertFalse(latch.observe(0.2, True, (30, 0)))
        self.assertTrue(latch.observe(0.4, True, (30, 0)))
        latch.clicked(0.4)
        latch.observe(0.5, False, (30, 0))
        self.assertFalse(latch.observe(0.6, True, (30, 0)))
        self.assertFalse(latch.observe(3, True, (90, 0)))


class ClickTests(unittest.TestCase):
    def native(self):
        native = WindowsInput.__new__(WindowsInput)
        native.input_lock = threading.Lock()
        self.packets = []

        def send(count, pointer, size):
            self.packets.append((pointer._obj.data.mi.dwFlags, pointer._obj.data.mi.dx, pointer._obj.data.mi.dy))
            return 1

        native.api = SimpleNamespace(GetAsyncKeyState=lambda k: 0, SendInput=send)
        native.foreground = lambda: 123
        native.modifiers_down = lambda: False
        native.cursor_in_window = lambda hwnd: True
        return native

    def test_right_click_releases_when_cancelled_during_press(self):
        native = self.native()
        cancel = threading.Event()
        cancel.wait = lambda seconds: cancel.set()
        self.assertTrue(native.right_click(123, cancel, lambda: True))
        self.assertEqual(self.packets, [(8, 0, 0), (16, 0, 0)])
        self.assertFalse(native.right_click(123, cancel, lambda: True))

    def test_context_and_final_cursor_recheck_block_wrong_clicks(self):
        native = self.native()
        cancel = threading.Event()
        self.assertFalse(native.right_click(456, cancel, lambda: True))
        self.assertFalse(native.right_click(123, cancel, lambda: False))
        native.cursor_in_window = lambda hwnd: False
        self.assertFalse(native.right_click(123, cancel, lambda: True))
        self.assertEqual(self.packets, [])

    def test_holding_mouse_or_modifier_blocks_click(self):
        native = self.native()
        native.api.GetAsyncKeyState = lambda k: 0x8000 if k == 2 else 0
        self.assertFalse(native.right_click(123, threading.Event(), lambda: True))
        native.api.GetAsyncKeyState = lambda k: 0
        native.modifiers_down = lambda: True
        self.assertFalse(native.right_click(123, threading.Event(), lambda: True))
        self.assertEqual(self.packets, [])


class MonitorTests(unittest.TestCase):
    def test_fifty_percent_is_enough_to_right_click(self):
        _, frame = bag_frame()
        for score, expected in ((0.49, 0), (0.50, 1), (0.56, 1), (0.60, 1)):
            with self.subTest(score=score):
                monitor = LootMonitor.__new__(LootMonitor)
                monitor.hwnd, monitor.events = 123, queue.Queue()
                monitor.cancel = threading.Event()
                monitor.matcher = SimpleNamespace(score=lambda frame: score, signature=lambda frame: 1)
                clock, clicks = [0.0], []
                def wait(seconds):
                    clock[0] += seconds
                    if clock[0] >= 0.6:
                        monitor.cancel.set()
                monitor.cancel.wait = wait
                def click(hwnd, cancel, recheck):
                    if recheck():
                        clicks.append(True)
                    return True
                monitor.native = SimpleNamespace(foreground=lambda: 123, right_click=click)
                with patch("cursor_loot.CursorReader", return_value=SimpleNamespace(capture=lambda: frame)), patch("cursor_loot.time.monotonic", lambda: clock[0]):
                    monitor.run()
                self.assertEqual(len(clicks), expected)
                self.assertFalse(any(kind == "loot_error" for kind, _ in monitor.events.queue))

    def test_cursor_changes_before_click_cancel_click_without_matching_under_input_lock(self):
        reference, frame = bag_frame()
        changed = CursorFrame(np.zeros_like(frame.image), frame.mask, frame.position)
        clock, attempts, clicks = [0.0], [], []
        monitor = LootMonitor.__new__(LootMonitor)
        monitor.hwnd, monitor.events = 123, queue.Queue()
        monitor.cancel = threading.Event()
        monitor.matcher = CursorMatcher(reference)
        monitor.cancel.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)

        class Reader:
            def capture(self):
                return changed if attempts else frame

        def click(hwnd, cancel, recheck):
            attempts.append(True)
            with patch.object(monitor.matcher, "score", side_effect=AssertionError("点击锁内不应重新匹配")):
                if recheck():
                    clicks.append(True)
            cancel.set()
            return False

        monitor.native = SimpleNamespace(foreground=lambda: 123, right_click=click)
        with patch("cursor_loot.CursorReader", Reader), patch("cursor_loot.time.monotonic", lambda: clock[0]):
            monitor.run()
        self.assertTrue(attempts)
        self.assertFalse(clicks)
        self.assertFalse(any(kind == "loot_error" for kind, _ in monitor.events.queue))

    def test_persistent_bag_only_clicks_once_then_new_appearance_clicks_again(self):
        reference, frame = bag_frame()
        clock, clicks = [0.0], []

        class Cancel:
            stopped = False

            def is_set(self): return self.stopped
            def set(self): self.stopped = True
            def wait(self, seconds):
                clock[0] += seconds
                if clock[0] >= 4:
                    self.stopped = True

        class Reader:
            def capture(self):
                return None if 2 <= clock[0] < 2.6 else frame

        native = SimpleNamespace(foreground=lambda: 123)

        def click(hwnd, cancel, recheck):
            if cancel.is_set() or not recheck():
                return False
            clicks.append(clock[0])
            return True

        native.right_click = click
        monitor = LootMonitor.__new__(LootMonitor)
        monitor.native, monitor.hwnd = native, 123
        monitor.events, monitor.cancel = queue.Queue(), Cancel()
        monitor.matcher = CursorMatcher(reference)
        with patch("cursor_loot.CursorReader", Reader), patch("cursor_loot.time.monotonic", lambda: clock[0]):
            monitor.run()
        self.assertEqual(len(clicks), 2)
        self.assertGreater(clicks[1] - clicks[0], 2)

    def test_no_capture_or_click_in_another_foreground_window(self):
        monitor = LootMonitor.__new__(LootMonitor)
        monitor.native, monitor.hwnd = SimpleNamespace(foreground=lambda: 456), 123
        monitor.events, monitor.cancel = queue.Queue(), threading.Event()
        monitor.cancel.wait = lambda seconds: monitor.cancel.set()
        with patch("cursor_loot.CursorReader") as reader:
            monitor.run()
            reader.return_value.capture.assert_not_called()


class IsolationTests(unittest.TestCase):
    def test_loot_toggle_and_start_failure_never_stop_combat(self):
        app = CombatApp.__new__(CombatApp)
        app.worker = Mock()
        app.root, app.loot_status = Mock(), Mock()
        app.cursor_loot, app.stop_on_error = Mock(), Mock()
        app.cursor_loot.get.return_value = True
        app.loot_arming_id = app.loot_monitor = None
        app.last_game_hwnd, app.hotkeys_ok = 123, True
        app.native = SimpleNamespace(is_own_window=lambda hwnd: False)
        app.events, app.persist = queue.Queue(), Mock()
        with patch("auto_combat.LootMonitor", side_effect=ValueError("bag.jpg 无效")):
            app.save_loot_toggle()
        app.worker.stop.assert_not_called()
        app.worker.start.assert_not_called()
        app.stop_on_error.set.assert_not_called()
        app.cursor_loot.set.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
