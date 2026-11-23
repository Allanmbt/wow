"""鼠标附近的自绘拾取和采矿指针识别。"""

import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass


LOOT_THRESHOLD = 0.50


@dataclass
class ScreenMatch:
    score: float
    position: tuple
    region: dict
    template: object
    mask: object


class ScreenLootProbe:
    def __init__(self, native, hwnd, reference):
        import cv2
        import numpy as np

        self.native, self.hwnd = native, hwnd
        self.capture = None
        if reference.shape[2] == 4:
            # 带透明通道的素材已剔除背景，保留袋子及银灰色镐头的纹理。
            mask = (reference[:, :, 3] >= 240).astype(np.uint8) * 255
            scales = (0.5, 0.6, 0.667, 0.75, 0.8, 0.875, 0.9, 1.0, 1.1, 1.125,
                      1.25, 1.333, 1.5, 1.667, 1.75, 2.0)
            self.minimum_score, self.maximum_color_error = 0.65, 35
        else:
            hsv = cv2.cvtColor(reference, cv2.COLOR_BGR2HSV)
            mask = (((hsv[:, :, 0] <= 35) | (hsv[:, :, 0] >= 170))
                    & (hsv[:, :, 1] >= 65) & (hsv[:, :, 2] >= 40)).astype(np.uint8) * 255
            scales = (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30, 0.35, 0.40,
                      0.45, 0.50, 0.60, 0.75, 1.0, 1.25, 1.5)
            self.minimum_score, self.maximum_color_error = LOOT_THRESHOLD, 35
        mask = cv2.erode(mask, np.ones((3, 3), np.uint8))
        ys, xs = np.nonzero(mask)
        if len(xs) < 40:
            raise ValueError("指针模板中缺少可匹配的像素")
        crop = np.s_[ys.min():ys.max()+1, xs.min():xs.max()+1]
        image, mask = reference[:, :, :3][crop], mask[crop]
        self.templates = []
        seen = set()
        for scale in scales:
            size = (round(image.shape[1] * scale), round(image.shape[0] * scale))
            if min(size) < 10 or max(size) > 160 or size in seen:
                continue
            seen.add(size)
            template = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
            valid = cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST)
            if np.count_nonzero(valid) >= 30:
                self.templates.append((template, valid))

    def position(self):
        point = w.POINT()
        if not self.native.api.GetCursorPos(ctypes.byref(point)):
            raise OSError("无法读取鼠标位置")
        return point.x, point.y

    def grab(self, region):
        import cv2
        import mss
        import numpy as np

        if self.capture is None:
            self.capture = mss.MSS()  # 在拾取线程中创建，与战斗截图各自独立。
        return cv2.cvtColor(np.array(self.capture.grab(region)), cv2.COLOR_BGRA2BGR)

    def find(self):
        import cv2
        import numpy as np

        position = self.position()
        bounds = self.native.client_bounds(self.hwnd)
        x, y = position
        if not (bounds['left'] <= x < bounds['left'] + bounds['width']
                and bounds['top'] <= y < bounds['top'] + bounds['height']):
            return None
        left, top = max(bounds['left'], x - 24), max(bounds['top'], y - 24)
        right = min(bounds['left'] + bounds['width'], x + 180)
        bottom = min(bounds['top'] + bounds['height'], y + 180)
        region = dict(left=left, top=top, width=right-left, height=bottom-top)
        return self.match(self.grab(region), position, (left, top))

    def match(self, image, position, origin):
        import cv2
        import numpy as np

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        best = None
        for template, mask in self.templates:
            height, width = template.shape[:2]
            if height > image.shape[0] or width > image.shape[1]:
                continue
            needle = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            scores = cv2.matchTemplate(gray, needle, cv2.TM_CCOEFF_NORMED, mask=mask)
            scores = np.nan_to_num(scores, nan=-1, posinf=-1, neginf=-1)
            # 指针图案必须出现在鼠标热点附近。
            for axis, coord in ((1, position[0]-origin[0]), (0, position[1]-origin[1])):
                indices = np.arange(scores.shape[axis])
                outside = abs(indices - coord) > 24
                if axis == 1:
                    scores[:, outside] = -1
                else:
                    scores[outside, :] = -1
            _, score, _, (x, y) = cv2.minMaxLoc(scores)
            if score < self.minimum_score or (best and score <= best.score):
                continue
            patch = image[y:y+height, x:x+width]
            error = np.abs(patch.astype(np.float32) - template.astype(np.float32))[mask > 0].mean()
            if error <= self.maximum_color_error:
                best = ScreenMatch(float(score), position,
                                   dict(left=origin[0]+x, top=origin[1]+y, width=width, height=height),
                                   template, mask)
        return best

    def verify(self, hit):
        import cv2
        import numpy as np

        if not self.same_position(hit.position):
            return False
        image = self.grab(hit.region)
        # 仅复查上次识别到的局部纹理，背景动画不会使整帧指纹失效。
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        target = cv2.cvtColor(hit.template, cv2.COLOR_BGR2GRAY)
        score = float(cv2.matchTemplate(gray, target, cv2.TM_CCOEFF_NORMED, mask=hit.mask)[0, 0])
        error = np.abs(image.astype(np.float32) - hit.template.astype(np.float32))[hit.mask > 0].mean()
        return bool(np.isfinite(score) and score >= self.minimum_score and error <= self.maximum_color_error)

    def same_position(self, position):
        return max(abs(a-b) for a, b in zip(self.position(), position)) <= 4

    def close(self):
        if self.capture is not None:
            capture, self.capture = self.capture, None
            capture.close()
