"""CH9329/CH9329F UART-to-USB keyboard and mouse output."""

import time
from dataclasses import dataclass


HEADER = bytes.fromhex("57 AB")
GET_INFO = 0x01
KEYBOARD_REPORT = 0x02
RELATIVE_MOUSE_REPORT = 0x05
GET_CONFIG = 0x08
RELEASE_REPORT = bytes(8)
MOUSE_RELEASE = bytes((1, 0, 0, 0, 0))
MOUSE_BUTTONS = {"left": 1, "right": 2, "middle": 4}
DEFAULT_BAUD = 9600


def _frame(address, command, data):
    packet = HEADER + bytes((address, command, len(data))) + data
    return packet + bytes((sum(packet) & 0xFF,))


@dataclass(frozen=True)
class ChipInfo:
    version: int
    usb_ready: bool
    caps_lock: bool

    def describe(self):
        usb = "已枚举" if self.usb_ready else "未枚举，请检查 HID 端 USB 数据线"
        caps = "开" if self.caps_lock else "关"
        return f"芯片版本码 0x{self.version:02X} · USB {usb} · Caps Lock {caps}"


@dataclass(frozen=True)
class ChipConfig:
    mode: int
    serial_mode: int
    address: int
    baudrate: int

    def describe(self):
        mouse = "支持相对鼠标" if (self.mode & 0x7F) in (0, 2) else "当前未启用相对鼠标"
        return (f"工作模式 0x{self.mode:02X} · {mouse} · 串口模式 0x{self.serial_mode:02X}"
                f" · 地址 0x{self.address:02X} · 波特率 {self.baudrate}")


