import queue
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from wow_assistant.combat.policy import TargetGrace
from wow_assistant.combat.worker import CombatWorker
from wow_assistant.vision.target import DEAD, MISSING, NON_HOSTILE, REFERENCE, UNKNOWN, TargetFrameDetector, TargetMonitor, saved_frame


def sample_frames():
    dead = cv2.imdecode(np.frombuffer(REFERENCE.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    dead[5:21, 6:152] = (16, 30, 118)
    unknown = dead.copy()
    unknown[27:43, 3:153] = (10, 20, 33)

    def live(length):
        frame = unknown.copy()
        frame[27:43, 3:3+length] = (27, 150, 41)
        cv2.putText(frame, "500 / 999", (45, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
        return frame

    return live(150), live(75), dead, unknown, np.zeros_like(dead)


class TargetFrameTests(unittest.TestCase):
    def test_read_alive_dead_missing_and_uncertain(self):
        full, half, dead, unknown, missing = sample_frames()
        detector = TargetFrameDetector()
        self.assertEqual(detector.read(full).percent, 100)
        self.assertEqual(detector.read(half).percent, 50)
        self.assertEqual(detector.read(dead), DEAD)
        self.assertEqual(detector.read(unknown), UNKNOWN)
        self.assertEqual(detector.read(missing), MISSING)

    def test_green_and_yellow_name_backgrounds_are_not_eligible_even_with_full_health(self):
        full = sample_frames()[0]
        detector = TargetFrameDetector()
        for color in ((12, 112, 25), (18, 123, 161)):
            with self.subTest(color=color):
                frame = full.copy()
                frame[5:21, 6:152] = color
                cv2.putText(frame, "47", (56, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (3, 10, 220), 1)
                self.assertEqual(detector.read(frame), NON_HOSTILE)
                self.assertIsNone(detector.locate(frame))

    def test_locate_from_screen_and_reuse_relative_coordinates(self):
        frame = sample_frames()[1]
        desktop = np.zeros((440, 700, 4), np.uint8)
        desktop[264:324, 440:597, :3] = frame
        client = dict(left=0, top=0, width=690, height=400)
        saved = []

        class Capture:
            monitors = [{}, dict(left=0, top=0, width=700, height=440)]

            def __init__(self):
                self.regions = []

            def grab(self, region):
                self.regions.append(region)
                x, y, w, h = (region[key] for key in ("left", "top", "width", "height"))
                return desktop[y:y+h, x:x+w]

        capture = Capture()
        monitor = TargetMonitor(capture, lambda: client, None, saved.append)
        self.assertEqual(monitor.sample(3).percent, 50)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0], saved_frame(dict(left=440, top=264, width=157, height=60), client))

        desktop[264:324, 440:597] = 0
        desktop[269:329, 447:604, :3] = frame
        client.update(left=7, top=5)
        capture.regions.clear()
        restarted = TargetMonitor(capture, lambda: client, saved[0], saved.append)
        self.assertEqual(restarted.sample(0).percent, 50)
        self.assertEqual(capture.regions, [dict(left=447, top=269, width=237, height=60)])
        self.assertEqual(len(saved), 1)

    def test_locates_smaller_frame(self):
        half = cv2.resize(sample_frames()[1], (118, 45))
        image = np.zeros((160, 320, 3), np.uint8)
        image[50:95, 120:238] = half
        region, reading = TargetFrameDetector().locate(image)
        self.assertEqual(region, dict(left=120, top=50, width=118, height=45))
        self.assertLessEqual(abs(reading.percent - 50), 2)

    def test_grace_starts_at_first_loss_and_resets_on_next_hostile_target(self):
        grace = TargetGrace()
        alive = TargetFrameDetector().read(sample_frames()[0])
        grace.observe(MISSING, 0)
        self.assertFalse(grace.allows_attack(MISSING, 0))
        grace.observe(alive, 1)
        grace.observe(DEAD, 2)
        grace.observe(UNKNOWN, 6)
        self.assertTrue(grace.allows_attack(UNKNOWN, 16.99))
        self.assertFalse(grace.allows_attack(UNKNOWN, 17))
        grace.observe(alive, 18)
        grace.observe(NON_HOSTILE, 19)
        grace.observe(MISSING, 20)
        self.assertFalse(grace.allows_attack(NON_HOSTILE, 20))
        self.assertFalse(grace.allows_attack(MISSING, 20))
        self.assertEqual(grace.remaining(20), 14)
        grace.observe(alive, 21)
        grace.observe(MISSING, 22)
        self.assertTrue(grace.allows_attack(MISSING, 36.99))
        self.assertFalse(grace.allows_attack(MISSING, 37))

    def test_worker_attacks_during_grace_but_blocks_friendly_and_expires(self):
        full, half, dead, unknown, missing = sample_frames()
        client = dict(left=0, top=0, width=690, height=400)
        bounds = dict(left=440, top=264, width=157, height=60)
        seal = dict(left=10, top=20, width=40, height=40)
        reference = np.zeros((40, 40, 3), np.uint8)
        clock, sent, rejected = [0.0], [], []
        skill_grabs = []
        blocked_at_send = [False]
        friendly = half.copy()
        friendly[5:21, 6:152] = (15, 105, 35)

        def target_frame():
            if blocked_at_send[0]:
                return friendly
            return next(frame for deadline, frame in (
                (1.0, missing), (2.8, full), (3.6, dead), (4.1, unknown), (4.7, friendly),
                (6.9, full), (8.0, half), (26, missing),
            ) if clock[0] < deadline)

        class Capture:
            monitors = [{}, client]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def grab(self, rect):
                if rect == dict(bounds, width=237):
                    frame = np.pad(target_frame(), ((0, 0), (0, 80), (0, 0)))
                    return cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
                if rect["top"] == seal["top"]:
                    skill_grabs.append(clock[0])
                return np.zeros((rect["height"], rect["width"], 4), np.uint8)

        def send(key, hwnd, cancel, *, recheck=None):
            if key == "Q" and 1 < clock[0] < 2.8 and not rejected:
                blocked_at_send[0] = True
                self.assertFalse(recheck())
                blocked_at_send[0] = False
                rejected.append(key)
                return False
            if recheck is not None and not recheck():
                return False
            sent.append((clock[0], key))
            return True

        native = SimpleNamespace(foreground=lambda: 123, modifiers_down=lambda: False, send=send,
                                 client_bounds=lambda hwnd: client)
        worker = CombatWorker(native, queue.Queue())

        def wait(seconds):
            clock[0] += max(seconds, .01)
            if clock[0] >= 25:
                worker.cancel.set()

        worker.cancel.wait = wait
        options = dict(target_frame=saved_frame(bounds, client))
        for index, key in enumerate(("strike", "judgment", "sanction"), 1):
            options[key + "_region"] = dict(seal, left=60 * index)
            options[key + "_reference"] = np.full_like(reference, index)
        with (patch("mss.MSS", Capture), patch("wow_assistant.combat.worker.time.monotonic", lambda: clock[0]),
              patch("wow_assistant.combat.worker.classify_icon", return_value=(True, 1.0)),
              patch("wow_assistant.combat.worker.classify_attack_icon", return_value=(True, 1.0))):
            worker.run(123, seal, reference, .9, options)

        self.assertEqual(rejected, ["Q"])
        self.assertFalse(any(kind == "error" for kind, _ in worker.events.queue))
        self.assertTrue(skill_grabs)
        self.assertTrue(all(1 <= at < 4.36 or 4.7 <= at < 23.26 for at in skill_grabs))
        self.assertTrue(all(1.0 <= at < 4.1 or 4.7 <= at < 23.26 for at, _ in sent))
        self.assertTrue(any(2.8 <= at < 4.1 for at, _ in sent), sent)
        self.assertTrue(any(21 <= at < 23.26 for at, _ in sent), sent)
        for start, end in ((1.0, 2.8), (4.7, 7.1), (8.0, 19.0)):
            self.assertEqual({key for at, key in sent if start <= at < end}, {"Q", "E", "R"}, (start, end, sent))
