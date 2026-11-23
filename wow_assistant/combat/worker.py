"""战斗工作线程和输入前复核。"""

import threading
import time
from math import ceil

from ..geometry import region_on_screen
from ..vision.poison import CleansePolicy, PoisonDetector
from ..vision.seal import SEAL_NAMES, SealDancePolicy, SealMonitor, setup_complete
from ..vision.target import TargetMonitor, UNKNOWN
from .policy import AttackPolicy, CombatPolicy, TargetGrace
from .skills import ATTACK_SKILLS, SANCTION_KEY, _skill_capture_plan, classify_attack_icon, classify_icon

SKILL_SAMPLE_INTERVAL = 0.025
POISON_SAMPLE_INTERVAL = 0.050
COMBAT_POLL_INTERVAL = 0.005
COMBAT_IDLE_POLL_INTERVAL = 0.10


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

    def _send_attack(self, key, hwnd, target_ready=None):
        if self.mining_active is None and target_ready is None:
            return self.native.send(key, hwnd, self.cancel)
        # WindowsInput 在共用键鼠锁内、串口握手后再次检查。
        def recheck():
            return ((self.mining_active is None or not self.mining_active())
                    and (target_ready is None or target_ready()))
        return self.native.send(key, hwnd, self.cancel, recheck=recheck)

    def run(self, hwnd, region, reference, threshold, options=None):
        options = options or {}
        try:
            import cv2
            import mss
            import numpy as np
            from mss.exception import ScreenShotError
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
                target_monitor = None
                track_target = "target_frame" in options
                if track_target:
                    token = self.cancel
                    try:
                        target_monitor = TargetMonitor(capture, lambda: self.native.client_bounds(hwnd),
                                                       options["target_frame"],
                                                       lambda profile: self.events.put(("target_frame", (token, profile))))
                    except (OSError, ValueError) as exc:
                        self.events.put(("log", f"目标识别不可用 · {exc}"))
                seal_setup = options.get("seal_setup", {})
                dance_enabled = options.get("dance_enabled", lambda: False)
                seal_configured = setup_complete(seal_setup)
                seal_monitor = None
                seal_init_pending = bool(seal_setup)
                dance = SealDancePolicy()
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
                target_grace = TargetGrace()
                states = {key: None for key in ATTACK_SKILLS if key in skill_regions}
                scores = dict.fromkeys(states, 0.0)
                sanction_count = 0
                seal_score = 0.0
                ready, last_update = None, 0
                attack_was_enabled = False

                def read_target(*, force=False):
                    reading = target_monitor.sample(time.monotonic(), force=force) if target_monitor is not None else UNKNOWN
                    target_grace.observe(reading, time.monotonic())
                    return reading

                def check_target():
                    reading = read_target(force=True)
                    return target_grace.allows_attack(reading, time.monotonic())

                def send_q():
                    return target_ok and self._send_attack("Q", hwnd, check_target if track_target else None)

                def check_seal_action(expected):
                    if not dance_enabled() or not read_target(force=True).alive:
                        return False
                    try:
                        icon = cv2.cvtColor(np.asarray(capture.grab(region)), cv2.COLOR_BGRA2BGR)
                        empty, _ = classify_icon(icon, reference, threshold)
                    except (OSError, ScreenShotError, cv2.error):
                        return False
                    current = seal_monitor.sample(time.monotonic(), empty is True, force=True,
                                                  in_combat=target_monitor.reading.engaged is True)
                    return SealDancePolicy.eligible(current, target_monitor.reading) == expected

                while not self.cancel.is_set():
                    if self.native.foreground() != hwnd:
                        policy.ready_since = policy.changed_since = None
                        for skill in attacks.skills.values():
                            skill.ready_since = skill.changed_since = None
                        attacks.clear_burst()
                        next_skill_capture = next_poison_capture = 0.0
                        if target_monitor is not None:
                            target_monitor.reading = UNKNOWN
                            target_monitor.next_sample = 0.0
                        if seal_monitor is not None:
                            seal_monitor.next_sample = 0.0
                            dance.reset_observations()
                        poison_hit = None
                        cleanse.absent_since = None
                        now = time.monotonic()
                        if now-last_update >= 0.4:
                            self.events.put(("state", "已开启 · 等待回到游戏"))
                            last_update = now
                        self.cancel.wait(0.1)
                        continue
                    target_reading = read_target()
                    now = time.monotonic()
                    target_ok = not track_target or target_grace.allows_attack(target_reading, now)
                    if not target_ok and attack_was_enabled:
                        ready, seal_score = None, 0.0
                        policy.ready_since = policy.changed_since = None
                        for key in states:
                            states[key], scores[key] = None, 0.0
                        attacks.observe(now, states)
                        attacks.clear_burst()
                        next_skill_capture = 0.0
                    attack_was_enabled = target_ok
                    if target_ok and now >= next_skill_capture:
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
                    dance_on = dance_enabled()
                    if dance_on and target_reading.alive and seal_init_pending:
                        seal_init_pending = False
                        try:
                            seal_monitor = SealMonitor(capture, lambda: self.native.client_bounds(hwnd), seal_setup)
                        except (OSError, ValueError) as exc:
                            self.events.put(("log", f"圣印识别不可用 · {exc}"))
                    use_dance = dance_on and seal_monitor is not None and seal_configured
                    if seal_monitor is not None:
                        if not dance_on or not target_reading.alive:
                            seal_monitor.next_sample = 0.0
                            dance.reset_observations()
                        elif now >= seal_monitor.next_sample:
                            dance.observe(seal_monitor.sample(now, ready is True,
                                                              in_combat=target_reading.engaged is True,
                                                              check_storage=dance.pending is not None), now)
                    if mining:
                        policy.ready_since = policy.changed_since = None
                        attacks.clear_burst()
                    elif target_ok and not self.native.modifiers_down() and now >= cleanse.block_until:
                        seal_sent = False
                        waiting_for_seal = False
                        if use_dance and target_reading.alive:
                            action = dance.decide(seal_monitor.reading, target_reading, now)
                            if action and self._send_attack(action.key, hwnd, lambda: check_seal_action(action)):
                                sent_at = time.monotonic()
                                dance.sent(action, sent_at)
                                policy.next_e = max(policy.next_e, sent_at + 0.15)
                                if action.key == "Q":
                                    policy.q_count += 1
                                    policy.last_q = sent_at
                                seal_sent = True
                                self.events.put(("log", f"圣印舞发送 {action.key} · 第 {dance.attempts} 次 · "
                                                 f"等待{SEAL_NAMES[action.destination]}圣印确认"))
                            waiting_for_seal = dance.reserves_attack(seal_monitor.reading, target_reading)
                        elif not use_dance and policy.step(now, ready, try_send=send_q):
                            seal_sent = True
                            self.events.put(("log", "Q 已发送，等待圣印图标确认"))
                        if not seal_sent and not waiting_for_seal and now >= policy.next_e:
                            targets = attacks.candidates(now)
                            if targets:
                                key = SANCTION_KEY if targets == ("sanction",) else "E"
                                if self._send_attack(key, hwnd, check_target if track_target else None):
                                    attacks.sent(time.monotonic(), targets)
                                    if key == SANCTION_KEY:
                                        sanction_count += 1
                                    else:
                                        policy.e_count += 1
                    if now - last_update >= 0.4:
                        activity = "采矿中 · 暂停 Q/E/R" if mining else "运行中"
                        if track_target:
                            grace_left = ceil(target_grace.remaining(now))
                            target_note = target_reading.describe()
                            if not target_reading.alive and grace_left:
                                label = "维持攻击" if target_ok else "友军保护"
                                target_note = f"目标过渡 · {label} · 剩余 {grace_left} 秒"
                            activity = target_note if not mining else f"{activity} · {target_note}"
                        self.events.put(("state", f"{activity} · Q {policy.q_count}  E {policy.e_count}  "
                                                  f"R {sanction_count}  V {cleanse.count}"))
                        labels = {True: "就绪", False: "冷却", None: "未知"}
                        detail = f"圣印 {labels[ready]} {seal_score:.0%} · " + " · ".join(
                            f"{ATTACK_SKILLS[k][0]} {labels[states[k]]} {scores[k]:.0%}" for k in states)
                        if self.native.modifiers_down():
                            detail += " · 请松开 Ctrl/Shift/Alt/Win"
                        if dance_on:
                            if not target_reading.alive:
                                seal_detail = "等待活目标"
                            else:
                                seal_detail = seal_monitor.reading.describe() if seal_monitor is not None else "待校验"
                            if not seal_configured:
                                seal_detail += " · 请完成圣印校验"
                            elif target_reading.alive and dance.pending is not None:
                                seal_detail += f" · 等待切印确认（第 {dance.attempts} 次）"
                            elif (target_reading.alive and seal_monitor is not None
                                  and seal_monitor.reading.active in dance.used_stacks):
                                seal_detail += " · 等待新三层"
                            detail += f" · 圣印舞 {seal_detail}"
                        self.events.put(("diagnostic", detail))
                        last_update = now
                    wait = COMBAT_POLL_INTERVAL
                    if not target_ok:
                        now = time.monotonic()
                        deadline = now + COMBAT_IDLE_POLL_INTERVAL
                        if target_monitor is not None:
                            deadline = min(deadline, target_monitor.next_sample)
                        if detector:
                            deadline = min(deadline, next_poison_capture)
                        wait = max(COMBAT_POLL_INTERVAL, deadline - now)
                    self.cancel.wait(wait)
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            self.cancel.set()
            self.events.put(("ended", None))
