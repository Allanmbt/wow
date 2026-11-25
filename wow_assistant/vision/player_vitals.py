"""Read the paired player health and mana bars independently of the target."""

from dataclasses import dataclass

import cv2
import numpy as np
from mss.exception import ScreenShotError

from ..geometry import region_on_screen, valid_region


@dataclass(frozen=True)
class PlayerVitals:
    health_percent: int
    mana_percent: int
    sampled_at: float


def _metal(image):
    b, g, r = cv2.split(image.astype(np.int16))
    return ((r >= 115) & (g >= .76*r) & (g <= r+9) & (b >= .51*r) & (r-b >= 11))


def _fill(image, channel):
    bgr = image.astype(np.int16)
    fill = bgr[:, :, channel]
    others = np.delete(bgr, channel, axis=2).max(axis=2)
    columns = ((fill >= 64) & (fill-others >= 35)).mean(axis=0) >= .24
    columns = cv2.morphologyEx(columns.astype(np.uint8)[None, :], cv2.MORPH_CLOSE,
                              np.ones((1, 13), np.uint8))[0]
    filled = np.flatnonzero(columns)
    if not len(filled):
        return 0
    if filled[0] > 4:
        return None
    gaps = np.flatnonzero(columns[filled[0]:] == 0)
    end = filled[0] + gaps[0] if len(gaps) else len(columns)
    return round(100 * end / len(columns))


def read_bars(image):
    frame = cv2.resize(image, (280, 52), interpolation=cv2.INTER_LINEAR)
    metal = _metal(frame)[:, 20:-20]
    if any(metal[start:end].mean(axis=1).max() < .57 for start, end in ((0, 4), (21, 30), (48, 52))):
        return None
    health = _fill(frame[5:20, 3:-3], 1)
    mana = _fill(frame[31:46, 3:-3], 0)
    return None if health is None or mana is None else (health, mana)


def locate_bars(image):
    outline = cv2.morphologyEx(_metal(image).astype(np.uint8), cv2.MORPH_OPEN, np.ones((1, 123), np.uint8))
    _, _, boxes, _ = cv2.connectedComponentsWithStats(outline)
    matches = []
    for x, y, width, height, _ in boxes[1:]:
        if not 135 <= width <= 440 or height > 6:
            continue
        for frame_width in (width+6, width+10, round(width*1.41), round(width*1.43)):
            left = int(x + width - frame_width + 3)
            size = round(frame_width * 51/280)
            for top in (int(y), int(y-round(size*.46))):
                if left < 0 or top < 0 or left+frame_width > image.shape[1] or top+size > image.shape[0]:
                    continue
                box = left, top, int(frame_width), size
                reading = read_bars(image[top:top+size, left:left+frame_width])
                if reading is not None:
                    matches.append((sum(value > 0 for value in reading), -abs(left+frame_width/2-image.shape[1]/2), box, reading))
    if matches:
        _, _, box, reading = max(matches)
        return box, reading
    return None


class PlayerVitalsMonitor:
    def __init__(self, capture, client_bounds):
        self.capture, self.client_bounds = capture, client_bounds
        self.bounds = self.box = None
        self.next_search = 0.0

    def grab(self, bounds, box):
        x, y, width, height = box
        region = dict(left=bounds["left"]+x, top=bounds["top"]+y, width=width, height=height)
        if region_on_screen(region, self.capture.monitors[1:]):
            return np.asarray(self.capture.grab(region))[:, :, :3]
        return None

    def sample(self, now):
        try:
            bounds = self.client_bounds()
            if not valid_region(bounds):
                return None
            if bounds != self.bounds:
                self.bounds, self.box, self.next_search = bounds, None, 0.0
            if self.box is not None:
                image = self.grab(bounds, self.box)
                reading = read_bars(image) if image is not None else None
                if reading is not None:
                    return PlayerVitals(*reading, sampled_at=now)
                self.box = None
            if now >= self.next_search:
                self.next_search = now + 1.6
                width, height = bounds["width"], bounds["height"]
                x, y = round(width*.23), round(height*.42)
                image = self.grab(bounds, (x, y, width-2*x, round(height*.47)))
                found = locate_bars(image) if image is not None else None
                if found:
                    (left, top, w, h), reading = found
                    self.box = x+left, y+top, w, h
                    return PlayerVitals(*reading, sampled_at=now)
        except (OSError, ValueError, ScreenShotError, cv2.error):
            self.box = None
        return None
