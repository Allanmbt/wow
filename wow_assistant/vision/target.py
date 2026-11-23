"""识别固定目标框和血条填充比例。"""

from dataclasses import dataclass

import cv2
import numpy as np
from mss.exception import ScreenShotError

from ..geometry import region_on_screen
from ..paths import asset_path


REFERENCE = asset_path("target_frame.png")
FRAME_SIZE = (157, 60)
PORTRAIT_WIDTH = 80
SAMPLE_INTERVAL = 0.10
IDLE_SAMPLE_INTERVAL = 0.25
SEARCH_INTERVAL = 2.0


@dataclass(frozen=True)
class TargetReading:
    state: str
    percent: int | None = None
    health: tuple[int, int] | None = None
    engaged: bool | None = None

    @property
    def alive(self):
        return self.state == "alive"

    def at_least(self, percent):
        if self.health:
            current, maximum = self.health
            return current * 100 >= maximum * percent
        # 条宽存在取整和描边误差，临界值等可信的数字证据。
        return self.percent is not None and self.percent >= percent + 2

    def describe(self):
        if self.alive:
            prefix = {True: "战斗中", False: "已选中"}.get(self.engaged, "存活目标")
            if self.health:
                current, maximum = self.health
                return f"{prefix} · 目标血量 {100 * current / maximum:.1f}%"
            return f"{prefix} · 目标血量约 {self.percent}%"
        return {
            "dead": "目标已死亡 · Q/E/R 暂停",
            "missing": "未见目标框 · Q/E/R 暂停",
            "non_hostile": "目标名称栏非红底 · Q/E/R 暂停",
            "unknown": "目标存活未确认 · Q/E/R 暂停",
        }[self.state]


MISSING = TargetReading("missing")
NON_HOSTILE = TargetReading("non_hostile")
UNKNOWN = TargetReading("unknown")
DEAD = TargetReading("dead", 0)


def _yellow(image):
    blue, green, red = cv2.split(image.astype(np.int16))
    return ((red > 110) & (green > 85) & (red - green < 90)
            & (green - blue > 65) & (red - blue > 95)).astype(np.uint8)


def _name_background(frame):
    blue, green, red = cv2.split(frame[5:21, 5:152].astype(np.int16))
    hostile = ((red >= 45) & (red - green >= 22) & (red - blue >= 24)
               & (5 * (green - blue) <= 2 * (red - blue)))
    if _covers_name(hostile):
        return "hostile"
    green_background = (green >= 39) & (green - red >= 14) & (green - blue >= 16)
    yellow_background = ((red >= 50) & (green >= 40) & (red - blue >= 26)
                         & (green - blue >= 20) & (2 * (green - blue) >= red - blue))
    return "friendly" if _covers_name(green_background | yellow_background) else None


def _covers_name(mask):
    return mask.mean() >= 0.60 and mask[:, :21].mean() >= 0.50 and mask[:, -19:].mean() >= 0.50


class HealthDigits:
    symbols = "0123456789/"

    def __init__(self):
        atlas_path = asset_path("target_hp_digits.png")
        atlas = cv2.imdecode(np.frombuffer(atlas_path.read_bytes(), np.uint8), cv2.IMREAD_GRAYSCALE)
        if atlas is None or atlas.shape[1] != 99 or atlas.shape[0] % 12:
            raise ValueError("目标血量数字模板无效")
        self.glyphs = atlas.reshape(-1, 12, 11, 9).transpose(2, 0, 1, 3).reshape(11, -1, 108) > 0
        self.sizes = self.glyphs.sum(axis=2)

    def read(self, image):
        mask = ((image.min(axis=2) > 155) & (np.ptp(image, axis=2) < 60)).astype(np.uint8)
        _, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        components = sorted((int(box[0]), i, box) for i, box in enumerate(stats[1:], 1) if box[3] >= 7)
        if len(components) < 3:
            return None
        characters = []
        for _, index, (x, y, width, height, _) in components:
            if width > 9 or height > 12:
                return None
            glyph = np.zeros((12, 9), dtype=bool)
            glyph[:height, :width] = labels[y:y+height, x:x+width] == index
            vector = glyph.ravel()
            overlap = (self.glyphs & vector).sum(axis=2)
            scores = (overlap / (self.sizes + vector.sum() - overlap)).max(axis=1)
            runner_up, best = np.argsort(scores)[-2:]
            if scores[best] < 0.72 or scores[best] - scores[runner_up] < 0.10:
                return None
            characters.append(self.symbols[best])
        text = "".join(characters).split("/")
        if len(text) != 2 or not all(part.isdigit() for part in text):
            return None
        current, maximum = map(int, text)
        return (current, maximum) if 0 <= current <= maximum and maximum > 0 else None


