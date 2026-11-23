"""自动战斗助手界面、设置和启动控制。"""

import json
import queue
import sys
import threading
import time

from ..combat.skills import ATTACK_SKILLS, TEMPLATE, validate_skill_regions
from ..combat.worker import CombatWorker
from ..device.hotkeys import AUTOMATION_HOTKEY, Hotkeys
from ..device.windows import WindowsInput
from ..gathering.cursor import LootMonitor, MiningMonitor
from ..geometry import enable_dpi_awareness, region_on_screen
from ..paths import config_path
from ..vision.seal import load_seal_setup, righteous_reference_available, setup_complete
from ..vision.target import load_frame
from .calibration import Calibration

CONFIG = config_path("combat_config.json")


class CombatApp(Calibration):
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
        self.seal_dance = tk.BooleanVar(value=False)
        self.dance_enabled = threading.Event()
        self.seal_setup = {}
        self.status = tk.StringVar(value="已停止")
        self.detail = tk.StringVar()
        self.poison_region, self.poison_icon_size = None, None
        self.target_frame = None
        self.monitor_status = tk.StringVar()
        self.loot_status = tk.StringVar()
        self.mining_status = tk.StringVar()
        self.calibration_buttons = {}
        root.title("毒哥助手")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6f8")
        style.configure("TLabel", background="#f4f6f8", foreground="#263445", font=("Microsoft YaHei", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei", 17, "bold"), foreground="#162638")
        style.configure("Accent.TLabel", foreground="#176c63", font=("Microsoft YaHei", 10, "bold"))
        style.configure("Muted.TLabel", foreground="#596879", font=("Microsoft YaHei", 9))
        style.configure("Notice.TLabel", foreground="#8a5911", font=("Microsoft YaHei", 9))
        style.configure("Hid.TLabel", padding=(7, 3), font=("Microsoft YaHei", 9))
        style.configure("Ready.Hid.TLabel", background="#e3f2eb", foreground="#176548")
        style.configure("Missing.Hid.TLabel", background="#f8e9e9", foreground="#9e393d")
        style.configure("TButton", padding=(9, 4), font=("Microsoft YaHei", 9))
        style.configure("Primary.TButton", background="#176c8c", foreground="white")
        style.map("Primary.TButton", background=[("pressed", "#135a74"), ("active", "#1a7799")])
        style.configure("TCheckbutton", background="#f4f6f8", font=("Microsoft YaHei", 9))
        style.configure("Skill.TButton", background="#ffffff", bordercolor="#ccd6df", padding=(7, 3))
        style.map("Skill.TButton", background=[("pressed", "#dce6ee"), ("active", "#eaf0f5")])
        style.configure("Saved.Skill.TButton", background="#e5f3ec", foreground="#176548", bordercolor="#a9cfba")
        style.map("Saved.Skill.TButton", background=[("pressed", "#c7e4d5"), ("active", "#d9eee3")])

        panel = ttk.Frame(root, padding=(16, 13))
        panel.pack(fill="both", expand=True)
        header = ttk.Frame(panel)
        header.pack(fill="x")
        ttk.Label(header, text="毒哥助手", style="Title.TLabel").pack(side="left")
        self.hid_badge = ttk.Label(header, text="● 检测键鼠", style="Hid.TLabel")
        self.hid_badge.pack(side="right")
        ttk.Label(panel, text=f"{AUTOMATION_HOTKEY} 启停 · 离开游戏暂停",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 11))

        controls = ttk.Frame(panel)
        controls.pack(fill="x")
        for label, mode in (("战斗", self.combat_mode), ("拾取", self.cursor_loot),
                            ("采矿", self.cursor_mining), ("圣印舞", self.seal_dance)):
            ttk.Checkbutton(controls, text=label, variable=mode,
                            command=self.save_modes).pack(side="left", padx=(0, 9))
        ttk.Button(controls, text="停止全部", width=0, command=self.stop_all).pack(side="right")
        ttk.Button(controls, text="3 秒后开启", width=0, style="Primary.TButton",
                   command=self.delayed_start).pack(side="right", padx=(0, 6))

        ttk.Separator(panel).pack(fill="x", pady=9)
        skills = ttk.Frame(panel)
        skills.pack(fill="x")
        ttk.Label(skills, text="校验", style="Muted.TLabel").pack(side="left", padx=(0, 9))
        extra_actions = {"poison": self.calibrate_poison, "target": self.calibrate_target}
        for key, label in (("seal", "圣印 Q"), ("sanction", "制裁 R"), ("strike", "神圣打击 E"),
                           ("judgment", "审判 E"), ("poison", "减益栏 V"), ("target", "目标框")):
            action = extra_actions.get(key, lambda key=key: self.calibrate(key))
            button = ttk.Button(skills, text=f"{label} ✓", width=0, style="Skill.TButton", command=action)
            button.pack(side="left", padx=(0, 5))
            self.calibration_buttons[key] = (button, label)
        self.seal_menu_button = ttk.Menubutton(controls, text="圣印校验 ✓", direction="below")
        self.seal_menu_button.pack(side="left")
        seal_menu = tk.Menu(self.seal_menu_button, tearoff=False)
        self.seal_menu_button.configure(menu=seal_menu)
        self.seal_menu = seal_menu
        for key, caption in (("current", "当前圣印图标"), ("storage", "左侧储存增益区"),
                             ("righteous_icon", "正义储存三层图标")):
            seal_menu.add_command(label=caption, command=lambda key=key: self.calibrate_seal_dance(key))

        messages = ttk.Frame(panel)
        messages.pack(fill="x", pady=(9, 0))
        root.update_idletasks()
        content_width = max(480, controls.winfo_reqwidth(), skills.winfo_reqwidth())
        for row, (value, label_style) in enumerate(((self.status, "Accent.TLabel"), (self.monitor_status, "Notice.TLabel"),
                                                   (self.loot_status, "Notice.TLabel"), (self.mining_status, "Notice.TLabel"),
                                                   (self.detail, "Muted.TLabel"))):
            label = ttk.Label(messages, textvariable=value, style=label_style, wraplength=content_width)
            label.grid(row=row, column=0, sticky="w", pady=(0, 2))

            def refresh(*_, value=value, label=label):
                if value.get():
                    label.grid()
                else:
                    label.grid_remove()

            value.trace_add("write", refresh)
            refresh()
        self.load()
        self.update_labels()
        root.update_idletasks()
        root.minsize(content_width + 32, panel.winfo_reqheight())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(30, self.poll)
        root.after(0, self.refresh_hid_status)
        self._sync_gatherers()

    def refresh_hid_status(self):
        if (not (self.worker.running or self.loot_monitor or self.mining_monitor)
                and self.countdown_id is None and not self.selecting):
            def check():
                self.events.put(("hid_status", self.native.ch9329_ready(probe=True)))

            threading.Thread(target=check, daemon=True, name="CH9329Status").start()
        else:
            self.show_hid_status(self.native.ch9329_ready())
        self.root.after(2500, self.refresh_hid_status)

    def show_hid_status(self, ready):
        if ready is not None:
            self.hid_badge.configure(text="● 键鼠就绪" if ready else "● 键鼠未就绪",
                                     style="Ready.Hid.TLabel" if ready else "Missing.Hid.TLabel")

    def log(self, text):
        print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

    def load(self):
        import cv2
        import numpy as np
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
            for key, default in (("combat_mode", True), ("cursor_loot", False),
                                 ("cursor_mining", False), ("seal_dance", False)):
                enabled = data.get(key, default)
                if type(enabled) is not bool:
                    raise ValueError(f"{key} 必须为布尔值")
                getattr(self, key).set(enabled)
            self.sync_dance_setting()
            self.threshold = float(data.get("ready_threshold", 0.90))
            if not 0.8 <= self.threshold <= 0.99:
                raise ValueError("圣印阈值无效")
            self.region = data.get("seal_region")
            self.poison_region = data.get("poison_region")
            self.poison_icon_size = data.get("poison_icon_size")
            self.target_frame = load_frame(data.get("target_frame"))
            self.seal_setup = load_seal_setup(data.get("seal_setup"))
            def read(path):
                return cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR) if path.exists() else None
            # 手动更换本地图片后也允许加载；比较时归一化尺寸，不要求与旧坐标等大。
            self.reference = read(TEMPLATE)
            for key, (_, _, path) in ATTACK_SKILLS.items():
                self.e_regions[key], self.e_references[key] = data.get(key + "_region"), read(path)
            self.status.set("待启动")
            # 清理旧版的血条、经验等配置，保留技能坐标与原图片。
            if CONFIG.exists() and set(data) - {"combat_mode", "cursor_loot", "cursor_mining", "ready_threshold",
                                                "seal_region", "strike_region", "judgment_region", "sanction_region",
                                                "poison_region", "poison_icon_size", "target_frame", "seal_dance",
                                                "seal_setup"}:
                self.persist()
        except Exception as exc:
            self.status.set(f"配置加载失败，请检查：{exc}")

    def persist(self):
        data = dict(ready_threshold=self.threshold,
                    poison_region=self.poison_region, poison_icon_size=self.poison_icon_size,
                    seal_region=self.region, combat_mode=bool(self.combat_mode.get()),
                    cursor_loot=bool(self.cursor_loot.get()), cursor_mining=bool(self.cursor_mining.get()),
                    seal_dance=bool(self.seal_dance.get()))
        if self.target_frame:
            data["target_frame"] = self.target_frame
        if self.seal_setup:
            data["seal_setup"] = self.seal_setup
        data.update({key + "_region": rect for key, rect in self.e_regions.items()})
        temporary = CONFIG.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(CONFIG)

    def update_labels(self):
        saved = {key: self.e_regions.get(key) and self.e_references.get(key) is not None for key in ATTACK_SKILLS}
        saved["seal"] = self.region and self.reference is not None
        saved["poison"] = self.poison_region and self.poison_icon_size
        saved["target"] = self.target_frame
        for key, (button, label) in self.calibration_buttons.items():
            button.configure(text=f"{label} ✓" if saved[key] else label,
                             style="Saved.Skill.TButton" if saved[key] else "Skill.TButton")
        self.seal_menu_button.configure(text="圣印校验 ✓" if setup_complete(self.seal_setup) else "圣印校验")
        for index, (key, caption) in enumerate((("current", "当前圣印图标"), ("storage", "左侧储存增益区"),
                                                ("righteous_icon", "正义储存三层图标"))):
            saved = key in self.seal_setup or (key == "righteous_icon" and righteous_reference_available(self.seal_setup))
            self.seal_menu.entryconfigure(index, label=f"{caption} ✓" if saved else caption)
        try:
            validate_skill_regions(dict(seal=self.region, **self.e_regions))
        except ValueError as exc:
            self.status.set(str(exc))

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
        self.monitor_status.set("")
        if self.hotkeys_ok:
            self.detail.set("")
        self.status.set("已停止")

    def stop_all(self):
        self.stop()

    def save_modes(self):
        self.sync_dance_setting()
        try:
            self.persist()
        except Exception as exc:
            self.log(f"保存功能选择失败 · {exc}")
        self._sync_gatherers()
        if self.bound_window:
            self._sync_combat()

    def sync_dance_setting(self):
        if self.seal_dance.get():
            self.dance_enabled.set()
        else:
            self.dance_enabled.clear()

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
            status.set("")
            if not enabled or not self.bound_window:
                continue
            try:
                setattr(self, attr, monitor_type(self.native, self.bound_window, self.events))
            except (OSError, ValueError) as exc:
                status.set(f"{label}无法开启 · {exc}")
                self.log(f"{label}无法开启 · {exc}")

    def _mining_in_progress(self):
        monitor = self.mining_monitor
        return monitor is not None and monitor.attack_paused

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
            options = dict(poison_region=self.poison_region, poison_icon_size=self.poison_icon_size,
                           target_frame=self.target_frame, seal_setup=self.seal_setup,
                           dance_enabled=self.dance_enabled.is_set)
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
                elif kind == "hid_status":
                    self.show_hid_status(payload)
                elif kind == "hotkeys":
                    self.hotkeys_ok = True
                    self.detail.set("")
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
                elif kind == "monitor" and self.worker.running:
                    self.monitor_status.set("" if payload == "V 净化 · 未发现中毒" else payload)
                elif kind == "target_frame":
                    token, profile = payload
                    if token is self.worker.cancel and not token.is_set():
                        try:
                            self.save_target_frame(profile)
                        except OSError as exc:
                            self.log(f"保存目标框失败 · {exc}")
                elif kind == "diagnostic" and self.worker.running:
                    self.detail.set(payload)
                elif kind in ("loot_error", "mining_error"):
                    owner, text = payload
                    is_mining = kind == "mining_error"
                    attr = "mining_monitor" if is_mining else "loot_monitor"
                    if owner is not getattr(self, attr):
                        continue
                    status = self.mining_status if is_mining else self.loot_status
                    status.set(text)
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
