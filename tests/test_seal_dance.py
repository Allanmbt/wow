import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from wow_assistant.combat.worker import CombatWorker
from wow_assistant.vision.seal import (ASSET, COMMAND, NO_SEAL, RIGHTEOUS, RIGHTEOUS_STACK, SWITCH_GCD, SWITCH_RETRY_INTERVAL,
                        SealAction, SealDancePolicy, SealDetector, SealMonitor, SealReading, setup_complete)
from wow_assistant.vision.target import DEAD, MISSING, NON_HOSTILE, UNKNOWN, TargetReading, saved_frame


def reading(active, stored=(), three=()):
    return SealReading(active, frozenset(stored), frozenset(three), True)


def target(current, maximum=100, engaged=True):
    return TargetReading("alive", round(100 * current / maximum), (current, maximum), engaged)


class SealDanceTests(unittest.TestCase):
    def test_switch_at_twenty_with_matching_stacks_and_combat_border(self):
        command = reading(COMMAND, (COMMAND,), (COMMAND,))
        righteous = reading(RIGHTEOUS, (RIGHTEOUS,), (RIGHTEOUS,))
        self.assertEqual(SealDancePolicy.eligible(command, target(20)).key, "1")
        self.assertEqual(SealDancePolicy.eligible(righteous, target(20)).key, "Q")
        self.assertIsNone(SealDancePolicy.eligible(command, target(199, 1000)))
        self.assertIsNone(SealDancePolicy.eligible(command, target(100, engaged=False)))
        self.assertIsNone(SealDancePolicy.eligible(command, target(100, engaged=None)))
        self.assertIsNone(SealDancePolicy.eligible(command, TargetReading("alive", 20, engaged=True)))
        self.assertIsNone(SealDancePolicy.eligible(reading(COMMAND, (RIGHTEOUS,), (RIGHTEOUS,)), target(100)))

    def test_default_maintenance_requires_live_target_and_confirmed_seal(self):
        for unavailable in (DEAD, MISSING, NON_HOSTILE, UNKNOWN):
            self.assertIsNone(SealDancePolicy.eligible(reading(NO_SEAL), unavailable))
        self.assertIsNone(SealDancePolicy.eligible(reading(None), target(100)))
        self.assertEqual(SealDancePolicy.eligible(reading(NO_SEAL), target(3)), SealAction(COMMAND))
        self.assertEqual(SealDancePolicy.eligible(reading(RIGHTEOUS), target(100, engaged=False)), SealAction(COMMAND))
        self.assertIsNone(SealDancePolicy.eligible(reading(RIGHTEOUS), target(19)))

    def test_old_three_layers_do_not_block_the_reverse_switch_or_trigger_again(self):
        policy = SealDancePolicy()
        current_target = target(50)
        command = reading(COMMAND, (COMMAND,), (COMMAND,))
        policy.observe(command, 1)
        self.assertIsNone(policy.decide(command, current_target, 1))
        policy.observe(command, 1.2)
        action = policy.decide(command, current_target, 1.2)
        self.assertEqual(action, SealAction(RIGHTEOUS, COMMAND))
        policy.sent(action, 1.2)

        old_still_visible = reading(RIGHTEOUS, (COMMAND, RIGHTEOUS), (COMMAND, RIGHTEOUS))
        policy.observe(old_still_visible, 1.5)
        self.assertIsNone(policy.decide(old_still_visible, current_target, 1.5))
        self.assertEqual(policy.pending, action)
        policy.observe(old_still_visible, 1.7)
        self.assertIsNone(policy.pending)
        self.assertIsNone(policy.decide(old_still_visible, current_target, 2.0))
        reverse = policy.decide(old_still_visible, current_target, 2.3)
        self.assertEqual(reverse, SealAction(COMMAND, RIGHTEOUS))
        policy.sent(reverse, 2.3)

        both_still_visible = reading(COMMAND, (COMMAND, RIGHTEOUS), (COMMAND, RIGHTEOUS))
        policy.observe(both_still_visible, 2.6)
        policy.observe(both_still_visible, 2.8)
        self.assertIsNone(policy.decide(both_still_visible, current_target, 3.6))

        cleared = reading(COMMAND)
        policy.observe(cleared, 3.7)
        policy.observe(cleared, 3.9)
        policy.observe(command, 4.1)
        policy.observe(command, 4.3)
        self.assertEqual(policy.decide(command, current_target, 4.3), action)

    def test_retries_up_to_three_and_stops_when_no_longer_eligible(self):
        policy = SealDancePolicy()
        command = reading(COMMAND, (COMMAND,), (COMMAND,))
        action = SealAction(RIGHTEOUS, COMMAND)
        current_target = target(50)
        now = 1.0
        policy.sent(action, now)
        for attempt in (2, 3):
            policy.observe(command, now + 0.15)
            policy.observe(command, now + 0.30)
            self.assertIsNone(policy.decide(command, current_target, now + 0.40))
            now += SWITCH_RETRY_INTERVAL + 0.01
            self.assertIsNone(policy.decide(command, NON_HOSTILE, now))
            self.assertEqual(policy.decide(command, current_target, now), action)
            policy.sent(action, now)
            self.assertEqual(policy.attempts, attempt)

        policy.observe(command, now + 0.2)
        policy.observe(command, now + 0.50)
        self.assertIsNone(policy.pending)
        self.assertIsNone(policy.decide(command, current_target, now + 0.7))
        self.assertFalse(policy.reserves_attack(command, current_target))

    def test_expired_stack_requires_new_three_layer_evidence(self):
        policy = SealDancePolicy()
        policy.sent(SealAction(RIGHTEOUS, COMMAND), 1)
        expired = reading(COMMAND)
        policy.observe(expired, 16)
        policy.observe(expired, 16.2)
        self.assertIsNone(policy.pending)
        self.assertIsNone(policy.decide(expired, target(100), 16.2))

    def test_losing_target_after_first_attempt_leaves_retries_available(self):
        policy = SealDancePolicy()
        action = SealAction(RIGHTEOUS, COMMAND)
        command = reading(COMMAND, (COMMAND,), (COMMAND,))
        policy.sent(action, 1)
        policy.reset_observations()
        policy.observe(command, 3)
        policy.observe(command, 3.2)
        self.assertEqual(policy.decide(command, target(100), 3.2), action)

    def test_fixed_roles_and_dark_stack_digits(self):
        atlas = cv2.imdecode(np.frombuffer(ASSET.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        command, righteous, bright, dark = np.hsplit(atlas, 4)
        detector = SealDetector({})
        self.assertEqual(detector.read_current(command, False), COMMAND)
        self.assertEqual(detector.read_current(righteous, False), RIGHTEOUS)
        self.assertEqual(detector.read_current(np.zeros_like(command), True), NO_SEAL)
        for icon in (bright, dark):
            stored, three, known = detector.read_storage(icon)
            self.assertTrue(known)
            self.assertEqual(stored, {COMMAND})
            self.assertEqual(three, {COMMAND})
        self.assertFalse(detector.has_three(command))

        righteousness_sample = righteous.copy()
        righteousness_sample[29:48, 30:48] = dark[29:48, 30:48]
        encoded, size = detector.learn_righteous(righteousness_sample)
        profile = {"righteous_icon": encoded, "storage_size": size}
        stored, three, known = SealDetector(profile).read_storage(righteousness_sample)
        self.assertTrue(known)
        self.assertEqual(stored, {RIGHTEOUS})
        self.assertEqual(three, {RIGHTEOUS})

    def test_righteous_sample_works_in_existing_storage_box(self):
        icon = cv2.imdecode(np.frombuffer(RIGHTEOUS_STACK.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        image = np.pad(icon, ((2, 2), (4, 5), (0, 0)))
        detector = SealDetector({})
        self.assertEqual(detector.read_storage(image), (frozenset({RIGHTEOUS}), frozenset({RIGHTEOUS}), True))
        _, size = detector.learn_righteous(image)
        self.assertEqual(size, 45)
        self.assertTrue(setup_complete({"current": {}, "storage": {}}))

    def test_out_of_combat_skips_storage_unless_confirming_a_switch(self):
        monitor = SealMonitor(Mock(), lambda: {}, {})
        monitor._grab = Mock(return_value=None)

        monitor.sample(0.0, False, in_combat=False)
        self.assertEqual([call.args[0] for call in monitor._grab.call_args_list], ["current"])
        self.assertEqual(monitor.next_sample, 0.30)

        monitor._grab.reset_mock()
        monitor.sample(0.3, False, in_combat=False, check_storage=True)
        self.assertEqual([call.args[0] for call in monitor._grab.call_args_list], ["current", "storage"])

    def test_single_slot_calibration_can_recognize_three_layers_in_adjacent_slot(self):
        client = dict(left=0, top=0, width=400, height=250)
        region = dict(left=100, top=120, width=54, height=50)
        nearby = dict(left=44, top=120, width=166, height=50)
        frame = np.zeros((50, 166, 4), np.uint8)
        command = cv2.imdecode(np.frombuffer(ASSET.read_bytes(), np.uint8), cv2.IMREAD_COLOR)[:, 96:144]
        righteous = cv2.imdecode(np.frombuffer(RIGHTEOUS_STACK.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        frame[1:49, 61:109, :3] = command
        frame[2:48, 112:157, :3] = righteous
        cap = SimpleNamespace(monitors=[{}, client], grab=Mock(return_value=frame))
        monitor = SealMonitor(cap, lambda: client, {"storage": saved_frame(region, client)})

        self.assertEqual(monitor.sample(0, False).three, {COMMAND, RIGHTEOUS})
        cap.grab.assert_called_once_with(nearby)

    def test_worker_retries_through_gcd_and_reverses_with_old_stack_visible(self):
        rect = dict(left=10, top=10, width=40, height=40)
        image = np.zeros((40, 40, 3), np.uint8)
        clock, sent = [0.0], []
        changed_at = []
        enabled = [True]

        class Capture:
            monitors = [{}, dict(left=0, top=0, width=640, height=480)]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def grab(self, bounds):
                return np.zeros((bounds["height"], bounds["width"], 4), np.uint8)

        class Target:
            reading = target(20)

            def sample(self, now, *, force=False):
                return self.reading

        class Seals:
            next_sample = 0.0

            def sample(self, now, empty_slot, *, force=False, in_combat=True, check_storage=False):
                self.next_sample = now + 0.15
                if not changed_at:
                    self.reading = reading(COMMAND, (COMMAND,), (COMMAND,))
                else:
                    self.reading = reading(RIGHTEOUS, (COMMAND, RIGHTEOUS), (COMMAND, RIGHTEOUS))
                return self.reading

        def send(key, hwnd, cancel, *, recheck=None):
            if recheck is not None and not recheck():
                return False
            sent.append((clock[0], key))
            attempts = [at for at, pressed in sent if pressed == "1"]
            if key == "1" and clock[0] - attempts[0] >= 1:
                changed_at.append(clock[0])
            if key == "Q":
                enabled[0] = False
            return True

        worker = CombatWorker(SimpleNamespace(foreground=lambda: 123, modifiers_down=lambda: False, send=send), queue.Queue())

        def wait(seconds):
            clock[0] += seconds
            if clock[0] >= 3.0:
                worker.cancel.set()

        worker.cancel.wait = wait
        options = dict(target_frame=None, seal_setup=dict(current=1, storage=1, righteous_icon=1),
                       dance_enabled=lambda: enabled[0])
        for index, key in enumerate(("strike", "judgment"), 1):
            options[key + "_region"] = dict(rect, left=60 * index)
            options[key + "_reference"] = image
        with (patch("mss.MSS", Capture), patch("wow_assistant.combat.worker.time.monotonic", lambda: clock[0]),
              patch("wow_assistant.combat.worker.TargetMonitor", return_value=Target()),
              patch("wow_assistant.combat.worker.SealMonitor", return_value=Seals()),
              patch("wow_assistant.combat.worker.classify_icon", return_value=(False, 0.95)),
              patch("wow_assistant.combat.worker.classify_attack_icon", return_value=(True, 1.0))):
            worker.run(123, rect, image, .90, options)

        self.assertFalse(any(kind == "error" for kind, _ in worker.events.queue))
        flips = [(at, key) for at, key in sent if key in ("1", "Q")]
        self.assertEqual([key for _, key in flips], ["1", "1", "1", "Q"])
        self.assertGreaterEqual(flips[2][0] - flips[0][0], 1)
        self.assertGreaterEqual(flips[3][0], changed_at[0] + SWITCH_GCD)
        self.assertFalse(any(key in ("E", "R") and flips[0][0] < at < flips[3][0] for at, key in sent))
