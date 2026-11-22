"""悬停指针匹配和原地鼠标交互。"""

import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
from pathlib import Path
import threading
import time

from screen_loot import LOOT_THRESHOLD, ScreenLootProbe

BAG_PATH = Path(__file__).resolve().parent / "bag.jpg"
LOOT_TEMPLATE = BAG_PATH.with_name("loot_cursor.png")
MINING_TEMPLATE = BAG_PATH.with_name("mining_cursor.png")
MINING_CLEAR_SECONDS = 0.35


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("hCursor", w.HANDLE), ("pt", w.POINT)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD),
                ("hbmMask", w.HBITMAP), ("hbmColor", w.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", w.LONG), ("bmWidth", w.LONG), ("bmHeight", w.LONG),
                ("bmWidthBytes", w.LONG), ("bmPlanes", w.WORD), ("bmBitsPixel", w.WORD),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
                ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("header", BITMAPINFOHEADER), ("colors", w.DWORD * 1)]


@dataclass
class CursorFrame:
    image: object
    mask: object
    position: tuple
    handle: int = 0


class CursorReader:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
        self.user.CopyIcon.argtypes, self.user.CopyIcon.restype = [w.HANDLE], w.HANDLE
        self.user.GetIconInfo.argtypes = [w.HANDLE, ctypes.POINTER(ICONINFO)]
        self.user.DestroyIcon.argtypes = [w.HANDLE]
        self.user.DrawIconEx.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, w.HANDLE,
                                         ctypes.c_int, ctypes.c_int, w.UINT, w.HBRUSH, w.UINT]
        self.gdi.GetObjectW.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p]
        self.gdi.CreateCompatibleDC.argtypes, self.gdi.CreateCompatibleDC.restype = [w.HDC], w.HDC
        self.gdi.CreateDIBSection.argtypes = [w.HDC, ctypes.POINTER(BITMAPINFO), w.UINT,
                                             ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
        self.gdi.CreateDIBSection.restype = w.HBITMAP
        self.gdi.SelectObject.argtypes, self.gdi.SelectObject.restype = [w.HDC, w.HANDLE], w.HANDLE
        self.gdi.DeleteObject.argtypes = [w.HANDLE]
        self.gdi.DeleteDC.argtypes = [w.HDC]

    def capture(self):
        import numpy as np

        cursor = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
        if not self.user.GetCursorInfo(ctypes.byref(cursor)):
            raise OSError(f"无法读取鼠标指针（错误 {ctypes.get_last_error()}）")
        if not cursor.flags & 1 or not cursor.hCursor:
            return None
        copied = self.user.CopyIcon(cursor.hCursor)
        if not copied:
            return None  # 游戏恰好切换指针，下一帧重读。
        info, bitmap = ICONINFO(), BITMAP()
        dc = dib = previous = None
        try:
            if not self.user.GetIconInfo(copied, ctypes.byref(info)):
                return None
            handle = info.hbmColor or info.hbmMask
            if not self.gdi.GetObjectW(handle, ctypes.sizeof(bitmap), ctypes.byref(bitmap)):
                return None
            width, height = bitmap.bmWidth, bitmap.bmHeight
            if not info.hbmColor:
                height //= 2
            if not (8 <= width <= 256 and 8 <= height <= 256):
                return None
            dc = self.gdi.CreateCompatibleDC(None)
            header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width,
                                      biHeight=-height, biPlanes=1, biBitCount=32)
            bits = ctypes.c_void_p()
            dib = self.gdi.CreateDIBSection(dc, ctypes.byref(BITMAPINFO(header=header)),
                                             0, ctypes.byref(bits), None, 0)
            if not dc or not dib or not bits.value:
                raise OSError("无法创建指针图像缓冲区")
            previous = self.gdi.SelectObject(dc, dib)
            pixels = np.ctypeslib.as_array((ctypes.c_ubyte * (width * height * 4)).from_address(bits.value))
            pixels = pixels.reshape(height, width, 4)
            # 黑、白底各绘制一次，恢复不透明区域；无需把鼠标截进游戏画面。
            rendered = []
            for background in (0, 255):
                pixels[:] = background
                if not self.user.DrawIconEx(dc, 0, 0, copied, 0, 0, 0, None, 3):
                    return None
                self.gdi.GdiFlush()
                rendered.append(pixels[:, :, :3].copy())
            difference = np.abs(rendered[1].astype(np.int16) - rendered[0].astype(np.int16)).max(axis=2)
            mask = (difference < 80).astype(np.uint8) * 255
            return CursorFrame(rendered[0], mask, (cursor.pt.x, cursor.pt.y), cursor.hCursor)
        finally:
            if previous and dc:
                self.gdi.SelectObject(dc, previous)
            if dib:
                self.gdi.DeleteObject(dib)
            if dc:
                self.gdi.DeleteDC(dc)
            for handle in (info.hbmMask, info.hbmColor):
                if handle:
                    self.gdi.DeleteObject(handle)
            self.user.DestroyIcon(copied)


