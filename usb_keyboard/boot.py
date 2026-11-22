"""Enable the data port and keyboard before the Pico enumerates over USB."""

import usb_cdc
import usb_hid

usb_cdc.enable(console=False, data=True)
usb_hid.enable((usb_hid.Device.KEYBOARD,))
