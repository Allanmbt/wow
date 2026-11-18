"""保留怪物名称 OCR；默认进入自动战斗，--ocr 单独进入名称识别。"""

from __future__ import annotations

import base64
import ctypes
import json
import math
import queue
import sys
import threading
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DEFAULTS = {
    "region": None,
    "target": "腐皮食尸鬼",
    "fuzzy_threshold": 0.8,
    "min_confidence": 0.25,
    "scale": 3.0,
}


def normalize(text):
    """保留中英文和数字，忽略空格、标点及英文大小写。"""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in text if c.isalnum())


def match_target(text, target, threshold=0.8):
    """精确包含优先；模糊匹配比较目标长度附近的局部片段。"""
    text, target = normalize(text), normalize(target)
    if not text or not target:
        return False, 0.0
    if target in text:
        return True, 1.0
    # 1～2 字关键词只做精确匹配，避免单字误报。
    if len(target) < 3:
        return False, 0.0
    best = 0.0
    for length in range(max(1, len(target) - 1), min(len(text), len(target) + 1) + 1):
        for start in range(len(text) - length + 1):
            score = SequenceMatcher(None, target, text[start:start + length],
                                    autojunk=False).ratio()
            best = max(best, score)
    return best >= threshold, best


def valid_region(region):
    return (isinstance(region, dict)
            and all(type(region.get(k)) is int for k in ("left", "top", "width", "height"))
            and 4 <= region["width"] <= 16384
            and 4 <= region["height"] <= 16384)


def region_on_screen(region, monitors):
    """矩形必须完整落在一块显示器内，拒绝显示器间空隙及过期坐标。"""
    return valid_region(region) and any(
        region["left"] >= m["left"] and region["top"] >= m["top"]
        and region["left"] + region["width"] <= m["left"] + m["width"]
        and region["top"] + region["height"] <= m["top"] + m["height"]
        for m in monitors
    )


def load_config(path=CONFIG_PATH):
    config = DEFAULTS.copy()
    if not path.exists():
        return config, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("配置必须为 JSON 对象")
        if data.get("region") is not None and not valid_region(data["region"]):
            raise ValueError("区域坐标无效")
        config["region"] = data.get("region")
        if not isinstance(data.get("target", config["target"]), str):
            raise ValueError("目标名称必须为文本")
        config["target"] = data.get("target", config["target"]).strip()
        for key, low, high in (("fuzzy_threshold", 0.5, 1.0),
                               ("min_confidence", 0.0, 1.0), ("scale", 1.0, 5.0)):
            value = data.get(key, config[key])
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} 必须是数字")
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} 应在 {low}～{high} 之间")
            config[key] = float(value)
        return config, ""
    except (OSError, ValueError, TypeError) as exc:
        return DEFAULTS.copy(), f"配置加载失败，使用默认值（原文件暂不修改）：{exc}"


def save_config(config, path=CONFIG_PATH):
    # 同目录临时文件替换，避免中途退出留下半个 JSON。
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def prepare_image(frame, scale):
    import cv2

    # 保留彩色名字的信息，不强制二值化；低对比度交给 EasyOCR 再处理。
    return cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def decode_results(results, min_confidence):
    """保留每个文本框，按行排序；不把不同的行拼成一个怪物名。"""
    ordered = sorted(results, key=lambda item: (min(p[1] for p in item[0]),
                                               min(p[0] for p in item[0])))
    accepted = [str(text).strip() for _, text, confidence in ordered
                if float(confidence) >= min_confidence and normalize(str(text))]
    debug = "；".join(f"{text} ({float(confidence):.2f})"
                     for _, text, confidence in ordered) or "无文本框"
    return accepted, debug


