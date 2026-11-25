This script exploits a 3-byte global buffer overflow in NXP's Serial Download
Protocol (SDP) to run unsigned code before BootROM exit. It targets the i.MX
RT1060. Although I have not checked, the vulnerability likely exists on other
members of the i.MX RT series. Due to hardcoded offsets, the exploit will not
run unmodified on other vulnerable chips but should be easily portable.

The exploit can run in two modes: by default, it [patches the BootROM][rompatch]
using the ROMC peripheral to temporarily reopen High-Assurance Boot (HAB) for
subsequent SDP commands in the same boot. If you pass the `--permanent` flag,
the exploit instead permanently and irrevocably reopens HAB by blowing the
FIELD_RETURN eFuse. That puts the chip into a "field return" state, which is
distinct from the "open" state of a fresh chip, but both behave similarly for
most purposes, including booting unsigned images and debugging over SWD.

[rompatch]: https://oxide.computer/blog/exploiting-undocumented-hardware-blocks-in-the-lpc55s69

## Usage

Put an i.MX RT1060-series chip into SDP mode and connect it via USB. Ensure the
`hidapi` (for USB communication) and `keystone-engine` (for shellcode assembly)
Python packages are installed, then run the exploit:

    python3 main.py

If you use uv, you don't need to worry about dependencies:

    uv run main.py

If everything works, the script will print a hex dump of its payload followed by

    Writing payload...
    Exploit succeeded! HAB is disabled for this boot

