"""Portable logic tests. POSIX disk/output integration lives in test_zfs_rescue."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import struct
import sys
import tempfile
import tracemalloc
import unittest
from collections import Counter
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from zfs_reader import commands, extract as extraction
from zfs_reader.base import Corrupt, MAX_ENTRIES, ReadError, ResourceLimit
from zfs_reader.blocks import Block
from zfs_reader.browse import browse
from zfs_reader.objects import read_zap
from zfs_reader.selection import PendingPaths, Selection, relative_parts, resolve_path
from zfs_reader.pool import PoolView
from zfs_reader.scan import scan
from test_zfs_rescue import Fixture


def posix_decode(value):
    return value.decode("utf-8", "surrogateescape") if isinstance(value, bytes) else os.fsdecode(value)


class FakeNode:
    def __init__(self, fs, number, mode, data=b"", entries=None, links=1):
        self.fs, self.number = fs, number
        self.type = 20 if stat.S_ISDIR(mode) else 19
        self.attrs = dict(mode=mode, size=len(data), links=links, uid=1000, gid=1000)
        self.entries = entries if entries is not None else {}
        self.data, self.blocksize = data, 4
        self.bad_blocks = set()

    def block(self, number, metadata=False):
        if metadata:
            raise AssertionError("Fake data blocks must not be used as metadata")
        self.fs.payload_reads.append(self.number)
        if number in self.bad_blocks:
            raise ReadError("injected bad data block")
        return Block(self.data[number * 4:(number + 1) * 4], "<")

    def read_range(self, start, count):
        raise AssertionError("Browsing must not read symlink targets")


class FakeFilesystem:
    def __init__(self):
        self.root, self.os = 1, self
        self.nodes, self.node_reads, self.payload_reads, self.zap_reads = {}, [], [], []
        self.add(1, stat.S_IFDIR, entries={b"Fotos": (2,), b"Fotos_backup": (3,),
                                        b"Documentos": (4,), b"escape": (9,), b"empty": (10,)})
        self.add(2, stat.S_IFDIR, entries={b"uno.txt": (5,), b"viaje": (6,), b"alias": (5,)})
        self.add(3, stat.S_IFDIR, entries={b"broken": (8,)})
        self.add(4, stat.S_IFDIR, entries={b"nota.txt": (7,)})
        self.add(5, stat.S_IFREG, data=b"photodata", links=2)
        self.add(6, stat.S_IFDIR, entries={b"dos.txt": (11,)})
        self.add(7, stat.S_IFREG, data=b"document")
        self.add(8, stat.S_IFREG, data=b"bad data").bad_blocks.add(0)
        self.add(9, stat.S_IFLNK, data=b"/outside")
        self.add(10, stat.S_IFDIR)
        self.add(11, stat.S_IFREG, data=b"second photo")

    def add(self, number, mode, **kwargs):
        self.nodes[number] = node = FakeNode(self, number, mode, **kwargs)
        return node

    def node(self, number):
        self.node_reads.append(number)
        return self.nodes[number]

    def zap(self, number, errors=None, max_entries=MAX_ENTRIES):
        self.zap_reads.append(number)
        entries = self.nodes[number].entries
        if len(entries) > max_entries:
            raise ResourceLimit("injected directory entry limit")
        return dict(entries)

    def attributes(self, node):
        return dict(node.attrs)


class MemoryOutput:
    """Only test double for POSIX writes; not a Windows recovery implementation."""
    O_NOFOLLOW = O_CLOEXEC = 0
    fsdecode = staticmethod(posix_decode)

    def __init__(self):
        self.partsfd = (b"incomplete",)
        self.files, self.handles, self.rows = {}, {}, []
        self.dirs = {(b"tree",)}
        self.fd = 100

    def __getattr__(self, name):
        return getattr(os, name)

    @contextlib.contextmanager
    def directory(self, parts):
        key = (b"tree",) + parts
        assert key in self.dirs, key
        yield key

    def emit(self, **row):
        self.rows.append(row)

    def mkdir(self, name, mode, dir_fd):
        key = dir_fd + (name,)
        assert key not in self.dirs
        self.dirs.add(key)

    def open(self, name, flags, mode, dir_fd):
        key = dir_fd + (name,)
        assert key not in self.files
        self.files[key] = io.BytesIO()
        self.fd += 1
        self.handles[self.fd] = self.files[key]
        return self.fd

    def write(self, fd, data):
        return self.handles[fd].write(data)

    def lseek(self, fd, offset, whence):
        return self.handles[fd].seek(offset, whence)

    def ftruncate(self, fd, size):
        file = self.handles[fd]
        if size > len(file.getvalue()):
            file.seek(size - 1)
            file.write(b"\0")
        file.truncate(size)

    def fsync(self, fd):
        pass

    def utime(self, fd, ns):
        assert fd in self.handles

    def close(self, fd):
        pass

    def link(self, src, dst, src_dir_fd, dst_dir_fd, follow_symlinks):
        assert not follow_symlinks
        key = dst_dir_fd + (dst,)
        assert key not in self.files
        self.files[key] = self.files[src_dir_fd + (src,)]

    def unlink(self, name, dir_fd):
        del self.files[dir_fd + (name,)]

    def symlink(self, target, name, dir_fd):
        self.files[dir_fd + (name,)] = target


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeFilesystem()

    def extract(self, paths):
        selected = Selection(paths)
        selected.validate(self.fs)
        output = MemoryOutput()
        with patch.object(extraction, "os", output):
            stats = extraction.extract(self.fs, output, selection=selected)
        return output, stats

    def test_relative_paths_reject_traversal_absolute_and_empty_components(self):
        for path in ("", "/Fotos", "..", "../Fotos", "Fotos/..", "Fotos//uno",
                     "./Fotos", "Fotos/./uno", "Fotos/\0bad", "Fotos//"):
            with self.subTest(path=path), self.assertRaises(Corrupt):
                relative_parts(path)
        self.assertEqual(relative_parts("."), ())
        self.assertEqual(relative_parts("Fotos/"), (b"Fotos",))
        self.assertEqual(relative_parts(b"\xff"), (b"\xff",))

    def test_prefixes_match_components_not_similar_names(self):
        selected = Selection(["Fotos/viaje"])
        for parts in ((), (b"Fotos",), (b"Fotos", b"viaje"), (b"Fotos", b"viaje", b"dos.txt")):
            self.assertTrue(selected.includes(parts))
        self.assertFalse(selected.includes((b"Fotos_backup",)))
        self.assertFalse(selected.includes((b"Fotos", b"uno.txt")))

    def test_overlapping_duplicate_and_root_selections(self):
        self.assertEqual(Selection(["Fotos/viaje", "Fotos", "Fotos"]).paths, [(b"Fotos",)])
        self.assertEqual(Selection([".", "Fotos"]).paths, [()])
        self.assertTrue(Selection().includes((b"anything",)))

    def test_symlink_and_regular_file_cannot_be_intermediate_directories(self):
        for path in ("escape/file", "Fotos/uno.txt/file"):
            with self.subTest(path=path), self.assertRaises(Corrupt):
                Selection([path]).validate(self.fs)
        self.assertEqual(self.fs.payload_reads, [])

    def test_missing_path_is_not_silently_an_empty_selection(self):
        with self.assertRaises(Corrupt):
            Selection(["missing"]).validate(self.fs)

    def test_selection_resolves_non_utf8_bytes(self):
        self.fs.nodes[1].entries[b"\xff"] = (7,)
        self.assertEqual(resolve_path(self.fs, relative_parts(b"\xff")), 7)

    def test_selected_folder_only_and_hardlinks_preserved(self):
        output, stats = self.extract(["Fotos"])
        self.assertEqual(output.files[(b"tree", b"Fotos", b"uno.txt")].getvalue(), b"photodata")
        self.assertIs(output.files[(b"tree", b"Fotos", b"uno.txt")],
                      output.files[(b"tree", b"Fotos", b"alias")])
        self.assertEqual(output.files[(b"tree", b"Fotos", b"viaje", b"dos.txt")].getvalue(), b"second photo")
        self.assertNotIn(3, self.fs.node_reads)
        self.assertNotIn(4, self.fs.node_reads)
        self.assertNotIn(8, self.fs.payload_reads)
        self.assertFalse(stats.get("errors"))
        self.assertEqual(stats["hardlinks"], 1)
        self.assertEqual(output.rows[-1]["sha256"], hashlib.sha256(b"second photo").hexdigest())

    def test_nested_selection_creates_only_ancestors_and_selected_content(self):
        output, _ = self.extract(["Fotos/viaje"])
        self.assertIn((b"tree", b"Fotos"), output.dirs)
        self.assertEqual(set(output.files), {(b"tree", b"Fotos", b"viaje", b"dos.txt")})
        self.assertNotIn(5, self.fs.payload_reads)

    def test_multiple_paths_and_individual_files(self):
        output, stats = self.extract(["Fotos/uno.txt", "Documentos"])
        self.assertEqual(set(output.files), {(b"tree", b"Fotos", b"uno.txt"),
                                             (b"tree", b"Documentos", b"nota.txt")})
        self.assertEqual(stats["complete_files"], 2)

    def test_selected_empty_directory_is_created(self):
        output, stats = self.extract(["empty"])
        self.assertIn((b"tree", b"empty"), output.dirs)
        self.assertFalse(output.files)
        self.assertFalse(stats.get("errors"))

    def test_selected_damaged_file_remains_partial(self):
        output, stats = self.extract(["Fotos_backup"])
        self.assertEqual(stats["partial_files"], 1)
        self.assertNotIn((b"tree", b"Fotos_backup", b"broken"), output.files)
        self.assertTrue(any(row.get("event") == "unreadable_range" for row in output.rows))
        self.assertTrue(any(row.get("status") == "partial" for row in output.rows))

    def test_unfiltered_extraction_still_visits_all_supported_paths(self):
        # The test double cannot read an external symlink's target, so provide its SA value.
        original = self.fs.attributes
        self.fs.attributes = lambda node: ({**original(node), "symlink": b"/outside"}
                                           if node.number == 9 else original(node))
        output, stats = self.extract(None)
        self.assertIn((b"tree", b"Documentos", b"nota.txt"), output.files)
        self.assertIn((b"tree", b"Fotos", b"uno.txt"), output.files)
        self.assertEqual(stats["partial_files"], 1)
        self.assertEqual(output.files[(b"tree", b"escape")], b"/outside")

    def test_pending_path_budget_released_on_pop(self):
        pending = PendingPaths(max_bytes=400)
        pending.push(1, (b"x",), 1)
        with self.assertRaises(ResourceLimit):
            pending.push(2, (b"long" * 100,), 1)
        self.assertEqual(pending.pop(), (1, (b"x",), 1))
        self.assertEqual(pending.bytes, 0)
        self.assertFalse(pending)
        pending.push(1, (), 0)

    def test_large_unselected_tree_does_not_queue_or_read_sibling_nodes(self):
        self.fs.nodes[1].entries.update({f"unused-{i}".encode(): (1000 + i,) for i in range(20_000)})
        tracemalloc.start()
        try:
            output, _ = self.extract(["empty"])
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 32 << 20)
        self.assertEqual(set(self.fs.node_reads), {1, 10})
        self.assertFalse(output.files)


class BrowseTests(unittest.TestCase):
    def setUp(self):
        self.fs, self.rows = FakeFilesystem(), []

    def browse(self, **kwargs):
        return browse(self.fs, lambda **row: self.rows.append(row), **kwargs)

    def test_shallow_metadata_only_does_not_descend_or_read_payloads(self):
        stats = self.browse()
        self.assertEqual(self.fs.zap_reads, [1])
        self.assertEqual(self.fs.payload_reads, [])
        self.assertEqual({row["path"] for row in self.rows},
                         {".", "Fotos", "Fotos_backup", "Documentos", "escape", "empty"})
        self.assertFalse(stats.get("errors"))

    def test_recursive_listing_reports_logical_sizes_without_reading_bad_data(self):
        stats = self.browse(recursive=True)
        self.assertTrue(any(row["path"] == "Fotos_backup/broken" for row in self.rows))
        self.assertEqual(self.fs.payload_reads, [])
        self.assertEqual(stats["files"], 5)
        self.assertEqual(stats["logical_file_bytes"], 46)  # Includes both hardlink paths.
        self.assertFalse(stats.get("errors"))

    def test_subdirectory_and_directories_only(self):
        self.browse(path="Fotos", directories_only=True)
        self.assertEqual([row["path"] for row in self.rows], ["Fotos", "Fotos/viaje"])
        self.assertNotIn(3, self.fs.node_reads)

    def test_cycle_is_reported_not_followed_forever(self):
        self.fs.nodes[6].entries[b"loop"] = (2,)
        stats = self.browse(path="Fotos", recursive=True)
        self.assertEqual(stats["errors"], 1)
        self.assertTrue(any(row["event"] == "browse_error" for row in self.rows))

    def test_browse_rejects_file_symlink_and_missing_path(self):
        for path in ("Fotos/uno.txt", "escape", "missing"):
            with self.subTest(path=path), self.assertRaises(Corrupt):
                self.browse(path=path)

    def test_directory_and_global_limits_abort_instead_of_claiming_success(self):
        with self.assertRaises(ResourceLimit):
            self.browse(max_dir_entries=2)
        with self.assertRaises(Corrupt):
            self.browse(max_entries=1)


class ParserAndZapTests(unittest.TestCase):
    def test_help_is_portable_and_documents_selection(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            commands.cli(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--include", out.getvalue())
        self.assertIn("browse", out.getvalue())

    def test_invalid_flag_combinations_fail_before_sources_are_opened(self):
        cases = [
            ["extract", "--dataset", "vol1/DATA", "--dest", "new", "--include", "Fotos"],
            ["browse", "--dataset", "vol1/DATA", "--include", "Fotos", "--txg", "1"],
            ["check", "--dataset", "vol1/DATA", "--path", "Fotos"],
            ["browse", "--dataset", "vol1/DATA", "--max-dir-entries", "0"],
            ["browse", "--dataset", "vol1/DATA", "--max-entries", "0"],
            ["browse", "--dataset", "vol1/DATA", "--max-dir-entries", "2000001"],
        ]
        for case in cases:
            with self.subTest(case=case), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    commands.cli([*case, "--source", "DO-NOT-OPEN"])
                self.assertEqual(caught.exception.code, 2)

    def test_micro_zap_limit_is_enforced(self):
        data = bytearray(512)
        struct.pack_into("<Q", data, 0, (1 << 63) + 3)
        for i, name in enumerate((b"one", b"two")):
            offset = 64 * (i + 1)
            struct.pack_into("<Q", data, offset, i + 1)
            data[offset + 14:offset + 14 + len(name)] = name
        node = SimpleNamespace(blocksize=512, maxblkid=0, block=lambda _: Block(bytes(data), "<"))
        self.assertEqual(len(read_zap(node, max_entries=2)), 2)
        with self.assertRaises(ResourceLimit):
            read_zap(node, [], max_entries=1)

    def test_fat_zap_declared_limit_checked_before_leaf_reads(self):
        data = bytearray(512)
        struct.pack_into("<2Q", data, 0, (1 << 63) + 1, 0x2F52AB2AB)
        struct.pack_into("<2Q", data, 64, 1, 100_001)
        reads = []

        def block(number):
            reads.append(number)
            self.assertEqual(number, 0)
            return Block(bytes(data), "<")

        node = SimpleNamespace(blocksize=512, maxblkid=1, block=block)
        with self.assertRaises(ResourceLimit):
            read_zap(node, max_entries=100_000)
        self.assertEqual(reads, [0])


class CommandFlowTests(unittest.TestCase):
    """Exercise CLI wiring with explicit source/destination doubles, not real devices."""
    def setUp(self):
        self.fs = FakeFilesystem()
        self.uber = SimpleNamespace(txg=100, public=lambda: dict(txg=100, root_id="testroot"))
        self.info = dict(pool="vol1", pool_guid=555, top_guid=777, vdev_id=0)
        self.source = SimpleNamespace(st=SimpleNamespace(st_dev=1, st_ino=2, st_rdev=0),
                                      path="fake-image", stats={}, close=lambda: None)
        self.source_module = ModuleType("zfs_reader.sources")
        self.source_module.Source = lambda *args: self.source
        self.view = SimpleNamespace(dataset=lambda name: self.fs, uber=self.uber,
                                    reader=SimpleNamespace(stats={}))

    def run_cli(self, args, destination=None):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"zfs_reader.sources": self.source_module}))
            stack.enter_context(patch.object(commands, "os", SimpleNamespace(name="posix", fsdecode=os.fsdecode)))
            stack.enter_context(patch.object(commands, "scan", return_value=(self.info, [self.uber])))
            stack.enter_context(patch.object(commands, "PoolView", return_value=self.view))
            if destination is not None:
                stack.enter_context(patch.object(commands, "Destination", return_value=destination))
                stack.enter_context(patch.object(extraction, "os", destination))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = commands.cli([*args, "--source", "fake-image", "--dataset", "vol1/DATA"])
        return code, out.getvalue()

    def test_browse_jsonl_and_human_modes(self):
        code, text = self.run_cli(["browse"])
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(rows[-1]["status"], "metadata_listed")
        code, text = self.run_cli(["browse", "--human", "--directories-only"])
        self.assertEqual(code, 0)
        self.assertIn("TXG: 100  root-id: testroot", text)
        self.assertIn('"Fotos"', text)
        self.assertNotIn('"escape"', text)

    def test_invalid_selection_fails_before_destination_creation(self):
        with patch.object(commands, "Destination") as destination:
            code, _ = self.run_cli(["extract", "--txg", "100", "--include", "missing", "--dest", "new"])
        self.assertEqual(code, 1)
        destination.assert_not_called()

    def test_selective_cli_records_scope_and_selected_paths(self):
        destination = MemoryOutput()
        destination.path = "new"
        destination.close = lambda *args: None
        code, text = self.run_cli(["extract", "--txg", "100", "--include", "Fotos/viaje",
                                   "--dest", "new"], destination)
        self.assertEqual(code, 0, text)
        run = destination.rows[0]
        self.assertEqual(run["scope"], "selected_paths")
        self.assertEqual(run["included_paths"], [{"path": "Fotos/viaje",
                                                   "path_bytes_hex": b"Fotos/viaje".hex()}])
        self.assertEqual(destination.rows[-1]["scope"], "selected_paths")
        self.assertEqual(set(destination.files), {(b"tree", b"Fotos", b"viaje", b"dos.txt")})

    def test_resource_limit_does_not_fall_back_to_an_older_txg(self):
        code, text = self.run_cli(["browse", "--max-dir-entries", "1"])
        self.assertEqual(code, 1)
        self.assertFalse(any(json.loads(line).get("event") == "selected" for line in text.splitlines()))


class ReadOnlyImage:
    """Portable test adapter only. Production Source/OS safety are not replaced."""
    def __init__(self, path, offset=0, length=None):
        self.path = str(path)
        self.file = open(path, "rb")
        self.offset = offset
        self.size = os.fstat(self.file.fileno()).st_size - offset if length is None else length
        self.label_size = self.size - self.size % (256 << 10)
        self.stats = Counter()

    def read(self, offset, count):
        if offset < 0 or count <= 0 or offset + count > self.size:
            raise ReadError("outside test image")
        self.file.seek(self.offset + offset)
        data = self.file.read(count)
        if len(data) != count:
            raise ReadError("short test image read")
        self.stats["read_calls"] += 1
        self.stats["bytes_read"] += count
        return data

    def close(self):
        self.file.close()


class SyntheticImageTests(unittest.TestCase):
    """Real format/checksum/replica/selection logic with portable image/output I/O."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def view(self, fixture):
        sources = [ReadOnlyImage(path, fixture.offset, fixture.size) for path in fixture.paths]
        for source in sources:
            self.addCleanup(source.close)
        info, candidates = scan(sources)
        return PoolView(sources, info, next(ub for ub in candidates if ub.txg == 100))

    def extract(self, view, paths):
        fs = view.dataset("tank/rescue")
        selected = Selection(paths)
        selected.validate(fs)
        output = MemoryOutput()
        with patch.object(extraction, "os", output):
            stats = extraction.extract(fs, output, selection=selected)
        return output, stats

    def test_real_metadata_browsing_does_not_require_file_contents(self):
        fixture = Fixture(self.temp.name)
        for index in (0, 1):
            fixture.damage(index, fixture.payload_offsets["hello"])
            fixture.damage(index, fixture.payload_offsets["binary"])
        before = [hashlib.sha256(p.read_bytes()).digest() for p in fixture.paths]
        view, rows = self.view(fixture), []
        with patch.object(extraction, "os", SimpleNamespace(fsdecode=posix_decode)):
            stats = browse(view.dataset("tank/rescue"), lambda **row: rows.append(row), recursive=True)
        self.assertFalse(stats.get("errors"))
        self.assertTrue(any(row["path"] == "folder/hello.txt" for row in rows))
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).digest() for p in fixture.paths])

    def test_real_selective_extraction_and_replica_checksum_fallback(self):
        fixture = Fixture(self.temp.name)
        fixture.damage(0, fixture.payload_offsets["hello"])
        before = [hashlib.sha256(p.read_bytes()).digest() for p in fixture.paths]
        view = self.view(fixture)
        output, stats = self.extract(view, ["folder", "alias"])
        self.assertFalse(stats.get("errors"))
        self.assertEqual(output.files[(b"tree", b"folder", b"hello.txt")].getvalue(), b"A" * 512)
        self.assertEqual(output.files[(b"tree", b"folder", b"\xff.bin")].getvalue(), b"binary")
        self.assertIs(output.files[(b"tree", b"alias")], output.files[(b"tree", b"folder", b"hello.txt")])
        self.assertNotIn((b"tree", b"short"), output.files)
        self.assertGreater(view.reader.stats["replica_fallbacks"], 0)
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).digest() for p in fixture.paths])

    def test_real_corrupt_unselected_payload_does_not_fail_selection(self):
        fixture = Fixture(self.temp.name)
        for index in (0, 1):
            fixture.damage(index, fixture.payload_offsets["binary"])
        output, stats = self.extract(self.view(fixture), ["folder/hello.txt"])
        self.assertFalse(stats.get("errors"))
        self.assertEqual(set(output.files), {(b"tree", b"folder", b"hello.txt")})

    def test_real_corrupt_selected_payload_is_quarantined(self):
        fixture = Fixture(self.temp.name)
        for index in (0, 1):
            fixture.damage(index, fixture.payload_offsets["binary"])
        output, stats = self.extract(self.view(fixture), ["folder"])
        self.assertEqual(stats["partial_files"], 1)
        self.assertNotIn((b"tree", b"folder", b"\xff.bin"), output.files)
        partial = next(row for row in output.rows if row.get("status") == "partial")
        self.assertEqual(partial["missing_bytes"], 6)

    def test_real_big_endian_and_partition_offset_with_selection(self):
        fixture = Fixture(self.temp.name, endian=">", partition_offset=2 << 20)
        output, stats = self.extract(self.view(fixture), ["short"])
        self.assertFalse(stats.get("errors"))
        self.assertEqual(output.files[(b"tree", b"short")].getvalue(), b"inline payload")

    def test_real_full_extraction_remains_available(self):
        fixture = Fixture(self.temp.name)
        output, stats = self.extract(self.view(fixture), None)
        self.assertFalse(stats.get("errors"))
        self.assertEqual(output.files[(b"tree", b"sparse")].getvalue(),
                         b"F" * 512 + bytes(512) + b"G" * 512)
        self.assertEqual(output.files[(b"tree", b"indirect")].getvalue(), b"F" * 512 + b"G" * 512)
        self.assertEqual(output.files[(b"tree", b"link")], b"../../outside")


if __name__ == "__main__":
    unittest.main()
