ENTRY(bruce_direct_usb_send)

SECTIONS {
    /* The stock USB headset configuration is no longer advertised by XUSB. */
    .series_map 0x6011e59e : { KEEP(*(.series_map)) }
    .payload 0x6013d5a8 : {
        LONG(bruce_direct_usb_send)
        LONG(bruce_direct_usb_receive)
        LONG(bruce_direct_usb_event)
        LONG(bruce_direct_ble_send)
        LONG(bruce_direct_ble_receive)
        LONG(bruce_direct_ble_report_map)
        LONG(bruce_direct_usb_init)
        LONG(bruce_direct_poll_period)
        KEEP(*(.text.bruce_direct_*))
        *(.text .text.*)
        *(.rodata .rodata.*)
    }

    /* Alignment word immediately above the original zeroed globals. */
    report_buffers = 0x2002a824;
    /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment) }
}

ASSERT(SIZEOF(.payload) <= 0x840, "XInput payload exceeds the zero initializer region")
ASSERT(SIZEOF(.series_map) == 283, "Unexpected BLE report map size")
