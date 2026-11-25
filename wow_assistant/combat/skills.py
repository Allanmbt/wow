"""技能定义、截图规划和图标匹配。"""

from ..geometry import region_on_screen
from ..paths import asset_path

TEMPLATE = asset_path("seal_ready.png")
E_SKILLS = {
    "strike": ("神圣打击", 10.0, asset_path("strike_ready.png")),
    "judgment": ("审判", 8.0, asset_path("judgment_ready.png")),
}
ATTACK_SKILLS = {"sanction": ("制裁", 0.0, asset_path("sanction_ready.png")), **E_SKILLS}
ATTACK_SKILLS["hammer"] = ("愤怒之锤", 0.0, asset_path("hammer_ready.png"))
OPTIONAL_ATTACK_SKILLS = frozenset(("sanction", "hammer"))
SANCTION_KEY = "R"
HAMMER_KEY = "F"
HAMMER_THRESHOLD = 20
HAMMER_UNAVAILABLE = asset_path("hammer_unavailable.png")


def validate_skill_regions(regions, *, include_hammer=True):
    names = dict(seal="圣印（第2格）", strike="神圣打击（第1格）", judgment="审判（第4格）",
                 sanction="制裁", hammer="愤怒之锤")
    items = [(key, rect) for key, rect in regions.items() if rect and (include_hammer or key != "hammer")]
    for i, (key, a) in enumerate(items):
        for other, b in items[i+1:]:
            width = max(0, min(a["left"]+a["width"], b["left"]+b["width"])-max(a["left"], b["left"]))
            height = max(0, min(a["top"]+a["height"], b["top"]+b["height"])-max(a["top"], b["top"]))
            area = min(a["width"]*a["height"], b["width"]*b["height"])
            if area > 0 and width*height/area > 0.55:
                raise ValueError(f"{names[key]}与{names[other]}框到了同一格，请分别重新定位")


