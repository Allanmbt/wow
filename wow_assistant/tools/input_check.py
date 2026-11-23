"""CH9329 串口诊断和单次键鼠测试。"""

import argparse
import threading
import time

from ..device.windows import configured_ch9329


def main():
    parser = argparse.ArgumentParser(description="CH9329 串口诊断")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list-ports", action="store_true", help="列出 Windows 串口")
    action.add_argument("--probe", action="store_true", help="读取芯片和 USB 枚举状态，无按键输出")
    action.add_argument("--test-q", action="store_true", help="3 秒后通过 CH9329 按一次 Q")
    action.add_argument("--test-mouse", action="store_true", help="3 秒后通过 CH9329 在指针当前位置右键一次")
    parser.add_argument("--ch9329", metavar="COM", help="覆盖配置中的端口，例如 COM7 或 auto")
    parser.add_argument("--baud", type=int, help="覆盖配置中的串口波特率")
    parser.add_argument("--address", type=lambda value: int(value, 0), help="覆盖配置中的芯片地址")
    parser.add_argument("--trace", action="store_true", help="打印串口 TX/RX 十六进制帧")
    args = parser.parse_args()
    if args.list_ports:
        try:
            from serial.tools.list_ports import comports
        except ImportError:
            parser.exit(1, "列出串口需要 pyserial，请在当前 Python 环境安装。\n")
        for port in sorted(comports()):
            print(f"{port.device} · {port.description}")
        return

    try:
        keyboard = configured_ch9329(args.ch9329, args.baud, args.address,
                                     trace=(lambda message: print(message, flush=True)) if args.trace else None)
    except ValueError as exc:
        parser.error(str(exc))
    if keyboard is None:
        parser.error("请在 input_config.json、CH9329_PORT 或 --ch9329 指定端口")
    try:
        info = keyboard.probe()
        print(f"{keyboard.name} · {info.describe()}", flush=True)
        if args.probe:
            print(keyboard.read_config().describe())
            return 0 if info.usb_ready else 2
        if not info.usb_ready:
            return 2
        if args.test_q:
            print("3 秒后按一次 Q，请将焦点切到接收端的记事本。", flush=True)
        else:
            print("3 秒后右键一次，请将鼠标指针移到接收端的记事本编辑区。", flush=True)
        time.sleep(3)
        cancel = threading.Event()
        if args.test_q:
            keyboard.tap("Q", cancel, hold_seconds=0.035)
            print("CH9329 已确认 Q 按下和松开。")
        else:
            keyboard.click("right", cancel)
            print("CH9329 已确认鼠标右键按下和松开。")
    except OSError as exc:
        parser.exit(1, f"{exc}\n")
    finally:
        keyboard.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