class OCRWorker:
    """单线程顺序截图/识别，不积压任务；代号用于丢弃暂停前的结果。"""

    def __init__(self):
        self.condition = threading.Condition()
        self.events = queue.Queue()
        self.running = False
        self.closed = False
        self.generation = 0
        self.config = None
        self.latest = None
        threading.Thread(target=self._loop, daemon=True, name="OCR").start()

    def start(self, config):
        with self.condition:
            self.config = dict(config, region=dict(config["region"]))
            self.generation += 1
            self.running = True
            self.condition.notify_all()

    def pause(self):
        with self.condition:
            self.running = False
            self.generation += 1
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def emit(self, generation, kind, payload):
        with self.condition:
            if not self.closed and self.running and generation == self.generation:
                self.events.put((generation, kind, payload))

    def _loop(self):
        reader = None
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.running)
                if self.closed:
                    return
                generation, config = self.generation, self.config
            started = time.monotonic()
            try:
                if reader is None:
                    self.emit(generation, "status", "正在加载 OCR 模型，首次运行需联网下载……")
                    import easyocr
                    import torch

                    # 限制 CPU 并行度，减少对游戏的争抢。
                    torch.set_num_threads(2)
                    reader = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
                with self.condition:
                    if generation != self.generation or not self.running:
                        continue
                # 初始化不计入采样周期，避免模型加载后连续截取两帧。
                started = time.monotonic()
                import cv2
                import mss
                import numpy as np

                # MSS 对象在使用它的线程内创建，不跨线程共享截图句柄。
                with mss.MSS() as capture:
                    if not region_on_screen(config["region"], capture.monitors[1:]):
                        raise ValueError("识别区域不在当前显示器内，请重新框选")
                    frame = cv2.cvtColor(np.array(capture.grab(config["region"])),
                                         cv2.COLOR_BGRA2BGR)
                image = prepare_image(frame, config["scale"])
                captured_at = datetime.now().strftime("%H:%M:%S")
                with self.condition:
                    if generation == self.generation and self.running:
                        self.latest = (frame, image, captured_at)
                results = reader.readtext(image, detail=1, paragraph=False, workers=0,
                                          min_size=5, batch_size=1, width_ths=1.0)
                names, debug = decode_results(results, config["min_confidence"])
                matches = [match_target(name, config["target"], config["fuzzy_threshold"])
                           for name in names]
                best = max((score for matched, score in matches if matched), default=0.0)
                self.emit(generation, "result", (names, debug, best, captured_at,
                                                 time.monotonic() - started))
            except Exception as exc:
                # 单次截图/OCR 异常可恢复；模型初始化失败需用户点击继续重试。
                self.emit(generation, "fatal" if reader is None else "error",
                          f"{type(exc).__name__}: {exc}")
            with self.condition:
                self.condition.wait_for(
                    lambda: self.closed or not self.running or generation != self.generation,
                    timeout=max(0.05, 1.0 - (time.monotonic() - started)))


