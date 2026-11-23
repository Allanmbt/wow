"""读取插件圣印条、左侧储存图标，并确认每次切印。"""

import base64
import binascii
from dataclasses import dataclass

import cv2
import numpy as np
from mss.exception import ScreenShotError

from ..geometry import region_on_screen
from ..paths import asset_path
from .target import load_frame


COMMAND = "command"
RIGHTEOUS = "righteous"
NO_SEAL = "none"
SEAL_NAMES = {COMMAND: "命令", RIGHTEOUS: "正义", NO_SEAL: "无"}
SWITCH_MIN_HP = 20
SAMPLE_INTERVAL = 0.15
OUT_OF_COMBAT_INTERVAL = 0.30
SWITCH_GCD = 1.05
SWITCH_RETRY_INTERVAL = 0.52
SWITCH_ATTEMPTS = 3
SWITCH_TIMEOUT = 1.8
ASSET = asset_path("seal_dance_icons.png")
RIGHTEOUS_STACK = asset_path("seal_righteous_stack.png")
ICON_SIZE = 48


def _photo(encoded):
    if not isinstance(encoded, str) or len(encoded) > 30000:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None
    except (binascii.Error, cv2.error, ValueError):
        return None
    return image if image is not None and image.shape[:2] == (ICON_SIZE, ICON_SIZE) else None


def load_seal_setup(data):
    if not isinstance(data, dict):
        return {}
    setup = {key: region for key in ("current", "storage") if (region := load_frame(data.get(key)))}
    if _photo(data.get("righteous_icon")) is not None:
        setup["righteous_icon"] = data["righteous_icon"]
        size = data.get("storage_size")
        if type(size) is int and 20 <= size <= 160:
            setup["storage_size"] = size
    return setup


def setup_complete(setup):
    return all(key in setup for key in ("current", "storage")) and righteous_reference_available(setup)


def righteous_reference_available(setup):
    return "righteous_icon" in setup or RIGHTEOUS_STACK.is_file()


@dataclass(frozen=True)
class SealReading:
    active: str | None = None
    stored: frozenset[str] = frozenset()
    three: frozenset[str] = frozenset()
    storage_known: bool = False

    def describe(self):
        active = SEAL_NAMES.get(self.active, "未确认")
        stacks = "、".join(SEAL_NAMES[seal] for seal in (COMMAND, RIGHTEOUS) if seal in self.three)
        return f"当前圣印 {active} · " + (f"{stacks}三层" if stacks else "未确认三层")


def _bright_digits(icon):
    lower = icon[29:48, 30:48]
    return ((lower.min(axis=2) > 140) & (np.ptp(lower, axis=2) < 65)).astype(np.uint8)


def _descriptor(icon, size):
    tile = cv2.resize(icon, (size, size))
    inset = max(1, round(size * 0.10))
    return cv2.cvtColor(tile[inset:round(size * 0.73), inset:size-inset], cv2.COLOR_BGR2GRAY)


def _single_storage_slot(profile):
    return profile and profile["width"] <= 1.45 * profile["height"]


