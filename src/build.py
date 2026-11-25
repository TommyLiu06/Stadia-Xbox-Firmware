#!/usr/bin/env python3
"""Build bruce_pvt_xbox (XInput + BLE + 1000 Hz + rumble fix) from the official image.

The only input is the unmodified PVT retail image
``bruce_pvt_a_prod_signed.bin``; everything else is compiled from the five files
next to this script.

    pip install -r requirements.txt
    python3 build.py bruce_pvt_a_prod_signed.bin [output.bin]

Result must be exactly 1,050,984 bytes with SHA-256
    e2d681121fd9248c2c34e7948f93a96b48249fa1e744552a4885272a04a82c72
"""
from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "bruce_pvt_xbox.EXPERIMENTAL-INVALID-SIGNATURE.bin"

# ----------------------------------------------------------------- image layout
PRODUCTION_SHA256 = "466ddd2dbcd8d1a66e2826f25849a6e5e45d9bb7e696e3fe0d500af56a5507a3"
RUMBLE_SHA256 = "a665b6c68d2535bebd4bd33e80141811675e1773f371cabd8c10246c036293d9"
STOCK_HEADSET_SHA256 = "00b03a6f9b82b9e05bb4a142299357072f9c560b16e216dad37802ce7fb23872"

FLASH_BASE = 0x60040000
PAYLOAD_ADDRESS = 0x6013D5A8
PAYLOAD_LIMIT = 0x840
METADATA_OFFSET = 0xFF000          # IVT + CSF + signature, must stay untouched
SERIES_MAP_ADDRESS = 0x6011E59E
RUMBLE_PROPERTY_OFFSET = 0xCB28C   # step 1: GATT Write-Without-Response bit

ENTRY_TABLE = struct.Struct("<8I")

# ----------------------------------------------------------------- step 1
def apply_rumble_fix(image: bytes) -> bytes:
    """Advertise Write Without Response on the HID output characteristic (0x2A4D)."""
    if hashlib.sha256(image).hexdigest() != PRODUCTION_SHA256:
        raise ValueError("Not the official PVT image (SHA-256 mismatch)")
    if image[RUMBLE_PROPERTY_OFFSET] != 0x0A:
        raise ValueError("Expected properties byte 0x0A at 0xCB28C")
    fixed = bytearray(image)
    fixed[RUMBLE_PROPERTY_OFFSET] = 0x0E   # Read | Write | WriteWithoutResponse
    fixed = bytes(fixed)
    changed = [i for i, (a, b) in enumerate(zip(image, fixed)) if a != b]
    if changed != [RUMBLE_PROPERTY_OFFSET]:
        raise AssertionError("Rumble fix touched more than one byte")
    return fixed


# ----------------------------------------------------------------- toolchain
def compile_payload() -> tuple[bytes, bytes]:
    """Cross-compile for the Cortex-M7 and return (.payload, .series_map)."""
    with tempfile.TemporaryDirectory(prefix="bruce-") as tmp:
        env = dict(os.environ)
        env.update(TMP=tmp, TEMP=tmp, TMPDIR=tmp,
                   ZIG_LOCAL_CACHE_DIR=str(HERE / ".build" / "cache-local"),
                   ZIG_GLOBAL_CACHE_DIR=str(HERE / ".build" / "cache"))
        (HERE / ".build").mkdir(exist_ok=True)

        elf = Path(tmp) / "bruce.elf"
        raw = Path(tmp) / "section.bin"

        subprocess.run([
            sys.executable, "-m", "ziglang", "cc",
            "-target", "thumb-freestanding-eabi", "-mcpu=cortex_m7",
            "-Oz", "-ffreestanding",
            "-fno-unwind-tables", "-fno-asynchronous-unwind-tables",
            "-Wall", "-Wextra", "-Werror", "-nostdlib",
            "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
            "-Wl,-e,bruce_direct_usb_send",
            f"-Wl,-T,{HERE / 'bruce_direct.ld'}",
            f"-I{HERE}",
            str(HERE / "stadia_xinput.c"),
            str(HERE / "bruce_direct.c"),
            "-o", str(elf),
        ], cwd=HERE, env=env, check=True)

        def section(name: str) -> bytes:
            subprocess.run(
                [sys.executable, "-m", "ziglang", "objcopy",
                 "-O", "binary", "-j", name, str(elf), str(raw)],
                cwd=HERE, env=env, check=True,
            )
            return raw.read_bytes()

        return section(".payload"), section(".series_map")


# ----------------------------------------------------------------- step 2
def thumb_bl(address: int, target: int) -> int:
    offset = (target & ~1) - (address + 4)
    if offset & 1 or not -(1 << 24) <= offset < (1 << 24):
        raise ValueError("Target out of range for a Thumb BL")
    sign = (offset >> 24) & 1
    j1 = 1 ^ ((offset >> 23) & 1) ^ sign
    j2 = 1 ^ ((offset >> 22) & 1) ^ sign
    upper = 0xF000 | (sign << 10) | ((offset >> 12) & 0x3FF)
    lower = 0xD000 | (j1 << 13) | (j2 << 11) | ((offset >> 1) & 0x7FF)
    return upper | (lower << 16)


