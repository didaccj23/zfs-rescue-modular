from __future__ import annotations

import bisect
import fcntl
import os
import stat
import struct
import sys
from collections import Counter

from .base import DATA_START, LABEL_SIZE, MAX_BLOCK, ReadError, Unsupported, require

class RescueMap:
    """ddrescue map positions are absolute within the input image/device."""
    def __init__(self, path):
        intervals = []
        end = 0
        with open(path, encoding="ascii") as file:
            for line in file:
                parts = line.split("#", 1)[0].split()
                if not parts:
                    continue
                if len(parts) == 3 and parts[1] in ("?", "*", "/", "-", "+"):
                    continue  # current_pos current_status current_pass
                require(len(parts) == 3 and parts[2] in "?*/-+", "Malformed ddrescue map")
                start, size = int(parts[0], 0), int(parts[1], 0)
                require(start >= end and size > 0, "Overlapping/invalid ddrescue ranges")
                end = start + size
                if parts[2] == "+":
                    if intervals and intervals[-1][1] == start:
                        intervals[-1] = (intervals[-1][0], end)
                    else:
                        intervals.append((start, end))
        self.intervals = intervals
        self.starts = [x[0] for x in intervals]

    def covers(self, start, size):
        i = bisect.bisect_right(self.starts, start) - 1
        return i >= 0 and start + size <= self.intervals[i][1]


class Source:
    def __init__(self, path, offset=0, length=None, mapfile=None):
        self.path = os.path.realpath(path)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            self.st = os.fstat(self.fd)
            if stat.S_ISREG(self.st.st_mode):
                total = self.st.st_size
            elif stat.S_ISBLK(self.st.st_mode) or stat.S_ISCHR(self.st.st_mode):
                buf = bytearray(8)
                if sys.platform.startswith("linux") and stat.S_ISBLK(self.st.st_mode):
                    fcntl.ioctl(self.fd, 0x80081272, buf, True)  # BLKGETSIZE64, read-only
                elif sys.platform.startswith("freebsd"):
                    fcntl.ioctl(self.fd, 0x40086481, buf, True)  # DIOCGMEDIASIZE
                else:
                    raise Unsupported("Device sizing supported on Linux/FreeBSD only")
                total = struct.unpack("=Q", buf)[0]
            else:
                raise Unsupported("Source must be a regular image or disk/partition device")
            self.offset = offset
            self.size = total - offset if length is None else length
            require(offset >= 0 and offset % 512 == 0 and self.size >= DATA_START + 2 * LABEL_SIZE
                    and offset + self.size <= total, "Invalid vdev offset/length")
            self.label_size = self.size - self.size % LABEL_SIZE
            self.map = RescueMap(mapfile) if mapfile else None
            self.stats = Counter()
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    def read(self, offset, length):
        if offset < 0 or not 0 < length <= MAX_BLOCK or offset + length > self.size:
            raise ReadError(f"Read outside vdev: {offset}+{length}")
        absolute = self.offset + offset
        if self.map and not self.map.covers(absolute, length):
            self.stats["map_rejections"] += 1
            raise ReadError(f"ddrescue map does not mark all bytes as rescued: {absolute}+{length}")
        self.stats["read_calls"] += 1
        try:
            data = os.pread(self.fd, length, absolute)
        except OSError as exc:
            self.stats["io_errors"] += 1
            raise ReadError(f"{self.path} at {absolute}+{length}: {exc}") from exc
        if len(data) != length:
            self.stats["short_reads"] += 1
            raise ReadError(f"Short read: {self.path} at {absolute}: {len(data)}/{length}")
        self.stats["bytes_read"] += length
        return data