You can then use standard SDP tooling (e.g. [NXP's `sdphost`][sdphost]) to load
and execute an image during the same boot, which will not be signature checked.

[sdphost]: https://spsdk.readthedocs.io/en/latest/apps/sdphost.html

To permanently reopen HAB, run with `--permanent`:

    python3 main.py --permanent
    uv run main.py --permanent

You will see the following confirmation prompt for the permanent exploit:

    THIS ACTION IS IRREVERSIBLE. Type "REOPEN" (case-sensitive) to permanently
    reopen HAB on your device, or anything else to cancel:

By typing "REOPEN", you accept any consequences that may arise from blowing the
FIELD_RETURN eFuse on your chip and putting it into field return mode.

## Vulnerability details

NXP's BootROM implements a download mode called Serial Download Protocol (SDP),
which accepts memory read, write, and execute commands over either UART or USB
HID. Unlike most other debugging interfaces, SDP is not automatically disabled
on HAB-closed chips; however, closing HAB does restrict the memory ranges
accessible via SDP and forbid execution of unsigned code.

Nonetheless, SDP is a compelling target for vulnerability research: the USB
stack and command handlers present a large attack surface, and SDP runs before
the BootROM locks the `FIELD_RETURN` eFuse via the `OCOTP.SW_STICKY` register,
meaning code executed within SDP can in effect reopen a closed HAB.

The following text references the RT1060 BootROM with HAB version 4.3.7, a copy
of which can be found [here][bootrom][^hash-bootrom]. Since NXP doesn't publish
BootROM source code, names of some symbols are made up. The BootROM uses a USB
stack that more or less matches [the one in NXP's MCUXpresso SDK][sdk-usb], so
I used the SDK's naming where applicable. Here are the addresses of symbols I
mention, with SDK symbols linked to SDK source code:

[bootrom]: https://github.com/Zenofex/SoC-BootROMs/blob/main/nxp/imxrt1060.bootrom.bin
[^hash-bootrom]: `imxrt1060.bootrom.bin`; SHA-256 `a5d9af17f0e21866774df91c1e634e955e72b1442aedd84c8aac8eefc8e58f8e`

- `sdp_usb_run()`: `0x0020_ef7c`
- `sdp_usb_pump()`: `0x0020_ed38`
- `enable_mpu()`: `0x0020_cab8`
- `sdp_hid_callback()`: `0x0020_ea7e`
- `sdp_hid_request` (`uint8_t[2048]`): `0x2020_723c`
- `sdp_dev`: `0x2020_7a3c`
- `sdp_dev_cfg` ([`usb_device_class_config_list_struct_t`][type-device-class-config-list]): `0x2020_3d08`
- [`USB_DeviceClassInit()`][fn-classinit]: `0x0020_d758`
- [`USB_DeviceEhciIsrFunction()`][fn-ehciisrfunction]: `0x0020_e34c`

[sdk-usb]: https://github.com/nxp-mcuxpresso/mcuxsdk-middleware-usb/tree/MCUX_2.11.0
[fn-classinit]: https://github.com/nxp-mcuxpresso/mcuxsdk-middleware-usb/blob/MCUX_2.11.0/output/source/device/class/usb_device_class.c#L416-L437
[fn-ehciisrfunction]: https://github.com/nxp-mcuxpresso/mcuxsdk-middleware-usb/blob/MCUX_2.11.0/device/usb_device_ehci.c#L1995-L2003
[type-device-class-config-list]: https://github.com/nxp-mcuxpresso/mcuxsdk-middleware-usb/blob/MCUX_2.11.0/output/source/device/class/usb_device_class.h#L149-L160

The BootROM sets up USB for SDP in `sdp_usb_run()`: it calls the SDK's
`USB_DeviceClassInit()` with a pointer to `sdp_dev_cfg`, which contains a
single HID class configuration with a `classCallback` of `sdp_hid_callback()`.

When a HID `Set_Report` request arrives, the SDK [calls][trace-classcallback]
`classCallback`, passing a [`usb_device_hid_report_struct_t`][type-hid-report]
with `reportType`, `reportId`, and `reportLength` populated. The callback
populates `reportBuffer` with a buffer in which to
[receive][trace-setuprequest] the report's payload, optionally adjusting
`reportLength`. It can also reject the report entirely.

[trace-classcallback]: https://github.com/nxp-mcuxpresso/mcuxsdk-middleware-usb/blob/MCUX_2.11.0/output/source/device/class/usb_device_hid.c#L576-L582
[trace-setuprequest]: https://github.com/nxp-mcuxpresso/mcuxsdk-middleware-usb/blob/MCUX_2.11.0/output/source/device/usb_device_ch9.c#L1067-L1115
[type-hid-report]: https://github.com/nxp-mcuxpresso/mcuxsdk-middleware-usb/blob/MCUX_2.11.0/output/source/device/class/usb_device_hid.h#L54-L86

The BootROM uses a 2048-byte global buffer, `sdp_hid_request`, to hold report
payloads. `sdp_hid_callback()` dutifully checks that `reportLength` does not
exceed 2048 bytes before setting `reportBuffer` to `sdp_hid_request`... **plus
3 bytes**. Wait, what? The vulnerability is indeed that simple: HID reports are
written to `sdp_hid_request` at offset 3 instead of offset 0, meaning up to 3
bytes overflow into adjacent memory[^why].

[^why]: I suspect the offset was added to prevent the report's ID, which is
    always the first byte of a HID report, from dealigning the rest of its
    data. However, the BootROM copies the report's data into a separate
    (aligned) buffer before doing anything with it, so in practice things would
    have worked fine without the offset.

So, what's in those three bytes? In a stroke of luck for us, the very next
global after `sdp_hid_request` is `sdp_dev`, a struct representing the SDP USB
device, whose first field is a (32-bit, little-endian) pointer to the SDK's
[`usb_device_struct_t`][type-device], which indirectly holds almost all device
state, including callback function pointers and the base address of
memory-mapped EHCI registers. With control over the low three bytes of that
pointer, we can point it to any address in the `0x20xx_xxxx` range, which
includes the entirety of both DTCM and OCRAM.

[type-device]: https://github.com/nxp-mcuxpresso/mcuxsdk-middleware-usb/blob/MCUX_2.11.0/device/usb_device_dci.h#L124-L152

Even better for us, the pointer is read only by `sdp_usb_pump()` (called in an
infinite loop while SDP is running over USB), which passes it by value to
`USB_DeviceEhciIsrFunction()` to handle outstanding transfers and events. As
such, a HID request that corrupts the pointer will finish cleanly before the
corruption takes effect.

## Exploitation

There are likely numerous ways to achieve code execution from this
vulnerability. The script in this repository does it by forging a
`usb_device_struct_t` with `epCallback`s that point to shellcode and a
`controllerHandle` that points to a forged
[`usb_device_ehci_state_struct_t`][type-ehci-state], which itself points to
forged EHCI hardware registers and data structures indicating that endpoints
have completed transfers pending. The shellcode and forged structures are
embedded inside the overflowing HID report and get stored in `sdp_hid_request`,
meaning the entire exploit takes only a single USB transfer. Additional details
are documented as comments in `main.py`.

[type-ehci-state]: https://github.com/nxp-mcuxpresso/mcuxsdk-middleware-usb/blob/MCUX_2.11.0/device/usb_device_ehci.h#L139-L168

Notably, I had expected to have to call a BootROM function to disable the MPU
before calling my shellcode, as the i.MX RT1060's Reference Manual says the
following:

> The boot ROM enables MPU protection under HAB-closed mode, to prevent
> execution of any other supported memory regions than the ROM region, during
> boot code execution.

And indeed the BootROM calls `enable_mpu()` when HAB is closed. However, that
function has a logic bug where, after configuring all the MPU regions, it
doesn't actually enable the MPU! There is code in the function to enable the
MPU, but it only runs when region configuration ends early due to an invalid
region, which never happens because the hardcoded regions are all valid.

## Disclosure

I chose not to report this bug privately to NXP. Although secure boot can be
implemented in a freedom-preserving way, like on some unlockable phones,
embedded devices (where i.MX RT chips are used) almost always use it to stop us
users from altering software on our own devices. A vulnerability whose main
effect is to take back a freedom we should have been given all along does not
make us any less secure and can go unreported.

## Provenance

This vulnerability was discovered, exploited, and documented by me, Thomas
Hebb, with no assistance from AI tooling of any kind. My copy of this
repository is hosted at https://codeberg.org/tchebb/nxp-sdp-overrun.
