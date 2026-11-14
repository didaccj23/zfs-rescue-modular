"""Terminal picker tests; portable screen doubles never access physical disks."""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from zfs_reader import commands, extract as extraction, tui
from zfs_reader.base import Corrupt, LOG, ResourceLimit, Unsupported
from zfs_reader.selection import Selection
import test_selection as support
from test_selection import FakeFilesystem, MemoryOutput, ReadOnlyImage
from test_zfs_rescue import Fixture


class Screen:
    def __init__(self, keys, size=(24, 100)):
        self.keys = iter(keys)
        self.size = size
        self.frames, self.lines = [], {}

    def getmaxyx(self):
        return self.size

    def keypad(self, enabled):
        self.keypad_enabled = enabled

    def erase(self):
        self.lines = {}

    def addstr(self, row, column, text, attribute=0):
        height, width = self.size
        assert 0 <= row < height and column == 0 and len(text) < width, (row, text)
        assert "\n" not in text and "\x1b" not in text and "\r" not in text, repr(text)
        self.lines[row] = text

    def refresh(self):
        self.frames.append("\n".join(self.lines.get(i, "") for i in range(self.size[0])))

    def get_wch(self):
        key = next(self.keys)  # Unexpected extra prompts fail rather than hang a test.
        if isinstance(key, BaseException):
            raise key
        if isinstance(key, tuple):
            self.size = key
            return Terminal.KEY_RESIZE
        return key


class Terminal:
    A_REVERSE = 1
    (KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_PPAGE, KEY_NPAGE, KEY_HOME,
     KEY_END, KEY_ENTER, KEY_BACKSPACE, KEY_RESIZE) = range(1001, 1012)

    class error(Exception):
        pass

    def __init__(self, keys, size=(24, 100)):
        self.screen = Screen(keys, size)
        self.restored = False

    def curs_set(self, visibility):
        pass

    def wrapper(self, function):
        try:
            return function(self.screen)
        finally:
            self.restored = True


