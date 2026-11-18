"""自动战斗测试：使用假时钟/假输入，不向游戏发送任何按键。"""

import ctypes
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from auto_combat import (AttackPolicy, CombatPolicy, CombatWorker, Hotkeys, INPUT,
                         WindowsInput, icon_similarity, classify_icon, classify_attack_icon)


class MouseToggleTests(unittest.TestCase):
    def setUp(self):
        self.hotkeys = Hotkeys.__new__(Hotkeys)
        self.hotkeys.events = queue.Queue()
        self.hotkeys.mouse_pressed = False

    def test_upper_side_button_toggles_once_until_released(self):
        self.assertTrue(self.hotkeys.handle_mouse(0x020B, 2 << 16))
        self.assertTrue(self.hotkeys.handle_mouse(0x020B, 2 << 16))
        self.assertEqual(self.hotkeys.events.qsize(), 1)
        self.assertEqual(self.hotkeys.events.get_nowait(), ("toggle", None))
        self.assertTrue(self.hotkeys.handle_mouse(0x020C, 2 << 16))
        self.assertTrue(self.hotkeys.events.empty())
        self.hotkeys.handle_mouse(0x020B, 2 << 16)
        self.assertEqual(self.hotkeys.events.get_nowait(), ("toggle", None))

    def test_other_mouse_buttons_pass_through(self):
        self.assertFalse(self.hotkeys.handle_mouse(0x020B, 1 << 16))
        self.assertFalse(self.hotkeys.handle_mouse(0x020C, 1 << 16))
        self.assertFalse(self.hotkeys.handle_mouse(0x0201, 0))
        self.assertFalse(self.hotkeys.handle_mouse(0x020A, 2 << 16))
        self.assertTrue(self.hotkeys.events.empty())


class PolicyTests(unittest.TestCase):
    def test_q_policy_never_generates_periodic_e(self):
        p = CombatPolicy(0)
        self.assertIsNone(p.step(0, True))
        self.assertEqual(p.step(0.61, True), "Q")
        self.assertIsNone(p.step(0.7, False))
        self.assertIsNone(p.step(0.81, False))
        self.assertIsNone(p.step(1.17, False))

    def test_stun_five_seconds_then_q_retry_succeeds(self):
        p = CombatPolicy(0)
        q_times = []
        for tick in range(601):
            now = tick / 50
            # 眩晕至 5 秒；6.6 秒附近重试后图标发生变化。
            ready = not (p.last_q is not None and p.last_q >= 5 and now >= p.last_q + 0.15)
            if p.step(now, ready) == "Q":
                q_times.append(now)
        self.assertEqual(len(q_times), 4)
        self.assertGreater(q_times[-1], 5)
        self.assertFalse(p.pending)
        self.assertEqual(p.e_count, 0)

    def test_no_q_during_thirty_second_buff_then_refresh(self):
        p = CombatPolicy(0)
        p.step(0, True)
        self.assertEqual(p.step(0.61, True), "Q")
        for tick in range(50, 1500):
            self.assertNotEqual(p.step(tick / 50, False), "Q")
        p.step(30.62, True)
        self.assertEqual(p.step(31.23, True), "Q")

    def test_existing_buff_does_not_refresh_on_start(self):
        p = CombatPolicy(0)
        for tick in range(500):
            self.assertNotEqual(p.step(tick / 50, False), "Q")
        self.assertEqual(p.e_count, 0)

    def test_gcd_shorter_than_two_seconds_not_success(self):
        p = CombatPolicy(0)
        p.step(0, True)
        p.step(0.61, True)
        p.step(0.8, False)
        p.step(2.0, False)
        self.assertTrue(p.pending)
        p.step(2.1, True)
        self.assertEqual(p.step(2.72, True), "Q")

    def test_q_keeps_retrying_until_manual_stop(self):
        p = CombatPolicy(0)
        p.step(0, True)
        for now in (0.61, 2.62, 4.63, 6.64):
            self.assertEqual(p.step(now, True), "Q")
        self.assertEqual(p.step(8.65, True), "Q")

    def test_ready_flicker_does_not_spam_q(self):
        p = CombatPolicy(0)
        for tick in range(1000):
            self.assertNotEqual(p.step(tick / 50, tick % 2 == 0), "Q")

    def test_stunned_gray_icon_does_not_confirm_q(self):
        p = CombatPolicy(0)
        p.step(0, True)
        p.step(0.61, True)
        for tick in range(40, 260):
            self.assertNotEqual(p.step(tick / 50, None), "Q")
        self.assertTrue(p.pending)
        self.assertEqual(p.q_guard_until, 0)
        p.step(5.3, True)
        self.assertEqual(p.step(5.91, True), "Q")

    def test_unknown_icon_does_not_stop_session(self):
        p = CombatPolicy(0)
        p.step(0, True)
        p.step(0.61, True)
        self.assertIsNone(p.step(12.62, None))

    def test_no_backlog_of_e_after_delay(self):
        p = CombatPolicy(0)
        self.assertIsNone(p.step(50, False))
        self.assertIsNone(p.step(50, False))


