"""只读 CH9329 串口诊断。"""

import argparse
import os

from ch9329_output import CH9329Keyboard, DEFAULT_BAUD


def main():
    parser = argparse.ArgumentParser(description="CH9329 串口诊断")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list-ports", action="store_true", help="列出 Windows 串口")
    action.add_argument("--probe", action="store_true", help="读取芯片和 USB 枚举状态，无按键输出")
    parser.add_argument("--ch9329", metavar="COM", default=os.environ.get("CH9329_PORT", "").strip(),
                        help="使用 CH9329 键盘，例如 COM3")
    parser.add_argument("--baud", type=int, default=str(os.environ.get("CH9329_BAUD", DEFAULT_BAUD)),
                        help="芯片串口波特率，默认 9600")
    parser.add_argument("--address", type=lambda value: int(value, 0),
                        default=os.environ.get("CH9329_ADDRESS", "0"), help="芯片地址，默认 0x00")
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

    if not args.ch9329:
        parser.error("--probe 需要 --ch9329 COMx 或 CH9329_PORT")
    try:
        keyboard = CH9329Keyboard(args.ch9329, args.baud, args.address,
                                  trace=(lambda message: print(message, flush=True)) if args.trace else None)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        info = keyboard.probe()
        print(f"{keyboard.name} · {info.describe()}")
        print(keyboard.read_config().describe())
    except OSError as exc:
        parser.exit(1, f"{exc}\n")
    finally:
        keyboard.close()
    return 0 if info.usb_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
