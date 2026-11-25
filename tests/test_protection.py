import queue
import threading
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from wow_assistant.combat.protection import HEAL_RETRY, ProtectionPolicy, ProtectionThresholds
from wow_assistant.combat.worker import CombatWorker
from wow_assistant.device.ch9329 import CH9329Keyboard, KEYBOARD_REPORT, RELEASE_REPORT
from wow_assistant.device.windows import WindowsInput
from wow_assistant.vision.player_vitals import PlayerVitals
from wow_assistant.vision.seal import COMMAND, CURRENT_BADGE, WISDOM, SealDetector, SealReading
from wow_assistant.vision.seal_badge import WISDOM_ASSET
from wow_assistant.vision.target import TargetReading


class ProtectionTests(unittest.TestCase):
    def test_heal_retries_after_cast_window_and_needs_two_fresh_recovery_readings(self):
        policy = ProtectionPolicy()
        first = PlayerVitals(49, 29, 1.0)
        policy.observe(first)
        self.assertTrue(policy.heal_due(1.0, first))
        self.assertFalse(policy.wisdom_due(1.0, first, COMMAND))
        policy.heal_sent(1.0)
        still_low = PlayerVitals(48, 27, 1.0 + HEAL_RETRY - .1)
        self.assertFalse(policy.heal_due(still_low.sampled_at, still_low))
        self.assertTrue(policy.heal_due(1.0 + HEAL_RETRY, still_low))
        healthy = PlayerVitals(54, 26, 5.2)
        policy.observe(healthy)
        policy.observe(healthy)
        self.assertTrue(policy.healing)
        self.assertFalse(policy.heal_due(5.2, healthy))
        policy.observe(PlayerVitals(50, 25, 5.4))
        policy.observe(PlayerVitals(53, 24, 5.6))
        self.assertTrue(policy.healing)
        policy.observe(PlayerVitals(55, 22, 5.8))
        self.assertFalse(policy.healing)
        self.assertTrue(policy.mana_locked)

    def test_low_mana_holds_until_two_samples_strictly_above_70(self):
        policy = ProtectionPolicy()
        policy.observe(PlayerVitals(80, 30, 1))
        self.assertFalse(policy.mana_locked)
        low = PlayerVitals(80, 29, 2)
        policy.observe(low)
        self.assertTrue(policy.wisdom_due(2, low, COMMAND))
        self.assertFalse(policy.wisdom_due(2, low, WISDOM))
        self.assertFalse(policy.wisdom_due(3, low, COMMAND))
        for time, mana in ((3, 70), (4, 71), (5, 70), (6, 75)):
            policy.observe(PlayerVitals(80, mana, time))
            self.assertTrue(policy.mana_locked)
        policy.observe(PlayerVitals(80, 74, 7))
        self.assertFalse(policy.mana_locked)

    def test_healing_and_wisdom_rounds_are_limited_to_three_quick_presses(self):
        policy = ProtectionPolicy()
        policy.observe(PlayerVitals(41, 20, 2.0))
        for now in (2.0, 2.31, 2.62):
            reading = PlayerVitals(41, 20, now)
            self.assertTrue(policy.heal_due(now, reading))
            policy.heal_sent(now)
        self.assertFalse(policy.heal_due(2.94, PlayerVitals(41, 20, 2.94)))
        self.assertFalse(policy.heal_due(6.61, PlayerVitals(41, 20, 6.61)))
        self.assertTrue(policy.heal_due(6.63, PlayerVitals(41, 20, 6.63)))
        policy.observe(PlayerVitals(75, 20, 7.0))
        policy.observe(PlayerVitals(75, 20, 7.2))
        for now in (7.2, 7.51, 7.82):
            self.assertTrue(policy.wisdom_due(now, PlayerVitals(75, 20, now), COMMAND))
            policy.wisdom_sent(now)
        self.assertFalse(policy.wisdom_due(8.14, PlayerVitals(75, 20, 8.14), COMMAND))
        policy.observe_seal(WISDOM, 8.2)
        self.assertFalse(policy.wisdom_due(8.2, PlayerVitals(75, 20, 8.2), WISDOM))

    def test_wisdom_renews_after_thirty_seconds_when_mana_is_still_below_custom_recovery(self):
        policy = ProtectionPolicy(ProtectionThresholds(45, 60, 35, 80))
        policy.observe(PlayerVitals(72, 34, 1.0))
        self.assertTrue(policy.mana_locked)
        policy.observe_seal(WISDOM, 2.0)
        self.assertFalse(policy.wisdom_due(31.9, PlayerVitals(72, 75, 31.9), WISDOM))
        for now in (32.0, 32.31, 32.62):
            self.assertTrue(policy.wisdom_due(now, PlayerVitals(72, 75, now), WISDOM))
            policy.wisdom_sent(now)
        self.assertFalse(policy.wisdom_due(32.94, PlayerVitals(72, 75, 32.94), WISDOM))
        policy.observe(PlayerVitals(72, 80, 33.0))
        self.assertTrue(policy.mana_locked)
        policy.observe(PlayerVitals(72, 81, 33.2))
        policy.observe(PlayerVitals(72, 84, 33.4))
        self.assertFalse(policy.mana_locked)
        self.assertFalse(policy.wisdom_due(63.0, PlayerVitals(72, 84, 63.0), WISDOM))

    def test_custom_health_thresholds_and_validation(self):
        policy = ProtectionPolicy(ProtectionThresholds(45, 60, 25, 75))
        policy.observe(PlayerVitals(46, 30, 1.0))
        self.assertFalse(policy.healing)
        policy.observe(PlayerVitals(44, 30, 1.2))
        self.assertTrue(policy.healing)
        policy.observe(PlayerVitals(51, 30, 1.4))
        self.assertTrue(policy.heal_due(1.4, PlayerVitals(51, 30, 1.4)))
        policy.configure(ProtectionThresholds(43, 49, 25, 75))
        policy.observe(PlayerVitals(51, 30, 1.6))
        policy.observe(PlayerVitals(51, 30, 1.8))
        self.assertFalse(policy.healing)
        with self.assertRaises(ValueError):
            ProtectionThresholds(60, 50)

    def test_wisdom_badge_uses_existing_current_seal_calibration(self):
        image = cv2.imdecode(np.frombuffer(WISDOM_ASSET.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        detector = SealDetector({"current_format": CURRENT_BADGE})
        kind, (x, y, w, h) = detector.locate_current(image)
        self.assertEqual(kind, CURRENT_BADGE)
        self.assertEqual(detector.read_current(image[y:y+h, x:x+w], False, wisdom=True), WISDOM)
        self.assertIsNone(detector.read_current(image, False))

    def test_wisdom_and_potion_use_main_row_digits_and_healing_uses_f4(self):
        keyboard = CH9329Keyboard("COM3")
        keyboard.prepare, keyboard._report = Mock(), Mock()
        native = WindowsInput.__new__(WindowsInput)
        native.input_lock, native._hid_keyboard = threading.Lock(), keyboard
        native._can_send = Mock(return_value=True)
        cancel = threading.Event()
        for key in ("2", "9", "F4"):
            self.assertTrue(native.send(key, 1, cancel, hold_seconds=0))
        usages = [call.args[:2] for call in keyboard._report.call_args_list]
        self.assertEqual(usages, [(KEYBOARD_REPORT, bytes((0, 0, 0x1F, 0, 0, 0, 0, 0))),
                                  (KEYBOARD_REPORT, RELEASE_REPORT),
                                  (KEYBOARD_REPORT, bytes((0, 0, 0x26, 0, 0, 0, 0, 0))),
                                  (KEYBOARD_REPORT, RELEASE_REPORT),
                                  (KEYBOARD_REPORT, bytes((0, 0, 0x3D, 0, 0, 0, 0, 0))),
                                  (KEYBOARD_REPORT, RELEASE_REPORT)])
        self.assertEqual({call.args[0] for call in native._can_send.call_args_list}, {0x32, 0x39, 0x73})

    def test_potion_uses_custom_threshold_and_rearms_only_after_recovery(self):
        policy = ProtectionPolicy(ProtectionThresholds(potion_start=18))
        self.assertEqual(ProtectionThresholds(health_start=12).potion_start, 15)
        for health in (0, 18):
            self.assertFalse(policy.potion_due(1.0, PlayerVitals(health, 60, 1.0)))
        for now in (1.0, 1.31, 1.62):
            self.assertTrue(policy.potion_due(now, PlayerVitals(17, 60, now)))
            policy.potion_sent(now)
        self.assertFalse(policy.potion_due(10.0, PlayerVitals(14, 60, 10.0)))
        policy.observe(PlayerVitals(18, 60, 10.1))
        policy.observe(PlayerVitals(18, 60, 10.2))
        self.assertTrue(policy.potion_due(10.3, PlayerVitals(17, 60, 10.3)))

    def test_worker_prioritizes_potion_and_stops_retries_when_health_rises(self):
        clock = [0.0]
        sent = []
        native = Mock(foreground=Mock(return_value=25), modifiers_down=Mock(return_value=False))
        worker = CombatWorker(native, queue.Queue())

        def send(key, hwnd, cancel, *, recheck):
            if recheck():
                sent.append(key)
                return True
            return False

        def sample(now):
            health = 62 if "F4" in sent else (25 if sent.count("9") >= 2 else 12)
            return PlayerVitals(health, 80, now)

        def wait(seconds):
            clock[0] = round(clock[0] + seconds, 6)
            if clock[0] >= 1.3:
                worker.cancel.set()

        native.send.side_effect = send
        worker.cancel.wait = wait
        with (patch("mss.MSS"), patch("wow_assistant.combat.worker.time.monotonic", lambda: clock[0]),
              patch("wow_assistant.combat.worker.PlayerVitalsMonitor", return_value=Mock(sample=sample))):
            worker.run(25, None, None, .9, dict(combat_enabled=False, protection_enabled=lambda: True))
        self.assertFalse(any(kind == "error" for kind, _ in worker.events.queue), list(worker.events.queue))
        self.assertEqual(sent, ["9", "9", "F4"])

    def test_worker_heals_first_locks_wisdom_then_resumes_dance(self):
        clock = [0.0]
        sent = []
        rect = dict(left=50, top=50, width=32, height=32)
        reference = np.zeros((32, 32, 3), np.uint8)
        target = TargetReading("alive", 80, (800, 1000), True)
        target_monitor = Mock(sample=Mock(return_value=target), reading=target)
        native = Mock(foreground=Mock(return_value=25), modifiers_down=Mock(return_value=False))
        worker = CombatWorker(native, queue.Queue())

        def send(key, hwnd, cancel, *, recheck=None):
            if recheck is None or recheck():
                sent.append((clock[0], key))
                return True
            return False

        def sample(now):
            return PlayerVitals(39 if now < 6.0 else 62, 25 if now < 8.5 else 74, now)

        def wait(seconds):
            clock[0] = round(clock[0] + seconds, 6)
            if clock[0] >= 10.5:
                worker.cancel.set()

        class Capture:
            monitors = [{}, dict(left=0, top=0, width=800, height=600)]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def grab(self, box):
                return np.zeros((box["height"], box["width"], 4), np.uint8)

        class Seals:
            next_sample = 0.0
            reading = SealReading()

            def read_active(self):
                return WISDOM if any(key == "2" for _, key in sent) else COMMAND

            def sample(self, now, *args, **kwargs):
                self.next_sample = now + .15
                active = COMMAND if any(key == "Q" for _, key in sent) else self.read_active()
                self.reading = SealReading(active=active, storage_known=True)
                return self.reading

        native.send.side_effect = send
        worker.cancel.wait = wait
        with (patch("mss.MSS", Capture), patch("wow_assistant.combat.worker.time.monotonic", lambda: clock[0]),
              patch("wow_assistant.combat.worker.PlayerVitalsMonitor", return_value=Mock(sample=sample)),
              patch("wow_assistant.combat.worker.TargetMonitor", return_value=target_monitor),
              patch("wow_assistant.combat.worker.SealMonitor", return_value=Seals()),
              patch("wow_assistant.combat.worker.classify_icon", return_value=(True, 1.0)),
              patch("wow_assistant.combat.worker.classify_attack_icon", return_value=(False, 1.0))):
            worker.run(25, rect, reference, .9, dict(
                protection_enabled=lambda: True, dance_enabled=lambda: True,
                target_frame=None, seal_setup={"current": {}, "storage": {}},
                strike_region=dict(rect, left=100), strike_reference=reference,
                judgment_region=dict(rect, left=150), judgment_reference=reference,
            ))
        self.assertFalse(any(kind == "error" for kind, _ in worker.events.queue), list(worker.events.queue))
        self.assertEqual([key for _, key in sent], ["F4"] * 6 + ["2", "Q"])
        self.assertLess(sent[2][0] - sent[0][0], 1.0)
        self.assertGreaterEqual(sent[3][0], sent[2][0] + HEAL_RETRY)
        self.assertLess(sent[5][0] - sent[3][0], 1.0)
        self.assertGreaterEqual(sent[6][0], 6.2)
        self.assertGreaterEqual(sent[7][0], 8.7)
