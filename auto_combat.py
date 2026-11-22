"""Q/E 持续战斗、V 自动净化与独立拾取；总开关默认关闭。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from monster_ocr import enable_dpi_awareness, region_on_screen, select_screen_region
from poison_monitor import PoisonDetector, CleansePolicy
from cursor_loot import LootMonitor, MiningMonitor
from input_output import HARDWAREINPUT, INPUT, INPUTDATA, KEYBDINPUT, MOUSEINPUT, WindowsInput

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "combat_config.json"
TEMPLATE = BASE / "seal_ready.png"
E_SKILLS = {
    "strike": ("神圣打击", 10.0, BASE / "strike_ready.png"),
    "judgment": ("审判", 8.0, BASE / "judgment_ready.png"),
}
ATTACK_SKILLS = {"sanction": ("制裁", 0.0, BASE / "sanction_ready.png"), **E_SKILLS}  # 制裁冷却由图标决定。
SANCTION_KEY = "R"
SKILL_SAMPLE_INTERVAL = 0.025
POISON_SAMPLE_INTERVAL = 0.050
COMBAT_POLL_INTERVAL = 0.005
AUTOMATION_HOTKEY = "鼠标 5 或 F10"
F10_VK = 0x79
MODIFIER_KEYS = (0x10, 0x11, 0x12, 0x5B, 0x5C)
MODIFIER_EVENTS = (*MODIFIER_KEYS, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)


def validate_skill_regions(regions):
    names = dict(seal="圣印（第2格）", strike="神圣打击（第1格）", judgment="审判（第4格）", sanction="制裁")
    items = [(key, rect) for key, rect in regions.items() if rect]
    for i, (key, a) in enumerate(items):
        for other, b in items[i+1:]:
            width = max(0, min(a["left"]+a["width"], b["left"]+b["width"])-max(a["left"], b["left"]))
            height = max(0, min(a["top"]+a["height"], b["top"]+b["height"])-max(a["top"], b["top"]))
            area = min(a["width"]*a["height"], b["width"]*b["height"])
            if area > 0 and width*height/area > 0.55:
                raise ValueError(f"{names[key]}与{names[other]}框到了同一格，请分别重新定位")


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

    def step(self, now, ready, *, try_send=None):
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
            if try_send is not None and not try_send():
                return None
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
    """制裁优先，随后按审判和神圣打击的共享 E 规则发键。"""

    def __init__(self):
        self.skills = {key: AttackSkill(name, cd) for key, (name, cd, _) in ATTACK_SKILLS.items()}
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
        if self.skills["sanction"].due(now):
            return ("sanction",)
        if self.burst_left and now <= self.burst_until:
            followup = tuple(key for key in self.burst_targets
                             if self.skills[key].ready_since is not None
                             and now >= self.skills[key].guard_until)
            if followup:
                return followup
            self.burst_left = 0
        return tuple(key for key in E_SKILLS if self.skills[key].due(now))

    def sent(self, now, candidates):
        if candidates == ("sanction",):
            self.burst_left = 0  # 制裁发出后，下一次 E 按最新图标重新判定。
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
    return _normalized_icon_similarity(icon_image(frame), icon_image(reference))


def _normalized_icon_similarity(current, baseline):
    import numpy as np

    difference = current.astype(np.float32) - baseline.astype(np.float32)
    return 1.0 - float(np.abs(difference).mean()) / 255.0


def classify_icon(frame, reference, threshold=0.90):
    """三态：True 待用、False 圣印亮起、None 遮挡/眩晕变灰等未知。"""
    import cv2

    current, baseline = icon_image(frame), icon_image(reference)
    score = _normalized_icon_similarity(current, baseline)
    if score >= threshold:
        return True, score
    current = cv2.cvtColor(current, cv2.COLOR_BGR2HSV)
    baseline = cv2.cvtColor(baseline, cv2.COLOR_BGR2HSV)
    # 用户截图中圣印开启时底色明显变亮；灰色公共冷却/眩晕不应当作成功。
    bright = current[:, :, 2].mean() >= baseline[:, :, 2].mean() + 20
    colored = current[:, :, 1].mean() >= max(30, baseline[:, :, 1].mean() * 0.6)
    return (False if bright and colored else None), score


def classify_attack_icon(frame, reference):
    """待用模板严格校验中心纹理，避免倒计时最后一两秒被平均相似度漏掉。"""
    import cv2

    current, baseline = icon_image(frame), icon_image(reference)
    score = _normalized_icon_similarity(current, baseline)
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


def _skill_capture_plan(regions, monitors):
    left = min(rect["left"] for rect in regions.values())
    top = min(rect["top"] for rect in regions.values())
    right = max(rect["left"] + rect["width"] for rect in regions.values())
    bottom = max(rect["top"] + rect["height"] for rect in regions.values())
    combined = dict(left=left, top=top, width=right-left, height=bottom-top)
    total_area = sum(rect["width"] * rect["height"] for rect in regions.values())
    # 相隔较远或跨显示器时，继续分别抓取，避免大面积无效像素。
    if combined["width"] * combined["height"] > 2 * total_area or not region_on_screen(combined, monitors):
        return [(rect, {key: (slice(None), slice(None))}) for key, rect in regions.items()]

    crops = {}
    for key, rect in regions.items():
        x, y = rect["left"] - left, rect["top"] - top
        crops[key] = (slice(y, y + rect["height"]), slice(x, x + rect["width"]))
    return [(combined, crops)]


class LowLevelKeyboardEvent(ctypes.Structure):
    _fields_ = [("virtual_key", w.DWORD), ("scan_code", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra_info", ctypes.c_size_t)]


class LowLevelMouseEvent(ctypes.Structure):
    _fields_ = [("point", w.POINT), ("mouse_data", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra_info", ctypes.c_size_t)]


class Hotkeys:
    """监听实体鼠标 5 和 F10，并转交 Windows 正常处理。"""

    def __init__(self, events):
        self.events = events
        self.thread_id = None
        self.pressed = False
        self.mouse_pressed = False
        self.modified = False
        self.closing = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="Hotkeys")
        self.thread.start()

    def on_toggle(self):
        self.events.put(("toggle", None))

    def keyboard_event(self, message, event, modifiers_down):
        if event.flags & 0x10:  # LLKHF_INJECTED
            return
        down = message in (0x0100, 0x0104)
        if self.pressed and down and event.virtual_key in MODIFIER_EVENTS:
            self.modified = True
        if event.virtual_key != F10_VK:
            return
        if down:
            if not self.pressed:
                self.pressed = True
                self.modified = bool(event.flags & 0x20) or modifiers_down()
        elif self.pressed:
            self.pressed = False
            if not self.modified and not (event.flags & 0x20) and not modifiers_down():
                self.on_toggle()

    def mouse_event(self, message, event):
        if event.mouse_data >> 16 != 2 or event.flags & 0x01:  # XBUTTON2, LLMHF_INJECTED
            return
        if message == 0x020B:  # WM_XBUTTONDOWN
            self.mouse_pressed = True
        elif self.mouse_pressed:  # WM_XBUTTONUP
            self.mouse_pressed = False
            self.on_toggle()

    def run(self):
        api = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
        api.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, w.HMODULE, w.DWORD]
        api.SetWindowsHookExW.restype = w.HANDLE
        api.CallNextHookEx.argtypes = [w.HANDLE, ctypes.c_int, w.WPARAM, w.LPARAM]
        api.CallNextHookEx.restype = ctypes.c_ssize_t
        api.UnhookWindowsHookEx.argtypes = [w.HANDLE]
        api.GetAsyncKeyState.argtypes = [ctypes.c_int]
        api.GetAsyncKeyState.restype = w.SHORT
        kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
        kernel.GetModuleHandleW.restype = w.HMODULE

        def modifiers_down():
            return any(api.GetAsyncKeyState(key) & 0x8000 for key in MODIFIER_KEYS)

        @callback_type
        def keyboard_callback(code, message, data):
            if code == 0 and message in (0x0100, 0x0101, 0x0104, 0x0105) and not self.closing.is_set():
                event = ctypes.cast(data, ctypes.POINTER(LowLevelKeyboardEvent)).contents
                self.keyboard_event(message, event, modifiers_down)
            return api.CallNextHookEx(None, code, message, data)

        @callback_type
        def mouse_callback(code, message, data):
            if code == 0 and message in (0x020B, 0x020C) and not self.closing.is_set():
                event = ctypes.cast(data, ctypes.POINTER(LowLevelMouseEvent)).contents
                self.mouse_event(message, event)
            return api.CallNextHookEx(None, code, message, data)

        msg = w.MSG()
        api.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self.thread_id = kernel.GetCurrentThreadId()
        keyboard_hook = mouse_hook = None
        try:
            module = kernel.GetModuleHandleW(None)
            keyboard_hook = api.SetWindowsHookExW(13, keyboard_callback, module, 0)  # WH_KEYBOARD_LL
            if not keyboard_hook:
                raise OSError(f"F10 监听失败（Windows 错误 {ctypes.get_last_error()}）")
            mouse_hook = api.SetWindowsHookExW(14, mouse_callback, module, 0)  # WH_MOUSE_LL
            if not mouse_hook:
                raise OSError(f"鼠标 5 监听失败（Windows 错误 {ctypes.get_last_error()}）")
            self.events.put(("hotkeys", f"{AUTOMATION_HOTKEY}自动总开关已就绪"))
            while not self.closing.is_set():
                result = api.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0 or self.closing.is_set():
                    break
        except Exception as exc:
            self.events.put(("hotkey_error", str(exc)))
        finally:
            if mouse_hook:
                api.UnhookWindowsHookEx(mouse_hook)
            if keyboard_hook:
                api.UnhookWindowsHookEx(keyboard_hook)

    def close(self):
        self.closing.set()
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        self.thread.join(timeout=0.5)


class CombatWorker:
    def __init__(self, native, events, *, mining_active=None):
        self.native, self.events = native, events
        self.mining_active = mining_active
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

    def _send_attack(self, key, hwnd):
        if self.mining_active is None:
            return self.native.send(key, hwnd, self.cancel)
        # WindowsInput 在共用键鼠锁内、串口握手后再次检查。
        return self.native.send(key, hwnd, self.cancel, recheck=lambda: not self.mining_active())

    def run(self, hwnd, region, reference, threshold, options=None):
        options = options or {}
        try:
            import cv2
            import mss
            import numpy as np
            with mss.MSS() as capture:
                monitors = capture.monitors[1:]
                if not region_on_screen(region, monitors):
                    raise ValueError("圣印区域已超出屏幕，请重新定位")
                skill_regions = {"seal": region}
                skill_references = {"seal": reference}
                for key, (name, _, _) in ATTACK_SKILLS.items():
                    skill_region = options.get(key + "_region")
                    skill_reference = options.get(key + "_reference")
                    if key == "sanction" and (skill_region is None or skill_reference is None):
                        continue
                    if not region_on_screen(skill_region, monitors) or skill_reference is None:
                        raise ValueError(f"请先校准{name}的就绪图标，再开启自动攻击")
                    skill_regions[key] = skill_region
                    skill_references[key] = skill_reference
                capture_plan = _skill_capture_plan(skill_regions, monitors)
                poison_region = options.get("poison_region")
                detector = None
                if poison_region:
                    try:
                        if not region_on_screen(poison_region, monitors):
                            raise ValueError("减益栏超出屏幕，请重新校验")
                        detector = PoisonDetector(options.get("poison_icon_size"))
                    except Exception as exc:
                        self.events.put(("monitor", f"净化未启用：{exc}"))
                cleanse = CleansePolicy()
                poison_hit = None
                next_skill_capture = next_poison_capture = 0.0
                policy, attacks = CombatPolicy(time.monotonic()), AttackPolicy()
                states = {key: None for key in ATTACK_SKILLS if key in skill_regions}
                scores = dict.fromkeys(states, 0.0)
                sanction_count = 0
                seal_score = 0.0
                ready, last_update = None, 0

                def send_q():
                    return self._send_attack("Q", hwnd)

                while not self.cancel.is_set():
                    if self.native.foreground() != hwnd:
                        policy.ready_since = policy.changed_since = None
                        for skill in attacks.skills.values():
                            skill.ready_since = skill.changed_since = None
                        next_skill_capture = next_poison_capture = 0.0
                        poison_hit = None
                        cleanse.absent_since = None
                        now = time.monotonic()
                        if now-last_update >= 0.4:
                            self.events.put(("state", "已开启 · 等待回到游戏"))
                            last_update = now
                        self.cancel.wait(0.1)
                        continue
                    now = time.monotonic()
                    if now >= next_skill_capture:
                        next_skill_capture = now + SKILL_SAMPLE_INTERVAL
                        for grab_region, crops in capture_plan:
                            image = cv2.cvtColor(np.asarray(capture.grab(grab_region)), cv2.COLOR_BGRA2BGR)
                            for key, crop in crops.items():
                                frame, baseline = image[crop], skill_references[key]
                                if key == "seal":
                                    ready, seal_score = classify_icon(frame, baseline, threshold)
                                else:
                                    states[key], scores[key] = classify_attack_icon(frame, baseline)
                        now = time.monotonic()
                        attacks.observe(now, states)
                    if detector and now >= next_poison_capture:
                        next_poison_capture = now + POISON_SAMPLE_INTERVAL
                        image = cv2.cvtColor(np.asarray(capture.grab(poison_region)), cv2.COLOR_BGRA2BGR)
                        poison_hit = detector.find(image)
                        if cleanse.observe(time.monotonic(), poison_hit):
                            self.events.put(("log", "V 后中毒图标持续消失，已确认解除"))
                    now = time.monotonic()
                    if (cleanse.due(now, poison_hit) and not self.native.modifiers_down()
                            and self.native.send("V", hwnd, self.cancel)):
                        cleanse.sent(time.monotonic())
                        self.events.put(("monitor", f"已发送 V · 等待中毒图标消失 · 累计 {cleanse.count} 次"))
                    elif detector and now-last_update >= 0.4:
                        self.events.put(("monitor", "V 净化 · 未发现中毒" if poison_hit is None else "V 净化 · 中毒仍在，等待冷却后重试"))
                    mining = self.mining_active is not None and self.mining_active()
                    if mining:
                        policy.ready_since = policy.changed_since = None
                        attacks.burst_left = 0
                    elif not self.native.modifiers_down() and now >= cleanse.block_until:
                        if policy.step(now, ready, try_send=send_q):
                            self.events.put(("log", "Q 已发送，等待圣印图标确认"))
                        elif now >= policy.next_e:
                            targets = attacks.candidates(now)
                            if targets:
                                key = SANCTION_KEY if targets == ("sanction",) else "E"
                                if self._send_attack(key, hwnd):
                                    attacks.sent(time.monotonic(), targets)
                                    if key == SANCTION_KEY:
                                        sanction_count += 1
                                    else:
                                        policy.e_count += 1
                    if now - last_update >= 0.4:
                        activity = "采矿中 · 暂停 Q/E/R" if mining else "运行中"
                        self.events.put(("state", f"{activity} · Q {policy.q_count}  E {policy.e_count}  "
                                                  f"R {sanction_count}  V {cleanse.count}"))
                        labels = {True: "就绪", False: "冷却", None: "未知"}
                        detail = f"圣印 {labels[ready]} {seal_score:.0%} · " + " · ".join(
                            f"{ATTACK_SKILLS[k][0]} {labels[states[k]]} {scores[k]:.0%}" for k in states)
                        if self.native.modifiers_down():
                            detail += " · 请松开 Ctrl/Shift/Alt/Win"
                        self.events.put(("diagnostic", detail))
                        last_update = now
                    self.cancel.wait(COMBAT_POLL_INTERVAL)
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
        self.native = WindowsInput(require_ch9329=True)
        self.worker = CombatWorker(self.native, self.events, mining_active=self._mining_in_progress)
        self.hotkeys = Hotkeys(self.events)
        self.loot_monitor = None
        self.mining_monitor = None
        self.bound_window = None
        self.region, self.reference, self.threshold = None, None, 0.90
        self.e_regions = {key: None for key in ATTACK_SKILLS}
        self.e_references = {key: None for key in ATTACK_SKILLS}
        self.selecting, self.hotkeys_ok, self.countdown_id = False, False, None
        self.start_generation = 0
        self.combat_mode = tk.BooleanVar(value=True)
        self.cursor_loot = tk.BooleanVar(value=False)
        self.cursor_mining = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="已停止")
        self.detail = tk.StringVar(value=f"正在监听{AUTOMATION_HOTKEY}……")
        self.poison_region, self.poison_icon_size = None, None
        self.monitor_status = tk.StringVar(value="V 净化 · 首次请校验减益栏")
        self.loot_status = tk.StringVar(value="自动拾取：关闭")
        self.mining_status = tk.StringVar(value="自动采矿：关闭")
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
        ttk.Label(panel, text=f"{AUTOMATION_HOTKEY}开启已勾选功能，再按全部停止", style="Accent.TLabel").pack(anchor="w", pady=(4, 14))
        row = ttk.Frame(panel)
        row.pack(fill="x", pady=(0, 12))
        ttk.Button(row, text="3 秒后开启", style="Primary.TButton", command=self.delayed_start).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="停止全部", command=self.stop_all).pack(side="left")
        for label, mode in (("自动采矿", self.cursor_mining), ("自动拾取", self.cursor_loot),
                            ("自动战斗", self.combat_mode)):
            ttk.Checkbutton(row, text=label, variable=mode, command=self.save_modes).pack(side="right")
        ttk.Separator(panel).pack(fill="x", pady=(0, 12))
        ttk.Label(panel, textvariable=self.status, style="Accent.TLabel", wraplength=660).pack(anchor="w")
        ttk.Label(panel, textvariable=self.monitor_status, style="Poison.TLabel", wraplength=660).pack(anchor="w", pady=6)
        ttk.Label(panel, textvariable=self.loot_status, style="Muted.TLabel", wraplength=660).pack(anchor="w")
        ttk.Label(panel, textvariable=self.mining_status, style="Muted.TLabel", wraplength=660).pack(anchor="w")
        ttk.Label(panel, text=f"键盘 {self.native.keyboard_name} · 鼠标 {self.native.mouse_name}",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(4, 0))
        ttk.Separator(panel).pack(fill="x", pady=12)
        ttk.Label(panel, text="首次设置 · 校验后自动保存", style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        row = ttk.Frame(panel)
        row.pack(fill="x")
        for label, action in (("圣印", lambda: self.calibrate("seal")),
                              ("神圣打击", lambda: self.calibrate("strike")),
                              ("审判", lambda: self.calibrate("judgment")),
                              ("制裁 (R)", lambda: self.calibrate("sanction")),
                              ("校验减益栏 · V", self.calibrate_poison)):
            ttk.Button(row, text=label, command=action).pack(side="left", padx=(0, 6))
        ttk.Label(panel, textvariable=self.regions_label, style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(8, 4))
        ttk.Label(panel, text="Q 圣印 · R 制裁 · E 审判/神圣打击 · V 净化", style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(8, 0))
        ttk.Label(panel, textvariable=self.detail, style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(4, 0))
        self.load()
        self.update_labels()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(30, self.poll)
        self._sync_gatherers()
        # 按当前字体/DPI 计算高度，保留底部快捷键与识别诊断行。
        root.update_idletasks()
        height = max(500, root.winfo_reqheight()+36)
        root.minsize(700, height)
        root.geometry(f"720x{height}")

    def log(self, text):
        print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

    def load(self):
        import cv2
        import numpy as np
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
            for key, default in (("combat_mode", True), ("cursor_loot", False), ("cursor_mining", False)):
                enabled = data.get(key, default)
                if type(enabled) is not bool:
                    raise ValueError(f"{key} 必须为布尔值")
                getattr(self, key).set(enabled)
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
            for key, (_, _, path) in ATTACK_SKILLS.items():
                self.e_regions[key], self.e_references[key] = data.get(key + "_region"), read(path)
            self.status.set(f"就绪 · {AUTOMATION_HOTKEY} 开启")
            # 清理旧版的血条、经验等配置，保留技能坐标与原图片。
            if CONFIG.exists() and set(data) - {"combat_mode", "cursor_loot", "cursor_mining", "ready_threshold",
                                                "seal_region", "strike_region", "judgment_region", "sanction_region",
                                                "poison_region", "poison_icon_size"}:
                self.persist()
        except Exception as exc:
            self.status.set(f"配置加载失败，请检查：{exc}")

    def persist(self):
        data = dict(ready_threshold=self.threshold,
                    poison_region=self.poison_region, poison_icon_size=self.poison_icon_size,
                    seal_region=self.region, combat_mode=bool(self.combat_mode.get()),
                    cursor_loot=bool(self.cursor_loot.get()), cursor_mining=bool(self.cursor_mining.get()))
        data.update({key + "_region": rect for key, rect in self.e_regions.items()})
        temporary = CONFIG.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(CONFIG)

    def update_labels(self):
        skills = [("圣印", self.region, self.reference)] + [
            (name, self.e_regions.get(k), self.e_references.get(k)) for k, (name, _, _) in ATTACK_SKILLS.items()]
        self.regions_label.set(" / ".join(f"{name}：{'已保存' if rect and image is not None else '待定位'}"
                                         for name, rect, image in skills) + (" / 减益栏：已校验" if self.poison_region else " / 减益栏：待校验"))
        try:
            validate_skill_regions(dict(seal=self.region, **self.e_regions))
        except ValueError as exc:
            self.status.set(str(exc))
            self.regions_label.set("技能位置冲突 · 请重新定位圣印和神圣打击")

    def stop(self):
        self.start_generation = getattr(self, "start_generation", 0)+1
        if self.countdown_id is not None:
            self.root.after_cancel(self.countdown_id)
            self.countdown_id = None
        self.bound_window = None
        gatherers = [monitor for monitor in (self.loot_monitor, self.mining_monitor) if monitor is not None]
        self.worker.cancel.set()
        for monitor in gatherers:
            monitor.cancel.set()
        self.worker.stop()
        self._sync_gatherers()
        self.status.set("已停止")

    def stop_all(self):
        self.stop()

    def save_modes(self):
        try:
            self.persist()
        except Exception as exc:
            self.log(f"保存功能选择失败 · {exc}")
        self._sync_gatherers()
        if self.bound_window:
            self._sync_combat()

    def _sync_gatherers(self):
        for enabled, attr, status, label, monitor_type in (
            (self.cursor_loot.get(), "loot_monitor", self.loot_status, "自动拾取", LootMonitor),
            (self.cursor_mining.get(), "mining_monitor", self.mining_status, "自动采矿", MiningMonitor),
        ):
            monitor = getattr(self, attr)
            if enabled and self.bound_window and monitor and monitor.running:
                continue
            if monitor:
                monitor.stop()
                setattr(self, attr, None)
            if not enabled or not self.bound_window:
                hint = f"待{AUTOMATION_HOTKEY}开启" if enabled else "关闭"
                status.set(f"{label} · {hint}")
                continue
            try:
                setattr(self, attr, monitor_type(self.native, self.bound_window, self.events))
                status.set(f"{label} · 已绑定窗口")
            except (OSError, ValueError) as exc:
                status.set(f"{label}无法开启 · {exc}")
                self.log(f"{label}无法开启 · {exc}")

    def _mining_in_progress(self):
        monitor = self.mining_monitor
        return monitor is not None and monitor.attack_paused

    def calibrate(self, key):
        if self.selecting:
            return
        self.stop()
        self.selecting = True
        self.log("框选一个完整就绪图标；圣印需未开启，攻击技能需无倒计时")
        self.root.withdraw()
        def selected(region, frame):
            try:
                import cv2
                regions = dict(seal=self.region, **self.e_regions)
                regions[key] = region
                validate_skill_regions(regions)
                if not (16 <= region["width"] <= 200 and 16 <= region["height"] <= 200
                        and 0.65 <= region["width"] / region["height"] <= 1.5 and frame.std() >= 8):
                    raise ValueError("请仅框选一个完整技能图标（16～200 像素）")
                if key != "seal":
                    center = cv2.cvtColor(icon_image(frame)[12:36, 12:36], cv2.COLOR_BGR2GRAY)
                    if center.std() < 3:
                        raise ValueError("图标中心缺少纹理，请重新框选")
                path = TEMPLATE if key == "seal" else ATTACK_SKILLS[key][2]
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
            self.log("收到开启指令，但仍在框选；请先完成或取消框选")
            return
        if not self.hotkeys_ok:
            self.status.set(f"{AUTOMATION_HOTKEY}监听尚未就绪，请检查底部提示")
            self.log(self.status.get())
            return
        self.start_generation = getattr(self, "start_generation", 0)+1
        self._await_game_window(self.start_generation, time.monotonic()+2.0)

    def _await_game_window(self, generation, deadline, candidate=None, since=None):
        # 只在 Tk 主线程调度，不阻塞界面；空句柄不丢弃开启请求。
        if generation != self.start_generation:
            return
        self.countdown_id = None
        if not self.hotkeys_ok or self.selecting:
            self.status.set("开启已取消 · 快捷键未就绪或正在框选")
            return
        now = time.monotonic()
        hwnd = self.native.foreground()
        valid = bool(hwnd) and not self.native.is_own_window(hwnd) and self.native.is_game_window(hwnd)
        if valid and hwnd == candidate and since is not None and now-since >= 0.10:
            self._start_with_window(hwnd)
            return
        if now >= deadline:
            self.status.set(f"开启未完成 · 2 秒内未取得稳定的游戏前台，请回游戏再按{AUTOMATION_HOTKEY}")
            self.log(self.status.get())
            return
        if not valid:
            candidate, since = None, None
        elif hwnd != candidate:
            candidate, since = hwnd, now
        self.status.set(f"等待游戏前台稳定 · 再按{AUTOMATION_HOTKEY}可取消")
        self.countdown_id = self.root.after(100, lambda: self._await_game_window(
            generation, deadline, candidate, since))

    def _start_with_window(self, hwnd):
        if not any(mode.get() for mode in (self.combat_mode, self.cursor_loot, self.cursor_mining)):
            self.status.set("请先勾选要开启的自动功能")
            return
        self.bound_window = hwnd
        self.status.set(f"已开启 · 再按{AUTOMATION_HOTKEY}停止全部")
        self._sync_gatherers()
        self._sync_combat()
        if not (self.worker.running or self.loot_monitor or self.mining_monitor):
            self.bound_window = None

    def _sync_combat(self):
        if not self.combat_mode.get():
            if self.worker.running:
                self.worker.stop()
                self.status.set(f"自动战斗已关闭 · {AUTOMATION_HOTKEY}停止全部")
            return
        if self.worker.running:
            return
        try:
            validate_skill_regions(dict(seal=self.region, **self.e_regions))
            options = dict(poison_region=self.poison_region, poison_icon_size=self.poison_icon_size)
            import mss
            with mss.MSS() as capture:
                if self.reference is None or not region_on_screen(self.region, capture.monitors[1:]):
                    raise ValueError("请先定位圣印图标（2）")
                for key, (name, _, _) in ATTACK_SKILLS.items():
                    image, rect = self.e_references.get(key), self.e_regions.get(key)
                    if key == "sanction" and (image is None or rect is None):
                        continue
                    if image is None or not region_on_screen(rect, capture.monitors[1:]):
                        raise ValueError(f"请先定位{name}就绪图标")
                    options[key + "_region"], options[key + "_reference"] = dict(rect), image.copy()
            self.worker.start(self.bound_window, dict(self.region), self.reference.copy(), self.threshold, options)
            self.log("自动攻击已开启")
        except Exception as exc:
            self.status.set(f"自动战斗未开启 · {exc}")
            self.log(f"自动战斗未开启 · {exc}")

    def toggle_automation(self):
        stopping = self.bound_window is not None or self.countdown_id is not None
        self.log(f"收到{AUTOMATION_HOTKEY} · {'停止全部' if stopping else '开启已勾选功能'}")
        if stopping:
            self.stop()
        else:
            self.start()

    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "toggle":
                    self.toggle_automation()
                elif kind == "hotkeys":
                    self.hotkeys_ok = True
                    self.detail.set("快捷键已就绪 · 切出暂停发键，切回继续")
                elif kind == "hotkey_error":
                    self.hotkeys_ok = False
                    self.stop_all()
                    self.detail.set(payload)
                    self.log(payload)
                elif kind == "state" and self.worker.running:
                    self.status.set(payload)
                elif kind in ("error", "finished"):
                    self.status.set(payload)
                    self.log(payload)
                elif kind == "monitor":
                    self.monitor_status.set(payload)
                elif kind == "diagnostic" and self.worker.running:
                    self.detail.set(payload)
                elif kind in ("loot", "mining", "loot_error", "mining_error"):
                    owner, text = payload
                    is_mining = kind.startswith("mining")
                    attr = "mining_monitor" if is_mining else "loot_monitor"
                    if owner is not getattr(self, attr):
                        continue
                    status = self.mining_status if is_mining else self.loot_status
                    status.set(text)
                    if kind.endswith("_error"):
                        owner.stop()
                        setattr(self, attr, None)
                        self.log(text)
                elif kind == "log":
                    self.log(payload)
        except queue.Empty:
            pass
        self.root.after(30, self.poll)

    def close(self):
        self.stop()
        self.hotkeys.close()
        self.native.close()
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
