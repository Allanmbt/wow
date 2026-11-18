"""Q/E 持续战斗、V 自动净化与独立拾取；总开关默认关闭。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from monster_ocr import enable_dpi_awareness, region_on_screen, select_screen_region
from poison_monitor import PoisonDetector, CleansePolicy
from cursor_loot import LootMonitor

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "combat_config.json"
TEMPLATE = BASE / "seal_ready.png"
E_SKILLS = {
    "strike": ("神圣打击", 10.0, BASE / "strike_ready.png"),
    "judgment": ("审判", 8.0, BASE / "judgment_ready.png"),
}


@dataclass
class CombatPolicy:
    """使用单调时钟；不把“按键发送成功”当成“技能施放成功”。"""

    started: float
    retry_interval: float = 2.0
    stable_seconds: float = 0.6
    ready_since: float | None = None
    changed_since: float | None = None
    last_q: float | None = None
    next_e: float = 0.0
    q_guard_until: float = 0.0
    pending: bool = False
    pending_since: float | None = None
    attempts: int = 0
    q_count: int = 0
    e_count: int = 0

    def __post_init__(self):
        self.next_e = self.started + 0.8

    def step(self, now, ready):
        if ready is True:
            self.changed_since = None
            if self.ready_since is None:
                self.ready_since = now
        elif ready is False:
            self.ready_since = None
            if self.changed_since is None:
                self.changed_since = now
            # 公共冷却可能短暂遮挡图标，连续变化 2 秒才记为已开启。
            if self.pending and now - self.changed_since >= 2.0:
                self.pending = False
                self.pending_since = None
                self.attempts = 0
                self.q_guard_until = self.last_q + 27.0
        else:
            # 灰掉、遮挡等未知状态不算圣印成功，等待恢复后继续重试。
            self.ready_since = None
            self.changed_since = None

        stable = self.ready_since is not None and now - self.ready_since >= self.stable_seconds
        retry_due = self.last_q is None or now - self.last_q >= self.retry_interval
        if stable and retry_due and now >= self.q_guard_until:
            self.last_q = now
            if not self.pending:
                self.pending_since = now
            self.pending = True
            self.attempts += 1
            self.q_count += 1
            self.next_e = max(self.next_e, now + 0.15)
            return "Q"
        return None


@dataclass
class AttackSkill:
    name: str
    cooldown: float
    ready_since: float | None = None
    changed_since: float | None = None
    last_sent: float | None = None
    guard_until: float = 0.0
    pending: bool = False
    attempts: int = 0
    retry_after_change: bool = False

    def observe(self, now, state):
        if state is True:
            self.changed_since = None
            if self.ready_since is None:
                self.ready_since = now
        elif state is False:
            if self.pending:
                self.retry_after_change = True
            self.ready_since = None
            if self.changed_since is None:
                self.changed_since = now
            # 短暂公共冷却不记为技能成功，变灰持续 2 秒才建立 CD 保护。
            if self.pending and now - self.changed_since >= 2.0:
                self.pending = False
                self.attempts = 0
                self.guard_until = self.last_sent + self.cooldown
        else:
            self.ready_since = self.changed_since = None

    def due(self, now):
        return (self.ready_since is not None and now - self.ready_since >= 0.15
                and now >= self.guard_until
                and (self.last_sent is None or now - self.last_sent >= 2.0 or self.retry_after_change))


class AttackPolicy:
    """双就绪开启最多 4 次短连发；全冷却立即结束，之后仍按图标触发。"""

    def __init__(self):
        self.skills = {key: AttackSkill(name, cd) for key, (name, cd, _) in E_SKILLS.items()}
        self.last_e = -float("inf")
        self.burst_left = 0
        self.burst_until = 0.0
        self.burst_targets = set()
        self.burst_marked = set()

    def observe(self, now, states):
        for key, skill in self.skills.items():
            skill.observe(now, states.get(key))

    def candidates(self, now):
        if now - self.last_e < 0.18:
            return ()
        if self.burst_left and now <= self.burst_until:
            followup = tuple(key for key in self.burst_targets
                             if self.skills[key].ready_since is not None
                             and now >= self.skills[key].guard_until)
            if followup:
                return followup
            self.burst_left = 0
        candidates = tuple(key for key, skill in self.skills.items() if skill.due(now))
        return candidates

    def sent(self, now, candidates):
        continuing = self.burst_left > 0 and now <= self.burst_until
        if not continuing:
            self.burst_marked = set()
            self.burst_targets = set(candidates)
            self.burst_left = 4 if len(candidates) == 2 else 1
            self.burst_until = now + 0.9
        self.burst_left -= 1
        self.last_e = now
        for key in candidates:
            skill = self.skills[key]
            skill.last_sent, skill.pending = now, True
            skill.retry_after_change = False
            if key not in self.burst_marked:
                skill.attempts += 1
                self.burst_marked.add(key)


def icon_image(frame):
    import cv2

    if frame is None or frame.size == 0 or min(frame.shape[:2]) < 8:
        raise ValueError("图标截图过小，请框选完整的单个技能图标")
    h, width = frame.shape[:2]
    # 排除边框和快捷键字样附近的外圈，保留中心的倒计时覆盖区域。
    frame = frame[max(1, h // 10):h - max(1, h // 10),
                  max(1, width // 10):width - max(1, width // 10)]
    return cv2.GaussianBlur(cv2.resize(frame, (48, 48)), (3, 3), 0)


def icon_similarity(frame, reference):
    import numpy as np

    return 1.0 - float(np.abs(icon_image(frame).astype(np.float32)
                              - icon_image(reference).astype(np.float32)).mean()) / 255.0


def classify_icon(frame, reference, threshold=0.90):
    """三态：True 待用、False 圣印亮起、None 遮挡/眩晕变灰等未知。"""
    import cv2

    score = icon_similarity(frame, reference)
    if score >= threshold:
        return True, score
    current = cv2.cvtColor(icon_image(frame), cv2.COLOR_BGR2HSV)
    baseline = cv2.cvtColor(icon_image(reference), cv2.COLOR_BGR2HSV)
    # 用户截图中圣印开启时底色明显变亮；灰色公共冷却/眩晕不应当作成功。
    bright = current[:, :, 2].mean() >= baseline[:, :, 2].mean() + 20
    colored = current[:, :, 1].mean() >= max(30, baseline[:, :, 1].mean() * 0.6)
    return (False if bright and colored else None), score


def classify_attack_icon(frame, reference):
    """待用模板严格校验中心纹理，避免倒计时最后一两秒被平均相似度漏掉。"""
    import cv2

    current, baseline = icon_image(frame), icon_image(reference)
    score = icon_similarity(frame, reference)
    center = slice(12, 36)
    patch = cv2.cvtColor(current[center, center], cv2.COLOR_BGR2GRAY)
    target = cv2.cvtColor(baseline[center, center], cv2.COLOR_BGR2GRAY)
    if target.std() < 3 or patch.std() < 3:
        return None, score
    correlation = float(cv2.matchTemplate(patch, target, cv2.TM_CCOEFF_NORMED)[0, 0])
    if score >= 0.94 and correlation >= 0.97:
        return True, score
    hsv, ready_hsv = cv2.cvtColor(current, cv2.COLOR_BGR2HSV), cv2.cvtColor(baseline, cv2.COLOR_BGR2HSV)
    gray = hsv[:, :, 1].mean() < ready_hsv[:, :, 1].mean() * 0.65
    dark = hsv[:, :, 2].mean() < ready_hsv[:, :, 2].mean() * 1.15
    return (False if gray and dark else None), score


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class INPUTDATA(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("data", INPUTDATA)]


class WindowsInput:
    def __init__(self):
        self.input_lock = threading.Lock()
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.GetForegroundWindow.restype = w.HWND
        self.api.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.api.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.api.SendInput.restype = w.UINT
        self.api.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
        self.api.ClientToScreen.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
        self.api.WindowFromPoint.argtypes, self.api.WindowFromPoint.restype = [w.POINT], w.HWND
        self.api.GetAncestor.argtypes, self.api.GetAncestor.restype = [w.HWND, w.UINT], w.HWND

    def cursor_in_window(self, hwnd):
        point = w.POINT()
        if not self.api.GetCursorPos(ctypes.byref(point)):
            return False
        target = self.api.WindowFromPoint(point)
        return bool(target) and (self.api.GetAncestor(target, 2) or target) == hwnd

    def right_click(self, hwnd, cancel, recheck):
        with self.input_lock:
            if (cancel.is_set() or self.foreground() != hwnd or self.modifiers_down()
                    or any(self.api.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4))
                    or not self.cursor_in_window(hwnd) or not recheck()):
                return False
            # 比对过程中仍可能被取消，发送前再次检查。
            if cancel.is_set() or self.foreground() != hwnd:
                return False
            down = INPUT(type=0, data=INPUTDATA(mi=MOUSEINPUT(0, 0, 0, 0x0008, 0, 0)))
            up = INPUT(type=0, data=INPUTDATA(mi=MOUSEINPUT(0, 0, 0, 0x0010, 0, 0)))
            if self.api.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT)) != 1:
                raise OSError("右键按下失败")
            try:
                cancel.wait(0.035)
            finally:
                if self.api.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT)) != 1:
                    raise OSError("右键松开失败")
            return True

    def client_bounds(self, hwnd):
        rect, origin = w.RECT(), w.POINT()
        if not self.api.GetClientRect(hwnd, ctypes.byref(rect)) or not self.api.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise OSError("无法读取游戏窗口范围")
        if rect.right < 100 or rect.bottom < 100:
            raise ValueError("游戏窗口不可见或太小")
        return dict(left=origin.x, top=origin.y, width=rect.right, height=rect.bottom)

    def foreground(self):
        return self.api.GetForegroundWindow()

    def is_own_window(self, hwnd):
        pid = w.DWORD()
        self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()

    def modifiers_down(self):
        return any(self.api.GetAsyncKeyState(key) & 0x8000
                   for key in (0x10, 0x11, 0x12, 0x5B, 0x5C))

    def send(self, key, hwnd, cancel):
        with self.input_lock:
            # 最后一次检查靠近 SendInput；焦点丢失后不向其他应用继续打字。
            if cancel.is_set() or self.foreground() != hwnd:
                return False
            vk = ord(key)
            if self.modifiers_down() or self.api.GetAsyncKeyState(vk) & 0x8000:
                return False
            scan = self.api.MapVirtualKeyW(vk, 0)
            down = INPUT(type=1, data=INPUTDATA(ki=KEYBDINPUT(0, scan, 0x0008, 0, 0)))
            up = INPUT(type=1, data=INPUTDATA(ki=KEYBDINPUT(0, scan, 0x0008 | 0x0002, 0, 0)))
            # 按下后短暂保持，让游戏有机会采样；finally 总会松开，不留下长按。
            if self.api.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT)) != 1:
                raise OSError("SendInput 按下失败，请检查游戏和工具的权限级别是否一致")
            try:
                cancel.wait(0.035)
            finally:
                if self.api.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT)) != 1:
                    raise OSError("SendInput 松开失败，自动战斗已停止")
            return True


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", w.POINT), ("mouseData", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class Hotkeys:
    """专用消息线程监听鼠标 XButton2，并注册 F10 紧急停止。"""

    def __init__(self, events):
        self.events = events
        self.thread_id = None
        self.mouse_pressed = False
        self.closing = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="Hotkeys")
        self.thread.start()

    def handle_mouse(self, message, mouse_data):
        if message not in (0x020B, 0x020C) or (mouse_data >> 16) & 0xFFFF != 2:
            return False
        if message == 0x020B:
            if not self.mouse_pressed:
                self.mouse_pressed = True
                self.events.put(("toggle", None))
        else:
            self.mouse_pressed = False
        # 消费这个侧键，避免同时触发浏览器前进或游戏中的原侧键绑定。
        return True

    def run(self):
        api = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        msg = w.MSG()
        api.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self.thread_id = kernel.GetCurrentThreadId()
        registered = []
        hook = None
        try:
            hook_proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
            api.SetWindowsHookExW.argtypes = [ctypes.c_int, hook_proc_type, w.HINSTANCE, w.DWORD]
            api.SetWindowsHookExW.restype = w.HANDLE
            api.CallNextHookEx.argtypes = [w.HANDLE, ctypes.c_int, w.WPARAM, w.LPARAM]
            api.CallNextHookEx.restype = ctypes.c_ssize_t
            api.UnhookWindowsHookEx.argtypes = [w.HANDLE]
            kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
            kernel.GetModuleHandleW.restype = w.HMODULE

            @hook_proc_type
            def mouse_hook(code, message, pointer):
                if code == 0 and message in (0x020B, 0x020C):
                    event = ctypes.cast(pointer, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    if self.handle_mouse(message, event.mouseData):
                        return 1
                return api.CallNextHookEx(None, code, message, pointer)

            # 回调在消息线程内一直保有引用，只入队，不在钩子中截图或发键。
            hook = api.SetWindowsHookExW(14, mouse_hook, kernel.GetModuleHandleW(None), 0)
            if not hook:
                raise OSError(f"鼠标侧键监听失败（错误 {ctypes.get_last_error()}）")
            for identifier, vk in ((2, 0x79),):
                if not api.RegisterHotKey(None, identifier, 0x4000, vk):
                    raise OSError(f"热键注册失败（错误 {ctypes.get_last_error()}），请关闭其他实例或占用热键的程序")
                registered.append(identifier)
            self.events.put(("hotkeys", "鼠标上侧键 XButton2 开关 / F10 紧急停止 已就绪"))
            while not self.closing.is_set() and api.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:
                    self.events.put(("stop", None))
        except Exception as exc:
            self.events.put(("hotkey_error", str(exc)))
        finally:
            if hook:
                api.UnhookWindowsHookEx(hook)
            for identifier in registered:
                api.UnregisterHotKey(None, identifier)

    def close(self):
        self.closing.set()
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        self.thread.join(timeout=0.5)


class CombatWorker:
    def __init__(self, native, events):
        self.native, self.events = native, events
        self.cancel = threading.Event()
        self.thread = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive() and not self.cancel.is_set()

    def stop(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(timeout=1.0)

    def start(self, hwnd, region, reference, threshold, options=None):
        self.stop()
        if self.thread and self.thread.is_alive():
            raise RuntimeError("上一轮仍在结束，请稍后再开启")
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=self.run, args=(hwnd, region, reference, threshold, options or {}),
                                       daemon=True, name="Combat")
        self.thread.start()

    def run(self, hwnd, region, reference, threshold, options=None):
        options = options or {}
        try:
            import cv2
            import mss
            import numpy as np
            with mss.MSS() as capture:
                if not region_on_screen(region, capture.monitors[1:]):
                    raise ValueError("圣印区域已超出屏幕，请重新定位")
                for key, (name, _, _) in E_SKILLS.items():
                    if not region_on_screen(options.get(key + "_region"), capture.monitors[1:]) or options.get(key + "_reference") is None:
                        raise ValueError(f"请先校准{name}的就绪图标，再开启自动攻击")
                poison_region = options.get("poison_region")
                detector = None
                if poison_region:
                    try:
                        if not region_on_screen(poison_region, capture.monitors[1:]):
                            raise ValueError("减益栏超出屏幕，请重新校验")
                        detector = PoisonDetector(options.get("poison_icon_size"))
                    except Exception as exc:
                        self.events.put(("monitor", f"净化未启用：{exc}"))
                cleanse = CleansePolicy()
                poison_hit, last_poison_capture = None, -float("inf")
                policy, attacks = CombatPolicy(time.monotonic()), AttackPolicy()
                states = {key: None for key in E_SKILLS}
                ready, last_capture, last_update = None, -float("inf"), 0
                while not self.cancel.is_set():
                    if self.native.foreground() != hwnd:
                        policy.ready_since = policy.changed_since = None
                        for skill in attacks.skills.values():
                            skill.ready_since = skill.changed_since = None
                        last_capture = -float("inf")
                        poison_hit = None
                        cleanse.absent_since = None
                        last_poison_capture = -float("inf")
                        now = time.monotonic()
                        if now-last_update >= 0.4:
                            self.events.put(("state", "已开启 · 等待回到游戏"))
                            last_update = now
                        self.cancel.wait(0.1)
                        continue
                    now = time.monotonic()
                    # 50ms 轮询让 180ms 短连发前拿到较新的技能状态。
                    if now - last_capture >= 0.05:
                        frame = cv2.cvtColor(np.array(capture.grab(region)), cv2.COLOR_BGRA2BGR)
                        ready, _ = classify_icon(frame, reference, threshold)
                        for key in E_SKILLS:
                            image = cv2.cvtColor(np.array(capture.grab(options[key + "_region"])), cv2.COLOR_BGRA2BGR)
                            states[key], _ = classify_attack_icon(image, options[key + "_reference"])
                        now = time.monotonic()
                        attacks.observe(now, states)
                        last_capture = now
                    if detector and now-last_poison_capture >= 0.10:
                        image = cv2.cvtColor(np.array(capture.grab(poison_region)), cv2.COLOR_BGRA2BGR)
                        poison_hit = detector.find(image)
                        last_poison_capture = time.monotonic()
                        if cleanse.observe(last_poison_capture, poison_hit):
                            self.events.put(("log", "V 后中毒图标持续消失，已确认解除"))
                    now = time.monotonic()
                    if (cleanse.due(now, poison_hit) and not self.native.modifiers_down()
                            and self.native.send("V", hwnd, self.cancel)):
                        cleanse.sent(time.monotonic())
                        self.events.put(("monitor", f"已发送 V · 等待中毒图标消失 · 累计 {cleanse.count} 次"))
                    elif detector and now-last_update >= 0.4:
                        self.events.put(("monitor", "V 净化 · 未发现中毒" if poison_hit is None else "V 净化 · 中毒仍在，等待冷却后重试"))
                    if not self.native.modifiers_down() and now >= cleanse.block_until:
                        key = policy.step(now, ready)
                        targets = ()
                        if key is None and now >= policy.next_e:
                            targets = attacks.candidates(now)
                            if targets:
                                key = "E"
                        if key and self.native.send(key, hwnd, self.cancel):
                            if key == "E":
                                attacks.sent(time.monotonic(), targets)
                                policy.e_count += 1
                            else:
                                self.events.put(("log", "Q 已发送，等待圣印图标确认"))
                    if now - last_update >= 0.4:
                        self.events.put(("state", f"运行中 · Q {policy.q_count}  E {policy.e_count}  V {cleanse.count}"))
                        last_update = now
                    self.cancel.wait(0.01)
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            self.cancel.set()
            self.events.put(("ended", None))


class CombatApp:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.events = root, queue.Queue()
        self.native = WindowsInput()
        self.worker = CombatWorker(self.native, self.events)
        self.hotkeys = Hotkeys(self.events)
        self.loot_monitor = None
        self.last_game_hwnd = None
        self.loot_arming_id = None
        self.region, self.reference, self.threshold = None, None, 0.90
        self.e_regions = {key: None for key in E_SKILLS}
        self.e_references = {key: None for key in E_SKILLS}
        self.selecting, self.hotkeys_ok, self.countdown_id = False, False, None
        self.cursor_loot = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="已停止")
        self.detail = tk.StringVar(value="正在注册鼠标侧键/F10……")
        self.poison_region, self.poison_icon_size = None, None
        self.monitor_status = tk.StringVar(value="V 净化 · 首次请校验减益栏")
        self.loot_status = tk.StringVar(value="自动拾取：关闭")
        self.regions_label = tk.StringVar()
        root.title("毒哥助手 · Q / E / V")
        root.geometry("720x470")
        root.minsize(700, 450)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6f8")
        style.configure("TLabel", background="#f4f6f8", foreground="#263445", font=("Microsoft YaHei", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei", 19, "bold"), foreground="#162638")
        style.configure("Accent.TLabel", foreground="#14796c", font=("Microsoft YaHei", 11, "bold"))
        style.configure("Muted.TLabel", foreground="#687787", font=("Microsoft YaHei", 9))
        style.configure("Poison.TLabel", foreground="#8a5911")
        style.configure("TButton", padding=(10, 6), font=("Microsoft YaHei", 10))
        style.configure("Primary.TButton", background="#176c8c", foreground="white")
        style.configure("TCheckbutton", background="#f4f6f8", font=("Microsoft YaHei", 10))
        panel = ttk.Frame(root, padding=22)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="毒哥助手", style="Title.TLabel").pack(anchor="w")
        ttk.Label(panel, text="上侧键 开 / 关     ·     F10 停止全部", style="Accent.TLabel").pack(anchor="w", pady=(4, 14))
        row = ttk.Frame(panel)
        row.pack(fill="x", pady=(0, 12))
        ttk.Button(row, text="3 秒后开启", style="Primary.TButton", command=self.delayed_start).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="停止全部", command=self.stop_all).pack(side="left")
        ttk.Checkbutton(row, text="自动拾取 · ≥50% 原地右键", variable=self.cursor_loot,
                        command=self.save_loot_toggle).pack(side="right")
        ttk.Separator(panel).pack(fill="x", pady=(0, 12))
        ttk.Label(panel, textvariable=self.status, style="Accent.TLabel", wraplength=660).pack(anchor="w")
        ttk.Label(panel, textvariable=self.monitor_status, style="Poison.TLabel", wraplength=660).pack(anchor="w", pady=6)
        ttk.Label(panel, textvariable=self.loot_status, style="Muted.TLabel", wraplength=660).pack(anchor="w")
        ttk.Separator(panel).pack(fill="x", pady=12)
        ttk.Label(panel, text="首次设置 · 校验后自动保存", style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(panel)
        row.pack(fill="x")
        for label, action in (("圣印", lambda: self.calibrate("seal")),
                              ("神圣打击", lambda: self.calibrate("strike")),
                              ("审判", lambda: self.calibrate("judgment")),
                              ("校验减益栏 · V", self.calibrate_poison)):
            ttk.Button(row, text=label, command=action).pack(side="left", padx=(0, 6))
        ttk.Label(panel, textvariable=self.regions_label, style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(8, 4))
        ttk.Label(panel, text="Q 圣印 · E 攻击 · V 净化。战斗结束继续运行，侧键手动停止。", style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(8, 0))
        ttk.Label(panel, textvariable=self.detail, style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(4, 0))
        self.load()
        self.update_labels()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(30, self.poll)
        if self.cursor_loot.get():
            self.loot_status.set("拾取已选 · 回游戏按侧键绑定窗口")

    def log(self, text):
        print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

    def load(self):
        import cv2
        import numpy as np
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
            loot = data.get("cursor_loot", False)
            if type(loot) is not bool:
                raise ValueError("cursor_loot 必须为布尔值")
            self.cursor_loot.set(loot)
            self.threshold = float(data.get("ready_threshold", 0.90))
            if not 0.8 <= self.threshold <= 0.99:
                raise ValueError("圣印阈值无效")
            self.region = data.get("seal_region")
            self.poison_region = data.get("poison_region")
            self.poison_icon_size = data.get("poison_icon_size")
            if self.poison_region and self.poison_icon_size:
                self.monitor_status.set("V 净化 · 减益栏已校验")
            def read(path):
                return cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR) if path.exists() else None
            # 手动更换本地图片后也允许加载；比较时归一化尺寸，不要求与旧坐标等大。
            self.reference = read(TEMPLATE)
            for key, (_, _, path) in E_SKILLS.items():
                self.e_regions[key], self.e_references[key] = data.get(key + "_region"), read(path)
            self.status.set("就绪 · 上侧键开启")
            # 清理旧版的 R / 血条 / 经验 / 拾取配置，保留三技能坐标与原图片。
            if CONFIG.exists() and set(data) - {"cursor_loot", "ready_threshold", "seal_region", "strike_region", "judgment_region", "poison_region", "poison_icon_size"}:
                self.persist()
        except Exception as exc:
            self.status.set(f"配置加载失败，请检查：{exc}")

    def persist(self):
        data = dict(ready_threshold=self.threshold,
                    poison_region=self.poison_region, poison_icon_size=self.poison_icon_size,
                    seal_region=self.region, cursor_loot=bool(self.cursor_loot.get()))
        data.update({key + "_region": rect for key, rect in self.e_regions.items()})
        temporary = CONFIG.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(CONFIG)

    def update_labels(self):
        skills = [("圣印", self.region, self.reference)] + [
            (name, self.e_regions[k], self.e_references[k]) for k, (name, _, _) in E_SKILLS.items()]
        self.regions_label.set(" / ".join(f"{name}：{'已保存' if rect and image is not None else '待定位'}"
                                         for name, rect, image in skills) + (" / 减益栏：已校验" if self.poison_region else " / 减益栏：待校验"))

    def stop(self):
        if self.countdown_id is not None:
            self.root.after_cancel(self.countdown_id)
            self.countdown_id = None
        self.worker.stop()
        self.status.set("已停止")

    def stop_loot(self):
        if self.loot_arming_id is not None:
            self.root.after_cancel(self.loot_arming_id)
            self.loot_arming_id = None
        if self.loot_monitor is not None:
            self.loot_monitor.stop()
            self.loot_monitor = None

    def stop_all(self):
        self.stop()
        self.stop_loot()
        self.cursor_loot.set(False)
        self.loot_status.set("自动拾取：已关闭")
        try:
            self.persist()
        except Exception as exc:
            self.log(f"保存停止状态失败：{exc}")

    def save_loot_toggle(self):
        # 独立开关不改变 Q/E/V，不等待攻击线程的状态。
        self.stop_loot()
        try:
            self.persist()
            if not self.cursor_loot.get():
                self.loot_status.set("自动拾取：关闭")
            elif self.last_game_hwnd:
                self.arm_loot(self.last_game_hwnd)
            else:
                self.loot_status.set("3 秒内切回游戏，将只开启自动拾取，不启动 Q/E")
                self.loot_arming_id = self.root.after(3000, self.arm_loot)
        except Exception as exc:
            self.loot_status.set(f"自动拾取设置失败：{exc}")

    def arm_loot(self, hwnd=None):
        if self.loot_arming_id is not None:
            self.root.after_cancel(self.loot_arming_id)
        self.loot_arming_id = None
        if not self.cursor_loot.get():
            return
        if not self.hotkeys_ok:
            self.cursor_loot.set(False)
            self.loot_status.set("自动拾取未开启：侧键/F10 未就绪，请关闭旧实例再重启")
            self.persist()
            return
        hwnd = hwnd or self.native.foreground()
        if not hwnd or self.native.is_own_window(hwnd):
            self.loot_status.set("请切回游戏后按侧键开启，或取消再勾选自动拾取")
            return
        if self.loot_monitor and self.loot_monitor.running and self.loot_monitor.hwnd == hwnd:
            return
        self.stop_loot()
        try:
            self.loot_monitor = LootMonitor(self.native, hwnd, self.events)
            self.last_game_hwnd = hwnd
            self.loot_status.set("自动拾取：已绑定游戏窗口，直接比对 bag.jpg，无需校准")
        except Exception as exc:
            self.cursor_loot.set(False)
            self.loot_status.set(f"自动拾取无法开启：{exc}")
            self.persist()

    def calibrate(self, key):
        if self.selecting:
            return
        self.stop()
        self.selecting = True
        self.log("框选一个完整就绪图标；圣印需未开启，神圣打击/审判需无倒计时")
        self.root.withdraw()
        def selected(region, frame):
            try:
                import cv2
                if not (16 <= region["width"] <= 200 and 16 <= region["height"] <= 200
                        and 0.65 <= region["width"] / region["height"] <= 1.5 and frame.std() >= 8):
                    raise ValueError("请仅框选一个完整技能图标（16～200 像素）")
                if key != "seal":
                    center = cv2.cvtColor(icon_image(frame)[12:36, 12:36], cv2.COLOR_BGR2GRAY)
                    if center.std() < 3:
                        raise ValueError("图标中心缺少纹理，请重新框选")
                path = TEMPLATE if key == "seal" else E_SKILLS[key][2]
                ok, encoded = cv2.imencode(".png", frame)
                if not ok:
                    raise ValueError("PNG 编码失败")
                path.write_bytes(encoded.tobytes())
                if key == "seal":
                    self.region, self.reference = region, frame
                else:
                    self.e_regions[key], self.e_references[key] = region, frame
                self.persist()
                self.update_labels()
                self.status.set("图片与坐标已本地保存，下次启动无需再定位")
            except Exception as exc:
                self.status.set(f"定位失败：{exc}")
        self.root.after(200, lambda: select_screen_region(self.root, selected,
                           lambda: setattr(self, "selecting", False), include_frame=True))

    def calibrate_poison(self):
        if self.selecting:
            return
        self.stop()
        self.selecting = True
        self.monitor_status.set("请在中毒时框选自己的整排减益图标（含图标，不必包含倒计时）")
        self.root.withdraw()
        def selected(region, frame):
            try:
                if not (20 <= region["height"] <= 400 and 20 <= region["width"] <= 1200):
                    raise ValueError("请框选自己的减益栏，范围应在 1200×400 像素以内")
                hit = PoisonDetector().find(frame)
                if hit is None:
                    raise ValueError("未核验到这两种中毒图标，请在中毒时重新框选")
                old_region, old_size = self.poison_region, self.poison_icon_size
                self.poison_region, self.poison_icon_size = dict(region), hit.size
                try:
                    self.persist()
                except Exception:
                    self.poison_region, self.poison_icon_size = old_region, old_size
                    raise
                self.update_labels()
                self.monitor_status.set(f"V 净化 · 校验成功 {hit.score:.0%} · 已保存，下次直接使用")
            except Exception as exc:
                self.monitor_status.set(f"净化校验失败：{exc}")
        self.root.after(200, lambda: select_screen_region(self.root, selected,
                           lambda: setattr(self, "selecting", False), include_frame=True))

    def delayed_start(self):
        self.stop()
        if not self.selecting:
            self.status.set("3 秒后开启，请切回游戏")
            self.countdown_id = self.root.after(3000, self.start)

    def start(self):
        self.countdown_id = None
        if self.selecting:
            return
        if not self.hotkeys_ok:
            self.status.set("鼠标侧键/F10 尚未就绪，请关闭旧实例")
            return
        hwnd = self.native.foreground()
        if not hwnd or self.native.is_own_window(hwnd):
            self.status.set("请切回游戏按鼠标上侧键开启")
            return
        try:
            options = dict(poison_region=self.poison_region, poison_icon_size=self.poison_icon_size)
            import mss
            with mss.MSS() as capture:
                if self.reference is None or not region_on_screen(self.region, capture.monitors[1:]):
                    raise ValueError("请先定位圣印图标（2）")
                for key, (name, _, _) in E_SKILLS.items():
                    image, rect = self.e_references[key], self.e_regions[key]
                    if image is None or not region_on_screen(rect, capture.monitors[1:]):
                        raise ValueError(f"请先定位{name}就绪图标")
                    options[key + "_region"], options[key + "_reference"] = dict(rect), image.copy()
            self.worker.start(hwnd, dict(self.region), self.reference.copy(), self.threshold, options)
            self.last_game_hwnd = hwnd
            if self.cursor_loot.get():
                self.arm_loot(hwnd)
            self.status.set("已开启；侧键/F10 停止")
            self.log("自动攻击已开启")
        except Exception as exc:
            self.status.set(str(exc))

    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "toggle":
                    self.stop() if self.worker.running or self.countdown_id is not None else self.start()
                elif kind == "stop":
                    self.stop_all()
                elif kind == "hotkeys":
                    self.hotkeys_ok = True
                    self.detail.set("快捷键已就绪 · 切出暂停发键，切回继续")
                elif kind == "hotkey_error":
                    self.hotkeys_ok = False
                    self.stop_all()
                    self.detail.set(payload)
                elif kind == "state" and self.worker.running:
                    self.status.set(payload)
                elif kind in ("error", "finished"):
                    self.status.set(payload)
                    self.log(payload)
                elif kind == "monitor":
                    self.monitor_status.set(payload)
                elif kind == "loot" and self.cursor_loot.get():
                    owner, text = payload
                    if owner is self.loot_monitor:
                        self.loot_status.set(text)
                elif kind == "loot_error":
                    owner, text = payload
                    if owner is not self.loot_monitor:
                        continue
                    self.cursor_loot.set(False)
                    self.stop_loot()
                    self.loot_status.set(text)
                    self.log(text)
                    try:
                        self.persist()
                    except Exception as exc:
                        self.log(f"保存拾取状态失败：{exc}")
                elif kind == "log":
                    self.log(payload)
        except queue.Empty:
            pass
        self.root.after(30, self.poll)

    def close(self):
        self.stop()
        self.stop_loot()
        self.hotkeys.close()
        self.root.destroy()


def main():
    if sys.platform != "win32":
        print("仅支持 Windows 10/11")
        return 1
    enable_dpi_awareness()
    import tkinter as tk
    root = tk.Tk()
    app = CombatApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
