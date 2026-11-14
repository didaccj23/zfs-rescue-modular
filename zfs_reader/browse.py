"""Metadata-only directory browsing. No file data blocks or symlink targets are read."""
from __future__ import annotations

import stat
from collections import Counter

from .base import MAX_ENTRIES, RescueError, ResourceLimit, checked_name, require
from .extract import path_record
from .selection import MAX_DIR_ENTRIES, PendingPaths, relative_parts, resolve_path


def browse(fs, emit, path=".", recursive=False, directories_only=False,
           max_entries=MAX_ENTRIES, max_dir_entries=MAX_DIR_ENTRIES):
    start = relative_parts(path)
    root = resolve_path(fs, start, max_dir_entries)
    node = fs.os.node(root)
    require(node.type == 20 and stat.S_ISDIR(fs.attributes(node)["mode"]),
            "--path must name a real directory, not a file or symlink")
    pending = PendingPaths()
    pending.push(root, start, len(start))
    directories = set()
    stats = Counter()
    visited = 0
    while pending:
        number, parts, depth = pending.pop()
        visited += 1
        require(visited <= max_entries, "Directory traversal limit exceeded")
        record = dict(object=number, **path_record(parts))
        try:
            node = fs.os.node(number)
            attrs = fs.attributes(node)
            mode = attrs["mode"]
            if stat.S_ISDIR(mode):
                require(node.type == 20 and number not in directories, "Invalid/cyclic directory object")
                directories.add(number)
                kind = "directory"
                stats["directories"] += 1
            elif stat.S_ISREG(mode):
                require(node.type == 19, "Invalid regular file object")
                kind = "file"
                stats["files"] += 1
                stats["logical_file_bytes"] += attrs["size"]
            elif stat.S_ISLNK(mode):
                kind = "symlink"
                stats["symlinks"] += 1
            else:
                kind = "special"
                stats["special_entries"] += 1
            if not directories_only or kind == "directory":
                emit(event="entry", kind=kind,
                     logical_size=attrs["size"] if kind == "file" else None, **record)
            if kind == "directory" and (recursive or parts == start):
                errors = []
                entries = fs.os.zap(number, errors, max_entries=max_dir_entries)
                for name, value in reversed(sorted(entries.items())):
                    try:
                        checked_name(name)
                        require(len(value) == 1, "Invalid directory ZAP value")
                        child = value[0] & ((1 << 48) - 1)
                        require(child != 0, "Zero directory object number")
                        pending.push(child, parts + (name,), depth + 1)
                    except ResourceLimit:
                        raise
                    except RescueError as exc:
                        errors.append(f"Entry {name!r}: {exc}")
                for error in errors:
                    stats["errors"] += 1
                    emit(event="browse_error", error=error, **record)
        except ResourceLimit:
            raise
        except RescueError as exc:
            stats["errors"] += 1
            emit(event="browse_error", error=str(exc), **record)
    return dict(stats)
