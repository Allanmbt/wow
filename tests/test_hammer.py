import queue
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from wow_assistant.combat.policy import AttackPolicy
from wow_assistant.combat.skills import HAMMER_THRESHOLD, HAMMER_UNAVAILABLE, HammerIcon
from wow_assistant.combat.worker import CombatWorker
from wow_assistant.vision.target import DEAD, NON_HOSTILE, UNKNOWN, TargetReading


class HammerTests(unittest.TestCase):
    def test_only_live_targets_strictly_below_threshold_qualify(self):
        self.assertTrue(TargetReading("alive", 20, (199, 1000)).below(HAMMER_THRESHOLD))
        self.assertTrue(TargetReading("alive", 19).below(HAMMER_THRESHOLD))
        for reading in (DEAD, NON_HOSTILE, UNKNOWN, TargetReading("alive"),
                        TargetReading("alive", 20), TargetReading("alive", 19, (200, 1000)),
                        TargetReading("alive", 21, (210, 1000))):
            with self.subTest(reading=reading):
                self.assertFalse(reading.below(HAMMER_THRESHOLD))

    def test_gray_and_countdown_do_not_count_as_ready(self):
        dark = cv2.imdecode(np.frombuffer(HAMMER_UNAVAILABLE.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        bright = np.clip(dark.astype(np.float32) * 1.6, 0, 255).astype(np.uint8)
        cooldown = bright.copy()
        cv2.putText(cooldown, "1", (19, 37), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(cooldown, "1", (19, 37), cv2.FONT_HERSHEY_SIMPLEX, .7, (255, 255, 255), 1, cv2.LINE_AA)
        for icon in (HammerIcon(), HammerIcon(bright)):
            self.assertIs(icon.classify(dark)[0], False)
            self.assertIs(icon.classify(bright)[0], True)
            self.assertIsNot(icon.classify(cooldown)[0], True)

    def test_ready_hammer_preempts_e_and_unready_hammer_yields(self):
        policy = AttackPolicy()
        policy.set_hammer_enabled(True)
        policy.observe(0, dict(judgment=True))
        policy.sent(.2, policy.candidates(.2))
        policy.observe(.22, dict(hammer=True, judgment=True))
        self.assertEqual(policy.candidates(.4, hammer_allowed=True), ("hammer",))
        policy.sent(.4, ("hammer",))
        self.assertEqual(policy.candidates(.6, hammer_allowed=False), ())
        self.assertEqual(policy.candidates(2.25, hammer_allowed=False), ("judgment",))
        policy.observe(2.3, dict(hammer=False, judgment=True))
        self.assertEqual(policy.candidates(2.4, hammer_allowed=True), ("judgment",))
        policy.observe(2.5, dict(hammer=True, judgment=True, sanction=True))
        self.assertEqual(policy.candidates(2.7, hammer_allowed=True), ("sanction",))
        policy.set_hammer_enabled(False)
        self.assertNotIn("hammer", policy.skills)

    def test_disabling_hammer_discards_its_burst_and_keeps_e_ready(self):
        policy = AttackPolicy()
        policy.set_hammer_enabled(True)
        policy.observe(0, dict(hammer=True, judgment=True))
        policy.sent(.2, policy.candidates(.2, hammer_allowed=True))
        policy.set_hammer_enabled(False)
        self.assertEqual(policy.candidates(.41), ("judgment",))
        policy.set_hammer_enabled(True)
        self.assertEqual(policy.candidates(.42, hammer_allowed=True), ("judgment",))

    def test_worker_rechecks_target_and_icon_before_sending_f(self):
        class Capture:
            monitors = [{}, dict(left=0, top=0, width=800, height=600)]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def grab(self, box):
                return np.zeros((box["height"], box["width"], 4), np.uint8)

        region = dict(left=50, top=50, width=32, height=32)
        reference = np.zeros((32, 32, 3), np.uint8)
        execute_target = TargetReading("alive", 19, (190, 1000))
        boundary_target = TargetReading("alive", 20, (200, 1000))
        cases = (
            (execute_target, True, 0, False, "F", "F"),
            (boundary_target, True, 0, False, "F", "E"),
            (execute_target, False, 0, False, "F", "E"),
            (execute_target, True, None, False, "E", "E"),
            (execute_target, True, .4, False, "F", "F"),
            (execute_target, True, 0, True, "F", "E"),
        )
        for fresh_target, fresh_icon, enable_at, disable_before_send, first_attempt, expected in cases:
            with self.subTest(enable_at=enable_at, disable_before_send=disable_before_send,
                              fresh_target=fresh_target, fresh_icon=fresh_icon):
                clock = [0.0]
                sent, attempted = [], []
                disabled = False
                native = Mock(foreground=lambda: 25, modifiers_down=lambda: False)
                worker = CombatWorker(native, queue.Queue())
                icon = Mock()
                icon.classify.side_effect = lambda frame: (fresh_icon if "F" in attempted else True, 1.0)

                def send(key, hwnd, cancel, *, recheck):
                    nonlocal disabled
                    attempted.append(key)
                    if key == "F" and disable_before_send:
                        disabled = True
                    if not recheck():
                        return False
                    sent.append(key)
                    worker.cancel.set()
                    return True

                def wait(seconds):
                    clock[0] += seconds
                    if clock[0] > 1.5:
                        worker.cancel.set()

                def read_target(now, *, force):
                    return fresh_target if force or "F" in attempted else execute_target

                native.send.side_effect = send
                worker.cancel.wait = wait
                options = dict(target_frame=None, strike_region=dict(region, left=18), strike_reference=reference,
                               judgment_region=dict(region, left=114), judgment_reference=reference)
                if enable_at is not None:
                    options["hammer_enabled"] = lambda: not disabled and clock[0] >= enable_at
                with (patch("mss.MSS", Capture), patch("wow_assistant.combat.worker.time.monotonic", lambda: clock[0]),
                      patch("wow_assistant.combat.worker.PlayerVitalsMonitor"),
                      patch("wow_assistant.combat.worker.TargetMonitor", return_value=Mock(sample=read_target)),
                      patch("wow_assistant.combat.worker.HammerIcon", return_value=icon) as load_icon,
                      patch("wow_assistant.combat.worker.classify_icon", return_value=(False, 1.0)),
                      patch("wow_assistant.combat.worker.classify_attack_icon", return_value=(True, 1.0))):
                    worker.run(25, region, reference, .9, options)
                self.assertFalse(any(kind == "error" for kind, _ in worker.events.queue), list(worker.events.queue))
                self.assertEqual(attempted[0], first_attempt)
                self.assertEqual(sent, [expected])
                if enable_at is None:
                    load_icon.assert_not_called()
                    icon.classify.assert_not_called()
