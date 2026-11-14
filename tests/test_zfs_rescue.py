"""Synthetic on-disk fixtures and fault injection; never uses physical disks.

Fixtures are independently encoded from the documented OpenZFS field layouts.
They are NOT images produced by a running ZFS implementation.
"""
import contextlib
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid
import zlib

from zfs_reader import base, blocks, objects, commands
from zfs_reader import sources as source_io
from zfs_reader import extract as extraction


def packed_string(value):
    value = value.encode() if isinstance(value, str) else value
    return struct.pack(">I", len(value)) + value + bytes((-len(value)) % 4)


def nvl(values):
    result = bytearray(struct.pack(">II", 0, 1))
    for name, value in values.items():
        if isinstance(value, str):
            typ, count, payload = 9, 1, packed_string(value)
        elif isinstance(value, int):
            typ, count, payload = 8, 1, struct.pack(">Q", value)
        elif isinstance(value, dict):
            typ, count, payload = 19, 1, nvl(value)
        else:
            typ, count, payload = 20, len(value), b"".join(nvl(x) for x in value)
        body = packed_string(name) + struct.pack(">II", typ, count) + payload
        result.extend(struct.pack(">II", len(body) + 8, len(body) + 16) + body)
    return bytes(result) + bytes(8)


def seal_label(data, offset, endian="<"):
    result = bytearray(data)
    struct.pack_into(endian + "Q", result, len(result) - 40, 0x210DA7AB10C7A11)
    struct.pack_into(endian + "4Q", result, len(result) - 32, offset, 0, 0, 0)
    digest = hashlib.sha256(result).digest()
    result[-32:] = struct.pack(endian + "4Q", *struct.unpack(">4Q", digest))
    return bytes(result)


def micro(values, size=512, endian="<"):
    data = bytearray(size)
    struct.pack_into(endian + "Q", data, 0, (1 << 63) + 3)
    for i, (key, value) in enumerate(values.items()):
        if isinstance(key, str):
            key = key.encode()
        if len(key) > 49 or (i + 2) * 64 > size:
            raise ValueError("fixture micro ZAP overflow")
        off = (i + 1) * 64
        struct.pack_into(endian + "Q", data, off, value)
        data[off + 14:off + 14 + len(key)] = key
    return bytes(data)


def fat_leaf(values, endian="<", size=512):
    data = bytearray(size)
    struct.pack_into(endian + "Q", data, 0, 1 << 63)
    struct.pack_into(endian + "I", data, 24, 0x2AB1EAF)
    struct.pack_into(endian + "H", data, 30, len(values))
    nhash, start = size // 32, 48 + size // 16
    data[48:start] = b"\xff" * (start - 48)
    next_chunk = 0

    def allocate_array(payload):
        nonlocal next_chunk
        first = next_chunk
        for off in range(0, len(payload), 21):
            index = next_chunk
            next_chunk += 1
            pos = start + index * 24
            if pos + 24 > size:
                raise ValueError("fixture fat ZAP overflow")
            data[pos] = 251
            part = payload[off:off + 21]
            data[pos + 1:pos + 1 + len(part)] = part
            struct.pack_into(endian + "H", data, pos + 22,
                             next_chunk if off + 21 < len(payload) else 65535)
        return first

    for bucket, (name, (width, vals)) in enumerate(values.items()):
        name = name.encode() if isinstance(name, str) else name
        index = next_chunk
        next_chunk += 1
        name_index = allocate_array(name + b"\0")
        val_index = allocate_array(b"".join(v.to_bytes(width, "big") for v in vals))
        pos = start + index * 24
        struct.pack_into(endian + "BB5HIQ", data, pos, 252, width, 65535,
                         name_index, len(name) + 1, val_index, len(vals), 0, 0)
        struct.pack_into(endian + "H", data, 48 + bucket * 2, index)
    return bytes(data)


