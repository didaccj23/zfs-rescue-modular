from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid
from collections import Counter

from .base import LOG, MAX_ENTRIES, RescueError, ResourceLimit, Unsupported, checked_name, require
from .selection import MAX_DIR_ENTRIES, PendingPaths, Selection

def device_ancestors(dev):
    """Linux block device ancestry, including partitions and stacked devices."""
    pending, seen = [dev], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        path = Path(f"/sys/dev/block/{os.major(current)}:{os.minor(current)}")
        if not path.exists():
            continue
        path = path.resolve()
        candidates = list((path / "slaves").glob("*")) if (path / "slaves").exists() else []
        if (path / "partition").exists():
            candidates.append(path.parent)
        for child in candidates:
            try:
                major, minor = map(int, (child / "dev").read_text().strip().split(":"))
                pending.append(os.makedev(major, minor))
            except (OSError, ValueError):
                raise Unsupported("Cannot determine destination block device ancestry")
    return seen


class Destination:
    """New private run directory; all writes are anchored to owned directory FDs."""
    def __init__(self, path, sources):
        requested = os.path.abspath(path)
        parent, name = os.path.split(requested)
        checked_name(os.fsencode(name))
        parent = os.path.realpath(parent)
        self.path = os.path.join(parent, name)
        parentfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.fds = []
        try:
            destination_dev = os.fstat(parentfd).st_dev
            raw_sources = [s for s in sources if not stat.S_ISREG(s.st.st_mode)]
            if raw_sources and not sys.platform.startswith("linux"):
                raise Unsupported("Extract from images on this OS; raw-device destination ancestry check is Linux-only")
            destination_devices = device_ancestors(destination_dev)
            if raw_sources:
                require(Path(f"/sys/dev/block/{os.major(destination_dev)}:{os.minor(destination_dev)}").exists(),
                        "Cannot prove destination is separate from raw sources; use images")
            for source in raw_sources:
                require(not destination_devices & device_ancestors(source.st.st_rdev),
                        "Destination is on a source disk or a dependent device")
            # mkdir is exclusive. Never reuse a pre-existing output directory.
            os.mkdir(name, 0o700, dir_fd=parentfd)
            self.rootfd = self._track(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                              dir_fd=parentfd))
            for subdir in (b"tree", b"incomplete"):
                os.mkdir(subdir, 0o700, dir_fd=self.rootfd)
            self.treefd = self._track(os.open(b"tree", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                              dir_fd=self.rootfd))
            self.partsfd = self._track(os.open(b"incomplete", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                               dir_fd=self.rootfd))
            reportfd = os.open(b"manifest.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                               0o600, dir_fd=self.rootfd)
            self.report = os.fdopen(reportfd, "w", encoding="utf-8", buffering=1)
        except BaseException:
            for fd in reversed(self.fds):
                os.close(fd)
            raise
        finally:
            os.close(parentfd)

    def _track(self, fd):
        self.fds.append(fd)
        return fd

    def emit(self, **record):
        self.report.write(json.dumps(record, ensure_ascii=True) + "\n")

    @contextlib.contextmanager
    def directory(self, parts):
        fd = os.dup(self.treefd)
        try:
            for part in parts:
                checked_name(part)
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = nxt
            yield fd
        finally:
            os.close(fd)

    def close(self):
        try:
            self.report.flush()
            os.fsync(self.report.fileno())
        finally:
            self.report.close()
            for fd in reversed(self.fds):
                os.close(fd)


def path_record(parts):
    raw = b"/".join(parts)
    return {"path": os.fsdecode(raw) or ".", "path_bytes_hex": raw.hex()}


def write_all(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError(errno.EIO, "Short/zero output write")
        view = view[count:]


def extract(fs, dest, max_file_size=16 << 40, max_entries=MAX_ENTRIES,
            selection=None, max_dir_entries=MAX_DIR_ENTRIES):
    selection = selection if selection is not None else Selection()
    stats = Counter()
    stack = PendingPaths()
    stack.push(fs.root, (), 0)
    directories = set()
    hardlinks = {}
    hardlink_bytes = 0
    visited = 0
    while stack:
        number, parts, depth = stack.pop()
        visited += 1
        require(visited <= max_entries and depth <= 256, "Directory traversal limit exceeded")
        record = dict(object=number, **path_record(parts))
        try:
            dn = fs.os.node(number)
            attrs = fs.attributes(dn)
            record["attributes"] = {k: v for k, v in attrs.items() if k != "symlink"}
            mode, size = attrs["mode"], attrs["size"]
            if stat.S_ISDIR(mode):
                require(dn.type == 20 and number not in directories, "Invalid/cyclic directory object")
                directories.add(number)
                if parts:
                    with dest.directory(parts[:-1]) as parentfd:
                        os.mkdir(parts[-1], 0o700, dir_fd=parentfd)
                errors = []
                entries = fs.os.zap(number, errors, max_entries=max_dir_entries)
                for name, value in reversed(sorted(entries.items())):
                    child_parts = parts + (name,)
                    if not selection.includes(child_parts):
                        stats["selection_skipped_entries"] += 1
                        continue
                    try:
                        checked_name(name)
                        require(len(value) == 1, "Invalid directory ZAP value")
                        child = value[0] & ((1 << 48) - 1)
                        require(child != 0, "Zero directory object number")
                        stack.push(child, child_parts, depth + 1)
                    except ResourceLimit:
                        raise
                    except RescueError as exc:
                        errors.append(f"Entry {name!r}: {exc}")
                if errors:
                    stats["errors"] += 1
                    dest.emit(event="entry", status="directory_partial", errors=errors, **record)
                else:
                    dest.emit(event="entry", status="directory", **record)
                stats["directories"] += 1
            elif stat.S_ISREG(mode):
                require(dn.type == 19 and bool(parts), "Invalid regular file object")
                require(size <= max_file_size, f"File exceeds configured size limit: {size}")
                if number in hardlinks:
                    previous, digest = hardlinks[number]
                    with dest.directory(previous[:-1]) as srcfd, dest.directory(parts[:-1]) as dstfd:
                        os.link(previous[-1], parts[-1], src_dir_fd=srcfd, dst_dir_fd=dstfd, follow_symlinks=False)
                    stats["hardlinks"] += 1
                    dest.emit(event="entry", status="hardlink", target=os.fsdecode(b"/".join(previous)),
                              sha256=digest, **record)
                    continue
                temporary = f"{number}-{uuid.uuid4().hex}.part".encode()
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=dest.partsfd)
                digest = hashlib.sha256()
                missing = 0
                try:
                    require(size == 0 or dn.blocksize > 0, "Nonempty file without a block size")
                    for offset in range(0, size, dn.blocksize or 512):
                        count = min(dn.blocksize, size - offset)
                        try:
                            block = dn.block(offset // dn.blocksize, metadata=False)
                            if block is None:
                                # A validated hole pointer is the only implicit source of zeroes.
                                digest.update(bytes(count))
                                os.lseek(fd, count, os.SEEK_CUR)
                                stats["sparse_bytes"] += count
                            else:
                                require(len(block.data) >= count, "File data block is shorter than expected")
                                data = block.data[:count]
                                write_all(fd, data)
                                digest.update(data)
                        except RescueError as exc:
                            missing += count
                            os.lseek(fd, offset + count, os.SEEK_SET)
                            dest.emit(event="unreadable_range", offset=offset, length=count,
                                      error=str(exc), **path_record(parts), object=number)
                    os.ftruncate(fd, size)
                    if "mtime" in attrs:
                        seconds, nanos = attrs["mtime"]
                        if 0 <= nanos < 1_000_000_000 and seconds < (1 << 33):
                            ns = seconds * 1_000_000_000 + nanos
                            os.utime(fd, ns=(ns, ns))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if missing:
                    stats["errors"] += 1
                    stats["partial_files"] += 1
                    dest.emit(event="entry", status="partial", missing_bytes=missing,
                              partial_file="incomplete/" + os.fsdecode(temporary), **record)
                else:
                    cost = 0
                    if attrs.get("links", 1) > 1:
                        cost = 256 + sum(64 + len(part) for part in parts)
                        if hardlink_bytes + cost > 64 << 20:
                            raise ResourceLimit("Hardlink path memory budget exceeded; extract a smaller selection")
                    with dest.directory(parts[:-1]) as parentfd:
                        os.link(temporary, parts[-1], src_dir_fd=dest.partsfd, dst_dir_fd=parentfd,
                                follow_symlinks=False)
                    os.unlink(temporary, dir_fd=dest.partsfd)
                    result_hash = digest.hexdigest()
                    # Single-link files need no long-lived path bookkeeping.
                    if cost:
                        hardlinks[number] = parts, result_hash
                        hardlink_bytes += cost
                    stats["complete_files"] += 1
                    stats["complete_bytes"] += size
                    dest.emit(event="entry", status="complete", sha256=result_hash, **record)
                    LOG.info("Recovered %r (%d bytes)", record["path"], size)
            elif stat.S_ISLNK(mode):
                require(bool(parts) and dn.type == 19 and size <= 65536, "Invalid symlink")
                if "symlink" in attrs:
                    target = attrs["symlink"]
                else:
                    target = dn.read_range(0, size)
                require(len(target) == size and target and b"\0" not in target, "Invalid symlink target")
                with dest.directory(parts[:-1]) as parentfd:
                    os.symlink(target, parts[-1], dir_fd=parentfd)
                stats["symlinks"] += 1
                dest.emit(event="entry", status="symlink", target=os.fsdecode(target), target_hex=target.hex(), **record)
            else:
                raise Unsupported(f"Special inode mode {mode:o}; device nodes, sockets and FIFOs are not created")
        except ResourceLimit:
            raise
        except RescueError as exc:
            stats["errors"] += 1
            dest.emit(event="entry", status="failed", error=str(exc), **record)
            LOG.warning("Cannot recover %r: %s", record["path"], exc)
    return dict(stats)
