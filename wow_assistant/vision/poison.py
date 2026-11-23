"""在已校验的减益栏内查找中毒图标；模板列表可继续扩展。"""

from dataclasses import dataclass
from ..paths import asset_path

POISON_TEMPLATES = (asset_path("poison_1.png"), asset_path("poison_2.png"))


@dataclass
class PoisonHit:
    name: str
    score: float
    size: int


class PoisonDetector:
    def __init__(self, icon_size=None, paths=POISON_TEMPLATES):
        import cv2
        import numpy as np

        self.templates = []
        sizes = range(20, 65, 2) if icon_size is None else range(max(16, icon_size-2), icon_size+3, 2)
        for path in paths:
            reference = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            if reference is None or min(reference.shape[:2]) < 12:
                raise ValueError(f"中毒模板无效：{path.name}")
            for size in sizes:
                image = cv2.resize(reference, (size, size), interpolation=cv2.INTER_AREA)
                # 不比较外边框、光点和倒计时文字，保留图标主体纹理。
                margin = max(2, round(size*0.12))
                image = image[margin:-margin, margin:-margin]
                self.templates.append((path.stem, size, image, cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)))

    def find(self, frame):
        import cv2
        import numpy as np

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        best = None
        for name, size, image, needle in self.templates:
            h, width = needle.shape
            if h > gray.shape[0] or width > gray.shape[1]:
                continue
            scores = cv2.matchTemplate(gray, needle, cv2.TM_CCOEFF_NORMED)
            _, score, _, (x, y) = cv2.minMaxLoc(scores)
            if not np.isfinite(score) or score < 0.88 or (best and score <= best.score):
                continue
            patch = frame[y:y+h, x:x+width]
            error = np.abs(patch.astype(np.float32)-image.astype(np.float32)).mean()
            if error <= 35:
                best = PoisonHit(name, float(score), size)
        return best


class CleansePolicy:
    """出现即净化，仍中毒则在公共冷却后复查重试；无图标不发 V。"""
    def __init__(self):
        self.last_sent = -float("inf")
        self.block_until = 0.0
        self.count = 0
        self.pending = False
        self.absent_since = None
        self.confirmed = 0

    def observe(self, now, hit):
        if hit is not None:
            self.absent_since = None
        elif self.pending:
            if self.absent_since is None:
                self.absent_since = now
            elif now-self.absent_since >= 0.25:
                self.pending = False
                self.confirmed += 1
                return True
        return False

    def due(self, now, hit):
        return hit is not None and now-self.last_sent >= 1.6

    def sent(self, now):
        self.last_sent = now
        self.block_until = now+1.5
        self.count += 1
        self.pending = True
        self.absent_since = None
