# Copyright (C) 2026 Thomas Hebb
# SPDX-License-Identifier: GPL-3.0-only

from collections import namedtuple
from struct import Struct


class StructTuple:
    """Serialize field values into the struct."""
    def _pack(self):
        return self._struct.pack(*self)

    """Create a new instance from a serialized struct."""
    @classmethod
    def _unpack(cls, data):
        return cls._make(cls._struct.unpack(data))

    """Create a new instance from the beginning of a buffer.

    Bytes from the beginning of the buffer are deserialized into a new instance,
    which is returned along with the unused portion of the buffer.
    """
    @classmethod
    def _unpack_start(cls, data):
        parsed = cls._unpack(data[:cls._struct.size])
        remainder = data[cls._struct.size:]
        return parsed, remainder


"""Like namedtuple(), but with an associated struct for serialization."""
def structtuple(typename, field_names, struct_format):
    cls = namedtuple(typename, field_names)
    cls._struct = Struct(struct_format)
    cls.__bases__ += (StructTuple,)
    return cls
