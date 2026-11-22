"""只读诊断鼠标 5 和 F10、前台窗口、战斗配置和图标。"""

import argparse
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import queue
import sys
import time

from auto_combat import (BASE, CONFIG, TEMPLATE, ATTACK_SKILLS, Hotkeys, WindowsInput,
                         classify_icon, classify_attack_icon, validate_skill_regions)
from monster_ocr import enable_dpi_awareness, region_on_screen


class DiagnosticInput(WindowsInput):
    def send(self, *args, **kwargs):
        raise AssertionError("诊断模式禁止发送键盘输入")

    def right_click(self, *args, **kwargs):
        raise AssertionError("诊断模式禁止发送鼠标输入")


class WindowInspector:
    def __init__(self, native):
        self.native = native
        api = native.api
        api.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        api.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]

    def describe(self, hwnd):
        if not hwnd:
            return dict(hwnd=0, title="无前台窗口", pid=0, process="", own=False)
        pid, title, kind = w.DWORD(), ctypes.create_unicode_buffer(512), ctypes.create_unicode_buffer(256)
        self.native.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        self.native.api.GetWindowTextW(hwnd, title, len(title))
        self.native.api.GetClassNameW(hwnd, kind, len(kind))
        process = "路径不可读"
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        if handle:
            try:
                buffer, size = ctypes.create_unicode_buffer(2048), w.DWORD(2048)
                if self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    process = buffer.value
            finally:
                self.kernel.CloseHandle(handle)
        return dict(hwnd=int(hwnd), title=title.value, window_class=kind.value,
                    pid=pid.value, process=process, own=pid.value == os.getpid())

    def snapshot(self):
        # 先记录前台，再记录鼠标所在窗口；光标在游戏内不等于游戏获得焦点。
        foreground = self.describe(self.native.foreground())
        point = w.POINT()
        hover = dict(hwnd=0, title="无法读取鼠标位置")
        if self.native.api.GetCursorPos(ctypes.byref(point)):
            hwnd = self.native.api.WindowFromPoint(point)
            root = self.native.api.GetAncestor(hwnd, 2) if hwnd else 0
            hover = self.describe(root or hwnd)
        return dict(foreground=foreground, mouse_window=hover)


class DiagnosticHotkeys(Hotkeys):
    def on_toggle(self):
        self.events.put(("raw_hotkey", None))
        super().on_toggle()


def window_line(window):
    return (f"标题={window.get('title')!r} | PID={window.get('pid')} | "
            f"HWND={window.get('hwnd')} | 本进程={window.get('own')}")


def load_config():
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    validate_skill_regions({key: data.get(key+"_region") for key in ("seal", *ATTACK_SKILLS)})
    return data


def probe_icons(native, snapshot, output, number, label, log):
    import cv2
    import mss
    import numpy as np

    fg = snapshot["foreground"]
    log(f"[3 前台/{label}] {window_line(fg)}")
    log(f"    进程={fg.get('process')}")
    log(f"    鼠标所在窗口：{window_line(snapshot['mouse_window'])}")
    allowed = bool(fg["hwnd"]) and not fg["own"]
    log("    原程序前台检查=" + ("通过；请核对上述标题是否确实是游戏" if allowed else "未通过：空窗口或工具自身"))
    data = load_config()
    log("[4 配置] 文件可读、技能位置互不冲突")
    keys = [("seal", "圣印", TEMPLATE),
            *((key, name, path) for key, (name, _, path) in ATTACK_SKILLS.items()
              if key != "sanction" or data.get("sanction_region"))]
    modifiers = [name for vk, name in ((0x11, "Ctrl"), (0x10, "Shift"), (0x12, "Alt"), (0x5B, "左Win"), (0x5C, "右Win"))
                 if native.api.GetAsyncKeyState(vk) & 0x8000]
    log("[5 发键条件] 按住的修饰键=" + ("、".join(modifiers) if modifiers else "无"))
    bounds = None
    if allowed:
        try:
            bounds = native.client_bounds(fg["hwnd"])
        except Exception as exc:
            log(f"    读取前台客户区失败：{exc}")
    states = {True: "就绪", False: "冷却/已开启", None: "未知"}
    results = dict(foreground=fg, q_ready=None, modifiers=modifiers, inside={})
    with mss.MSS() as capture:
        for key, name, path in keys:
            region = data.get(key+"_region")
            if not region_on_screen(region, capture.monitors[1:]):
                log(f"[6 图标/{name}] 失败：坐标未配置或超出屏幕 {region}")
                continue
            reference = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            if reference is None:
                raise ValueError(f"无法读取模板 {path.name}")
            image = cv2.cvtColor(np.array(capture.grab(region)), cv2.COLOR_BGRA2BGR)
            state, score = (classify_icon(image, reference, float(data.get("ready_threshold", .90)))
                            if key == "seal" else classify_attack_icon(image, reference))
            inside = (bounds is not None and region["left"] >= bounds["left"] and region["top"] >= bounds["top"]
                      and region["left"]+region["width"] <= bounds["left"]+bounds["width"]
                      and region["top"]+region["height"] <= bounds["top"]+bounds["height"])
            log(f"[6 图标/{name}] {states[state]} | 相似度={score:.1%} | 位于前台客户区={inside}")
            results["inside"][key] = inside
            ok, encoded = cv2.imencode(".png", image)
            if ok:
                (output/f"{number}_{label}_{key}.png").write_bytes(encoded.tobytes())
            if key == "seal":
                results["q_ready"] = state
                log("    Q 判断=" + ("本帧符合；还需连续稳定约0.6秒" if state is True else "本帧不符合，不会自动Q"))
    log("[7 结果] 本测试未发送任何 Q/E/V 或右键；以上只检查识别与启动条件。")
    return results


