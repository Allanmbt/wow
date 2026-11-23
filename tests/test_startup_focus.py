"""复现总开关指令已收到但 GetForegroundWindow 暂时返回 0 的启动回归测试。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wow_assistant.device.windows import WindowsInput
from wow_assistant.ui.app import CombatApp


class Scheduler:
    def __init__(self):
        self.now, self.serial, self.pending = 0.0, 0, {}

    def after(self, delay, callback):
        self.serial += 1
        self.pending[self.serial] = (self.now+delay/1000, callback)
        return self.serial

    def after_cancel(self, identifier):
        self.pending.pop(identifier, None)

    def advance(self, until):
        while self.pending:
            identifier = min(self.pending, key=lambda key: self.pending[key][0])
            when, callback = self.pending[identifier]
            if when > until:
                break
            self.pending.pop(identifier)
            self.now = when
            callback()
        self.now = until


class StartupTests(unittest.TestCase):
    def make_app(self, foreground):
        app = CombatApp.__new__(CombatApp)
        app.root = Scheduler()
        app.selecting, app.hotkeys_ok, app.countdown_id = False, True, None
        app.start_generation = 0
        app.status, app.log, app.worker = Mock(), Mock(), Mock()
        app.bound_window = app.loot_monitor = app.mining_monitor = None
        app.cursor_loot, app.cursor_mining = Mock(), Mock()
        app.loot_status, app.mining_status = Mock(), Mock()
        app.monitor_status, app.detail = Mock(), Mock()
        app.native = SimpleNamespace(foreground=lambda: foreground(app.root.now),
            is_own_window=lambda hwnd: hwnd == 999, is_game_window=lambda hwnd: hwnd == 123)
        app._start_with_window = Mock()
        return app

    def test_null_foreground_then_game_starts_once_without_second_click(self):
        app = self.make_app(lambda now: 0 if now < .6 else 123)
        with patch("wow_assistant.ui.app.time.monotonic", lambda: app.root.now):
            app.start()
            app.root.advance(.5)
            app._start_with_window.assert_not_called()
            app.root.advance(1.2)
        app._start_with_window.assert_called_once_with(123)
        self.assertIsNone(app.countdown_id)

    def test_foreground_must_be_stable_and_reset_after_null(self):
        app = self.make_app(lambda now: 123 if now < .05 or now >= .4 else 0)
        with patch("wow_assistant.ui.app.time.monotonic", lambda: app.root.now):
            app.start()
            app.root.advance(.45)
            app._start_with_window.assert_not_called()
            app.root.advance(.8)
        app._start_with_window.assert_called_once_with(123)

    def test_second_hotkey_press_stops_both_gatherers_and_keeps_mode_selections(self):
        app = self.make_app(lambda now: 123)
        del app._start_with_window
        app.combat_mode = Mock()
        app.combat_mode.get.return_value = False
        app.worker.running = False
        app.cursor_loot.get.return_value = app.cursor_mining.get.return_value = True
        app.events = Mock()
        loot, mining = Mock(), Mock()
        with (patch("wow_assistant.ui.app.time.monotonic", lambda: app.root.now),
              patch("wow_assistant.ui.app.LootMonitor", return_value=loot),
              patch("wow_assistant.ui.app.MiningMonitor", return_value=mining)):
            app.toggle_automation()
            app.root.advance(0.3)
        self.assertEqual(app.bound_window, 123)
        self.assertIs(app.loot_monitor, loot)
        self.assertIs(app.mining_monitor, mining)
        app.toggle_automation()
        self.assertIsNone(app.bound_window)
        self.assertIsNone(app.loot_monitor)
        self.assertIsNone(app.mining_monitor)
        loot.cancel.set.assert_called_once()
        mining.cancel.set.assert_called_once()
        app.worker.cancel.set.assert_called_once()
        loot.stop.assert_called_once()
        mining.stop.assert_called_once()
        app.cursor_loot.set.assert_not_called()
        app.cursor_mining.set.assert_not_called()

    def test_null_foreground_times_out_without_binding_hovered_game(self):
        app = self.make_app(lambda now: 0)
        with patch("wow_assistant.ui.app.time.monotonic", lambda: app.root.now):
            app.start()
            app.root.advance(2.2)
        app._start_with_window.assert_not_called()
        self.assertIsNone(app.countdown_id)
        self.assertFalse(app.root.pending)
        self.assertIn("2 秒", app.status.set.call_args.args[0])

    def test_tool_and_other_apps_are_not_bound_as_game(self):
        app = self.make_app(lambda now: 999 if now < .5 else 321)
        with patch("wow_assistant.ui.app.time.monotonic", lambda: app.root.now):
            app.start()
            app.root.advance(2.2)
        app._start_with_window.assert_not_called()

    def test_manual_stop_cancels_wait_and_stale_callback(self):
        app = self.make_app(lambda now: 0 if now < .6 else 123)
        with patch("wow_assistant.ui.app.time.monotonic", lambda: app.root.now):
            app.start()
            stale = next(iter(app.root.pending.values()))[1]
            app.root.advance(.3)
            app.stop()
            stale()
            app.root.advance(2)
        app._start_with_window.assert_not_called()
        app.worker.stop.assert_called_once()
        self.assertFalse(app.root.pending)

    def test_delayed_start_uses_same_recovery(self):
        app = self.make_app(lambda now: 0 if now < 3.5 else 123)
        with patch("wow_assistant.ui.app.time.monotonic", lambda: app.root.now):
            app.delayed_start()
            app.root.advance(3.3)
            app._start_with_window.assert_not_called()
            app.root.advance(4)
        app._start_with_window.assert_called_once_with(123)

    def test_game_class_or_title_is_recognized(self):
        native = WindowsInput.__new__(WindowsInput)
        for title, kind, expected in (("魔兽世界", "", True), ("", "GxWindowClass", True),
                                      ("World of Warcraft", "", True), ("PowerShell", "ConsoleWindowClass", False)):
            with self.subTest(title=title):
                native.api = SimpleNamespace(GetClassNameW=lambda hwnd, buffer, size: setattr(buffer, "value", kind),
                    GetWindowTextW=lambda hwnd, buffer, size: setattr(buffer, "value", title))
                self.assertEqual(native.is_game_window(123), expected)


if __name__ == "__main__":
    unittest.main()
