# Copyright (C) 2026 Thomas Hebb
# SPDX-License-Identifier: GPL-3.0-only

"""A memory buffer to be loaded at a known address on a target device.

Provides helpers to translate between a buffer offset and a target address
(absfromrel(), relfromabs()), populate regions of the buffer (placeabs(),
placerel()), print a hex dump (dump()), and reserve regions (allocate()). The
underlying buffer can be accessed via the buf field.
"""
class TargetMemory:
    DUMP_ALIGNMENT = 16
    DUMP_MASK = ~(DUMP_ALIGNMENT - 1)

    def __init__(self, base):
        self.base = base
        self.buf = bytearray()

    def placerel(self, offset, data):
        count = offset - len(self.buf)
        if count > 0:
            self.buf.extend(b"\x00" * count)

        self.buf[offset:offset+len(data)] = data

    def placeabs(self, addr, data):
        self.placerel(addr - self.base, data)

    def absfromrel(self, offset):
        return self.base + offset

    def relfromabs(self, addr):
        return addr - self.base

    def allocate(self, size, align=4):
        addr = self.absfromrel(len(self.buf))

        if align != 1 and (unaligned := addr % align) != 0:
            padding = align - unaligned
            addr += padding
            size += padding

        self.buf.extend(b"\x00" * size)
        return addr

    def dump(self):
        if not self.buf:
            return

        start = self.base & self.DUMP_MASK
        end = self.base + len(self.buf)

        for addr in range(start, end, self.DUMP_ALIGNMENT):
            offset_start = addr - self.base
            offset_end = offset_start + self.DUMP_ALIGNMENT

            print(f"{addr:08x}: ", end="")
            if offset_start < 0:
                print("   " * -offset_start, end="")
                offset_start = 0

            print(self.buf[offset_start:offset_end].hex(' '))
