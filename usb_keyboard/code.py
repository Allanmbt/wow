"""CircuitPython USB keyboard bridge. The host controls one key at a time."""

import time


class KeyboardBridge:
    def __init__(self, link, keyboard):
        self.link = link
        self.keyboard = keyboard
        self.report = bytearray(8)
        self.release_at = None

    def release(self):
        self.report[2] = 0
        self.keyboard.send_report(self.report)
        self.release_at = None

    def tick(self, now):
        if self.release_at is not None and (now >= self.release_at or not self.link.connected):
            self.release()
        if not self.link.connected:
            return

        command = self.link.read(1)
        if not command:
            return
        if command == b"?":
            if self.release_at is not None:
                self.release()
            self.link.write(b"HID1")
        elif command == b"\x00":
            self.release()
            self.link.write(b"+")
        elif 4 <= command[0] <= 29 and self.release_at is None:
            # Keep a finite hold even if the host process stops before releasing.
            self.release_at = now + 0.30
            self.report[2] = command[0]
            self.keyboard.send_report(self.report)
            self.link.write(b"+")
        else:
            self.link.write(b"-")


def main():
    import usb_cdc
    import usb_hid

    link = usb_cdc.data
    link.timeout = 0
    link.write_timeout = 0.05
    bridge = KeyboardBridge(link, usb_hid.devices[0])
    while True:
        try:
            bridge.tick(time.monotonic())
        except OSError:
            # Retry a pending release when USB becomes available again.
            pass
        time.sleep(0.001)


if __name__ == "__main__":
    main()