def build(base: bytes, payload: bytes, series_map: bytes) -> bytes:
    rumble = apply_rumble_fix(base)
    if hashlib.sha256(rumble).hexdigest() != RUMBLE_SHA256:
        raise AssertionError("Unexpected rumble-fixed image")

    size = (len(payload) + 3) & ~3
    if len(payload) <= ENTRY_TABLE.size or size > PAYLOAD_LIMIT:
        raise ValueError(f"Payload is {len(payload)} bytes, limit is {PAYLOAD_LIMIT:#x}")
    (usb_send, usb_receive, usb_event, ble_send, ble_receive,
     ble_report_map, usb_init, poll_period) = ENTRY_TABLE.unpack_from(payload)
    for entry in (usb_send, usb_receive, usb_event, ble_send, ble_receive,
                  ble_report_map, usb_init, poll_period):
        if not (entry & 1 and PAYLOAD_ADDRESS + ENTRY_TABLE.size <= entry
                < PAYLOAD_ADDRESS + len(payload)):
            raise ValueError(f"Invalid Thumb callback address {entry:#x}")

    image = bytearray(rumble)

    def write(address: int, data: bytes) -> None:
        offset = address - FLASH_BASE
        end = offset + len(data)
        if offset < 0 or end > len(image):
            raise ValueError(f"Address {address:#x} is outside the image")
        image[offset:end] = data

    def write_word(address: int, before: int, after: int) -> None:
        offset = address - FLASH_BASE
        if struct.unpack_from("<I", image, offset)[0] != before:
            raise ValueError(f"Unexpected source word at {address:#x}")
        struct.pack_into("<I", image, offset, after)

    # Payload goes into a flash hole that the stock image leaves zeroed.
    offset = PAYLOAD_ADDRESS - FLASH_BASE
    if any(image[offset:offset + size]):
        raise ValueError("Payload region is not empty")
    image[offset:offset + len(payload)] = payload

    # Move the zero-initialiser ranges so startup does not wipe the payload.
    write_word(0x600405F4, PAYLOAD_ADDRESS, PAYLOAD_ADDRESS + size)
    write_word(0x600405F8, 0x20002740, 0x20002740 + size)
    write_word(0x60040608, 0x20002740, 0x20002740 + size)
    write_word(0x6004061C, 0x2002A824, 0x2002A828)

    # BLE report map replaces the stock USB headset descriptor.
    map_offset = SERIES_MAP_ADDRESS - FLASH_BASE
    stock = bytes(image[map_offset:map_offset + len(series_map)])
    if hashlib.sha256(stock).hexdigest() != STOCK_HEADSET_SHA256:
        raise ValueError("Unexpected stock descriptor at the report-map address")
    write(SERIES_MAP_ADDRESS, series_map)

    # Redirect the entry points.
    write_word(0x6004C480, 0x600D0E17, usb_send)
    write_word(0x60058C0C, 0x60058B65, usb_receive)
    write_word(0x600741C8, 0x60060D55, usb_event)
    write_word(0x6004C1D8, 0x60081551, ble_send)
    write_word(0x6008144C, 0x600DF4C1, ble_receive)
    write_word(0x60060F90, thumb_bl(0x60060F90, 0x600550E5),
               thumb_bl(0x60060F90, usb_init))
    write_word(0x60076588, thumb_bl(0x60076588, 0x60076439),
               thumb_bl(0x60076588, poll_period))
    # Tail branch keeps the original return address of the Report Map event.
    write(0x600DF4F0, struct.pack("<I", thumb_bl(0x600DF4F0, ble_report_map) & ~0x40000000))

    # Identity and report references.
    write(0x60081574, bytes.fromhex("112c"))          # 16-byte BLE cache + report ID
    write(0x6012639B, bytes.fromhex("03020101"))      # OUT report 3, IN report 1
    write(0x60126561, bytes.fromhex("025e04130b0905"))  # Xbox PnP ID

    if bytes(image[METADATA_OFFSET:]) != base[METADATA_OFFSET:]:
        raise AssertionError("IVT / CSF / signature region changed")
    return bytes(image)


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        return 2
    base = Path(sys.argv[1]).read_bytes()
    output = Path(sys.argv[2]) if len(sys.argv) == 3 else OUTPUT
    payload, series_map = compile_payload()
    image = build(base, payload, series_map)
    output.write_bytes(image)
    print(f"payload {len(payload)} B, report map {len(series_map)} B")
    print(f"wrote   {output} ({len(image)} bytes)")
    print(f"sha256  {hashlib.sha256(image).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