class AttackIconTests(unittest.TestCase):
    def test_both_ready_share_one_e_then_wait_for_each_cooldown(self):
        p = AttackPolicy()
        p.observe(0, dict(strike=True, judgment=True))
        self.assertEqual(p.candidates(0.14), ())
        targets = p.candidates(0.41)
        self.assertEqual(set(targets), {"strike", "judgment"})
        p.sent(0.41, targets)
        p.observe(0.6, dict(strike=False, judgment=False))
        p.observe(2.61, dict(strike=False, judgment=False))
        self.assertEqual(p.candidates(7.9), ())
        p.observe(8.42, dict(strike=False, judgment=True))
        self.assertEqual(p.candidates(8.83), ("judgment",))
        p.sent(8.83, ("judgment",))
        p.observe(10.42, dict(strike=True, judgment=False))
        self.assertEqual(p.candidates(10.84), ("strike",))

    def test_no_periodic_e_when_both_icons_are_cooling(self):
        p = AttackPolicy()
        for tick in range(1000):
            now = tick / 20
            p.observe(now, dict(strike=False, judgment=False))
            self.assertEqual(p.candidates(now), ())

    def test_short_gcd_does_not_confirm_skill_cooldown(self):
        p = AttackPolicy()
        p.observe(0, dict(strike=True))
        p.sent(0.41, p.candidates(0.41))
        p.observe(0.6, dict(strike=False))
        p.observe(2.0, dict(strike=True))
        self.assertTrue(p.skills["strike"].pending)
        self.assertEqual(p.skills["strike"].guard_until, 0)
        self.assertEqual(p.candidates(2.42), ("strike",))

    def test_one_failed_skill_retries_without_recasting_cooling_peer(self):
        p = AttackPolicy()
        p.observe(0, dict(strike=True, judgment=True))
        p.sent(0.41, p.candidates(0.41))
        p.observe(0.6, dict(strike=False, judgment=True))
        p.observe(2.61, dict(strike=False, judgment=True))
        self.assertEqual(p.candidates(2.61), ("judgment",))

    def test_ready_flash_before_expected_cooldown_end_does_not_fire(self):
        p = AttackPolicy()
        p.observe(0, dict(strike=True, judgment=True))
        p.sent(0.41, p.candidates(0.41))
        p.observe(0.6, dict(strike=False, judgment=False))
        p.observe(2.61, dict(strike=False, judgment=False))
        p.observe(5, dict(strike=True, judgment=True))
        self.assertEqual(p.candidates(5.5), ())

    def test_no_target_keeps_bounded_e_retry_interval(self):
        p = AttackPolicy()
        p.observe(0, dict(strike=True))
        for now in (0.41, 2.42, 4.43, 6.44):
            p.sent(now, p.candidates(now))
        self.assertEqual(p.candidates(8.45), ("strike",))

    def test_unknown_icon_never_triggers_or_confirms(self):
        p = AttackPolicy()
        p.observe(0, dict(strike=True))
        p.sent(0.41, p.candidates(0.41))
        for now in (1, 3, 5, 10):
            p.observe(now, dict(strike=None))
            self.assertEqual(p.candidates(now), ())
        self.assertTrue(p.skills["strike"].pending)

    def test_candidates_do_not_consume_attempt_when_input_not_sent(self):
        p = AttackPolicy()
        p.observe(0, dict(strike=True))
        for now in (1, 2, 3):
            self.assertEqual(p.candidates(now), ("strike",))
        self.assertEqual(p.skills["strike"].attempts, 0)

    def test_both_ready_send_short_followups_only_for_remaining_ready_skill(self):
        p = AttackPolicy()
        p.observe(0, dict(strike=True, judgment=True))
        first = p.candidates(0.2)
        p.sent(0.2, first)
        p.observe(0.25, dict(strike=False, judgment=True))
        self.assertEqual(p.candidates(0.3), ())
        self.assertEqual(p.candidates(0.4), ("judgment",))
        p.sent(0.4, ("judgment",))
        self.assertEqual(p.skills["judgment"].attempts, 1)
        p.observe(0.45, dict(strike=False, judgment=False))
        self.assertEqual(p.candidates(0.6), ())

    def test_burst_is_bounded_even_if_icons_never_change(self):
        p = AttackPolicy()
        p.observe(0, dict(strike=True, judgment=True))
        for now in (0.2, 0.4, 0.6, 0.8):
            candidates = p.candidates(now)
            self.assertEqual(set(candidates), {"strike", "judgment"})
            p.sent(now, candidates)
        self.assertEqual(p.candidates(1.0), ())
        self.assertEqual(p.skills["strike"].attempts, 1)
        self.assertEqual(p.skills["judgment"].attempts, 1)

    def test_gcd_recovery_can_retry_without_old_two_second_global_wait(self):
        p = AttackPolicy()
        p.observe(0, dict(judgment=True))
        p.sent(0.2, p.candidates(0.2))
        p.observe(0.3, dict(judgment=False))
        p.observe(1.6, dict(judgment=True))
        self.assertEqual(p.candidates(1.76), ("judgment",))

    def reference(self):
        import numpy as np

        return np.random.default_rng(7).integers(20, 240, (60, 60, 3), dtype=np.uint8)

    def test_ready_and_gray_cooldown_image_classification(self):
        import cv2

        reference = self.reference()
        self.assertIs(classify_attack_icon(reference, reference)[0], True)
        gray = cv2.cvtColor(cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        gray = (gray * 0.6).astype("uint8")
        self.assertIs(classify_attack_icon(gray, reference)[0], False)

    def test_center_countdown_blocks_ready_even_when_background_matches(self):
        import cv2

        reference = self.reference()
        countdown = reference.copy()
        cv2.putText(countdown, "1", (21, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 5)
        cv2.putText(countdown, "1", (21, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
        self.assertIsNot(classify_attack_icon(countdown, reference)[0], True)


class InputTests(unittest.TestCase):
    def native(self):
        native = WindowsInput.__new__(WindowsInput)
        native.input_lock = threading.Lock()
        self.packets = []

        def send(count, ptr, size):
            self.packets.append(ptr._obj.data.ki.dwFlags)
            return 1

        native.api = SimpleNamespace(GetAsyncKeyState=lambda k: 0, MapVirtualKeyW=lambda k, m: 18,
                                     SendInput=send)
        native.foreground = lambda: 123
        return native

    def test_cancel_during_key_down_still_releases_key(self):
        native = self.native()
        cancel = threading.Event()

        def wait(seconds):
            cancel.set()

        cancel.wait = wait
        self.assertTrue(native.send("E", 123, cancel))
        self.assertEqual(self.packets, [8, 10])

    def test_foreground_and_stop_block_input(self):
        native = self.native()
        cancel = threading.Event()
        self.assertFalse(native.send("E", 456, cancel))
        cancel.set()
        self.assertFalse(native.send("Q", 123, cancel))
        self.assertEqual(self.packets, [])

    def test_modifiers_block_input(self):
        native = self.native()
        native.api.GetAsyncKeyState = lambda k: 0x8000 if k == 0x11 else 0
        self.assertFalse(native.send("E", 123, threading.Event()))
        self.assertEqual(self.packets, [])

    def test_windows_input_structure_size(self):
        self.assertEqual(ctypes.sizeof(INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


class WorkerTests(unittest.TestCase):
    def test_missing_attack_calibration_blocks_before_any_input(self):
        events = queue.Queue()
        worker = CombatWorker(SimpleNamespace(), events)

        class Capture:
            monitors = [{}, dict(left=0, top=0, width=1920, height=1080)]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        with patch("mss.MSS", Capture):
            worker.run(123, dict(left=10, top=10, width=40, height=40), object(), 0.9)
        kind, payload = events.get_nowait()
        self.assertEqual(kind, "error")
        self.assertIn("校准神圣打击", payload)
        self.assertTrue(worker.cancel.is_set())

    def test_focus_loss_pauses_without_capture_or_input(self):
        native = SimpleNamespace(foreground=lambda: 456)
        events = queue.Queue()
        worker = CombatWorker(native, events)

        class Capture:
            monitors = [{}, dict(left=0, top=0, width=1920, height=1080)]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        worker.cancel.wait = lambda seconds: worker.cancel.set()
        with patch("mss.MSS", Capture):
            rect = dict(left=10, top=10, width=40, height=40)
            worker.run(123, rect, None, 0.9, dict(strike_region=rect, strike_reference=object(),
                                                  judgment_region=rect, judgment_reference=object()))
        self.assertTrue(worker.cancel.is_set())
        kind, payload = events.get_nowait()
        self.assertEqual(kind, "state")
        self.assertIn("等待回到游戏", payload)

    def test_template_similarity(self):
        import numpy as np

        dark = np.full((50, 50, 3), 20, dtype=np.uint8)
        self.assertEqual(icon_similarity(dark, dark), 1)
        self.assertLess(icon_similarity(dark, 255 - dark), 0.9)

    def test_gray_or_black_is_unknown_not_active(self):
        import numpy as np

        gold = np.full((50, 50, 3), (20, 75, 110), dtype=np.uint8)
        bright = np.full((50, 50, 3), (40, 140, 200), dtype=np.uint8)
        gray = np.full((50, 50, 3), 170, dtype=np.uint8)
        self.assertIs(classify_icon(gold, gold)[0], True)
        self.assertIs(classify_icon(bright, gold)[0], False)
        self.assertIsNone(classify_icon(gray, gold)[0])
        self.assertIsNone(classify_icon(np.zeros_like(gold), gold)[0])


if __name__ == "__main__":
    unittest.main()
