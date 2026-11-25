"""战斗工作线程和输入前复核。"""

import threading
import time
from math import ceil

from ..geometry import region_on_screen
from ..vision.player_vitals import PlayerVitalsMonitor
from ..vision.poison import CleansePolicy, PoisonDetector
from ..vision.seal import SEAL_NAMES, WISDOM, SealDancePolicy, SealMonitor, setup_complete
from ..vision.target import TargetMonitor, UNKNOWN
from .policy import AttackPolicy, CombatPolicy, TargetGrace
from .protection import DEFAULT_THRESHOLDS, HEAL_KEY, POTION_KEY, WISDOM_KEY, ProtectionPolicy
from .skills import (ATTACK_SKILLS, HAMMER_KEY, HAMMER_THRESHOLD, OPTIONAL_ATTACK_SKILLS, SANCTION_KEY,
                     HammerIcon, _skill_capture_plan, classify_attack_icon, classify_icon,
                     infer_hammer_region, validate_skill_regions)

SKILL_SAMPLE_INTERVAL = 0.025
POISON_SAMPLE_INTERVAL = 0.050
COMBAT_POLL_INTERVAL = 0.005
COMBAT_IDLE_POLL_INTERVAL = 0.10
PLAYER_VITALS_INTERVAL = 0.20


