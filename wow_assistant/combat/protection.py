"""Player-resource thresholds and timing for healing and mana protection."""

from dataclasses import dataclass

from ..vision.seal import WISDOM

HEAL_KEY = "F4"
WISDOM_KEY = "2"
POTION_KEY = "9"
HEAL_RETRY = 4.0
WISDOM_RETRY = 1.65
WISDOM_DURATION = 30.0
VITALS_FRESH_FOR = .65
PRESS_INTERVAL = .30
PRESS_WINDOW = .95


@dataclass(frozen=True)
class ProtectionThresholds:
    health_start: int = 50
    health_recover: int = 50
    mana_start: int = 30
    mana_recover: int = 70
    potion_start: int = 15

    def __post_init__(self):
        for start, recover in ((self.health_start, self.health_recover), (self.mana_start, self.mana_recover)):
            if type(start) is not int or type(recover) is not int or not 1 <= start <= recover <= 99:
                raise ValueError("阈值须为 1～99 的整数，恢复阈值须大于或等于触发阈值")
        if type(self.potion_start) is not int or not 1 <= self.potion_start <= 99:
            raise ValueError("药水阈值须为 1～99 的整数")


DEFAULT_THRESHOLDS = ProtectionThresholds()


class _KeyAttempts:
    def __init__(self):
        self.started = self.last = None
        self.count = 0

    def due(self, now, repeat_after=None):
        if self.last is None or (repeat_after is not None and now >= self.last + repeat_after):
            return True
        return self.pending(now) and now >= self.last + PRESS_INTERVAL

    def pending(self, now):
        return self.started is not None and self.count < 3 and now <= self.started + PRESS_WINDOW

    def sent(self, now):
        if self.started is None or now > self.started + PRESS_WINDOW:
            self.started, self.count = now, 0
        self.last = now
        self.count += 1


class ProtectionPolicy:
    def __init__(self, thresholds=DEFAULT_THRESHOLDS):
        self.thresholds = thresholds
        self.healing = self.mana_locked = False
        self.last_sample = None
        self.health_recovered = self.mana_recovered = self.potion_recovered = 0
        self.heal_keys = _KeyAttempts()
        self.wisdom_keys = _KeyAttempts()
        self.potion_keys = _KeyAttempts()
        self.wisdom_since = None

    def configure(self, thresholds):
        if self.thresholds != thresholds:
            self.thresholds = thresholds
            self.last_sample = None
            self.health_recovered = self.mana_recovered = self.potion_recovered = 0

    def observe(self, reading):
        if reading is None:
            self.health_recovered = self.mana_recovered = self.potion_recovered = 0
            return
        if reading.sampled_at == self.last_sample:
            return
        self.last_sample = reading.sampled_at
        health, mana = reading.health_percent, reading.mana_percent
        if self.potion_keys.last is not None:
            self.potion_recovered = self.potion_recovered + 1 if health >= self.thresholds.potion_start else 0
            if self.potion_recovered >= 2:
                self.potion_keys = _KeyAttempts()
                self.potion_recovered = 0
        if health < self.thresholds.health_start:
            self.healing = True
        self.health_recovered = (self.health_recovered + 1
                                 if self.healing and health > self.thresholds.health_recover else 0)
        if self.health_recovered >= 2:
            self.healing = False
        if mana < self.thresholds.mana_start and not self.mana_locked:
            self.mana_locked = True
            self.wisdom_keys = _KeyAttempts()
            self.wisdom_since = None
        self.mana_recovered = (self.mana_recovered + 1
                               if self.mana_locked and mana > self.thresholds.mana_recover else 0)
        if self.mana_recovered >= 2:
            self.mana_locked = False

    def heal_due(self, now, reading):
        return (self.healing and reading is not None and now - reading.sampled_at <= VITALS_FRESH_FOR
                and 0 < reading.health_percent <= self.thresholds.health_recover
                and self.heal_keys.due(now, HEAL_RETRY))

    def heal_sent(self, now):
        self.heal_keys.sent(now)

    def potion_needed(self, now, reading):
        return (reading is not None and now - reading.sampled_at <= VITALS_FRESH_FOR
                and 0 < reading.health_percent < self.thresholds.potion_start)

    def potion_due(self, now, reading):
        return self.potion_needed(now, reading) and self.potion_keys.due(now)

    def potion_pending(self, now, reading):
        return self.potion_needed(now, reading) and self.potion_keys.pending(now)

    def potion_sent(self, now):
        self.potion_keys.sent(now)

    def observe_seal(self, active, now):
        if active == WISDOM and self.wisdom_since is None:
            self.wisdom_since = now
            self.wisdom_keys = _KeyAttempts()
        elif active is not None and active != WISDOM:
            self.wisdom_since = None

    def wisdom_due(self, now, reading, active):
        if active in (WISDOM, None) and self.wisdom_since is not None and now < self.wisdom_since + WISDOM_DURATION:
            return False
        if active == WISDOM and self.wisdom_since is None:
            return False
        repeat_after = WISDOM_DURATION if active in (WISDOM, None) else WISDOM_RETRY
        return (self.mana_locked and not self.healing and reading is not None
                and now - reading.sampled_at <= VITALS_FRESH_FOR and reading.health_percent >= self.thresholds.health_start
                and reading.mana_percent <= self.thresholds.mana_recover and self.wisdom_keys.due(now, repeat_after))

    def wisdom_sent(self, now):
        self.wisdom_keys.sent(now)
