from __future__ import annotations

import hashlib
import logging
import struct
import zlib


LOG = logging.getLogger("zfs-rescue")
LABEL_SIZE = 256 << 10
DATA_START = 4 << 20
MAX_BLOCK = 32 << 20
MASK64 = (1 << 64) - 1
UBER_MAGIC = 0xBAB10C
ECK_MAGIC = 0x210DA7AB10C7A11
MAX_ENTRIES = 2_000_000


class RescueError(Exception):
    pass


class Unsupported(RescueError):
    pass


class Corrupt(RescueError):
    pass


class ReadError(RescueError):
    pass


def require(condition, message):
    if not condition:
        raise Corrupt(message)


def unpack(fmt, data, offset=0):
    try:
        return struct.unpack_from(fmt, data, offset)
    except struct.error as exc:
        raise Corrupt(f"Truncated structure at {offset}: {fmt}") from exc


def u64(data, offset=0, endian="<"):
    return unpack(endian + "Q", data, offset)[0]


def checked_name(name: bytes):
    require(bool(name) and name not in (b".", b"..") and
            b"/" not in name and b"\0" not in name, f"Unsafe name: {name!r}")
    return name


def checksum(data, algorithm, endian):
    if algorithm == 8:
        return struct.unpack(">4Q", hashlib.sha256(data).digest())
    if algorithm == 11:
        try:
            digest = hashlib.new("sha512_256", data).digest()
        except ValueError as exc:
            raise Unsupported("SHA512/256 unavailable in this Python/OpenSSL") from exc
        return struct.unpack(endian + "4Q", digest)
    if algorithm == 7:
        require(len(data) % 4 == 0, "Unaligned Fletcher4 input")
        a = b = c = d = 0
        for (word,) in struct.iter_unpack(endian + "I", data):
            a = (a + word) & MASK64
            b = (b + a) & MASK64
            c = (c + b) & MASK64
            d = (d + c) & MASK64
        return a, b, c, d
    if algorithm == 6:
        require(len(data) % 16 == 0, "Unaligned Fletcher2 input")
        a = b = c = d = 0
        for x, y in struct.iter_unpack(endian + "QQ", data):
            a = (a + x) & MASK64
            b = (b + y) & MASK64
            c = (c + a) & MASK64
            d = (d + b) & MASK64
        return a, b, c, d
    raise Unsupported(f"Checksum {algorithm} unsupported (including OFF/INHERIT)")


def verify_label(data, offset):
    """The verifier is the byte offset relative to this leaf vdev, NOT disk/GPT."""
    require(len(data) >= 40, "Truncated label checksum")
    magic = data[-40:-32]
    endian = next((e for e in ("<", ">")
                   if struct.pack(e + "Q", ECK_MAGIC) == magic), None)
    require(endian is not None, "Missing embedded checksum magic")
    expected = unpack(endian + "4Q", data, len(data) - 32)
    substituted = data[:-32] + struct.pack(endian + "4Q", offset, 0, 0, 0)
    require(checksum(substituted, 8, endian) == expected, "Label/uberblock checksum mismatch")
    return endian


