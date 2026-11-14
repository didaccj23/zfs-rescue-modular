"""Dataset-relative selection and bounded traversal; never resolve host paths."""
from __future__ import annotations

import os
import stat
from bisect import bisect_right

from .base import ResourceLimit, checked_name, require


MAX_DIR_ENTRIES = 100_000
MAX_PENDING_BYTES = 64 << 20


def relative_parts(path):
    raw = os.fsencode(path)
    require(bool(raw) and not raw.startswith(b"/"), "Use a nonempty dataset-relative path")
    if raw == b".":
        return ()
    # A trailing slash is convenient, but empty/interior dot components are unsafe.
    raw = raw[:-1] if raw.endswith(b"/") else raw
    parts = tuple(raw.split(b"/"))
    require(len(parts) <= 256, "Selected path exceeds depth limit")
    for part in parts:
        checked_name(part)
    return parts


def resolve_path(fs, parts, max_dir_entries=MAX_DIR_ENTRIES):
    """Resolve verified directory entries. Intermediate symlinks are never followed."""
    number = fs.root
    seen = set()
    for part in parts:
        node = fs.os.node(number)
        attrs = fs.attributes(node)
        require(node.type == 20 and stat.S_ISDIR(attrs["mode"]),
                "Selected path crosses a file or symlink, not a directory")
        require(number not in seen, "Cycle in selected path")
        seen.add(number)
        entries = fs.os.zap(number, max_entries=max_dir_entries)
        value = entries.get(part)
        require(isinstance(value, tuple) and len(value) == 1,
                f"Selected path not found or unreadable (raw bytes): {b'/'.join(parts)!r}")
        number = value[0] & ((1 << 48) - 1)
        require(number != 0, "Zero directory object number")
    return number


class Selection:
    def __init__(self, paths=None):
        requested = sorted(set(relative_parts(path) for path in paths)) if paths else [()]
        self.paths = []
        for parts in requested:
            # Sorted paths keep an ancestor directly before its descendants.
            if not self.paths or parts[:len(self.paths[-1])] != self.paths[-1]:
                self.paths.append(parts)

    def includes(self, parts):
        """Include a selected subtree and the directories leading to it, not siblings."""
        position = bisect_right(self.paths, parts)
        if position and parts[:len(self.paths[position - 1])] == self.paths[position - 1]:
            return True
        return position < len(self.paths) and self.paths[position][:len(parts)] == parts

    def validate(self, fs, max_dir_entries=MAX_DIR_ENTRIES):
        for parts in self.paths:
            number = resolve_path(fs, parts, max_dir_entries)
            node = fs.os.node(number)
            attrs = fs.attributes(node)
            require(stat.S_ISDIR(attrs["mode"]) or stat.S_ISREG(attrs["mode"]) or stat.S_ISLNK(attrs["mode"]),
                    "Selected path is not a supported directory, file or symlink")


class PendingPaths:
    """Bound pending path bookkeeping, independently of dataset or file size."""
    def __init__(self, max_bytes=MAX_PENDING_BYTES):
        self.items = []
        self.bytes = 0
        self.max_bytes = max_bytes

    def __bool__(self):
        return bool(self.items)

    def push(self, number, parts, depth):
        require(depth <= 256, "Directory depth limit exceeded")
        cost = 128 + sum(64 + len(part) for part in parts)
        if self.bytes + cost > self.max_bytes:
            raise ResourceLimit("Pending path memory budget exceeded; browse/extract a smaller subtree")
        self.items.append((number, parts, depth, cost))
        self.bytes += cost

    def pop(self):
        number, parts, depth, cost = self.items.pop()
        self.bytes -= cost
        return number, parts, depth
