"""真实中毒模板、持续战斗和 V 优先级的离线回归验证。"""
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from wow_assistant.combat.worker import CombatWorker
from wow_assistant.ui.app import CombatApp
from wow_assistant.vision.poison import CleansePolicy, PoisonDetector, PoisonHit, POISON_TEMPLATES


def scene(index=0, size=34, x=70):
    image = np.zeros((75, 240, 3), np.uint8)
    template = cv2.imdecode(np.frombuffer(POISON_TEMPLATES[index].read_bytes(), np.uint8), 1)
    image[28:28+size, x:x+size] = cv2.resize(template, (size, size))
    cv2.putText(image, "28 s", (x, 21), cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 255, 255), 1)
    return image


class PoisonTests(unittest.TestCase):
    def test_both_icons_different_size_position_and_timer(self):
        for index in (0, 1):
            for size in (28, 34, 42):
                with self.subTest(index=index, size=size):
                    frame = scene(index, size, x=140)
                    hit = PoisonDetector().find(frame)
                    self.assertIsNotNone(hit)
                    self.assertEqual(hit.name, f"poison_{index+1}")
                    # 首次校验记住尺寸，后续使用同一检测区域，不绑定图标格子。
                    frame[:25] = 255
                    self.assertIsNotNone(PoisonDetector(hit.size).find(frame))

    def test_absent_or_plain_green_is_not_poison(self):
        detector = PoisonDetector(34)
        self.assertIsNone(detector.find(np.zeros((75, 240, 3), np.uint8)))
        self.assertIsNone(detector.find(np.full((75, 240, 3), (5, 180, 15), np.uint8)))

    def test_cleanse_immediately_rechecks_after_gcd_only_while_poison_remains(self):
        policy = CleansePolicy()
        hit = PoisonHit("poison_1", 1, 34)
        self.assertTrue(policy.due(0, hit))
        policy.sent(0)
        self.assertFalse(policy.due(1.59, hit))
        self.assertTrue(policy.due(1.61, hit))
        self.assertFalse(policy.due(10, None))
        self.assertEqual(policy.block_until, 1.5)

    def test_confirmation_needs_persistent_disappearance_after_v(self):
        policy = CleansePolicy()
        self.assertFalse(policy.observe(0, None))
        policy.sent(0)
        self.assertFalse(policy.observe(.1, None))
        self.assertFalse(policy.observe(.2, PoisonHit("poison_2", 1, 34)))
        self.assertFalse(policy.observe(.3, None))
        self.assertTrue(policy.observe(.6, None))
        self.assertEqual(policy.confirmed, 1)
        self.assertFalse(policy.observe(1, None))

    def test_calibration_persists_and_reloads_without_changing_skill_coordinates(self):
        app = CombatApp.__new__(CombatApp)
        app.root, app.stop, app.monitor_status, app.status = Mock(), Mock(), Mock(), Mock()
        app.selecting = False
        app.poison_region = app.poison_icon_size = None
        app.target_frame = None
        app.seal_setup, app.dance_enabled = {}, threading.Event()
        app.protection, app.protection_enabled = Mock(), threading.Event()
        app.protection.get.return_value = False
        app.hammer, app.hammer_enabled = Mock(), threading.Event()
        app.hammer.get.return_value = False
        app.seal_dance, app.seal_menu_button, app.seal_menu = Mock(), Mock(), Mock()
        app.seal_dance.get.return_value = False
        app.region = dict(left=10, top=10, width=40, height=40)
        app.e_regions = dict(strike=app.region.copy(), judgment=app.region.copy())
        app.e_references = dict(strike=None, judgment=None)
        app.threshold, app.cursor_loot, app.calibration_buttons = .9, Mock(), {}
        app.cursor_loot.get.return_value = True
        app.combat_mode, app.cursor_mining = Mock(), Mock()
        app.combat_mode.get.return_value = True
        app.cursor_mining.get.return_value = False
        frame = scene()
        rect = dict(left=100, top=100, width=240, height=75)
        def select(root, selected, closed, **kwargs):
            selected(rect, frame)
            closed()
        with tempfile.TemporaryDirectory() as directory, patch("wow_assistant.ui.calibration.select_screen_region", select):
            config = Path(directory)/"combat_config.json"
            with patch("wow_assistant.ui.app.CONFIG", config):
                app.calibrate_poison()
                app.root.after.call_args.args[1]()
                saved = json.loads(config.read_text(encoding="utf-8"))
                self.assertEqual(saved["poison_region"], rect)
                self.assertEqual(saved["strike_region"], app.region)
                self.assertNotIn("stop_on_error", saved)
                app.poison_region = app.poison_icon_size = None
                app.load()
                self.assertEqual(app.poison_region, rect)
                self.assertIsNotNone(app.poison_icon_size)


class WorkerTests(unittest.TestCase):
    def run_scene(self, poisoned=False, change_focus=False):
        clock, sent = [0.0], []
        cancel = threading.Event()
        def wait(seconds):
            clock[0] += seconds
            if clock[0] >= 3:
                cancel.set()
        cancel.wait = wait
        def foreground():
            return 456 if change_focus and clock[0] < 1 else 123
        def send(key, hwnd, token):
            self.assertEqual(foreground(), 123)
            sent.append((clock[0], key))
            return True
        native = SimpleNamespace(foreground=foreground, modifiers_down=lambda: False, send=send)
        class Capture:
            monitors = [{}, dict(left=0, top=0, width=1920, height=1080)]
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def grab(self, region):
                self_test.assertEqual(foreground(), 123)
                return np.zeros((40, 40, 4), np.uint8)
        self_test = self
        rect = dict(left=10, top=10, width=40, height=40)
        options = dict(stop_on_error=True)
        for key in ("strike", "judgment"):
            options[key+"_region"], options[key+"_reference"] = rect.copy(), object()
        detector = Mock()
        detector.find.side_effect = lambda frame: PoisonHit("poison_1", 1, 34) if clock[0] < .5 else None
        if poisoned:
            options.update(poison_region=rect.copy(), poison_icon_size=34)
        events = queue.Queue()
        worker = CombatWorker(native, events)
        worker.cancel = cancel
        with patch("mss.MSS", Capture), patch("wow_assistant.combat.worker.time.monotonic", lambda: clock[0]), patch("wow_assistant.combat.worker.PoisonDetector", return_value=detector), patch("wow_assistant.combat.worker.classify_icon", return_value=(False, 1)), patch("wow_assistant.combat.worker.classify_attack_icon", return_value=(True, 1)):
            worker.run(123, rect, object(), .9, options)
        self.assertFalse(any(kind == "error" for kind, _ in events.queue))
        return sent

    def test_poison_sends_v_before_attacks_and_waits_for_gcd(self):
        sent = self.run_scene(poisoned=True)
        self.assertEqual(sent[0][1], "V")
        self.assertEqual(sum(key == "V" for _, key in sent), 1)
        attacks = [now for now, key in sent if key == "E"]
        self.assertTrue(attacks)
        self.assertGreaterEqual(attacks[0], 1.5)

    def test_focus_return_resumes_same_session_without_manual_restart(self):
        sent = self.run_scene(change_focus=True)
        self.assertTrue(sent)
        self.assertTrue(all(now >= 1 for now, _ in sent))


if __name__ == "__main__":
    unittest.main()