def decompress(data: bytes, comp: int, size: int) -> bytes:
    require(0 < size <= MAX_BLOCK, f"Invalid logical size {size}")
    if comp == 2:
        require(len(data) == size, "Uncompressed physical/logical size mismatch")
        return data
    if 5 <= comp <= 13:
        dec = zlib.decompressobj()
        try:
            out = dec.decompress(data, size + 1)
        except zlib.error as exc:
            raise Corrupt(f"Invalid ZFS gzip/zlib stream: {exc}") from exc
        require(len(out) == size and dec.eof and not dec.unconsumed_tail,
                "Wrong gzip output size or unfinished stream")
        return out
    if comp == 15:
        count = unpack(">I", data)[0]
        require(0 < count <= len(data) - 4, "Invalid ZFS LZ4 big-endian length")
        src = memoryview(data)[4:4 + count]
        # Pure Python raw LZ4 decoder, bounded by the BP's logical size.
        out = bytearray()
        pos = 0

        def length(initial):
            nonlocal pos
            total = initial
            if initial == 15:
                while True:
                    require(pos < len(src), "Truncated LZ4 length")
                    n = src[pos]
                    pos += 1
                    total += n
                    require(total <= size, "LZ4 length exceeds logical block")
                    if n != 255:
                        break
            return total

        while pos < len(src):
            token = src[pos]
            pos += 1
            n = length(token >> 4)
            require(pos + n <= len(src) and len(out) + n <= size, "Invalid LZ4 literal")
            out.extend(src[pos:pos + n])
            pos += n
            if pos == len(src):
                break
            require(pos + 2 <= len(src), "Missing LZ4 match offset")
            distance = int.from_bytes(src[pos:pos + 2], "little")
            pos += 2
            n = length(token & 15) + 4
            require(0 < distance <= len(out) and len(out) + n <= size, "Invalid LZ4 match")
            # Doubling copies correctly handle overlapping matches.
            while n:
                take = min(n, distance)
                out.extend(out[-distance:][:take])
                n -= take
        require(len(out) == size, "Wrong LZ4 output size")
        return bytes(out)
    if comp == 3:  # LZJB
        out = bytearray()
        pos = 0
        while len(out) < size:
            require(pos < len(data), "Missing LZJB copy map")
            cmap = data[pos]
            pos += 1
            for bit in range(8):
                if len(out) == size:
                    break
                if cmap & (1 << bit):
                    require(pos + 2 <= len(data), "Truncated LZJB match")
                    n = (data[pos] >> 2) + 3
                    distance = ((data[pos] << 8) | data[pos + 1]) & 1023
                    pos += 2
                    require(0 < distance <= len(out), "Invalid LZJB offset")
                    for _ in range(min(n, size - len(out))):
                        out.append(out[-distance])
                else:
                    require(pos < len(data), "Truncated LZJB literal")
                    out.append(data[pos])
                    pos += 1
        return bytes(out)
    if comp == 14:  # ZLE, parameter 64
        out = bytearray()
        pos = 0
        while len(out) < size:
            require(pos < len(data), "Truncated ZLE")
            n = data[pos] + 1
            pos += 1
            if n <= 64:
                require(pos + n <= len(data), "Truncated ZLE literal")
                out.extend(data[pos:pos + n])
                pos += n
            else:
                out.extend(bytes(n - 64))
            require(len(out) <= size, "ZLE output too large")
        return bytes(out)
    raise Unsupported(f"Compression {comp} unsupported; no guessing/decompression bypass")


class XDR:
    """Bounded decoder of the XDR nvlist fields needed for vdev configuration."""
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, n):
        require(0 <= n <= len(self.data) - self.pos, "Truncated XDR")
        result = self.data[self.pos:self.pos + n]
        self.pos += n
        return result

    def uint(self):
        return struct.unpack(">I", self.read(4))[0]

    def string(self):
        n = self.uint()
        s = self.read(n)
        self.read((-n) % 4)
        return s.decode("utf-8", "surrogateescape")

    def nvlist(self, depth=0):
        require(depth <= 16, "Excessively nested nvlist")
        require(self.uint() == 0, "Unsupported nvlist version")
        self.uint()  # nvflag
        values = {}
        for _ in range(8192):
            start = self.pos
            encoded, decoded = self.uint(), self.uint()
            if encoded == decoded == 0:
                return values
            end = start + encoded
            require(encoded >= 20 and decoded > 0 and end <= len(self.data), "Invalid nvpair length")
            name, typ, count = self.string(), self.uint(), self.uint()
            require(count <= 8192 and name not in values, "Invalid/duplicate nvpair")
            if typ == 1 and count == 0:
                value = True
            elif typ == 9 and count == 1:
                value = self.string()
            elif typ == 8 and count == 1:
                value = struct.unpack(">Q", self.read(8))[0]
            elif typ in (2, 3, 4, 5, 6, 21, 22, 23) and count == 1:
                value = self.uint()
            elif typ == 19 and count == 1:
                value = self.nvlist(depth + 1)
            elif typ == 20:
                value = [self.nvlist(depth + 1) for _ in range(count)]
            elif typ == 16:
                require(self.uint() == count, "Wrong XDR array length")
                value = [struct.unpack(">Q", self.read(8))[0] for _ in range(count)]
            else:
                value = {"unsupported_nvtype": typ, "count": count}
                self.pos = end
            require(self.pos == end, f"Unexpected nvpair length: {name}")
            values[name] = value
        raise Corrupt("Too many nvlist entries")


def parse_nvlist(data):
    require(len(data) >= 4 and data[0] == 1, "Only XDR label nvlists supported")
    return XDR(data[4:]).nvlist()
