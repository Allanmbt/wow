"""自动补圣印和 E/R 调度策略。"""

from dataclasses import dataclass
from random import randint, uniform

from .skills import ATTACK_SKILLS, E_SKILLS
from ..vision.target import NON_HOSTILE

TARGET_GRACE_SECONDS = 15.0
BURST_WINDOW_SECONDS = 0.50
ATTACK_MIN_GAP = 0.17


class TargetGrace:
    def __init__(self):
        self.had_hostile = False
        self.lost_at = None
        self.friendly_seen = False

    def observe(self, reading, now):
        if reading.alive:
            self.had_hostile = True
            self.lost_at = None
            self.friendly_seen = False
            return
        if reading == NON_HOSTILE:
            self.friendly_seen = True
        if self.had_hostile and self.lost_at is None:
            self.lost_at = now

    def remaining(self, now):
        return max(0.0, TARGET_GRACE_SECONDS - (now - self.lost_at)) if self.lost_at is not None else 0.0

    def allows_attack(self, reading, now):
        return reading.alive or (reading != NON_HOSTILE and not self.friendly_seen and self.remaining(now) > 0)


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
        self.last_attack = -float("inf")
        self.clear_burst()

    def clear_burst(self):
        self.burst_left = 0
        self.burst_until = self.burst_next = 0.0
        self.burst_targets = ()
        self.burst_marked = set()

    def observe(self, now, states):
        for key, skill in self.skills.items():
            skill.observe(now, states.get(key))
        if self.burst_left:
            self.burst_targets = tuple(key for key in self.burst_targets if states.get(key) is True)
            if not self.burst_targets:
                self.clear_burst()

    def candidates(self, now):
        if now - self.last_attack < ATTACK_MIN_GAP:
            return ()
        if self.skills["sanction"].due(now):
            return ("sanction",)
        if self.burst_left and now <= self.burst_until:
            followup = tuple(key for key in self.burst_targets
                             if self.skills[key].ready_since is not None
                             and now >= self.skills[key].guard_until)
            if followup:
                return followup if now >= self.burst_next else ()
            self.clear_burst()
        return tuple(key for key in E_SKILLS if self.skills[key].due(now))

    def sent(self, now, candidates):
        continuing = (self.burst_left > 0 and now <= self.burst_until
                      and all(key in self.burst_targets for key in candidates))
        if not continuing:
            self.burst_marked = set()
            self.burst_targets = candidates
            self.burst_left = randint(2, 3)
            self.burst_until = now + BURST_WINDOW_SECONDS
        self.burst_left -= 1
        self.burst_next = now + uniform(0.175, 0.195)
        self.last_attack = now
        for key in candidates:
            skill = self.skills[key]
            skill.last_sent, skill.pending = now, True
            skill.retry_after_change = False
            if key not in self.burst_marked:
                skill.attempts += 1
                self.burst_marked.add(key)
