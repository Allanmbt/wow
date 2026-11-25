"""Recognize the current-seal plugin's framed icon and fixed Chinese label."""

import cv2
import numpy as np

from ..paths import asset_path


BADGE_SIZE = 55
BADGE_ASSET = asset_path("seal_current_badges.png")
WISDOM_ASSET = asset_path("seal_wisdom_badge.png")


def _gold(image):
    blue, green, red = cv2.split(image.astype(np.int16))
    return ((red >= 164) & (green >= 132) & (red - blue >= 98)
            & (green - blue >= 85) & (red - green <= 88)).astype(np.uint8)


def _unit(values):
    length = np.linalg.norm(values)
    return values / length if length else values


def _name_signature(image):
    if not image.size:
        return None
    ink = _gold(image)
    x, y, width, height = cv2.boundingRect(ink)
    if height < 6 or width < 3 * height or width > 7 * height or ink.sum() < 35:
        return None
    name = cv2.resize(ink[y:y+height, x:x+width].astype(np.float32), (80, 20), interpolation=cv2.INTER_AREA)
    # 后两个字都是「圣印」，只比较能区分两种圣印的前两个字。
    return _unit(name[:, :40].ravel())


class SealBadgeDetector:
    def __init__(self):
        atlas = cv2.imdecode(np.frombuffer(BADGE_ASSET.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if atlas is None or atlas.shape[:2] != (80, 174):
            raise ValueError("当前圣印图标模板无效")
        ys, xs = np.ogrid[:BADGE_SIZE, :BADGE_SIZE]
        interior = (ys >= 4) & (ys < BADGE_SIZE - 4) & (xs >= 4) & (xs < BADGE_SIZE - 4)
        timer = (abs(ys - 27) <= 15) & (abs(xs - 27) <= 17)
        self.art_mask = interior & ~timer
        samples = np.hsplit(atlas, 2)
        self.art_reference = [self._art(image[:55, 16:71]) for image in samples]
        self.name_reference = [_name_signature(image[57:80]) for image in samples]
        wisdom = cv2.imdecode(np.frombuffer(WISDOM_ASSET.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if wisdom is None or wisdom.shape[:2] != (85, 80):
            raise ValueError("智慧圣印图标模板无效")
        self.wisdom_icon = wisdom[6:61, 14:69]
        self.art_reference = np.stack([*self.art_reference, self._art(self.wisdom_icon)])
        self.name_reference = np.stack([*self.name_reference, _name_signature(wisdom[62:85])])

    def _art(self, image):
        gray = cv2.cvtColor(cv2.resize(image, (BADGE_SIZE, BADGE_SIZE)), cv2.COLOR_BGR2GRAY)
        values = gray[self.art_mask].astype(np.float32)
        return _unit(values - values.mean())

    @staticmethod
    def _outlined(mask):
        edge = max(2, round(min(mask.shape) * 0.07))
        coverage = (mask[:edge].any(axis=0).mean(), mask[-edge:].any(axis=0).mean(),
                    mask[:, :edge].any(axis=1).mean(), mask[:, -edge:].any(axis=1).mean())
        return min(coverage) >= 0.72

    def match(self, image, *, wisdom=False):
        gold = _gold(image)
        _, _, boxes, _ = cv2.connectedComponentsWithStats(gold)
        result = None
        confirmed = []
        for x, y, width, height, _ in boxes[1:]:
            if not 18 <= min(width, height) <= max(width, height) <= 200 or not 0.88 <= width / height <= 1.14:
                continue
            x, y, width, height = map(int, (x, y, width, height))
            if not self._outlined(gold[y:y+height, x:x+width]):
                continue
            box = x, y, width, height
            count = 3 if wisdom else 2
            art = self.art_reference[:count] @ self._art(image[y:y+height, x:x+width])
            left = max(0, x - round(width * 0.30))
            right = x + width + round(width * 0.30)
            name = _name_signature(image[y+height+1:y+height+round(height*0.46), left:right])
            text = self.name_reference[:count] @ name if name is not None else None
            icon_choice = self._choose(art, 0.74, 0.12)
            label_choice = self._choose(text, 0.72, 0.14) if text is not None else None
            if icon_choice is not None and label_choice in (None, icon_choice):
                confirmed.append((icon_choice, box))
            elif icon_choice is None and label_choice is not None and art[label_choice] >= 0.42:
                confirmed.append((label_choice, box))
            result = (None, box)
        return confirmed[0] if len(confirmed) == 1 else result

    @staticmethod
    def _choose(scores, minimum, margin):
        runner_up, index = np.argsort(scores)[-2:]
        return int(index) if scores[index] >= minimum and scores[index] - scores[runner_up] >= margin else None