class MarksTests(unittest.TestCase):
    def test_empty_selection_is_not_whole_dataset(self):
        marks = tui.Marks()
        with self.assertRaises(Corrupt):
            marks.selection()
        self.assertEqual(marks.state(()), " ")

    def test_whole_folder_covers_children_without_matching_siblings(self):
        marks = tui.Marks()
        marks.toggle((b"Fotos",))
        self.assertEqual(marks.state((b"Fotos",)), "x")
        self.assertEqual(marks.state((b"Fotos", b"uno.txt")), "*")
        self.assertEqual(marks.state((b"Fotos_backup",)), " ")
        self.assertEqual(marks.state(()), "-")
        self.assertIn("Desmarca", marks.toggle((b"Fotos", b"uno.txt")))
        self.assertEqual(marks.selection().paths, [(b"Fotos",)])

    def test_parent_replaces_marked_descendants_and_deselection_is_explicit(self):
        marks = tui.Marks()
        for path in ((b"Fotos", b"uno"), (b"Fotos", b"viaje", b"dos"), (b"Documents",)):
            marks.toggle(path)
        self.assertEqual(marks.state((b"Fotos",)), "-")
        marks.toggle((b"Fotos",))
        self.assertEqual(marks.paths, [(b"Documents",), (b"Fotos",)])
        self.assertEqual(marks.bytes, sum(marks.cost(parts) for parts in marks.paths))
        marks.toggle(())
        self.assertEqual(marks.paths, [()])
        self.assertEqual(marks.state((b"Documents",)), "*")
        marks.toggle(())
        self.assertEqual(marks.paths, [])
        self.assertEqual(marks.bytes, 0)

    def test_mark_budget_failure_preserves_previous_marks(self):
        marks = tui.Marks(max_bytes=400)
        marks.toggle((b"a",))
        with self.assertRaises(ResourceLimit):
            marks.toggle((b"b",))
        self.assertEqual(marks.paths, [(b"a",)])
        marks.toggle((b"a",))
        marks.toggle((b"b",))
        self.assertEqual(marks.paths, [(b"b",)])

    def test_selection_binary_search_matches_component_reference(self):
        requested = ["a/one", "a/two", "ab", "c/d", "c/d/e", "z/one/leaf"]
        selected = Selection(requested)
        self.assertEqual(selected.paths, [(b"a", b"one"), (b"a", b"two"), (b"ab",),
                                          (b"c", b"d"), (b"z", b"one", b"leaf")])
        components = (b"a", b"ab", b"c", b"d", b"one", b"two", b"z", b"leaf")
        paths = [(), *((a,) for a in components),
                 *((a, b) for a in components for b in components),
                 (b"z", b"one", b"leaf", b"file"), (b"c", b"d", b"f")]
        for parts in paths:
            with self.subTest(parts=parts):
                expected = any(parts[:len(path)] == path or path[:len(parts)] == parts for path in selected.paths)
                self.assertEqual(selected.includes(parts), expected)


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeFilesystem()
        self.picker = tui.Picker(self.fs)

    def focus(self, name):
        self.picker.cursor = next(i for i, (key, _) in enumerate(self.picker.entries, 1) if key == name)

    def test_loading_and_marking_folder_are_lazy_metadata_only(self):
        self.assertEqual(self.fs.node_reads, [1])
        self.assertEqual(self.fs.zap_reads, [1])
        self.focus(b"Fotos")
        self.picker.toggle()
        self.assertEqual(self.fs.zap_reads, [1])
        self.assertNotIn(5, self.fs.node_reads)
        self.assertEqual(self.fs.payload_reads, [])
        self.assertEqual(self.picker.marks.selection().paths, [(b"Fotos",)])

    def test_open_back_and_marks_across_directories(self):
        self.focus(b"Fotos")
        previous_cursor = self.picker.cursor
        self.picker.open()
        self.focus(b"uno.txt")
        self.picker.toggle()
        self.picker.back()
        self.assertEqual(self.picker.cursor, previous_cursor)
        self.focus(b"Documentos")
        self.picker.toggle()
        selected = self.picker.marks.selection()
        selected.validate(self.fs)
        output = MemoryOutput()
        with patch.object(extraction, "os", output):
            stats = extraction.extract(self.fs, output, selection=selected)
        self.assertFalse(stats.get("errors"))
        self.assertEqual(set(output.files), {(b"tree", b"Fotos", b"uno.txt"),
                                             (b"tree", b"Documentos", b"nota.txt")})

    def test_files_and_symlinks_cannot_open_or_read_targets(self):
        for path in ("Fotos/uno.txt", "escape"):
            with self.subTest(path=path):
                picker = tui.Picker(self.fs, path.rpartition("/")[0] or ".")
                name = path.rpartition("/")[2].encode()
                picker.cursor = next(i for i, (key, _) in enumerate(picker.entries, 1) if key == name)
                before = self.fs.zap_reads[:]
                self.assertIn("Només", picker.open())
                self.assertEqual(self.fs.zap_reads, before)
                picker.toggle()
                self.assertEqual(picker.marks.selection().paths, [tuple(path.encode().split(b"/"))])
        self.assertEqual(self.fs.payload_reads, [])

    def test_current_folder_and_empty_folder_can_be_selected(self):
        self.focus(b"empty")
        self.picker.open()
        self.assertEqual(len(self.picker), 1)
        self.assertEqual(self.picker.cursor, 0)
        self.picker.toggle()
        self.assertEqual(self.picker.marks.selection().paths, [(b"empty",)])
        self.picker.back()
        self.picker.cursor = 0
        self.picker.toggle()
        self.assertEqual(self.picker.marks.selection().paths, [()])

    def test_starting_path_and_ancestor_cycles(self):
        picker = tui.Picker(self.fs, "Fotos/viaje")
        self.assertEqual(picker.parts, (b"Fotos", b"viaje"))
        self.fs.nodes[6].entries[b"loop"] = (1,)
        picker.back()
        picker.open()
        picker.cursor = next(i for i, (key, _) in enumerate(picker.entries, 1) if key == b"loop")
        with self.assertRaisesRegex(Corrupt, "Cycle"):
            picker.open()
        self.assertEqual(picker.parts, (b"Fotos", b"viaje"))

    def test_invalid_starting_paths(self):
        for path in ("missing", "escape", "Fotos/uno.txt", "../Fotos"):
            with self.subTest(path=path), self.assertRaises(Corrupt):
                tui.Picker(self.fs, path)

    def test_unreadable_folder_leaves_previous_directory_and_marks_intact(self):
        self.focus(b"Fotos")
        self.picker.toggle()
        original = self.fs.zap

        def damaged(number, *args, **kwargs):
            if number == 2:
                raise Corrupt("injected unreadable directory")
            return original(number, *args, **kwargs)

        with patch.object(self.fs, "zap", side_effect=damaged), self.assertRaises(Corrupt):
            self.picker.open()
        self.assertEqual(self.picker.parts, ())
        self.assertEqual(self.picker.parents, [])
        self.assertEqual(self.picker.marks.paths, [(b"Fotos",)])

    def test_partial_directory_warning_is_not_silently_hidden(self):
        original = self.fs.zap

        def partial(number, errors=None, **kwargs):
            if number == 2:
                errors.append("injected missing leaf")
            return original(number, errors, **kwargs)

        with patch.object(self.fs, "zap", side_effect=partial):
            terminal = Terminal(["j", "\n", "q"])
            tui._loop(terminal.screen, terminal, self.picker, "pool/ds",
                      dict(txg=1, root_id="root"), "new")
        self.assertTrue(any("ATENCIÓ: 1 errors" in frame for frame in terminal.screen.frames))
        self.assertTrue(any("missing leaf" in frame for frame in terminal.screen.frames))

    def test_unsafe_name_invalid_value_special_and_bad_metadata_cannot_mark(self):
        for name, value in ((b"..", (5,)), (b"bad", (0,)), (b"bad", b"x"), (b"bad", (5, 6))):
            with self.subTest(name=name, value=value):
                self.fs.nodes[1].entries = {name: value}
                picker = tui.Picker(self.fs)
                self.assertEqual(picker.entry(1).kind, "error")
                with self.assertRaises(Corrupt):
                    picker.toggle()
        self.fs.nodes[1].entries = {b"bad": (5,)}
        self.fs.nodes[5].attrs["mode"] = 0
        picker = tui.Picker(self.fs)
        self.assertEqual(picker.entry(1).kind, "special")
        with self.assertRaises(Corrupt):
            picker.toggle()
        with patch.object(self.fs, "node", side_effect=Corrupt("bad node")):
            picker.cache.clear()
            self.assertEqual(picker.entry(1).kind, "error")

    def test_limits_abort_and_metadata_cache_is_bounded(self):
        with self.assertRaises(ResourceLimit):
            tui.Picker(self.fs, max_dir_entries=1)
        with self.assertRaises(ResourceLimit):
            tui.Picker(self.fs, max_entries=1)
        picker = tui.Picker(self.fs, max_entries=7)
        picker.cursor = next(i for i, (key, _) in enumerate(picker.entries, 1) if key == b"Fotos")
        with self.assertRaises(ResourceLimit):
            picker.open()
        self.assertEqual(picker.parts, ())
        self.fs.nodes[1].entries = {str(i).encode(): (5,) for i in range(1000)}
        picker = tui.Picker(self.fs)
        self.assertEqual(picker.cache, {})
        for i in range(1, len(picker)):
            picker.entry(i)
        self.assertEqual(len(picker.cache), tui.ENTRY_CACHE_SIZE)
        self.assertEqual(self.fs.payload_reads, [])

    def test_non_utf8_and_control_names_preserve_original_selection_bytes(self):
        name = b"\xff\n\x1b[2J"
        self.fs.nodes[1].entries = {name: (5,)}
        picker = tui.Picker(self.fs)
        picker.toggle()
        self.assertEqual(picker.marks.selection().paths, [(name,)])
        self.assertNotIn("\x1b", tui.path_label((name,)))
        self.assertNotIn("\n", tui.path_label((name,)))


