"""屏幕区域验证和 Windows DPI 设置。"""

import ctypes


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