class CombatWorker:
    def __init__(self, native, events, *, mining_active=None):
        self.native, self.events = native, events
        self.mining_active = mining_active
        self.cancel = threading.Event()
        self.thread = None
        self._player_vitals = None

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive() and not self.cancel.is_set()

    @property
    def player_vitals(self):
        """Latest verified health/mana percentages, or None when unavailable."""
        return self._player_vitals if self.running else None

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
                get_client_bounds = getattr(self.native, "client_bounds", None)
                player_monitor = (PlayerVitalsMonitor(capture, lambda: get_client_bounds(hwnd))
                                  if get_client_bounds is not None else None)
                next_vitals = 0.0
                monitors = capture.monitors[1:]
                combat_enabled = options.get("combat_enabled", True)
                protection_enabled = options.get("protection_enabled", lambda: False)
                protection_thresholds = options.get("protection_thresholds", lambda: DEFAULT_THRESHOLDS)
                protection = None
                confirmed_seal = None
                next_wisdom_probe = 0.0
                skill_regions, skill_references = {}, {}
                if combat_enabled:
                    if not region_on_screen(region, monitors):
                        raise ValueError("圣印区域已超出屏幕，请重新定位")
                    skill_regions["seal"], skill_references["seal"] = region, reference
                    for key, (name, _, _) in ATTACK_SKILLS.items():
                        if key == "hammer":
                            continue
                        skill_region = options.get(key + "_region")
                        skill_reference = options.get(key + "_reference")
                        if key in OPTIONAL_ATTACK_SKILLS and (skill_region is None or skill_reference is None):
                            continue
                        if not region_on_screen(skill_region, monitors) or skill_reference is None:
                            raise ValueError(f"请先校准{name}的就绪图标，再开启自动攻击")
                        skill_regions[key] = skill_region
                        skill_references[key] = skill_reference
                capture_plan = _skill_capture_plan(skill_regions, monitors) if combat_enabled else ()
                target_monitor = None
                track_target = combat_enabled and "target_frame" in options
                if track_target:
                    token = self.cancel
                    try:
                        target_monitor = TargetMonitor(capture, lambda: self.native.client_bounds(hwnd),
                                                       options["target_frame"],
                                                       lambda profile: self.events.put(("target_frame", (token, profile))))
                    except (OSError, ValueError) as exc:
                        self.events.put(("log", f"目标识别不可用 · {exc}"))
                hammer_region = None
                hammer_icon = None
                hammer_requested = False
                hammer_enabled = options.get("hammer_enabled", lambda: False)
                seal_setup = options.get("seal_setup", {})
                dance_enabled = options.get("dance_enabled", lambda: False)
                seal_configured = setup_complete(seal_setup)
                seal_monitor = None
                seal_init_pending = bool(seal_setup)
                dance = SealDancePolicy()
                poison_region = options.get("poison_region") if combat_enabled else None
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
                sanction_count = hammer_count = 0
                seal_score = 0.0
                ready, last_update = None, 0
                attack_was_enabled = False

                def reset_attack_observations(now):
                    nonlocal ready, seal_score, next_skill_capture
                    ready, seal_score = None, 0.0
                    policy.ready_since = policy.changed_since = None
                    for key in states:
                        states[key], scores[key] = None, 0.0
                    attacks.observe(now, states)
                    attacks.clear_burst()
                    next_skill_capture = 0.0

                def prepare_seal_monitor():
                    nonlocal seal_init_pending, seal_monitor
                    if seal_init_pending:
                        seal_init_pending = False
                        try:
                            seal_monitor = SealMonitor(capture, lambda: self.native.client_bounds(hwnd), seal_setup)
                        except (OSError, ValueError) as exc:
                            self.events.put(("log", f"圣印识别不可用 · {exc}"))

                def read_target(*, force=False):
                    reading = target_monitor.sample(time.monotonic(), force=force) if target_monitor is not None else UNKNOWN
                    target_grace.observe(reading, time.monotonic())
                    return reading

                def check_target():
                    reading = read_target(force=True)
                    return target_grace.allows_attack(reading, time.monotonic())

                def read_hammer():
                    try:
                        frame = cv2.cvtColor(np.asarray(capture.grab(hammer_region)), cv2.COLOR_BGRA2BGR)
                        return hammer_icon.classify(frame)
                    except (OSError, ValueError, ScreenShotError, cv2.error):
                        return None, 0.0

                def check_hammer():
                    return (hammer_enabled() and read_target(force=True).below(HAMMER_THRESHOLD)
                            and read_hammer()[0] is True and hammer_enabled())

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
                    return SealDancePolicy.eligible(current, target_monitor.reading,
                                                    restore_wisdom=dance.restore_wisdom) == expected

                while not self.cancel.is_set():
                    if self.native.foreground() != hwnd:
                        self._player_vitals = None
                        next_vitals = 0.0
                        next_wisdom_probe = 0.0
                        if protection is not None:
                            protection.observe(None)
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
                    now = time.monotonic()
                    if player_monitor is not None and now >= next_vitals:
                        next_vitals = now + PLAYER_VITALS_INTERVAL
                        self._player_vitals = player_monitor.sample(now)
                    now = time.monotonic()
                    was_healing = protection is not None and protection.healing
                    was_mana_locked = protection is not None and protection.mana_locked
                    if protection_enabled():
                        if protection is None:
                            protection = ProtectionPolicy(protection_thresholds())
                        else:
                            protection.configure(protection_thresholds())
                        protection.observe(self._player_vitals)
                    else:
                        protection = None
                    healing = protection is not None and protection.healing
                    mana_locked = protection is not None and protection.mana_locked
                    if was_mana_locked != mana_locked or (healing and not was_healing):
                        dance.pending = None
                        dance.reset_observations()
                        next_wisdom_probe = 0.0
                        confirmed_seal = None
                        if was_mana_locked and not mana_locked:
                            dance.restore_wisdom = True
                        if seal_monitor is not None:
                            seal_monitor.next_sample = 0.0
                    potion_sent = False
                    if (protection is not None and protection.potion_due(now, self._player_vitals)
                            and not self.native.modifiers_down()):
                        def potion_still_due():
                            return protection_enabled() and protection.potion_due(time.monotonic(), self._player_vitals)

                        if self.native.send(POTION_KEY, hwnd, self.cancel, recheck=potion_still_due):
                            protection.potion_sent(time.monotonic())
                            self.events.put(("log", "深度保护 · 已发送数字 9 · 超强效治疗药水"))
                            potion_sent = True
                    potion_phase = potion_sent or (protection is not None and protection.potion_pending(now, self._player_vitals))
                    if healing or potion_phase:
                        if attack_was_enabled:
                            reset_attack_observations(now)
                            attack_was_enabled = False
                        next_poison_capture, poison_hit = 0.0, None
                        cleanse.absent_since = None
                        if not potion_phase and protection.heal_due(now, self._player_vitals) and not self.native.modifiers_down():
                            def heal_still_due():
                                return protection_enabled() and protection.heal_due(time.monotonic(), self._player_vitals)

                            if self.native.send(HEAL_KEY, hwnd, self.cancel, recheck=heal_still_due):
                                protection.heal_sent(time.monotonic())
                                self.events.put(("log", "保护 · 已发送 F4 圣光术 · 等待血量确认"))
                        if potion_sent or (healing and not was_healing) or now-last_update >= .4:
                            status = ("深度保护 · 正在使用超强效治疗药水" if potion_phase
                                      else f"保护 · 治疗优先 · 等待血量稳定高于 {protection.thresholds.health_recover}%")
                            self.events.put(("state", status))
                            last_update = now
                        self.cancel.wait(.05)
                        continue
                    wisdom_cast = False
                    protection_note = "保护 · 等待识别玩家血量和蓝量" if protection is not None and self._player_vitals is None else ""
                    if mana_locked:
                        if now >= next_wisdom_probe:
                            prepare_seal_monitor()
                            confirmed_seal = seal_monitor.read_active() if seal_monitor is not None else None
                            next_wisdom_probe = now + .20
                            protection.observe_seal(confirmed_seal, now)
                        if protection.wisdom_due(now, self._player_vitals, confirmed_seal) and not self.native.modifiers_down():
                            def wisdom_still_due():
                                return protection_enabled() and protection.wisdom_due(time.monotonic(), self._player_vitals, confirmed_seal)

                            if self.native.send(WISDOM_KEY, hwnd, self.cancel, recheck=wisdom_still_due):
                                sent_at = time.monotonic()
                                protection.wisdom_sent(sent_at)
                                policy.next_e = max(policy.next_e, sent_at + 1.05)
                                self.events.put(("log", f"保护 · 已发送数字 {WISDOM_KEY} · 智慧圣印"))
                                wisdom_cast = True
                        protection_note = ("保护 · 智慧圣印锁定" if confirmed_seal == WISDOM
                                           else "保护 · 低蓝等待确认智慧圣印")
                    if not combat_enabled:
                        if now-last_update >= .4:
                            self.events.put(("state", protection_note or "保护 · 监控玩家血量和蓝量"))
                            last_update = now
                        self.cancel.wait(.05)
                        continue
                    requested = target_monitor is not None and hammer_enabled()
                    if requested != hammer_requested:
                        hammer_requested = requested
                        if requested:
                            hammer_region = options.get("hammer_region") or infer_hammer_region(
                                region, skill_regions.get("judgment"))
                            try:
                                if not region_on_screen(hammer_region, monitors):
                                    raise ValueError("请先定位愤怒之锤图标")
                                validate_skill_regions(dict(skill_regions, hammer=hammer_region))
                                hammer_icon = HammerIcon(options.get("hammer_reference"))
                                states["hammer"], scores["hammer"] = None, 0.0
                            except (OSError, ValueError, cv2.error) as exc:
                                self.events.put(("log", f"愤怒之锤图标不可用 · {exc}"))
                        else:
                            hammer_icon = None
                            states.pop("hammer", None)
                            scores.pop("hammer", None)
                        attacks.set_hammer_enabled(hammer_icon is not None)
                    target_reading = read_target()
                    now = time.monotonic()
                    target_ok = not track_target or target_grace.allows_attack(target_reading, now)
                    hammer_allowed = hammer_icon is not None and target_reading.below(HAMMER_THRESHOLD)
                    if not target_ok and attack_was_enabled:
                        reset_attack_observations(now)
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
                        if hammer_icon is not None:
                            states["hammer"], scores["hammer"] = read_hammer() if hammer_allowed else (None, 0.0)
                        now = time.monotonic()
                        attacks.observe(now, states)
                    if detector and now >= next_poison_capture:
                        next_poison_capture = now + POISON_SAMPLE_INTERVAL
                        image = cv2.cvtColor(np.asarray(capture.grab(poison_region)), cv2.COLOR_BGRA2BGR)
                        poison_hit = detector.find(image)
                        if cleanse.observe(time.monotonic(), poison_hit):
                            self.events.put(("log", "V 后中毒图标持续消失，已确认解除"))
                    now = time.monotonic()
                    if (not wisdom_cast and cleanse.due(now, poison_hit) and not self.native.modifiers_down()
                            and self.native.send("V", hwnd, self.cancel)):
                        cleanse.sent(time.monotonic())
                        self.events.put(("monitor", f"已发送 V · 等待中毒图标消失 · 累计 {cleanse.count} 次"))
                    elif detector and now-last_update >= 0.4:
                        self.events.put(("monitor", "V 净化 · 未发现中毒" if poison_hit is None else "V 净化 · 中毒仍在，等待冷却后重试"))
                    mining = self.mining_active is not None and self.mining_active()
                    dance_on = dance_enabled() and not mana_locked
                    if dance_on and target_reading.alive and seal_init_pending:
                        prepare_seal_monitor()
                    use_dance = dance_on and seal_monitor is not None and seal_configured
                    if seal_monitor is not None:
                        if not dance_on or not target_reading.alive:
                            seal_monitor.next_sample = 0.0
                            dance.reset_observations()
                        elif now >= seal_monitor.next_sample:
                            seal_monitor.recognize_wisdom = dance.restore_wisdom
                            dance.observe(seal_monitor.sample(now, ready is True,
                                                              in_combat=target_reading.engaged is True,
                                                              check_storage=dance.pending is not None), now)
                    if mining:
                        policy.ready_since = policy.changed_since = None
                        attacks.clear_burst()
                    elif not wisdom_cast and target_ok and not self.native.modifiers_down() and now >= cleanse.block_until:
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
                        elif not use_dance and not mana_locked and policy.step(now, ready, try_send=send_q):
                            seal_sent = True
                            self.events.put(("log", "Q 已发送，等待圣印图标确认"))
                        if not seal_sent and not waiting_for_seal and now >= policy.next_e:
                            targets = attacks.candidates(now, hammer_allowed=hammer_allowed)
                            if targets:
                                key = {"sanction": SANCTION_KEY, "hammer": HAMMER_KEY}.get(targets[0], "E")
                                recheck = check_hammer if key == HAMMER_KEY else (check_target if track_target else None)
                                if self._send_attack(key, hwnd, recheck):
                                    attacks.sent(time.monotonic(), targets)
                                    if key == SANCTION_KEY:
                                        sanction_count += 1
                                    elif key == HAMMER_KEY:
                                        hammer_count += 1
                                    else:
                                        policy.e_count += 1
                    if now - last_update >= 0.4:
                        paused_keys = "Q/E/R/F" if hammer_icon is not None else "Q/E/R"
                        activity = f"采矿中 · 暂停 {paused_keys}" if mining else "运行中"
                        if track_target:
                            grace_left = ceil(target_grace.remaining(now))
                            target_note = target_reading.describe()
                            if not target_reading.alive and grace_left:
                                label = "维持攻击" if target_ok else "友军保护"
                                target_note = f"目标过渡 · {label} · 剩余 {grace_left} 秒"
                            activity = target_note if not mining else f"{activity} · {target_note}"
                        if protection_note:
                            activity = f"{activity} · {protection_note}"
                        hammer_status = f"  F {hammer_count}" if hammer_icon is not None else ""
                        self.events.put(("state", f"{activity} · Q {policy.q_count}  E {policy.e_count}  "
                                                  f"R {sanction_count}{hammer_status}  V {cleanse.count}"))
                        labels = {True: "就绪", False: "冷却", None: "未知"}
                        details = [f"圣印 {labels[ready]} {seal_score:.0%}"]
                        for skill, state in states.items():
                            label = "灰暗/不可用" if skill == "hammer" and state is False else labels[state]
                            details.append(f"{ATTACK_SKILLS[skill][0]} {label} {scores[skill]:.0%}")
                        detail = " · ".join(details)
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
            self._player_vitals = None
            self.cancel.set()
            self.events.put(("ended", None))