def _engaged(portrait):
    if portrait is None or min(portrait.shape[:2]) < 12:
        return None
    portrait = cv2.resize(portrait, (PORTRAIT_WIDTH, FRAME_SIZE[1]))
    blue, green, red = cv2.split(portrait.astype(np.float32))
    saturated_red = (red > 135) & (red > 1.6 * green) & (red > 1.6 * blue)
    gold = (red > 108) & (green > 78) & (green >= 0.60 * red) & (red >= green) & (green > blue + 11)
    if saturated_red[12:43, 69:79].mean() > 0.38:
        return True
    if saturated_red[12:43, 69:79].mean() < 0.16 and gold[10:44, 67:78].mean() > 0.27:
        return False
    return None


class TargetFrameDetector:
    def __init__(self):
        template = cv2.imdecode(np.frombuffer(REFERENCE.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if template is None or template.shape[:2] != FRAME_SIZE[::-1]:
            raise ValueError("目标框识别模板无效")
        self.edge = cv2.cvtColor(template[:, 1:6], cv2.COLOR_BGR2GRAY)
        self.death_text = _yellow(template[27:40, 61:96])
        self.digits = HealthDigits()

    def _frame_score(self, image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        edge = float(cv2.matchTemplate(gray[:, :6], self.edge, cv2.TM_CCOEFF_NORMED).max())
        inside = gray[:, 10:145]
        top = np.median(inside[:4].max(axis=0))
        divider = np.median(inside[42:47].max(axis=0))
        bottom = np.median(inside[56:60].max(axis=0))
        right = np.median(gray[6:54, 152:157].max(axis=1))
        if edge < 0.69 or min(top, divider, bottom, right) < 103:
            return 0.0
        if divider - np.median(inside[47:56]) < 42 or bottom - np.median(inside[48:56]) < 38:
            return 0.0
        return edge

    def _read_bar(self, frame, portrait=None):
        health = frame[27:43, 3:153]
        if cv2.minMaxLoc(cv2.matchTemplate(_yellow(health), self.death_text, cv2.TM_CCORR_NORMED))[1] >= 0.66:
            return DEAD
        background = _name_background(frame)
        if background != "hostile":
            return NON_HOSTILE if background == "friendly" else UNKNOWN

        blue, green, red = cv2.split(health.astype(np.int16))
        green_pixels = (green > 47) & (green - red > 19) & (green - blue > 19)
        filled = (green_pixels.sum(axis=0) >= 3).astype(np.uint8)
        filled = cv2.morphologyEx(filled[None, :], cv2.MORPH_CLOSE, np.ones((1, 3), np.uint8))[0]
        starts = np.flatnonzero(filled)
        if not len(starts) or starts[0] > 2:
            return UNKNOWN
        gaps = np.flatnonzero(filled[starts[0]:] == 0)
        end = int(starts[0] + gaps[0]) if len(gaps) else len(filled)
        if end - starts[0] < 2:
            return UNKNOWN
        fraction = 100 * end / len(filled)
        health_numbers = self.digits.read(health)
        if health_numbers:
            current, maximum = health_numbers
            if current == 0:
                return UNKNOWN
            shown = 100 * current / maximum
            if abs(shown - fraction) <= 5:
                return TargetReading("alive", max(1, round(shown)), health_numbers, _engaged(portrait))
        return TargetReading("alive", max(1, round(fraction)), engaged=_engaged(portrait))

    def read(self, image, portrait=None):
        if min(image.shape[:2]) < 20:
            return MISSING
        frame = cv2.resize(image, FRAME_SIZE, interpolation=cv2.INTER_LINEAR)
        return self._read_bar(frame, portrait) if self._frame_score(frame) else MISSING

    def locate(self, image):
        height, width = image.shape[:2]
        if min(width, height) < 20:
            return None
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        ratio = min(1.0, 680 / width, 500 / height)
        search = cv2.resize(gray, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA) if ratio < 1 else gray
        best = None
        best_score = 0.0
        heights = sorted(range(40, 85), key=lambda value: abs(value - 60))
        for frame_height in (*heights, 86, 90, 95, 101, 107, 114, 121):
            frame_width = round(FRAME_SIZE[0] * frame_height / FRAME_SIZE[1])
            if frame_width > width or frame_height > height:
                continue
            edge_width = max(4, round(self.edge.shape[1] * frame_height / FRAME_SIZE[1]))
            edge = cv2.resize(self.edge, (edge_width, frame_height), interpolation=cv2.INTER_LINEAR)
            small_edge = cv2.resize(edge, (max(2, round(edge_width * ratio)), round(frame_height * ratio)),
                                    interpolation=cv2.INTER_AREA)
            scores = cv2.matchTemplate(search, small_edge, cv2.TM_CCOEFF_NORMED)
            for _ in range(6):
                _, anchor_score, _, (sx, sy) = cv2.minMaxLoc(scores)
                if anchor_score < 0.71:
                    break
                scores[max(0, sy-3):sy+4, max(0, sx-4):sx+5] = -1
                x, y = round(sx / ratio), round(sy / ratio)
                margin = max(2, round(1 / ratio))
                left, top = max(0, x-margin), max(0, y-margin)
                probe = gray[top:y+frame_height+margin, left:x+edge_width+margin]
                if probe.shape[0] < frame_height or probe.shape[1] < edge_width:
                    continue
                _, _, _, (dx, dy) = cv2.minMaxLoc(cv2.matchTemplate(probe, edge, cv2.TM_CCOEFF_NORMED))
                x, y = max(0, left + dx - round(frame_height / FRAME_SIZE[1])), top + dy
                if x+frame_width > width or y+frame_height > height:
                    continue
                frame = cv2.resize(image[y:y+frame_height, x:x+frame_width], FRAME_SIZE)
                score = self._frame_score(frame)
                if score <= best_score:
                    continue
                extent = round(PORTRAIT_WIDTH * frame_height / FRAME_SIZE[1])
                portrait = image[y:y+frame_height, x+frame_width:x+frame_width+extent] if x+frame_width+extent <= width else None
                reading = self._read_bar(frame, portrait)
                if reading.alive or (reading == DEAD and _name_background(frame) == "hostile"):
                    best_score = score
                    best = (dict(left=x, top=y, width=frame_width, height=frame_height), reading)
                    if score >= 0.90:
                        return best
        return best


def saved_frame(region, client):
    return dict(region, left=region["left"] - client["left"], top=region["top"] - client["top"],
                client_width=client["width"], client_height=client["height"])


def load_frame(value):
    keys = ("left", "top", "width", "height", "client_width", "client_height")
    if (not isinstance(value, dict) or any(type(value.get(key)) is not int for key in keys)
            or value["left"] < 0 or value["top"] < 0 or min(value["width"], value["height"]) < 20
            or value["left"] + value["width"] > value["client_width"]
            or value["top"] + value["height"] > value["client_height"]):
        return None
    return {key: value[key] for key in keys}


class TargetMonitor:
    def __init__(self, capture, client_bounds, calibration, on_located):
        self.capture, self.client_bounds, self.on_located = capture, client_bounds, on_located
        self.detector = TargetFrameDetector()
        self.calibration = load_frame(calibration)
        self.reading = MISSING
        self.next_sample = self.next_search = 0.0

    def _grab(self, region):
        return cv2.cvtColor(np.asarray(self.capture.grab(region)), cv2.COLOR_BGRA2BGR)

    def sample(self, now, *, force=False):
        if now < self.next_sample and not force:
            return self.reading
        self.next_sample = now + SAMPLE_INTERVAL
        self.reading = UNKNOWN
        try:
            client = self.client_bounds()
            profile = self.calibration
            same_size = profile and (profile["client_width"], profile["client_height"]) == (client["width"], client["height"])
            if same_size:
                region = dict(left=client["left"] + profile["left"], top=client["top"] + profile["top"],
                              width=profile["width"], height=profile["height"])
                if region_on_screen(region, self.capture.monitors[1:]):
                    extent = round(PORTRAIT_WIDTH * region["height"] / FRAME_SIZE[1])
                    extended = dict(region, width=region["width"] + extent)
                    if (profile["left"] + extended["width"] <= client["width"]
                            and region_on_screen(extended, self.capture.monitors[1:])):
                        image = self._grab(extended)
                        self.reading = self.detector.read(image[:, :region["width"]], image[:, region["width"]:])
                    else:
                        self.reading = self.detector.read(self._grab(region))
                if self.reading != MISSING or force:
                    return self.reading
            if not force and now >= self.next_search:
                self.next_search = now + SEARCH_INTERVAL
                if same_size:
                    left = max(0, profile["left"] - 40)
                    top = max(0, profile["top"] - 40)
                    right = min(client["width"], profile["left"] + profile["width"] * 2)
                    bottom = min(client["height"], profile["top"] + profile["height"] * 2)
                else:
                    left, top = round(client["width"] * 0.47), round(client["height"] * 0.34)
                    right, bottom = client["width"], client["height"]
                search = dict(left=client["left"] + left, top=client["top"] + top,
                              width=right - left, height=bottom - top)
                if region_on_screen(search, self.capture.monitors[1:]):
                    match = self.detector.locate(self._grab(search))
                    if match:
                        region, self.reading = match
                        region = dict(region, left=search["left"] + region["left"], top=search["top"] + region["top"])
                        self.calibration = saved_frame(region, client)
                        self.on_located(self.calibration)
                    else:
                        self.reading = MISSING
        except (OSError, ScreenShotError, cv2.error):
            self.reading = UNKNOWN
        finally:
            if not force and not self.reading.alive:
                self.next_sample = now + IDLE_SAMPLE_INTERVAL
        return self.reading
