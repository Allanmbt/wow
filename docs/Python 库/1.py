#/usr/bin/env python
# -*- coding: UTF-8 -*-
import sys
import serial
import serial.tools.list_ports
from pych9329 import HIDController, SerialTransport
import time

# 读取固件版本命令（参考常见实现）
CMD_READ_VERSION = bytes([
    0x57, 0xAB,  # 帧头
    0x00,        # 地址
    0x03,        # 命令码：读取版本
    0x04,        # 数据长度
    0x00, 0x00, 0x00, 0x00,  # 数据（无）
    0x00         # 校验和占位，后面计算
])

# 常用波特率
baud_list = [9600, 115200, 4800, 19200, 38400, 57600]


# 计算校验和（累加和取低8位）
def calc_checksum(packet):
    return sum(packet[:-1]) & 0xFF


# 构建完整的读取版本命令包（自动计算校验和）
def build_version_query():
    pkt = bytearray(CMD_READ_VERSION)
    pkt[-1] = calc_checksum(pkt)  # 替换校验和
    return bytes(pkt)


# 解析版本回复（假定回复格式：57 AB 00 83 04 <版本数据> <校验>）
def parse_version_response(data):
    if len(data) < 8:
        return None
    if data[0] == 0x57 and data[1] == 0xAB and data[3] == 0x83:
        # 版本数据在 data[5:7]（通常为2字节ASCII码，如0x31 0x30 表示 "10"）
        version = f"{data[5]:02X}{data[6]:02X}"
        return version
    return None


# ---------- 端口选择 ----------
def select_port():
    """
    列出所有描述中包含 'USB-Enhanced-SERIAL CH343' 的串口，
    让用户通过编号选择，返回选中的端口名称（如 'COM3'）。
    如果没有匹配端口，则返回 None。
    """
    all_ports = list(serial.tools.list_ports.comports())
    # 只匹配包含 "USB-Enhanced-SERIAL CH343" 的端口（区分大小写，但描述通常固定）
    target_ports = []
    for port in all_ports:
        if 'usb-enhanced-serial ch343' in port.description.lower():
            target_ports.append(port)

    if not target_ports:
        print("❌ 未找到任何 USB-Enhanced-SERIAL CH343 设备，请检查连接。")
        return None

    print("✅ 找到以下可用设备：")
    for idx, port in enumerate(target_ports, start=1):
        print(f"  {idx}. {port.device}  ({port.description})")

    while True:
        try:
            choice = input("\n👉 请输入要选择的设备编号（输入 0 退出）: ").strip()
            if choice == '0':
                return None
            num = int(choice)
            if 1 <= num <= len(target_ports):
                selected = target_ports[num - 1]
                print(f"✅ 已选择：{selected.device} ({selected.description})")
                return selected.device
            else:
                print(f"⚠️ 编号范围 1~{len(target_ports)}，请重新输入。")
        except ValueError:
            print("⚠️ 请输入有效的数字编号。")
        except KeyboardInterrupt:
            print("\n👋 用户取消选择。")
            return None


# ---------- 波特率选择（手动/自动） ----------
def select_baud(port_name):
    menu = [
        ("手动输入波特率", select_baud_1),
        ("从常用列表选择", select_baud_2),
        ("自动适配（尝试常见波特率并发送查询命令）", select_baud_3)
    ]

    print("\n--- 波特率设置 ---")
    for idx, (desc, _) in enumerate(menu, 1):
        print(f"  {idx} {desc}")

    while True:
        mode = int(input("请选择模式（1/2/3）: ").strip())
        if not (1 <= mode <= len(menu)):
            mode = 1
        operation = menu[mode - 1][1]
        return operation(port_name)

# 手动输入波特率
def select_baud_1(port_name):
    while True:
        try:
            baud = int(input("请输入波特率（如 9600）: ").strip())
            if baud > 0:
                return baud
        except ValueError:
            print("⚠️ 请输入正整数")


# 从常用列表选择
def select_baud_2():
    print("常用波特率列表：")
    for idx, b in enumerate(baud_list, start=1):
        print(f"  {idx}. {b}")
    while True:
        try:
            choice = int(input("请输入编号: ").strip())
            if 1 <= choice <= len(baud_list):
                return baud_list[choice - 1]
        except ValueError:
            print("⚠️ 请输入数字")


# ---------- 自动适配波特率 ----------
def select_baud_3(port_name):
    if baud_list is None:
        return None

    query = build_version_query()  # 使用之前的查询命令
    candidates = []  # 存储 (波特率, 回复数据) 格式不匹配的候选项

    print(f"🔍 正在尝试波特率：{baud_list}")
    for baud in baud_list:
        try:
            ser = serial.Serial(port_name, baud, timeout=0.5)
            ser.flushInput()
            ser.flushOutput()
            ser.write(query)
            time.sleep(0.2)
            resp = ser.read(16)
            ser.close()

            if resp:
                # 尝试解析正确格式
                version = parse_version_response(resp)
                if version:
                    print(f"✅ 波特率 {baud} 解析成功！固件版本 = {version}")
                    return baud
                else:
                    # 格式不匹配，保存候选
                    candidates.append((baud, resp))
                    print(f"📥 波特率 {baud} 有回复但格式不匹配 (长度 {len(resp)})")
            else:
                print(f"⏳ 波特率 {baud} 无回复")
        except Exception as e:
            print(f"❌ 波特率 {baud} 打开失败：{e}")

    # 没有直接匹配成功，检查候选列表
    if candidates:
        print(f"\n📋 找到 {len(candidates)} 个有回复但格式不匹配的波特率：")
        for idx, (baud, resp) in enumerate(candidates, start=1):
            # 显示回复内容的前16字节（十六进制）
            hex_repr = resp.hex()[:32]  # 最多显示16字节
            print(f"  {idx}. {baud} bps  (回复: {hex_repr}...)")
        print("  0. 取消选择")
        while True:
            try:
                choice = input("\n👉 请选择要使用的波特率编号（0 退出）: ").strip()
                if choice == '0':
                    return None
                num = int(choice)
                if 1 <= num <= len(candidates):
                    selected_baud = candidates[num - 1][0]
                    print(f"✅ 用户选择波特率 {selected_baud}")
                    return selected_baud
                else:
                    print(f"⚠️ 编号范围 1~{len(candidates)}")
            except ValueError:
                print("⚠️ 请输入数字")
            except KeyboardInterrupt:
                print("\n👋 取消")
                return None
    else:
        print("❌ 所有波特率均无回复或无法适配，请手动选择。\n")
        select_baud(port_name)
        return None


def main():
    port = select_port()
    if port is None:
        exit(0)

    baudrate = select_baud(port)
    if baudrate is None:
        exit(0)

    # 测试发送 按键A
    with SerialTransport(port=port, baudrate=baudrate) as transport:
        controller = HIDController(transport)

        # 发送按键 'a'（包含按下和弹起）
        # press() 方法会自动处理按下和弹起动作
        controller.press('a')
        print("按键 'a' 已发送")


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()