class CursorMatcher:
    def __init__(self, reference):
        import cv2
        import numpy as np

        if (reference is None or reference.ndim != 3 or reference.shape[2] not in (3, 4)
                or min(reference.shape[:2]) < 12):
            raise ValueError("袋子图片无效或过小")
        self.last_signature, self.last_score = None, 0.0
        if reference.shape[2] == 3:
            # JPG 含截图背景：用当前指针的不透明区域在样本中匹配。
            self.reference = reference.copy()
            self.gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
            if self.gray.std() < 5:
                raise ValueError("袋子图片缺少可识别纹理")
            self.mask = None
            return
        mask = self.clean_mask((reference[:, :, 3] >= 176).astype(np.uint8) * 255)
        ys, xs = np.nonzero(mask)
        if len(xs) < 40:
            raise ValueError("袋子图片无效：不透明图形过小")
        crop = np.s_[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        # DrawIconEx 绘制在黑底上，半透明边缘按同样方式合成。
        rgb = reference[:, :, :3].astype(np.float32)
        self.reference = (rgb * (reference[:, :, 3:4] / 255.0)).astype(np.uint8)[crop]
        self.mask = mask[crop]
        self.gray = cv2.cvtColor(self.reference, cv2.COLOR_BGR2GRAY)
        self.last_signature, self.last_score = None, 0.0

    @staticmethod
    def clean_mask(mask):
        import cv2
        import numpy as np

        # 去掉抠图留下的孤立杂点，保留袋子与箭头等独立部件。
        count, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8))
        keep = np.zeros(count, np.uint8)
        keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= 8) * 255
        return keep[labels]

    @staticmethod
    def signature(frame):
        import zlib

        return (frame.image.shape, zlib.crc32(frame.image.tobytes()), zlib.crc32(frame.mask.tobytes()))

    def score(self, frame):
        if frame is None:
            return 0.0
        signature = self.signature(frame)
        if signature == self.last_signature:
            return self.last_score
        self.last_signature = signature
        self.last_score = self._match(frame)
        return self.last_score

    def _match(self, frame):
        import cv2
        import numpy as np

        if self.mask is None:
            return self._match_jpg(frame)
        cursor_mask = self.clean_mask(frame.mask)
        ys, xs = np.nonzero(cursor_mask)
        if len(xs) < 40:
            return 0.0
        image = frame.image[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        mask = cursor_mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        if min(image.shape[:2]) < 8:
            return 0.0
        saturation = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[:, :, 1]
        if saturation[mask > 0].mean() < 12:
            return 0.0  # 普通黑白箭头、文本插入光标不是彩色拾取袋子。
        height, width = self.gray.shape
        scale_x, scale_y = width / image.shape[1], height / image.shape[0]
        if not (0.2 <= scale_x <= 8.0 and 0.2 <= scale_y <= 8.0
                and 0.7 <= scale_x / scale_y <= 1.4):
            return 0.0
        # 透明外圈裁掉后按图形尺寸归一化，只比较双方不透明的部分。
        # 无需在背景中遍历 41 个比例，也不会把透明像素的 RGB 当作图案。
        # 用较小一方的分辨率比较，避免把 24/32 像素原生指针放大后与
        # 高清截图逐像素比较，插值细节损失会导致明明是袋子却始终低于阈值。
        width, height = min(width, image.shape[1]), min(height, image.shape[0])
        needle = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
        reference = cv2.resize(self.reference, (width, height), interpolation=cv2.INTER_AREA)
        valid = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST) > 0
        target = cv2.resize(self.mask, (width, height), interpolation=cv2.INTER_NEAREST) > 0
        common = valid & target
        if np.count_nonzero(common) / np.count_nonzero(valid | target) < 0.70:
            return 0.0
        gray = cv2.cvtColor(needle, cv2.COLOR_BGR2GRAY)
        baseline = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
        a, b = gray[common].astype(np.float32), baseline[common].astype(np.float32)
        if a.std() < 5 or b.std() < 5:
            return 0.0
        color_error = np.abs(reference.astype(np.float32) - needle.astype(np.float32))[common].mean()
        if color_error > 45:
            return 0.0
        score = float(np.corrcoef(a, b)[0, 1])
        return max(0.0, min(1.0, score))


    def _match_jpg(self, frame):
        import cv2
        import numpy as np

        ys, xs = np.nonzero(frame.mask)
        if len(xs) < 40:
            return 0.0
        image = frame.image[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        mask = frame.mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        if min(image.shape[:2]) < 8:
            return 0.0
        saturation = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[:, :, 1]
        if saturation[mask > 0].mean() < 12:
            return 0.0  # 普通黑白箭头、文本插入光标不是彩色拾取袋子。
        best = 0.0
        for factor in np.arange(0.50, 2.51, 0.05):
            width, height = round(image.shape[1] * factor), round(image.shape[0] * factor)
            if width > self.gray.shape[1] or height > self.gray.shape[0] or min(width, height) < 8:
                continue
            needle = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
            valid = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
            gray = cv2.cvtColor(needle, cv2.COLOR_BGR2GRAY)
            if np.count_nonzero(valid) < 30 or gray[valid > 0].std() < 5:
                continue
            scores = cv2.matchTemplate(self.gray, gray, cv2.TM_CCOEFF_NORMED, mask=valid)
            scores = np.nan_to_num(scores, nan=-1, posinf=-1, neginf=-1)
            _, score, _, (x, y) = cv2.minMaxLoc(scores)
            if score <= best:
                continue
            patch = self.reference[y:y + height, x:x + width]
            color_error = np.abs(patch.astype(np.float32) - needle.astype(np.float32))[valid > 0].mean()
            if color_error <= 45:
                best = float(score)
        return min(1.0, best)


class HoverLatch:
    """悬停稳定后点一次。图标消失或指针明显移动后允许再次触发。"""

    def __init__(self):
        self.since = self.absent = self.position = self.click_position = None
        self.latched = False
        self.last_click = -float("inf")

    def observe(self, now, matched, position):
        if not matched:
            self.since = self.position = None
            if self.absent is None:
                self.absent = now
            if now - self.absent >= 0.4:
                self.latched = False
            return False
        self.absent = None
        if self.latched and self.click_position and max(abs(a-b) for a, b in zip(position, self.click_position)) > 24:
            self.latched = False
        if self.position is None or max(abs(a - b) for a, b in zip(position, self.position)) > 4:
            self.since, self.position = now, position
        return not self.latched and now - self.since >= 0.15 and now - self.last_click >= 1.0

    def clicked(self, now):
        self.latched, self.last_click = True, now
        self.click_position = self.position

    def label(self, now):
        return "已点一次 · 等待新的悬停" if self.latched else "等待拾取指针稳定出现"


class MiningLatch:
    def __init__(self):
        self.since = self.position = None
        self.next_click = -float("inf")

    def observe(self, now, matched, position):
        if not matched:
            self.since = self.position = None
            return False
        if self.position is None or max(abs(a-b) for a, b in zip(position, self.position)) > 4:
            self.position, self.since = position, now
        return now >= self.next_click and now - self.since >= 0.15

    def clicked(self, now):
        self.next_click = now + 4.0

    def label(self, now):
        if self.position is not None and now < self.next_click:
            return f"采矿指针仍在 · {self.next_click - now:.1f} 秒后复点"
        return "等待采矿指针稳定出现"


class LootMonitor:
    label = "自动拾取"
    event = "loot"
    latch_type = HoverLatch

    def __init__(self, native, hwnd, events, path=LOOT_TEMPLATE):
        import cv2
        import numpy as np

        self.native, self.hwnd, self.events = native, hwnd, events
        self.screen_reference = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
        self.matcher = CursorMatcher(self.screen_reference)
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="CursorLoot")
        self.thread.start()

    @property
    def running(self):
        return self.thread.is_alive() and not self.cancel.is_set()

    def stop(self):
        self.cancel.set()
        self.thread.join(timeout=1.0)

    def on_cursor_sample(self, now, matched):
        """Receive a positive, negative, or unavailable cursor sample."""

    def run(self):
        screen = None
        try:
            native_note, screen_note = "", ""
            try:
                reader = CursorReader()
            except OSError as exc:
                reader, native_note = None, str(exc)
            reference = getattr(self, "screen_reference", None)
            if reference is not None:
                try:
                    screen = ScreenLootProbe(self.native, self.hwnd, reference)
                except Exception as exc:
                    screen_note = f"画面备用识别不可用：{exc}"
                    self.events.put(("log", screen_note))
            latch = self.latch_type()
            last_update = 0
            screen_retry_at = 0.0
            click_status = ""
            while not self.cancel.is_set():
                if self.native.foreground() != self.hwnd:
                    latch.since = latch.position = None
                    self.on_cursor_sample(time.monotonic(), None)
                    if time.monotonic() - last_update >= 0.7:
                        self.events.put((self.event, (self, f"{self.label} · 等待回到绑定窗口")))
                        last_update = time.monotonic()
                    self.cancel.wait(0.1)
                    continue
                observed = False
                try:
                    frame = reader.capture() if reader else None
                    observed = reader is not None
                except OSError as exc:
                    frame, native_note = None, str(exc)
                score = self.matcher.score(frame)
                native_score = score
                hit = None
                if score < LOOT_THRESHOLD and screen is not None:
                    observed = False
                    if time.monotonic() >= screen_retry_at:
                        try:
                            hit = screen.find()
                            observed = True
                        except Exception as exc:
                            screen_note = f"画面抓取失败，稍后重试 · {exc}"
                            self.events.put(("log", screen_note))
                            screen.close()
                            screen_retry_at = time.monotonic() + 0.5
                        if hit is not None:
                            score = hit.score
                position = hit.position if hit else (frame.position if frame else (0, 0))
                now = time.monotonic()
                matched = score >= LOOT_THRESHOLD
                self.on_cursor_sample(now, matched if observed else None)
                if latch.observe(now, matched, position):
                    if hit is not None:
                        # 截图和图像运算放在键鼠锁外，避免拖延 Q/E。
                        try:
                            verified = screen.verify(hit)
                        except Exception as exc:
                            verified = False
                            screen_note = f"画面复核失败，稍后重试 · {exc}"
                            self.events.put(("log", screen_note))
                            screen.close()
                            screen_retry_at = time.monotonic() + 0.5
                        verified_at = time.monotonic()
                        def recheck():
                            return (verified and time.monotonic() - verified_at <= 0.15
                                    and screen.same_position(position))
                    else:
                        matched_signature = self.matcher.signature(frame)
                        def recheck():
                            current = reader.capture()
                            return (current is not None
                                    and (self.matcher.signature(current) == matched_signature
                                         or bool(frame.handle and frame.handle == current.handle))
                                    and max(abs(a - b) for a, b in zip(current.position, position)) <= 4)
                    if self.native.right_click(self.hwnd, self.cancel, recheck):
                        latch.clicked(time.monotonic())
                        click_status = ""
                        source = "画面" if hit else "系统指针"
                        backend = getattr(self.native, "mouse_name", "")
                        output = f" · {backend}" if backend else ""
                        self.events.put(("log", f"{self.label} · {source}已匹配，在当前位置右键一次{output}"))
                    else:
                        reason = getattr(self.native, "mouse_block_reason", "") or "指针、鼠标位置或窗口已变化"
                        click_status = f" | 右键暂缓 · {reason}"
                        latch.since = latch.position = None
                elif score < LOOT_THRESHOLD:
                    click_status = ""
                if now - last_update >= 0.7:
                    label = latch.label(now)
                    route = (f"画面命中 {hit.score:.1%}" if hit else
                             ("系统指针已匹配" if native_score >= LOOT_THRESHOLD else
                              (screen_note if now < screen_retry_at or not screen else "画面备用扫描中")))
                    if frame is None:
                        route = f"系统指针不可见 {native_note}；{route}"
                    self.events.put((self.event, (self, f"{self.label} · {label} | 系统指针 {native_score:.1%} | {route}{click_status}")))
                    last_update = now
                self.cancel.wait(0.1)
        except Exception as exc:
            self.cancel.set()
            self.events.put((self.event + "_error", (self, f"{self.label}已停止 · {exc}")))
        finally:
            if screen is not None:
                screen.close()


class MiningMonitor(LootMonitor):
    label = "自动采矿"
    event = "mining"
    latch_type = MiningLatch

    def __init__(self, native, hwnd, events, path=MINING_TEMPLATE):
        self._attack_pause = threading.Event()
        self._missing_since = None
        super().__init__(native, hwnd, events, path)

    @property
    def attack_paused(self):
        return self._attack_pause.is_set() and not self.cancel.is_set()

    def on_cursor_sample(self, now, matched):
        if matched is not False:
            self._missing_since = None
            if matched:
                self._attack_pause.set()
        elif self._attack_pause.is_set():
            if self._missing_since is None:
                self._missing_since = now
            elif now - self._missing_since >= MINING_CLEAR_SECONDS:
                self._attack_pause.clear()