def fat_header(nleaves, nentries, pointers=(1,), endian="<", size=512):
    data = bytearray(size)
    struct.pack_into(endian + "2Q", data, 0, (1 << 63) + 1, 0x2F52AB2AB)
    shift = (len(pointers) - 1).bit_length()
    struct.pack_into(endian + "Q", data, 32, shift)
    struct.pack_into(endian + "2Q", data, 64, nleaves, nentries)
    for i, pointer in enumerate(pointers):
        struct.pack_into(endian + "Q", data, size // 2 + i * 8, pointer)
    return bytes(data)


def dnode(typ, pointers=(), bonus=b"", bonustype=0, blocksize=512,
          endian="<", levels=1, maxblkid=None, slots=1, spill=None):
    data = bytearray(512 * slots)
    data[:8] = bytes((typ, 9, levels, len(pointers), bonustype, 8, 2, 4 if spill else 0))
    struct.pack_into(endian + "HHB", data, 8, blocksize // 512, len(bonus), slots - 1)
    struct.pack_into(endian + "Q", data, 16, max(0, len(pointers) - 1) if maxblkid is None else maxblkid)
    for i, pointer in enumerate(pointers):
        data[64 + 128 * i:192 + 128 * i] = pointer
    begin = 64 + 128 * len(pointers)
    data[begin:begin + len(bonus)] = bonus
    if spill:
        data[-128:] = spill
    return bytes(data)


def embedded(payload, endian="<"):
    prop = ((len(payload) - 1) | ((len(payload) - 1) << 25) | (2 << 32) |
            (1 << 39) | (19 << 48) | ((endian == "<") << 63))
    words = [0] * 16
    stream = payload + bytes(112 - len(payload))
    j = 0
    for i in range(16):
        if i in (6, 10):
            continue
        words[i] = int.from_bytes(stream[j:j + 8], "little")
        j += 8
    words[6], words[10] = prop, 80
    return struct.pack(endian + "16Q", *words)


ATTRS = [("ZPL_MODE", 42, 8), ("ZPL_SIZE", 7, 8), ("ZPL_LINKS", 100, 8),
         ("ZPL_UID", 9, 8), ("ZPL_GID", 12, 8), ("ZPL_MTIME", 99, 16), ("ZPL_SYMLINK", 63, 0)]


def sa(mode, size, endian="<", target=None, layout=None):
    layout = layout if layout is not None else (3 if target is not None else 2)
    data = struct.pack(endian + "IHH", 0x2F505A, (1 << 10) | layout, len(target) if target is not None else 0)
    data += struct.pack(endian + "7Q", mode, size, 2, 1000, 1000, 1700000000, 123)
    if target is not None:
        data += target
    return data


class Fixture:
    def __init__(self, directory, endian="<", partition_offset=0):
        self.directory, self.endian, self.offset = Path(directory), endian, partition_offset
        self.size = 16 << 20
        self.next = 4 << 20
        self.chunks = []
        self.payload_offsets = {}
        self.build()
        self.paths = []
        for leaf in (111, 222):
            path = self.directory / f"disk-{leaf}.img"
            self.paths.append(path)
            with path.open("wb") as file:
                file.truncate(self.size + self.offset)
                for offset, data in self.chunks:
                    file.seek(self.offset + offset)
                    file.write(data)
                self.labels(file, leaf)

    def put(self, logical, typ=19, level=0, raw=None, comp=2):
        physical = logical if raw is None else raw
        physical += bytes((-len(physical)) % 512)
        logical_size = len(logical)
        if comp == 2:
            logical_size = len(physical)
        offset = self.next
        self.next += (len(physical) + 4095) & ~4095
        self.chunks.append((offset, physical))
        words = [0] * 16
        words[0] = ((len(physical) + 4095) & ~4095) // 512
        words[1] = (offset - (4 << 20)) // 512
        words[6] = ((logical_size // 512 - 1) | ((len(physical) // 512 - 1) << 16) |
                    (comp << 32) | (8 << 40) | (typ << 48) | (level << 56) |
                    ((self.endian == "<") << 63))
        words[10], words[11] = 80, 1
        words[12:] = struct.unpack(">4Q", hashlib.sha256(physical).digest())
        return struct.pack(self.endian + "16Q", *words), offset

    def put_bp(self, *args, **kwargs):
        return self.put(*args, **kwargs)[0]

    def objectset(self, nodes, typ):
        table = bytearray(16384)
        for number, node in nodes.items():
            table[number * 512:number * 512 + len(node)] = node
        tablebp = self.put_bp(bytes(table), typ=10)
        result = bytearray(1024)
        result[:512] = dnode(10, [tablebp], blocksize=16384, endian=self.endian)
        struct.pack_into(self.endian + "Q", result, 704, typ)
        return self.put_bp(bytes(result), typ=11)

    def build(self):
        e = self.endian
        zero = bytes(128)
        # A valid raw LZ4 sequence: one literal, 506 matched As, five trailing As.
        compressed = b"\x1fA\x01\x00\xff\xe8\x50AAAAA"
        lz4raw = struct.pack(">I", len(compressed)) + compressed
        abp, aoff = self.put(b"A" * 512, raw=lz4raw, comp=15)
        self.payload_offsets["hello"] = aoff
        bbp, boff = self.put(b"binary")
        self.payload_offsets["binary"] = boff
        fbp = self.put_bp(b"F" * 512)
        gbp = self.put_bp(b"G" * 512)
        indbp = self.put_bp(fbp + gbp + zero * 2, level=1)
        nodes = {}
        nodes[1] = dnode(21, [self.put_bp(micro({"ROOT": 2, "SA_ATTRS": 10}, endian=e), typ=21)], endian=e)
        rootzap = micro({"folder": 3, "alias": 4, "link": 6, "short": 7, "sparse": 8,
                         "empty": 9, "indirect": 13, "large-dnode": 15}, size=1024, endian=e)
        nodes[2] = dnode(20, [self.put_bp(rootzap, typ=20)], sa(stat.S_IFDIR | 0o755, 8, e),
                         44, 1024, e)
        folderzap = micro({"hello.txt": 4, b"\xff.bin": 5}, endian=e)
        nodes[3] = dnode(20, [self.put_bp(folderzap, typ=20)], sa(stat.S_IFDIR | 0o755, 2, e), 44, endian=e)
        nodes[4] = dnode(19, [abp], sa(stat.S_IFREG | 0o644, 512, e), 44, endian=e)
        legacy = bytearray(264)
        struct.pack_into(e + "4Q", legacy, 72, stat.S_IFREG | 0o644, 6, 3, 1)
        nodes[5] = dnode(19, [bbp], bytes(legacy), 17, endian=e)
        target = b"../../outside"
        nodes[6] = dnode(19, [zero], sa(stat.S_IFLNK | 0o777, len(target), e, target), 44, endian=e)
        nodes[7] = dnode(19, [embedded(b"inline payload", e)], sa(stat.S_IFREG | 0o644, 14, e), 44, endian=e)
        nodes[8] = dnode(19, [fbp, zero, gbp], sa(stat.S_IFREG | 0o644, 1536, e), 44, endian=e)
        nodes[9] = dnode(19, [zero], sa(stat.S_IFREG | 0o644, 0, e), 44, endian=e)
        nodes[10] = dnode(45, [self.put_bp(micro({"REGISTRY": 11, "LAYOUTS": 12}, endian=e), typ=45)], endian=e)
        registry = {name: attrid | (length << 24) for name, attrid, length in ATTRS}
        nodes[11] = dnode(46, [self.put_bp(micro(registry, endian=e), typ=46)], endian=e)
        layouts = {"2": (2, [a[1] for a in ATTRS[:-1]]), "3": (2, [a[1] for a in ATTRS])}
        nodes[12] = dnode(47, [self.put_bp(fat_header(1, 2, endian=e), typ=47),
                              self.put_bp(fat_leaf(layouts, endian=e), typ=47)], endian=e)
        nodes[13] = dnode(19, [indbp], sa(stat.S_IFREG | 0o644, 1024, e), 44,
                          endian=e, levels=2, maxblkid=1)
        spill = self.put_bp(sa(stat.S_IFREG | 0o644, 14, e).ljust(512, b"\0"), typ=44)
        nodes[15] = dnode(19, [embedded(b"large + spill!", e)], bonustype=44,
                          endian=e, slots=2, spill=spill)
        # Keep size consistent with the 14-byte payload above.
        self.fs_bp = self.objectset(nodes, 2)
        dslroot = bytearray(256)
        struct.pack_into(e + "Q", dslroot, 8, 6)
        struct.pack_into(e + "Q", dslroot, 32, 3)
        dsltarget = bytearray(256)
        struct.pack_into(e + "Q", dsltarget, 8, 5)
        dsbonus = bytearray(320)
        dsbonus[128:256] = self.fs_bp
        mosnodes = {
            1: dnode(1, [self.put_bp(micro({"root_dataset": 2}, endian=e), typ=1)], endian=e),
            2: dnode(12, [zero], bytes(dslroot), 12, endian=e),
            3: dnode(13, [self.put_bp(micro({"rescue": 4}, endian=e), typ=13)], endian=e),
            4: dnode(12, [zero], bytes(dsltarget), 12, endian=e),
            5: dnode(16, [zero], bytes(dsbonus), 16, endian=e),
            6: dnode(16, [zero], bytes(dsbonus), 16, endian=e),
        }
        self.root_bp = self.objectset(mosnodes, 1)
        self.bad_root_bp, off = self.put(bytes(1024), typ=11)
        self.chunks[-1] = off, b"broken" + bytes(1018)  # corrupt newest MOS, matching BP kept

    def labels(self, file, leaf):
        tree = {"type": "mirror", "id": 0, "guid": 777, "ashift": 12,
                "children": [{"type": "disk", "id": 0, "guid": 111},
                             {"type": "disk", "id": 1, "guid": 222}]}
        config = {"name": "tank", "pool_guid": 555, "guid": leaf, "top_guid": 777,
                  "txg": 101, "vdev_children": 1, "vdev_tree": tree}
        for base in (0, 256 << 10, self.size - (512 << 10), self.size - (256 << 10)):
            raw = b"\x01\x01\0\0" + nvl(config)
            raw = raw.ljust(112 << 10, b"\0")
            file.seek(self.offset + base + (16 << 10))
            file.write(seal_label(raw, base + (16 << 10), self.endian))
            for slot, txg, bp in ((0, 100, self.root_bp), (1, 101, self.bad_root_bp)):
                raw = bytearray(4096)
                struct.pack_into(self.endian + "5Q", raw, 0, 0xBAB10C, 5000, txg, 0, 1700000000)
                raw[40:168] = bp
                ub_off = base + (128 << 10) + slot * 4096
                file.seek(self.offset + ub_off)
                file.write(seal_label(raw, ub_off, self.endian))

    def source_args(self):
        return ["--source", str(self.paths[0]), "--source", str(self.paths[1]),
                "--offset", str(self.offset), "--offset", str(self.offset),
                "--length", str(self.size), "--length", str(self.size)]

    def damage(self, index, offset, value=b"corrupt"):
        with self.paths[index].open("r+b") as file:
            file.seek(self.offset + offset)
            file.write(value)


class RescueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def run_cli(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = commands.cli(args)
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_fletcher_reference_vectors_and_sha_endianness(self):
        for endian in ("<", ">"):
            self.assertEqual(base.checksum(struct.pack(endian + "4I", 1, 2, 3, 4), 7, endian), (10, 20, 35, 56))
            self.assertEqual(base.checksum(struct.pack(endian + "4Q", 1, 2, 3, 4), 6, endian), (4, 6, 5, 8))
            self.assertEqual(base.checksum(b"abc", 8, endian), struct.unpack(">4Q", hashlib.sha256(b"abc").digest()))
        with self.assertRaises(base.Unsupported):
            base.checksum(bytes(512), 2, "<")

    def test_lz4_against_system_library_and_bounded_invalid_streams(self):
        raw = b"\x1fA\x01\x00\xff\xe8\x50AAAAA"
        encoded = struct.pack(">I", len(raw)) + raw + bytes(20)
        self.assertEqual(base.decompress(encoded, 15, 512), b"A" * 512)
        name = ctypes.util.find_library("lz4")
        if name:
            lib = ctypes.CDLL(name)
            lib.LZ4_decompress_safe.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            dst = ctypes.create_string_buffer(512)
            self.assertEqual(lib.LZ4_decompress_safe(raw, dst, len(raw), 512), 512)
            self.assertEqual(dst.raw, b"A" * 512)
        for bad in (b"\0\0\x10\0abc", b"\0\0\0\3\0\0\0", b"\0\0\0\1\xf0"):
            with self.assertRaises(base.Corrupt):
                base.decompress(bad, 15, 512)
        self.assertEqual(base.decompress(zlib.compress(b"hi" * 256), 5, 512), b"hi" * 256)
        with self.assertRaises(base.Corrupt):
            base.decompress(zlib.compress(bytes(4096)), 5, 512)

    def test_uber_checksum_depends_on_slot_offset_and_whole_slot(self):
        for endian in ("<", ">"):
            raw = seal_label(bytes(4096), 131072, endian)
            self.assertEqual(base.verify_label(raw, 131072), endian)
            with self.assertRaises(base.Corrupt):
                base.verify_label(raw, 132096)
            changed = bytearray(raw)
            changed[2048] = 1
            with self.assertRaises(base.Corrupt):
                base.verify_label(bytes(changed), 131072)

    def test_embedded_payload_word_order_both_endiannesses(self):
        for endian in ("<", ">"):
            payload = bytes(range(112))
            bp = blocks.BP(embedded(payload, endian), endian)
            self.assertEqual(bp.psize, 112)
            self.assertEqual(bp.lsize, 112)
            self.assertEqual(bp.payload(), payload)

    def test_end_to_end_tree_sa_legacy_sparse_hardlink_and_old_txg(self):
        fixture = Fixture(self.directory)
        # Corrupt Disk 1 silently: a successful pread must not suppress Disk 2.
        fixture.damage(0, fixture.payload_offsets["hello"])
        before = [hashlib.sha256(p.read_bytes()).digest() for p in fixture.paths]
        output = self.directory / "recovery"
        code, rows = self.run_cli(["extract", *fixture.source_args(), "--dataset", "tank/rescue", "--dest", str(output)])
        self.assertEqual(code, 0, rows)
        tree = output / "tree"
        self.assertEqual((tree / "folder/hello.txt").read_bytes(), b"A" * 512)
        self.assertEqual((tree / "short").read_bytes(), b"inline payload")
        self.assertEqual((tree / "large-dnode").read_bytes(), b"large + spill!")
        self.assertEqual((tree / "indirect").read_bytes(), b"F" * 512 + b"G" * 512)
        self.assertEqual((tree / "sparse").read_bytes(), b"F" * 512 + bytes(512) + b"G" * 512)
        self.assertEqual((tree / "empty").stat().st_size, 0)
        self.assertEqual(os.readlink(tree / "link"), "../../outside")
        self.assertEqual((tree / "alias").stat().st_ino, (tree / "folder/hello.txt").stat().st_ino)
        self.assertEqual(Path(os.fsdecode(os.fsencode(tree / "folder") + b"/\xff.bin")).read_bytes(), b"binary")
        self.assertEqual(next(row["txg"] for row in rows if row.get("event") == "selected"), 100)
        self.assertGreater(rows[-1]["reader"].get("replica_fallbacks", 0), 0)
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).digest() for p in fixture.paths])
        manifest = [json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()]
        full = next(row for row in manifest if row.get("path") == "alias")
        self.assertEqual(full["sha256"], hashlib.sha256(b"A" * 512).hexdigest())

    def test_big_endian_and_partition_relative_offsets(self):
        fixture = Fixture(self.directory, endian=">", partition_offset=2 << 20)
        output = self.directory / "recovery"
        code, rows = self.run_cli(["extract", *fixture.source_args(), "--dataset", "tank/rescue", "--dest", str(output)])
        self.assertEqual(code, 0, rows)
        self.assertEqual((output / "tree/short").read_bytes(), b"inline payload")
        self.assertEqual((output / "tree/alias").read_bytes(), b"A" * 512)

    def test_exact_txg_never_falls_back(self):
        fixture = Fixture(self.directory)
        code, rows = self.run_cli(["check", *fixture.source_args(), "--dataset", "tank/rescue", "--txg", "101"])
        self.assertEqual(code, 1)
        self.assertFalse(any(row.get("event") == "selected" for row in rows))

    def test_unrecoverable_file_is_quarantined_with_ranges(self):
        fixture = Fixture(self.directory)
        for index in (0, 1):
            fixture.damage(index, fixture.payload_offsets["binary"])
        output = self.directory / "partial"
        code, _ = self.run_cli(["extract", *fixture.source_args(), "--dataset", "tank/rescue", "--dest", str(output)])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.lexists(os.fsencode(output / "tree/folder") + b"/\xff.bin"))
        rows = [json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()]
        partial = next(row for row in rows if row.get("status") == "partial")
        self.assertEqual(partial["missing_bytes"], 6)
        self.assertEqual((output / partial["partial_file"]).stat().st_size, 6)
        error = next(row for row in rows if row.get("event") == "unreadable_range")
        self.assertEqual((error["offset"], error["length"]), (0, 6))

    def test_source_readonly_bounds_short_read_and_io_fallback(self):
        fixture = Fixture(self.directory)
        with contextlib.ExitStack() as stack:
            sources = [source_io.Source(str(p)) for p in fixture.paths]
            for s in sources:
                stack.callback(s.close)
                self.assertEqual(fcntl.fcntl(s.fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
                with self.assertRaises(OSError) as cm:
                    os.write(s.fd, b"x")
                self.assertEqual(cm.exception.errno, errno.EBADF)
            with self.assertRaises(base.ReadError):
                sources[0].read(-1, 512)
            real = os.pread

            def bad_first(fd, count, offset):
                if fd == sources[0].fd:
                    raise OSError(errno.EIO, "injected")
                return real(fd, count, offset)

            with patch.object(source_io.os, "pread", side_effect=bad_first):
                reader = blocks.BlockReader(sources, 0, 100)
                self.assertEqual(len(reader.read(blocks.BP(fixture.root_bp, "<")).data), 1024)
                self.assertEqual(reader.stats["replica_fallbacks"], 1)
            with patch.object(source_io.os, "pread", return_value=b"short"):
                with self.assertRaises(base.ReadError):
                    sources[0].read(0, 512)

    def test_complementary_bad_sectors_and_no_false_zero_fill(self):
        fixture = Fixture(self.directory)
        bp = blocks.BP(fixture.root_bp, "<")
        physical = (4 << 20) + list(bp.dvas())[0][1]
        sources = [source_io.Source(str(p)) for p in fixture.paths]
        try:
            real = os.pread

            def broken(fd, count, offset):
                bad = physical + (0 if fd == sources[0].fd else 512)
                if offset < bad + 512 and offset + count > bad:
                    raise OSError(errno.EIO, "injected bad sector")
                return real(fd, count, offset)

            with patch.object(source_io.os, "pread", side_effect=broken):
                with self.assertRaises(base.ReadError):
                    blocks.BlockReader(sources, 0, 100).read(bp)
                reader = blocks.BlockReader(sources, 0, 100, sector_recovery=True)
                self.assertEqual(len(reader.read(bp).data), 1024)
                self.assertEqual(reader.stats["sector_assemblies"], 1)
        finally:
            for s in sources:
                s.close()

    def test_ddrescue_map_rejects_unknown_ranges(self):
        path = self.directory / "rescue.map"
        path.write_text("# ddrescue\n0x0 + 1\n0x0 0x200 +\n0x200 0x200 -\n0x400 0x200 +\n")
        m = source_io.RescueMap(path)
        self.assertTrue(m.covers(0, 512))
        self.assertFalse(m.covers(0, 1024))
        self.assertFalse(m.covers(512, 512))
        self.assertTrue(m.covers(1024, 512))

    def test_path_traversal_existing_destination_and_symlink_traversal(self):
        for name in (b"..", b".", b"a/b", b"\0", b""):
            with self.assertRaises(base.Corrupt):
                base.checked_name(name)
        dest = extraction.Destination(self.directory / "out", [])
        try:
            os.symlink(str(self.directory), "escape", dir_fd=dest.treefd)
            with self.assertRaises(OSError):
                with dest.directory((b"escape",)):
                    self.fail("followed recovered symlink")
            with self.assertRaises(FileExistsError):
                extraction.Destination(self.directory / "out", [])
        finally:
            dest.close()

    def test_fat_zap_cycles_rejected_and_other_leaves_survive(self):
        good = fat_leaf({"valid": (8, [99])})
        damaged = bytearray(good)
        # entry 0 points to itself in hash chain
        struct.pack_into("<H", damaged, 48 + 32 + 2, 0)
        with self.assertRaises(base.Corrupt):
            objects.parse_zap_leaf(blocks.Block(bytes(damaged), "<"))

        class Node:
            blocksize = 512
            maxblkid = 2

            def block(self, number):
                if number == 0:
                    return blocks.Block(fat_header(2, 2, (1, 2)), "<")
                if number == 1:
                    return blocks.Block(good, "<")
                raise base.ReadError("lost leaf")

        errors = []
        self.assertEqual(objects.read_zap(Node(), errors), {b"valid": (99,)})
        self.assertEqual(len(errors), 1)
        with self.assertRaises(base.ReadError):
            objects.read_zap(Node())

    def test_list_and_no_matching_txg(self):
        fixture = Fixture(self.directory)
        code, rows = self.run_cli(["list", *fixture.source_args()])
        self.assertEqual(code, 0)
        self.assertIn({"dataset": "tank/rescue", "dsl_object": 5}, rows)
        code, _ = self.run_cli(["scan", *fixture.source_args(), "--txg", "1"])
        self.assertEqual(code, 1)

    def test_other_dva_fallback_and_unsupported_bp_rejection(self):
        fixture = Fixture(self.directory)
        sources = [source_io.Source(str(p)) for p in fixture.paths]
        try:
            words = list(struct.unpack("<16Q", fixture.root_bp))
            bad = list(struct.unpack("<16Q", fixture.bad_root_bp))
            words[2:4] = words[0:2]
            words[0:2] = bad[0:2]
            bp = blocks.BP(struct.pack("<16Q", *words), "<")
            self.assertEqual(len(blocks.BlockReader(sources, 0, 100).read(bp).data), 1024)
            for bit, exception in ((61, base.Unsupported),):
                raw = bytearray(fixture.root_bp)
                struct.pack_into("<Q", raw, 48, base.u64(raw, 48) | (1 << bit))
                with self.assertRaises(exception):
                    blocks.BlockReader(sources, 0, 100).read(blocks.BP(bytes(raw), "<"))
            raw = bytearray(fixture.root_bp)
            struct.pack_into("<Q", raw, 8, base.u64(raw, 8) | (1 << 63))
            with self.assertRaisesRegex(base.ReadError, "Gang"):
                blocks.BlockReader(sources, 0, 100).read(blocks.BP(bytes(raw), "<"))
            raw = bytearray(fixture.root_bp)
            prop = (base.u64(raw, 48) & ~(255 << 40)) | (2 << 40)
            struct.pack_into("<Q", raw, 48, prop)
            with self.assertRaises(base.Unsupported):
                blocks.BlockReader(sources, 0, 100).read(blocks.BP(bytes(raw), "<"))
        finally:
            for source in sources:
                source.close()

    def test_duplicate_member_identity_is_rejected(self):
        fixture = Fixture(self.directory)
        with fixture.paths[1].open("r+b") as file:
            fixture.labels(file, 111)
        code, _ = self.run_cli(["scan", *fixture.source_args()])
        self.assertEqual(code, 1)

    def test_gpt_crc_backup_and_partition_geometry(self):
        fixture = Fixture(self.directory, partition_offset=2 << 20)
        path = fixture.paths[0]
        total = 19 << 20
        last = total // 512 - 1
        entries = bytearray(16384)
        entries[:16] = uuid.UUID("516e7cba-6ecf-11d6-8ff8-00022d09712b").bytes_le
        entries[16:32] = uuid.uuid4().bytes_le
        struct.pack_into("<2Q", entries, 32, fixture.offset // 512,
                         (fixture.offset + fixture.size) // 512 - 1)
        entries[56:62] = "zfs".encode("utf-16le")
        with path.open("r+b") as file:
            file.truncate(total)
            for current, backup, table_lba in ((1, last, 2), (last, 1, last - 32)):
                header = bytearray(512)
                header[:8] = b"EFI PART"
                struct.pack_into("<III", header, 8, 0x10000, 92, 0)
                struct.pack_into("<4Q", header, 24, current, backup, 34, last - 33)
                struct.pack_into("<QIII", header, 72, table_lba, 128, 128, zlib.crc32(entries))
                struct.pack_into("<I", header, 16, zlib.crc32(header[:92]))
                file.seek(current * 512)
                file.write(header)
                file.seek(table_lba * 512)
                file.write(entries)
            # Primary damaged; the independently verified backup must survive.
            file.seek(512 + 16)
            file.write(bytes(4))
        source = source_io.Source(path)
        try:
            result = commands.probe_gpt(source, 512)
            self.assertEqual(len(result["valid_gpt_copies"]), 1)
            partition = result["valid_gpt_copies"][0]["partitions"][0]
            self.assertEqual((partition["offset"], partition["length"]), (fixture.offset, fixture.size))
            self.assertIn("CRC", result["failed_gpt_copies"][0]["error"])
        finally:
            source.close()

    def test_output_enospc_aborts_without_publishing_file(self):
        fixture = Fixture(self.directory)
        output = self.directory / "full"
        with patch.object(extraction, "write_all", side_effect=OSError(errno.ENOSPC, "injected full destination")):
            code, _ = self.run_cli(["extract", *fixture.source_args(), "--dataset", "tank/rescue", "--dest", str(output)])
        self.assertEqual(code, 1)
        self.assertFalse((output / "tree/alias").exists())
        rows = [json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["event"], "aborted")

    def test_lzjb_zle_and_embedded_compressed_sizes(self):
        # Two independent minimal historical compression vectors.
        self.assertEqual(base.decompress(b"\0abcdefgh", 3, 8), b"abcdefgh")
        self.assertEqual(base.decompress(bytes([63]) + b"X" * 64 + bytes([255, 255, 127]), 14, 512),
                         b"X" * 64 + bytes(448))
        raw = b"\x1fA\x01\x00\xff\xe8\x50AAAAA"
        payload = struct.pack(">I", len(raw)) + raw
        words = list(struct.unpack("<16Q", embedded(payload)))
        words[6] = (511 | ((len(payload) - 1) << 25) | (15 << 32) | (1 << 39) | (19 << 48) | (1 << 63))
        bp = blocks.BP(struct.pack("<16Q", *words), "<")
        self.assertEqual((bp.lsize, bp.psize), (512, len(payload)))
        self.assertEqual(blocks.BlockReader([], 0, 100).read(bp).data, b"A" * 512)


    def test_packaged_entry_point_from_other_directory(self):
        fixture = Fixture(self.directory)
        fixture.damage(0, fixture.payload_offsets["hello"])
        before = [hashlib.sha256(p.read_bytes()).digest() for p in fixture.paths]
        output = self.directory / "subprocess-output"
        entry = Path(__file__).resolve().parent.parent / "zfs_rescue.py"
        # Generic names in the caller's cwd must not shadow package modules.
        for name in ("base", "sources", "blocks", "scan", "objects", "pool", "extract", "commands"):
            (self.directory / (name + ".py")).write_text("raise RuntimeError('module collision')\n")
        result = subprocess.run(
            [sys.executable, str(entry), "extract", *fixture.source_args(),
             "--dataset", "tank/rescue", "--dest", str(output)],
            cwd=self.directory, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((output / "tree/folder/hello.txt").read_bytes(), b"A" * 512)
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).digest() for p in fixture.paths])
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertGreater(rows[-1]["reader"].get("replica_fallbacks", 0), 0)

if __name__ == "__main__":
    unittest.main()