def print_summary(raw, accepted, probes, errors, output, log):
    log("========== 请截图下面的诊断汇总 ==========")
    log(f"A. 实体总开关通知：{len(raw)} 次")
    log(f"B. 开关逻辑接受：{accepted} 次")
    if raw:
        fg = raw[-1]["foreground"]
        log("C. 最后一次实体总开关事件前台 · " + window_line(fg))
        log("   进程=" + fg.get("process", ""))
        log("   鼠标所在窗口=" + raw[-1]["mouse_window"].get("title", "未知"))
    if probes:
        last = probes[-1]
        log(f"E. 探测{len(probes)}次；最后一次Q识别={last['q_ready']}（True才符合Q图标条件）")
        log(f"F. 图标坐标位于前台客户区：{last['inside']}")
        log(f"G. 最后探测时修饰键：{last['modifiers'] or '无'}")
    log("H. 异常="+("；".join(errors[-3:]) if errors else "无"))
    log(f"完整记录：{output/'trace.log'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=90)
    args = parser.parse_args()
    enable_dpi_awareness()
    output = BASE/"diagnostics"/time.strftime("%Y%m%d_%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    with (output/"trace.log").open("w", encoding="utf-8") as stream:
        def log(message):
            line = f"[{time.strftime('%H:%M:%S')}] {message}"
            print(line, flush=True)
            stream.write(line+"\n")
            stream.flush()
        log("只读诊断 v1：不会攻击/净化/拾取；请先关闭原战斗助手。")
        log(f"解释器={sys.executable} | 脚本={Path(__file__).resolve()}")
        log(f"结果目录={output}")
        log("操作：切到游戏并单击游戏内空白位置，单独按一下鼠标 5 或 F10，然后等待 2 秒。")
        log("重复2次，然后回此窗口截图。回终端按 Ctrl+C 或等 90 秒结束。")
        hotkeys = None
        raw, probes, errors = [], [], []
        accepted = 0
        try:
            native = DiagnosticInput()
            inspector = WindowInspector(native)
            try:
                load_config()
                log("[0 自检] 配置检查通过")
            except Exception as exc:
                log(f"[0 自检] 配置异常：{exc}")
                errors.append(f"配置异常：{exc}")
            events = queue.Queue()
            hotkeys = DiagnosticHotkeys(events)
            pending, number = [], 0
            deadline = time.monotonic()+max(1, args.seconds)
            while time.monotonic() < deadline:
                try:
                    kind, payload = events.get(timeout=.02)
                    if kind == "hotkey_error":
                        log(f"[1 监听失败] {payload}")
                        errors.append(payload)
                        break
                    if kind == "hotkeys":
                        log("[1 监听] 实体总开关已就绪，请切回游戏测试")
                    elif kind == "raw_hotkey":
                        snapshot = inspector.snapshot()
                        raw.append(snapshot)
                        log("[2 鼠标 5 和 F10] 此时前台 · " + window_line(snapshot["foreground"]))
                        stream.write(json.dumps(snapshot, ensure_ascii=False)+"\n")
                        stream.flush()
                    elif kind == "toggle":
                        number += 1
                        accepted += 1
                        log(f"[2 开关] 第{number}次热键输入已被开关逻辑接受")
                        pending.extend((time.monotonic()+delay, number, label) for delay, label in ((.03, "即刻"), (.35, "350毫秒"), (.85, "850毫秒")))
                except queue.Empty:
                    pass
                now = time.monotonic()
                due = [item for item in pending if item[0] <= now]
                pending = [item for item in pending if item[0] > now]
                for _, number_id, label in due:
                    try:
                        probes.append(probe_icons(native, inspector.snapshot(), output, number_id, label, log))
                    except Exception as exc:
                        log(f"[探测失败/{label}] {type(exc).__name__}: {exc}")
                        errors.append(f"{label}: {exc}")
            log(f"诊断已保存：{output/'trace.log'}")
        except KeyboardInterrupt:
            log("用户结束诊断")
        except Exception as exc:
            log(f"[诊断错误] {type(exc).__name__}: {exc}")
            errors.append(str(exc))
        finally:
            if hotkeys:
                hotkeys.close()
            print_summary(raw, accepted, probes, errors, output, log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
