"""自动扫描窗口中上部的红字错误；不再判断血条、经验或拾取。"""
import queue
import re
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

ERROR_TEMPLATE = Path(__file__).resolve().parent / "target_error.png"
STOP_PHRASES = ("现在没有可以攻击的目标", "现在没有可攻击的目标", "目标已死亡", "你不能攻击那个目标")
_reader = None
_reader_lock = threading.Lock()


def stop_reason(text):
    text = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for phrase in STOP_PHRASES:
        if phrase in text:
            return phrase
    if "目标" in text and ("已死" in text or "已经死亡" in text):
        return "目标已死亡"
    if "没有" in text and "攻击" in text and "目标" in text:
        return "没有可攻击的目标"
    if 5 <= len(text) <= 24 and "目" in text and ("攻击" in text or "死亡" in text):
        for phrase in STOP_PHRASES:
            if SequenceMatcher(None, text, phrase).ratio() >= 0.80:
                return phrase
    return None


def error_region(bounds):
    """相对游戏客户区自动定位，不依赖桌面绝对坐标。"""
    return dict(left=bounds["left"] + round(bounds["width"] * 0.15),
                top=bounds["top"] + round(bounds["height"] * 0.04),
                width=round(bounds["width"] * 0.70), height=round(bounds["height"] * 0.40))


def red_mask(frame):
    import cv2
    import numpy as np
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    return (((hsv[:, :, 0] < 12) | (hsv[:, :, 0] > 172))
            & (hsv[:, :, 1] > 110) & (hsv[:, :, 2] > 80)).astype(np.uint8) * 255


def text_candidates(frame):
    import cv2
    import numpy as np
    mask = red_mask(frame)
    grouped = cv2.dilate(mask, np.ones((3, 31), dtype=np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(grouped)
    result = []
    for x, y, width, height, area in stats[1:count]:
        if width < 65 or not 10 <= height <= 100 or width / height < 3:
            continue
        crop = mask[y:y + height, x:x + width]
        density = cv2.countNonZero(crop) / crop.size
        if not 0.035 <= density <= 0.55:
            continue
        ys, xs = np.nonzero(crop)
        result.append(crop[ys.min():ys.max() + 1, xs.min():xs.max() + 1])
    return result


def template_score(mask, reference):
    import cv2
    if mask.size == 0 or reference.size == 0 or mask.std() < 1 or reference.std() < 1:
        return 0.0
    if abs(mask.shape[1] / mask.shape[0] - reference.shape[1] / reference.shape[0]) > 2.0:
        return 0.0
    a = cv2.resize(mask, (480, 32), interpolation=cv2.INTER_AREA)
    b = cv2.resize(reference, (480, 32), interpolation=cv2.INTER_AREA)
    a = cv2.GaussianBlur(a, (3, 3), 0)
    b = cv2.GaussianBlur(b, (3, 3), 0)
    a = cv2.copyMakeBorder(a, 2, 2, 3, 3, cv2.BORDER_CONSTANT, value=0)
    return float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED).max())


def ocr_image(masks):
    import cv2
    import numpy as np
    masks = masks[:4]
    width = max(item.shape[1] for item in masks) + 16
    height = sum(item.shape[0] + 16 for item in masks)
    canvas = np.full((height, width), 255, dtype=np.uint8)
    y = 8
    for mask in masks:
        canvas[y:y + mask.shape[0], 8:8 + mask.shape[1]] = 255 - mask
        y += mask.shape[0] + 16
    # 这类细红字二倍放大反而会丢笔画；保留原尺寸，极小字才放大。
    if max(item.shape[0] for item in masks) < 14:
        return cv2.resize(canvas, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return canvas


class ErrorMonitor:
    """快速模板约每 0.1 秒检测；OCR 独立工作，不阻塞 Q/E。"""

    def __init__(self, native, hwnd, cancel, events):
        self.native, self.hwnd, self.cancel, self.events = native, hwnd, cancel, events
        self.reason = None
        self.error = None
        self.frames = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.ocr_thread = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="RedText")
        self.thread.start()

    def halt(self, reason):
        if self.cancel.is_set() or self.native.foreground() != self.hwnd:
            return
        with self.lock:
            if self.reason is None:
                self.reason = reason
                self.cancel.set()
                self.events.put(("finished", f"检测到红字“{reason}”，本轮已停止；鼠标上侧键重新开启"))

    def ocr_loop(self):
        global _reader
        try:
            if self.cancel.is_set():
                return
            import cv2
            import easyocr
            import torch
            self.events.put(("monitor", "红字模板检测已运行，OCR 正在准备；不影响 Q/E"))
            with _reader_lock:
                if _reader is None:
                    torch.set_num_threads(2)
                    _reader = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
                reader = _reader
            if self.cancel.is_set():
                return
            self.events.put(("monitor", "红字自动停攻已就绪（自动扫描窗口中上部）"))
            while not self.cancel.is_set():
                try:
                    captured_at, masks = self.frames.get(timeout=0.2)
                except queue.Empty:
                    continue
                if time.monotonic() - captured_at > 1.5:
                    continue
                image = ocr_image(masks)
                with _reader_lock:
                    result = reader.readtext(image, detail=1, paragraph=False, workers=0,
                                             min_size=5, decoder="beamsearch")
                text = "".join(str(text) for _, text, confidence in result if confidence >= 0.25)
                reason = stop_reason(text)
                if reason and time.monotonic() - captured_at <= 2.0:
                    self.halt(reason)
        except Exception as exc:
            if not self.cancel.is_set():
                self.events.put(("monitor", f"红字 OCR 不可用，仅保留已知提示模板：{exc}"))

    def run(self):
        try:
            import cv2
            import mss
            import numpy as np
            reference = None
            if ERROR_TEMPLATE.exists():
                image = cv2.imdecode(np.frombuffer(ERROR_TEMPLATE.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
                candidates = text_candidates(image) if image is not None else []
                if candidates:
                    reference = max(candidates, key=lambda item: item.shape[1])
            self.ocr_thread = threading.Thread(target=self.ocr_loop, daemon=True, name="RedTextOCR")
            self.ocr_thread.start()
            last_ocr = -float("inf")
            template_hits = 0
            with mss.MSS() as capture:
                while not self.cancel.is_set():
                    if self.native.foreground() != self.hwnd:
                        return
                    frame = cv2.cvtColor(np.array(capture.grab(error_region(self.native.client_bounds(self.hwnd)))),
                                         cv2.COLOR_BGRA2BGR)
                    candidates = text_candidates(frame)
                    matched = reference is not None and any(template_score(c, reference) >= 0.88 for c in candidates)
                    template_hits = template_hits + 1 if matched else 0
                    if template_hits >= 2:
                        self.halt(STOP_PHRASES[0])
                        return
                    now = time.monotonic()
                    if candidates and now - last_ocr >= 0.4:
                        try:
                            self.frames.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self.frames.put_nowait((now, candidates[:4]))
                        except queue.Full:
                            pass
                        last_ocr = now
                    self.cancel.wait(0.1)
        except Exception as exc:
            self.error = str(exc)
            self.events.put(("monitor", f"红字监测失败，Q/E 继续；请用侧键/F10 控制：{exc}"))