def enable_dpi_awareness():
    # 必须在创建 Tk 窗口及导入截图库之前设置，坐标使用物理像素。
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def select_screen_region(root, on_selected, on_closed, include_frame=False):
    import tkinter as tk
    from tkinter import messagebox

    selector = None
    try:
        import cv2
        import mss
        import numpy as np

        with mss.MSS() as capture:
            desktop = dict(capture.monitors[0])
            monitors = [dict(m) for m in capture.monitors[1:]]
            frame = cv2.cvtColor(np.array(capture.grab(desktop)), cv2.COLOR_BGRA2BGR)
        # 冻结桌面等比缩放展示；坐标按实际显示尺寸反算，兼容负坐标副屏。
        ratio = min(1, root.winfo_screenwidth() * 0.9 / desktop["width"],
                    root.winfo_screenheight() * 0.8 / desktop["height"])
        width, height = max(1, round(desktop["width"] * ratio)), max(1, round(desktop["height"] * ratio))
        preview = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".png", preview)
        if not ok:
            raise RuntimeError("截图预览编码失败")
        selector = tk.Toplevel(root)
        selector.title("拖动框选所需区域；Esc 取消（只选一块显示器内的小区域）")
        selector.resizable(False, False)
        photo = tk.PhotoImage(data=base64.b64encode(encoded.tobytes()))
        canvas = tk.Canvas(selector, width=width, height=height, highlightthickness=0)
        canvas.pack()
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas.photo = photo
        state = {}

        def finish():
            selector.destroy()
            root.deiconify()
            on_closed()

        def point(event):
            return max(0, min(width, event.x)), max(0, min(height, event.y))

        def press(event):
            state["start"] = point(event)
            canvas.delete("selection")

        def drag(event):
            if "start" in state:
                canvas.delete("selection")
                canvas.create_rectangle(*state["start"], *point(event), outline="#00ff00",
                                        width=2, tags="selection")

        def release(event):
            if "start" not in state:
                return
            x0, y0 = state.pop("start")
            x1, y1 = point(event)
            left, right = sorted((round(x0 * desktop["width"] / width),
                                  round(x1 * desktop["width"] / width)))
            top, bottom = sorted((round(y0 * desktop["height"] / height),
                                  round(y1 * desktop["height"] / height)))
            region = {"left": desktop["left"] + left, "top": desktop["top"] + top,
                      "width": right - left, "height": bottom - top}
            if not region_on_screen(region, monitors):
                selector.title("区域过小或跨越显示器，请重新拖动框选；Esc 取消")
                return
            finish()
            if include_frame:
                on_selected(region, frame[top:bottom, left:right].copy())
            else:
                on_selected(region)

        canvas.bind("<ButtonPress-1>", press)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", release)
        selector.bind("<Escape>", lambda event: finish())
        selector.protocol("WM_DELETE_WINDOW", finish)
        selector.lift()
        selector.focus_force()
    except Exception as exc:
        if selector is not None:
            selector.destroy()
        root.deiconify()
        on_closed()
        messagebox.showerror("无法框选", str(exc), parent=root)


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.config, warning = load_config()
        self.worker = OCRWorker()
        self.last_log = None
        self.selecting = False
        root.title("怪物名称识别")
        root.geometry("650x350")
        root.protocol("WM_DELETE_WINDOW", self.close)
        panel = ttk.Frame(root, padding=16)
        panel.pack(fill="both", expand=True)
        self.target = tk.StringVar(value=self.config["target"])
        self.status = tk.StringVar(value="就绪，请选择区域后启动")
        self.region_text = tk.StringVar()
        self.result = tk.StringVar(value="未识别")
        self.debug = tk.StringVar(value="OCR 原文与置信度将在这里显示")
        ttk.Label(panel, text="目标怪物（留空仅识别）：").pack(anchor="w")
        entry = ttk.Frame(panel)
        entry.pack(fill="x", pady=4)
        ttk.Entry(entry, textvariable=self.target).pack(side="left", fill="x", expand=True)
        ttk.Button(entry, text="应用目标", command=self.apply_target).pack(side="left", padx=6)
        ttk.Label(panel, textvariable=self.region_text).pack(anchor="w", pady=6)
        buttons = ttk.Frame(panel)
        buttons.pack(fill="x")
        for text, command in (("选择区域", self.select_region), ("启动 / 继续", self.start),
                              ("暂停", self.pause), ("保存截图", self.save_screenshot),
                              ("退出", self.close)):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=2)
        ttk.Label(panel, textvariable=self.result, font=("Microsoft YaHei", 14),
                  wraplength=610).pack(anchor="w", pady=16)
        ttk.Label(panel, textvariable=self.status, wraplength=610).pack(anchor="w")
        ttk.Label(panel, textvariable=self.debug, wraplength=610).pack(anchor="w", pady=10)
        self.update_region_label()
        if warning:
            self.log(warning)
            self.status.set(warning)
        root.after(100, self.poll)
        if not self.config["region"]:
            root.after(400, self.select_region)

    def log(self, message):
        print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)

    def persist(self):
        try:
            save_config(self.config)
        except OSError as exc:
            self.status.set(f"配置保存失败，本次仍可使用：{exc}")
            self.log(self.status.get())

    def update_region_label(self):
        r = self.config["region"]
        self.region_text.set("区域：尚未选择" if not r else
                             f"区域：({r['left']}, {r['top']})  {r['width']} × {r['height']} 像素")

    def apply_target(self):
        self.config["target"] = self.target.get().strip()
        self.persist()
        if self.worker.running:
            self.worker.start(self.config)
        self.last_log = None
        self.result.set("等待识别")

    def start(self):
        if self.selecting:
            return
        if not self.config["region"]:
            self.select_region()
            return
        self.config["target"] = self.target.get().strip()
        self.persist()
        self.last_log = None
        self.worker.start(self.config)
        self.result.set("等待识别")
        self.status.set("运行中，可最小化此窗口后返回游戏")
        self.log("开始识别")

    def pause(self):
        self.worker.pause()
        self.status.set("已暂停")

    def select_region(self):
        if self.selecting:
            return
        self.selecting = True
        self.pause()
        self.root.withdraw()
        # 等控制窗口离开桌面后再截屏，避免把窗口截进去。
        self.root.after(250, self.show_selector)

    def show_selector(self):
        def selected(region):
            self.config["region"] = region
            with self.worker.condition:
                self.worker.latest = None
            self.result.set("未识别")
            self.debug.set("等待新的识别结果")
            self.update_region_label()
            self.persist()

        def closed():
            self.selecting = False
            self.status.set("已暂停，点击启动 / 继续开始识别")

        select_screen_region(self.root, selected, closed)

    def save_screenshot(self):
        try:
            import cv2

            with self.worker.condition:
                latest = self.worker.latest
            if latest is None:
                self.status.set("尚无截图，请先启动识别")
                return
            frame, processed, timestamp = latest
            for name, image in (("last_capture.png", frame), ("last_processed.png", processed)):
                ok, encoded = cv2.imencode(".png", image)
                if not ok:
                    raise RuntimeError("PNG 编码失败")
                # 用 Python 写入，支持中文目录名。
                (BASE_DIR / name).write_bytes(encoded.tobytes())
            self.status.set(f"已保存 {timestamp} 的原图和放大图到程序目录")
            self.log(self.status.get())
        except Exception as exc:
            self.status.set(f"截图保存失败：{exc}")

    def poll(self):
        try:
            while True:
                generation, kind, payload = self.worker.events.get_nowait()
                if generation != self.worker.generation:
                    continue
                if kind == "result":
                    names, debug, score, timestamp, elapsed = payload
                    text = " / ".join(names) or "未识别"
                    message = f"发现目标：{text}（相似度 {score:.0%}）" if score else text
                    self.result.set(message)
                    self.debug.set(f"OCR：{debug}")
                    self.status.set(f"运行中 | 截图时间 {timestamp} | 本轮耗时 {elapsed:.2f} 秒")
                    key = (tuple(names), bool(score))
                    if key != self.last_log:
                        self.log(message)
                        self.last_log = key
                elif kind in ("error", "fatal"):
                    self.status.set(f"识别失败：{payload}")
                    self.result.set("识别失败")
                    self.debug.set("本轮无有效结果")
                    key = ("error", payload)
                    if self.last_log != key:
                        self.log(self.status.get())
                        self.last_log = key
                    if kind == "fatal":
                        self.worker.pause()
                        self.status.set(f"模型加载失败，修复网络或依赖后点击继续：{payload}")
                else:
                    self.status.set(payload)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self):
        self.worker.close()
        self.root.destroy()


def main():
    if "--ocr" not in sys.argv:
        from auto_combat import main as combat_main
        return combat_main()
    if sys.platform != "win32":
        print("此工具面向 Windows 10/11，请在 Windows 上运行。")
        return 1
    enable_dpi_awareness()
    import tkinter as tk

    root = tk.Tk()
    App(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