class ScreenTests(unittest.TestCase):
    def choose(self, keys, **kwargs):
        terminal = Terminal(keys, kwargs.pop("size", (24, 100)))
        result = tui.choose(FakeFilesystem(), "vol1/DATA", dict(txg=100, root_id="testroot"),
                            "/healthy/new", terminal=terminal, **kwargs)
        self.assertTrue(terminal.restored)
        self.assertTrue(terminal.screen.keypad_enabled)
        return result, terminal.screen.frames

    def test_keys_open_mark_review_return_and_confirm_whole_folder(self):
        selected, frames = self.choose(["j", " ", "\n", "l", Terminal.KEY_LEFT, "r", "n", "r", "s"])
        self.assertEqual(selected.paths, [(b"Fotos",)])
        self.assertTrue(any("[*]" in frame for frame in frames))
        self.assertTrue(any("Només s'obren carpetes" in frame for frame in frames))
        self.assertTrue(any("SENCERES" in frame for frame in frames))
        self.assertTrue(any("TXG: 100" in frame and "testroot" in frame for frame in frames))

    def test_individual_file_keyboard_selection(self):
        selected, _ = self.choose(["j", "\n", "j", " ", "r", "s"])
        self.assertEqual(selected.paths, [(b"Fotos", b"uno.txt")])

    def test_empty_recovery_and_cancel_never_default_to_all_files(self):
        result, frames = self.choose(["r", "s", "q"])
        self.assertIsNone(result)
        self.assertTrue(any("No hi ha res marcat" in frame for frame in frames))
        result, _ = self.choose([" ", "r", "q"])
        self.assertIsNone(result)

    def test_escape_requires_confirmation_and_preserves_selection(self):
        selected, frames = self.choose(["j", " ", "\x1b", "n", "r", "s"])
        self.assertEqual(selected.paths, [(b"Fotos",)])
        self.assertTrue(any("Segur que vols sortir?" in frame for frame in frames))

        result, frames = self.choose(["j", " ", "\x1b", "s"])
        self.assertIsNone(result)
        self.assertTrue(any("Segur que vols sortir?" in frame for frame in frames))

    def test_paging_home_end_backspace_and_resize(self):
        selected, _ = self.choose([Terminal.KEY_END, Terminal.KEY_HOME, "j", "j",
                                   Terminal.KEY_RIGHT, Terminal.KEY_BACKSPACE,
                                   Terminal.KEY_PPAGE, Terminal.KEY_NPAGE, (18, 80),
                                   Terminal.KEY_HOME, " ", "r", Terminal.KEY_END,
                                   Terminal.KEY_HOME, "s"])
        self.assertEqual(selected.paths, [()])
        result, frames = self.choose([(10, 40), " ", (24, 100), "q"])
        self.assertIsNone(result)
        self.assertTrue(any("80x18" in frame for frame in frames))

    def test_confirmation_scrolls_all_selected_paths(self):
        picker = tui.Picker(FakeFilesystem())
        for i in range(30):
            picker.marks.toggle((f"file-{i:02}".encode(),))
        terminal = Terminal(["r", Terminal.KEY_NPAGE, Terminal.KEY_END, "s"], (18, 80))
        selected = tui._loop(terminal.screen, terminal, picker, "pool/ds",
                             dict(txg=1, root_id="root"), "new")
        self.assertEqual(len(selected.paths), 30)
        self.assertIn("'file-29'", terminal.screen.frames[-1])

    def test_interrupt_and_terminal_failure_restore_log_level_and_screen(self):
        level = LOG.level
        terminal = Terminal([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            tui.choose(FakeFilesystem(), "pool/ds", dict(txg=1, root_id="root"), "new", terminal=terminal)
        self.assertTrue(terminal.restored)
        self.assertEqual(LOG.level, level)
        terminal = Terminal([Terminal.error("injected terminal error")])
        with self.assertRaisesRegex(Unsupported, "TERM"):
            tui.choose(FakeFilesystem(), "pool/ds", dict(txg=1, root_id="root"), "new", terminal=terminal)
        self.assertTrue(terminal.restored)
        self.assertEqual(LOG.level, level)

    def test_disk_text_is_escaped_in_headers_and_names(self):
        picker = tui.Picker(FakeFilesystem())
        terminal = Terminal(["q"])
        tui._loop(terminal.screen, terminal, picker, "pool/\n\x1b[2J",
                  dict(txg=1, root_id="root\r\n"), "/healthy/\x1bnew")
        self.assertIn("\\x1b", terminal.screen.frames[-1])
        self.assertEqual(tui.label("Música".encode("utf-8")), "'Música'")
        self.assertNotIn("\u202e", tui.label("abc\u202edef"))
        self.assertEqual(tui.label("漢"), "'\\u6f22'")

    def test_details_show_long_names_and_destination_without_opening_files(self):
        picker = tui.Picker(FakeFilesystem())
        destination = "/healthy/" + "long-directory/" * 10 + "unique-destination"
        terminal = Terminal(["i", "n", " ", "r", "i", "j", "n", "s"])
        selected = tui._loop(terminal.screen, terminal, picker, "pool/ds",
                             dict(txg=1, root_id="root"), destination)
        self.assertEqual(selected.paths, [(b"Documentos",)])
        details = [frame for frame in terminal.screen.frames if "RUTES COMPLETES" in frame]
        self.assertTrue(details)
        self.assertTrue(any("unique-destination" in frame.replace("\n", "") for frame in details))
        self.assertEqual(picker.fs.payload_reads, [])


class TerminalCommandTests(unittest.TestCase):
    def setUp(self):
        self.flow = support.CommandFlowTests()
        self.flow.setUp()

    def test_confirmed_session_uses_same_view_and_records_terminal_scope(self):
        terminal = Terminal(["j", " ", "r", "s"])
        destination = MemoryOutput()
        destination.path = "new"
        destination.close = lambda *args: None
        with patch.object(commands, "ensure_terminal", return_value=terminal):
            code, text = self.flow.run_cli(["select", "--dest", "new"], destination)
        self.assertEqual(code, 0, text)
        self.assertEqual(destination.rows[0]["selection_method"], "terminal")
        self.assertEqual(destination.rows[0]["selected"]["txg"], 100)
        self.assertEqual(destination.rows[0]["included_paths"][0]["path"], "Fotos")
        self.assertEqual(destination.rows[-1]["scope"], "selected_paths")
        self.assertNotIn((b"tree", b"Documentos", b"nota.txt"), destination.files)

    def test_cancel_and_interrupt_do_not_create_destination(self):
        for key, expected in (("q", 0), (KeyboardInterrupt(), 130)):
            with self.subTest(key=key), patch.object(commands, "Destination") as destination:
                with patch.object(commands, "ensure_terminal", return_value=Terminal([key])), \
                     patch.object(self.flow.source, "close") as close:
                    code, text = self.flow.run_cli(["select", "--dest", "new"])
                self.assertEqual(code, expected, text)
                destination.assert_not_called()
                close.assert_called_once()

    def test_no_tty_is_rejected_before_opening_sources(self):
        with patch.object(tui.sys, "stdin", io.StringIO()), patch.object(commands, "Destination") as destination:
            with patch.object(self.flow.source_module, "Source") as source:
                code, _ = self.flow.run_cli(["select", "--dest", "new"])
            source.assert_not_called()
            destination.assert_not_called()
        self.assertEqual(code, 1)

    def test_confirmed_selection_is_validated_before_creating_destination(self):
        with patch.object(commands, "ensure_terminal", return_value=Terminal([])):
            with patch.object(commands, "choose", return_value=Selection(["missing"])):
                with patch.object(commands, "Destination") as destination:
                    code, _ = self.flow.run_cli(["select", "--dest", "new"])
        self.assertEqual(code, 1)
        destination.assert_not_called()

    def test_resource_limit_restores_terminal_and_never_creates_destination(self):
        terminal = Terminal([" "])
        marks = tui.Marks(max_bytes=1)
        with patch.object(commands, "ensure_terminal", return_value=terminal):
            with patch.object(tui, "Marks", return_value=marks):
                with patch.object(commands, "Destination") as destination:
                    code, _ = self.flow.run_cli(["select", "--dest", "new"])
        self.assertEqual(code, 1)
        self.assertTrue(terminal.restored)
        destination.assert_not_called()

    def test_missing_curses_has_actionable_error(self):
        real_import = __import__

        def no_curses(name, *args, **kwargs):
            if name == "curses":
                raise ImportError("injected missing curses")
            return real_import(name, *args, **kwargs)

        tty = SimpleNamespace(isatty=lambda: True)
        with patch.object(tui.sys, "stdin", tty), patch.object(tui.sys, "stdout", tty):
            with patch("builtins.__import__", side_effect=no_curses):
                with self.assertRaisesRegex(Unsupported, "Python curses"):
                    tui.ensure_terminal()

    def test_path_start_and_fixed_txg_are_available(self):
        terminal = Terminal([Terminal.KEY_HOME, " ", "r", "s"])
        destination = MemoryOutput()
        destination.path = "new"
        destination.close = lambda *args: None
        with patch.object(commands, "ensure_terminal", return_value=terminal):
            code, text = self.flow.run_cli(["select", "--path", "Fotos/viaje",
                                           "--txg", "100", "--root-id", "testroot", "--dest", "new"], destination)
        self.assertEqual(code, 0, text)
        self.assertEqual(set(destination.files), {(b"tree", b"Fotos", b"viaje", b"dos.txt")})

    def test_invalid_flags_fail_before_the_terminal_or_sources(self):
        cases = [["select"], ["select", "--dest", "new", "--recursive"],
                 ["select", "--dest", "new", "--human"],
                 ["select", "--dest", "new", "--directories-only"],
                 ["select", "--dest", "new", "--include", "Fotos", "--txg", "100"]]
        for args in cases:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with patch.object(commands, "ensure_terminal") as ensure, self.assertRaises(SystemExit):
                    commands.cli([*args, "--dataset", "vol1/DATA", "--source", "DO-NOT-OPEN"])
                ensure.assert_not_called()


class SyntheticPickerTests(unittest.TestCase):
    def test_picker_recovers_whole_folder_and_raw_filename_with_replica_fallback(self):
        from zfs_reader.pool import PoolView
        from zfs_reader.scan import scan

        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            fixture = Fixture(directory)
            fixture.damage(0, fixture.payload_offsets["hello"])
            before = [hashlib.sha256(path.read_bytes()).digest() for path in fixture.paths]
            sources = [ReadOnlyImage(path) for path in fixture.paths]
            for source in sources:
                stack.callback(source.close)
            info, candidates = scan(sources)
            view = PoolView(sources, info, next(candidate for candidate in candidates if candidate.txg == 100))
            fs = view.dataset("tank/rescue")
            picker = tui.Picker(fs)
            picker.cursor = next(i for i, (name, _) in enumerate(picker.entries, 1) if name == b"folder")
            picker.toggle()
            picker.open()
            picker.cursor = next(i for i, (name, _) in enumerate(picker.entries, 1) if name == b"\xff.bin")
            self.assertEqual(picker.entry(picker.cursor).kind, "file")
            self.assertIn("Només", picker.open())
            selection = picker.marks.selection()
            selection.validate(fs)
            output = MemoryOutput()
            with patch.object(extraction, "os", output):
                stats = extraction.extract(fs, output, selection=selection)
            self.assertFalse(stats.get("errors"))
            self.assertEqual(set(output.files), {(b"tree", b"folder", b"hello.txt"),
                                                 (b"tree", b"folder", b"\xff.bin")})
            self.assertEqual(output.files[(b"tree", b"folder", b"hello.txt")].getvalue(), b"A" * 512)
            self.assertGreater(view.reader.stats["replica_fallbacks"], 0)
            self.assertEqual(before, [hashlib.sha256(path.read_bytes()).digest() for path in fixture.paths])


@unittest.skipUnless(os.name == "posix", "Real curses/PTY integration requires POSIX")
class PosixTerminalTests(unittest.TestCase):
    def run_terminal(self, directory, fixture, keys):
        import errno
        import fcntl
        import pty
        import select
        import struct
        import subprocess
        import termios
        import time

        output = Path(directory) / "picked"
        master, slave = pty.openpty()
        process = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            process = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).resolve().with_name("zfs_rescue.py")),
                 "select", *fixture.source_args(), "--dataset", "tank/rescue", "--path", "folder",
                 "--txg", "100", "--dest", str(output)],
                stdin=slave, stdout=slave, stderr=slave,
                env={**os.environ, "TERM": "xterm", "LINES": "24", "COLUMNS": "100"})
            os.close(slave)
            slave = None
            captured, sent = bytearray(), False
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    break
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not data:
                    break
                captured.extend(data)
                if not sent and b"Espai: marcar" in captured:
                    os.write(master, keys)
                    sent = True
            self.assertTrue(sent, captured.decode("utf-8", "replace"))
            self.assertEqual(process.wait(timeout=2), 0, captured.decode("utf-8", "replace"))
            return output
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            if slave is not None:
                os.close(slave)

    def test_real_terminal_can_select_file_without_opening_it(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            fixture.damage(0, fixture.payload_offsets["hello"])
            for index in (0, 1):
                fixture.damage(index, fixture.payload_offsets["binary"])
            before = [hashlib.sha256(path.read_bytes()).digest() for path in fixture.paths]
            output = self.run_terminal(directory, fixture, b" lrs")
            self.assertEqual((output / "tree/folder/hello.txt").read_bytes(), b"A" * 512)
            self.assertEqual(list((output / "tree/folder").iterdir()), [output / "tree/folder/hello.txt"])
            self.assertEqual(before, [hashlib.sha256(path.read_bytes()).digest() for path in fixture.paths])

    def test_real_terminal_cancel_creates_no_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(directory)
            output = self.run_terminal(directory, fixture, b"q")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
