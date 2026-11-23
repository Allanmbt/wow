"""全屏截图框选对话框。"""

import base64

from ..geometry import region_on_screen


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