class SealDetector:
    def __init__(self, setup):
        atlas = cv2.imdecode(np.frombuffer(ASSET.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if atlas is None or atlas.shape[:2] != (ICON_SIZE, ICON_SIZE * 4):
            raise ValueError("圣印图标模板无效")
        command, righteous, command_storage, dark_storage = np.hsplit(atlas, 4)
        built_in = None
        if RIGHTEOUS_STACK.is_file():
            built_in = cv2.imdecode(np.frombuffer(RIGHTEOUS_STACK.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        override = _photo(setup.get("righteous_icon"))
        if built_in is None and override is None and RIGHTEOUS_STACK.is_file():
            raise ValueError("正义储存图标模板无效")
        digit_sources = [command_storage]
        righteous_icons = [righteous]
        for reference in (built_in, override):
            if reference is not None:
                digit_sources.append(cv2.resize(reference, (ICON_SIZE, ICON_SIZE)))
                righteous_icons.insert(0, reference)
        self.three_templates = [_bright_digits(icon)[5:18, 5:15] for icon in digit_sources]
        self.current = (self._templates([command], range(16, 57)),
                        self._templates([righteous], range(16, 57)))
        size = setup.get("storage_size")
        profile = setup.get("storage")
        if size:
            sizes = range(max(20, size - 4), size + 5)
        elif _single_storage_slot(profile) and 30 <= profile["height"] <= 70:
            sizes = range(round(0.75 * profile["height"]), profile["height"] + 1)
        else:
            sizes = range(22, 70)
        self.storage = (self._templates([command_storage, dark_storage], sizes),
                        self._templates(righteous_icons, sizes))

    @staticmethod
    def _templates(icons, sizes):
        return [(size, _descriptor(icon, size)) for icon in icons for size in sizes]

    @staticmethod
    def _find(gray, templates):
        height, width = gray.shape
        best = (0.0, None)
        for size, tile in templates:
            if size > min(height, width):
                continue
            inset = max(1, round(size * 0.10))
            visible = gray[inset:height-size+inset+tile.shape[0],
                           inset:width-size+inset+tile.shape[1]]
            _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(visible, tile, cv2.TM_CCOEFF_NORMED))
            if score > best[0]:
                best = (score, (*point, size))
        return best

    def has_three(self, icon):
        mask = _bright_digits(cv2.resize(icon, (ICON_SIZE, ICON_SIZE)))
        for template in self.three_templates:
            _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(mask, template, cv2.TM_CCORR_NORMED))
            patch = mask[y:y+template.shape[0], x:x+template.shape[1]]
            if score >= 0.72 and 0.55 <= patch.sum() / template.sum() <= 1.65:
                return True
        return False

    def current_match(self, image):
        gray = cv2.cvtColor(image[:, :105], cv2.COLOR_BGR2GRAY)
        command, righteous = (self._find(gray, templates) for templates in self.current)
        best, runner_up = sorted((command[0], righteous[0]), reverse=True)
        if best >= 0.80 and best - runner_up >= 0.08:
            return (COMMAND, command[1], best) if command[0] > righteous[0] else (RIGHTEOUS, righteous[1], best)
        return None, None, best

    def read_current(self, image, empty_slot):
        seal, _, score = self.current_match(image)
        if seal is None and empty_slot and score < 0.80:
            return NO_SEAL
        return seal

    def read_storage(self, image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        found = {}
        for seal, templates in zip((COMMAND, RIGHTEOUS), self.storage):
            score, box = self._find(gray, templates)
            if score >= 0.74:
                found[seal] = (score, box)
        if len(found) == 2:
            first, second = found.values()
            _, (x, y, size) = first
            _, (other_x, other_y, other_size) = second
            if abs(x - other_x) < min(size, other_size) / 2 and abs(y - other_y) < min(size, other_size) / 2:
                if abs(first[0] - second[0]) < 0.08:
                    return frozenset(), frozenset(), False
                del found[COMMAND if first[0] < second[0] else RIGHTEOUS]
        three = {seal for seal, (_, (x, y, size)) in found.items() if self.has_three(image[y:y+size, x:x+size])}
        return frozenset(found), frozenset(three), True

    def learn_righteous(self, image):
        height, width = image.shape[:2]
        if min(height, width) < 20 or max(height, width) > 160:
            raise ValueError("请框住正义圣印的单个储存图标，右下角须有 3")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        score, box = self._find(gray, self.storage[1])
        candidates = []
        if score >= 0.75:
            x, y, size = box
            candidates.append(image[y:y+size, x:x+size])
        if 0.80 <= width / height <= 1.25:
            candidates.append(image)
        for candidate in candidates:
            if not self.has_three(candidate):
                continue
            tile = _descriptor(candidate, ICON_SIZE)
            command, righteous = (next(float(cv2.matchTemplate(tile, sample, cv2.TM_CCOEFF_NORMED)[0, 0])
                                       for size, sample in group if size == ICON_SIZE) for group in self.current)
            if tile.std() >= 12 and righteous >= 0.43 and command < righteous + 0.08:
                icon = cv2.resize(candidate, (ICON_SIZE, ICON_SIZE))
                encoded = base64.b64encode(cv2.imencode(".png", icon)[1]).decode("ascii")
                return encoded, round(sum(candidate.shape[:2]) / 2)
        raise ValueError("未确认正义储存三层，请框住右下角带 3 的完整正义图标")


class SealMonitor:
    def __init__(self, capture, client_bounds, setup):
        self.capture, self.client_bounds, self.setup = capture, client_bounds, setup
        self.detector = SealDetector(setup)
        self.next_sample = 0.0
        self.reading = SealReading()

    def _grab(self, key, client):
        profile = self.setup.get(key)
        if not profile or (profile["client_width"], profile["client_height"]) != (client["width"], client["height"]):
            return None
        region = dict(left=client["left"] + profile["left"], top=client["top"] + profile["top"],
                      width=profile["width"], height=profile["height"])
        if key == "storage" and _single_storage_slot(profile):
            # 储存增益会随其他图标的出现移动一个位置。
            margin = profile["height"] + 6
            left = max(client["left"], region["left"] - margin)
            right = min(client["left"] + client["width"], region["left"] + region["width"] + margin)
            nearby = dict(region, left=left, width=right - left)
            if region_on_screen(nearby, self.capture.monitors[1:]):
                region = nearby
        if not region_on_screen(region, self.capture.monitors[1:]):
            return None
        return cv2.cvtColor(np.asarray(self.capture.grab(region)), cv2.COLOR_BGRA2BGR)

    def sample(self, now, empty_slot, *, force=False, in_combat=True, check_storage=False):
        if now < self.next_sample and not force:
            return self.reading
        self.next_sample = now + (SAMPLE_INTERVAL if in_combat else OUT_OF_COMBAT_INTERVAL)
        try:
            client = self.client_bounds()
            active_image = self._grab("current", client)
            storage_image = self._grab("storage", client) if in_combat or check_storage else None
            active = self.detector.read_current(active_image, empty_slot) if active_image is not None else None
            stored, three, known = self.detector.read_storage(storage_image) if storage_image is not None else (frozenset(), frozenset(), False)
            self.reading = SealReading(active, stored, three, known)
        except (OSError, ScreenShotError, cv2.error):
            self.reading = SealReading()
        return self.reading


@dataclass(frozen=True)
class SealAction:
    destination: str
    consumed: str | None = None

    @property
    def key(self):
        return "1" if self.destination == RIGHTEOUS else "Q"


class SealDancePolicy:
    def __init__(self):
        self.pending = None
        self.attempts = 0
        self.started_at = self.sent_at = 0.0
        self.next_action_at = 0.0
        self.used_stacks = set()
        self.reset_observations()

    def reset_observations(self):
        self.last_reading = None
        self.stable_frames = 0
        self.last_active = None
        self.active_frames = 0

    def observe(self, reading, now):
        if reading == self.last_reading:
            self.stable_frames += 1
        else:
            self.last_reading, self.stable_frames = reading, 1
        if reading.active == self.last_active:
            self.active_frames += 1
        else:
            self.last_active, self.active_frames = reading.active, 1
        if self.stable_frames >= 2 and reading.storage_known:
            self.used_stacks.intersection_update(reading.three)
        if self.pending is None:
            return
        confirmed = self.active_frames >= 2 and reading.active == self.pending.destination
        timed_out = (now >= self.started_at + SWITCH_TIMEOUT
                     or (self.attempts >= SWITCH_ATTEMPTS and now >= self.sent_at + 0.45))
        if confirmed or timed_out:
            source = self.pending.consumed
            # 上次的三层可能仍在界面上。只锁住这一种储存，另一种三层仍可切印。
            if (source is not None and (confirmed or self.attempts >= SWITCH_ATTEMPTS)
                    and (not reading.storage_known or source in reading.three or self.stable_frames < 2)):
                self.used_stacks.add(source)
            self.next_action_at = self.sent_at + (2.0 if not confirmed and source is None else SWITCH_GCD)
            self.pending = None

    @staticmethod
    def eligible(reading, target):
        if not target.alive or reading.active is None:
            return None
        if reading.active == NO_SEAL or (reading.active == RIGHTEOUS and target.engaged is False):
            return SealAction(COMMAND)
        if (target.engaged is True and target.at_least(SWITCH_MIN_HP) and reading.storage_known
                and reading.active in reading.three):
            return SealAction(RIGHTEOUS if reading.active == COMMAND else COMMAND, reading.active)
        return None

    def decide(self, reading, target, now):
        if self.stable_frames < 2:
            return None
        action = self.eligible(reading, target)
        if action is None or action.consumed in self.used_stacks:
            return None
        if self.pending is not None:
            if (action == self.pending and self.attempts < SWITCH_ATTEMPTS
                    and now >= self.sent_at + SWITCH_RETRY_INTERVAL):
                return action
            return None
        return action if now >= self.next_action_at else None

    def reserves_attack(self, reading, target):
        if self.pending is not None:
            return (reading.active == self.pending.destination
                    or self.eligible(reading, target) == self.pending)
        action = self.eligible(reading, target)
        return action is not None and action.consumed not in self.used_stacks

    def sent(self, action, now):
        if self.pending is None:
            self.started_at, self.attempts = now, 0
        self.attempts += 1
        self.pending, self.sent_at = action, now
        self.reset_observations()