class CH9329Keyboard:
    """One serial link for keyboard and mouse reports with chip acknowledgements."""

    def __init__(self, port, baudrate=DEFAULT_BAUD, address=0, *, trace=None):
        self.port = port.strip()
        if not self.port or baudrate <= 0 or not 0 <= address <= 0xFE:
            raise ValueError("请指定有效串口、正波特率和 0x00–0xFE 芯片地址")
        self.baudrate, self.address, self.trace = baudrate, address, trace
        self.link = None
        self.info = None

    @property
    def name(self):
        return f"CH9329 USB HID ({self.port} / {self.baudrate})"

    def _open(self):
        if self.link is not None:
            return
        try:
            import serial
        except ImportError as exc:
            raise OSError("CH9329 需要 pyserial，请在当前 Python 环境安装") from exc
        try:
            self.link = serial.Serial(self.port, self.baudrate, timeout=0.05, write_timeout=0.2)
            self.link.reset_input_buffer()
        except (OSError, ValueError) as exc:
            self.close()
            raise OSError(f"无法打开 {self.port}，请确认端口并关闭占用它的串口工具。{exc}") from exc

    def _read(self, size, deadline):
        data = bytearray()
        while len(data) < size and time.monotonic() < deadline:
            data.extend(self.link.read(size - len(data)))
        return bytes(data)

    def _exchange(self, command, data, operation):
        request = _frame(self.address, command, data)
        try:
            if self.trace:
                self.trace(f"{operation} TX {request.hex(' ').upper()}")
            if self.link.write(request) != len(request):
                raise OSError("串口未写入完整请求")
            deadline = time.monotonic() + 0.5
            header = self._read(5, deadline)
            if len(header) != 5 or header[:2] != HEADER:
                received = header.hex(" ").upper() or "无数据"
                raise OSError(f"未收到有效帧头（{received}）。请检查波特率、芯片地址和串口协议模式")
            reply = header + self._read(header[4] + 1, deadline)
            if command == GET_CONFIG and header[4] == 50 and len(reply) == 56:
                # 部分 CH9329F 保留 50 的长度字段，但在校验和前发送 15 个附加字节。
                tail = self.link.read(15)
                if tail:
                    reply += tail + self._read(15 - len(tail), deadline)
            if self.trace:
                self.trace(f"{operation} RX {reply.hex(' ').upper()}")
            extended_config = command == GET_CONFIG and header[4] == 50 and len(reply) == 71
            if len(reply) != header[4] + 6 and not extended_config:
                raise OSError("应答帧不完整")
            if reply[-1] != sum(reply[:-1]) & 0xFF:
                raise OSError("应答校验和错误")
            if reply[2] != self.address:
                raise OSError(f"应答地址不匹配（0x{reply[2]:02X}）")
            payload = reply[5:5 + header[4]]
            if reply[3] == command | 0xC0:
                status = payload.hex(" ").upper() or "无数据"
                raise OSError(f"芯片拒绝指令（状态 {status}）")
            if reply[3] != command | 0x80:
                raise OSError(f"应答命令码不匹配（0x{reply[3]:02X}）")
            return payload
        except OSError as exc:
            raise OSError(f"{self.name} · {operation}失败。{exc}") from exc

    def probe(self):
        """Read chip state without sending a keyboard report."""
        self._open()
        try:
            payload = self._exchange(GET_INFO, b"", "读取芯片")
            if len(payload) != 8 or payload[1] not in (0, 1):
                raise OSError("芯片信息格式异常")
            self.info = ChipInfo(payload[0], payload[1] == 1, bool(payload[2] & 0x02))
            return self.info
        except BaseException:
            self.close()
            raise

    def prepare(self):
        info = self.info if self.info is not None else self.probe()
        if not info.usb_ready:
            self.close()
            raise OSError("CH9329 串口已应答，但 HID 端未枚举。请检查另一端 USB 数据线")

    def read_config(self):
        """Read work mode, serial mode, address and baud rate without writing settings."""
        self._open()
        try:
            payload = self._exchange(GET_CONFIG, b"", "读取配置")
            if len(payload) != 50:
                raise OSError("CH9329 配置应答长度异常")
            return ChipConfig(payload[0], payload[1], payload[2], int.from_bytes(payload[3:7], "big"))
        except BaseException:
            self.close()
            raise

    def _report(self, command, data, operation):
        status = self._exchange(command, data, operation)
        if status != b"\x00":
            raise OSError(f"{self.name} · {operation}失败。芯片状态 {status.hex(' ').upper() or '无数据'}")

    def tap(self, key, cancel, hold_seconds):
        if len(key) != 1 or not key.isascii() or key.upper() not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            raise ValueError("CH9329 输出目前支持 A–Z 键")
        usage = ord(key.upper()) - ord("A") + 4
        report = bytes((0, 0, usage, 0, 0, 0, 0, 0))
        return self._press_and_release(KEYBOARD_REPORT, report, RELEASE_REPORT, key.upper(), cancel, hold_seconds)

    def click(self, button, cancel, hold_seconds=0.035):
        if button not in MOUSE_BUTTONS:
            raise ValueError("CH9329 相对鼠标报告支持左键、右键和中键")
        # 位 0/1/2 是左/右/中键。X、Y 和滚轮都为零，指针保持原位。
        report = bytes((1, MOUSE_BUTTONS[button], 0, 0, 0))
        label = {"left": "鼠标左键", "right": "鼠标右键", "middle": "鼠标中键"}[button]
        return self._press_and_release(RELATIVE_MOUSE_REPORT, report, MOUSE_RELEASE, label, cancel, hold_seconds)

    def _press_and_release(self, command, down, up, label, cancel, hold_seconds):
        if not 0 <= hold_seconds < 0.25:
            raise ValueError("CH9329 按住时间须小于 250 毫秒")
        if cancel.is_set():
            return False
        self.prepare()
        if cancel.is_set():
            return False
        try:
            try:
                self._report(command, down, f"{label} 按下")
                cancel.wait(hold_seconds)
            finally:
                self._report(command, up, f"{label} 松开")
        except BaseException:
            self.close()
            raise
        return True

    def close(self):
        link, self.link = self.link, None
        self.info = None
        if link is not None:
            try:
                link.close()
            except OSError:
                pass