def icon_image(frame):
    import cv2

    if frame is None or frame.size == 0 or min(frame.shape[:2]) < 8:
        raise ValueError("图标截图过小，请框选完整的单个技能图标")
    h, width = frame.shape[:2]
    # 排除边框和快捷键字样附近的外圈，保留中心的倒计时覆盖区域。
    frame = frame[max(1, h // 10):h - max(1, h // 10),
                  max(1, width // 10):width - max(1, width // 10)]
    return cv2.GaussianBlur(cv2.resize(frame, (48, 48)), (3, 3), 0)


def icon_similarity(frame, reference):
    return _normalized_icon_similarity(icon_image(frame), icon_image(reference))


def _normalized_icon_similarity(current, baseline):
    import numpy as np

    difference = current.astype(np.float32) - baseline.astype(np.float32)
    return 1.0 - float(np.abs(difference).mean()) / 255.0


def classify_icon(frame, reference, threshold=0.90):
    """三态：True 待用、False 圣印亮起、None 遮挡/眩晕变灰等未知。"""
    import cv2

    current, baseline = icon_image(frame), icon_image(reference)
    score = _normalized_icon_similarity(current, baseline)
    if score >= threshold:
        return True, score
    current = cv2.cvtColor(current, cv2.COLOR_BGR2HSV)
    baseline = cv2.cvtColor(baseline, cv2.COLOR_BGR2HSV)
    # 用户截图中圣印开启时底色明显变亮；灰色公共冷却/眩晕不应当作成功。
    bright = current[:, :, 2].mean() >= baseline[:, :, 2].mean() + 20
    colored = current[:, :, 1].mean() >= max(30, baseline[:, :, 1].mean() * 0.6)
    return (False if bright and colored else None), score


def classify_attack_icon(frame, reference):
    """待用模板严格校验中心纹理，避免倒计时最后一两秒被平均相似度漏掉。"""
    import cv2

    current, baseline = icon_image(frame), icon_image(reference)
    score = _normalized_icon_similarity(current, baseline)
    center = slice(12, 36)
    patch = cv2.cvtColor(current[center, center], cv2.COLOR_BGR2GRAY)
    target = cv2.cvtColor(baseline[center, center], cv2.COLOR_BGR2GRAY)
    if target.std() < 3 or patch.std() < 3:
        return None, score
    correlation = float(cv2.matchTemplate(patch, target, cv2.TM_CCOEFF_NORMED)[0, 0])
    if score >= 0.94 and correlation >= 0.97:
        return True, score
    hsv, ready_hsv = cv2.cvtColor(current, cv2.COLOR_BGR2HSV), cv2.cvtColor(baseline, cv2.COLOR_BGR2HSV)
    gray = hsv[:, :, 1].mean() < ready_hsv[:, :, 1].mean() * 0.65
    dark = hsv[:, :, 2].mean() < ready_hsv[:, :, 2].mean() * 1.15
    return (False if gray and dark else None), score


def infer_hammer_region(seal, judgment):
    """按截图布局推断圣印和审判之间的单格坐标。"""
    if not seal or not judgment:
        return None
    left = seal["left"] + seal["width"]
    width = judgment["left"] - left
    height = round((seal["height"] + judgment["height"]) / 2)
    if not (16 <= height <= 200 and .68*height <= width <= 1.40*height
            and abs(seal["top"] - judgment["top"]) <= .25*height):
        return None
    return dict(left=left, top=round((seal["top"]+judgment["top"]) / 2), width=width, height=height)


class HammerIcon:
    def __init__(self, ready=None):
        import cv2
        import numpy as np

        dark = cv2.imdecode(np.frombuffer(HAMMER_UNAVAILABLE.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        self.dark = cv2.cvtColor(icon_image(dark), cv2.COLOR_BGR2GRAY)[6:42, 6:42]
        self.detail = self.dark >= 22
        self.ready = ready

    def classify(self, frame):
        import cv2
        import numpy as np

        normalized = icon_image(frame)
        gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
        _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(gray, self.dark, cv2.TM_CCOEFF_NORMED))
        size = self.dark.shape[0]
        aligned = gray[y:y+size, x:x+size]
        delta = (aligned.astype(np.float32)-self.dark)[self.detail]
        if score >= .85 and float(delta.mean()) <= 10:
            return False, score
        if self.ready is not None:
            return classify_attack_icon(frame, self.ready)
        if score < .88:
            return None, score
        # 就绪时整体变亮，倒计时数字会改变图标中心纹理。
        core = slice(5, 31)
        core_score = float(cv2.matchTemplate(aligned[core, core], self.dark[core, core], cv2.TM_CCOEFF_NORMED)[0, 0])
        color = normalized[y:y+size, x:x+size].astype(np.int16)
        blue, green, red = cv2.split(color)
        cyan = ((blue - red >= 15) & (green - red >= 12)).mean()
        if core_score >= .92 and delta.mean() >= 14 and (delta >= 9).mean() >= .62 and cyan >= .035:
            return True, score
        return None, score


def _skill_capture_plan(regions, monitors):
    left = min(rect["left"] for rect in regions.values())
    top = min(rect["top"] for rect in regions.values())
    right = max(rect["left"] + rect["width"] for rect in regions.values())
    bottom = max(rect["top"] + rect["height"] for rect in regions.values())
    combined = dict(left=left, top=top, width=right-left, height=bottom-top)
    total_area = sum(rect["width"] * rect["height"] for rect in regions.values())
    # 相隔较远或跨显示器时，继续分别抓取，避免大面积无效像素。
    if combined["width"] * combined["height"] > 2 * total_area or not region_on_screen(combined, monitors):
        return [(rect, {key: (slice(None), slice(None))}) for key, rect in regions.items()]

    crops = {}
    for key, rect in regions.items():
        x, y = rect["left"] - left, rect["top"] - top
        crops[key] = (slice(y, y + rect["height"]), slice(x, x + rect["width"]))
    return [(combined, crops)]
