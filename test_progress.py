"""Portable progress-display tests; no disk devices or terminal dependencies."""
from __future__ import annotations

import contextlib
import errno
import io
import json
import logging
import os
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from zfs_reader import commands, extract as extraction
from zfs_reader.base import LOG, ResourceLimit
from zfs_reader.progress import RecoveryProgress
from zfs_reader.selection import Selection
import test_selection as support
from test_selection import FakeFilesystem, MemoryOutput


class Tty(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        return 2


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def terminal(self, stream=None, size=(80, 24), term="xterm"):
        self.stack.enter_context(patch.dict(os.environ, TERM=term))
        self.stack.enter_context(patch("zfs_reader.progress.os.get_terminal_size",
                                      return_value=os.terminal_size(size)))
        return RecoveryProgress(stream if stream is not None else Tty())

    def test_only_five_physical_lines_are_reused_and_previous_output_survives(self):
        progress = self.terminal()
        with progress:
            for index in range(20):
                progress((f"file-{index}".encode(),))
            self.assertEqual(len(progress.lines), 5)

        # Minimal terminal emulator: fail on unexpected controls or line wrapping.
        rows, row, column = ["previous terminal output"] + [""] * 23, 1, 0
        for token in re.findall(r"\x1b\[\d+[AK]|\r|\n|[^\x1b\r\n]+", progress.stream.getvalue()):
            if token == "\r":
                column = 0
            elif token == "\n":
                row += 1
                self.assertLess(row, len(rows), "Progress scrolled outside its five-line area")
            elif token.endswith("A") and token.startswith("\x1b["):
                row -= int(token[2:-1])
                self.assertGreaterEqual(row, 1, "Progress overwrote previous terminal output")
            elif token == "\x1b[2K":
                rows[row] = ""
            else:
                self.assertNotIn("\x1b", token)
                self.assertLess(column + len(token), 80)
                rows[row] = rows[row][:column] + token
                column += len(token)
        self.assertEqual(rows[0], "previous terminal output")
        self.assertEqual(rows[1:6], [f"Recuperant: 'file-{index}'" for index in range(15, 20)])
        self.assertEqual(row, 6)  # Final newline leaves the next output below the area.

    def test_names_and_long_lines_cannot_inject_controls_or_wrap(self):
        progress = self.terminal(size=(30, 18))
        with progress:
            progress((b"bad\n\r\x1b[2J\xff",))
            self.assertIn("\\n\\r\\x1b", progress.stream.getvalue())
            progress((b"a" * 5000,))
            self.assertTrue(progress.stream.getvalue().endswith("\r\x1b[2K"))
            self.assertEqual(len(progress.lines), 2)
        for line in re.sub(r"\x1b\[\d+[AK]", "", progress.stream.getvalue()).splitlines():
            self.assertLessEqual(len(line), 29)
        self.assertIn("...", progress.stream.getvalue())

    def test_redirected_dumb_and_small_terminals_have_no_progress_or_logger_changes(self):
        for stream, size, term in ((io.StringIO(), (80, 24), "xterm"),
                                   (Tty(), (80, 24), "dumb"),
                                   (Tty(), (19, 24), "xterm"),
                                   (Tty(), (80, 5), "xterm")):
            with self.subTest(size=size, term=term):
                progress = self.terminal(stream, size, term)
                handlers, propagate = LOG.handlers[:], LOG.propagate
                with progress:
                    progress((b"file",))
                    self.assertEqual(LOG.handlers, handlers)
                    self.assertEqual(LOG.propagate, propagate)
                self.assertEqual(stream.getvalue(), "")

    def test_logging_shares_region_and_configuration_is_restored_on_interrupt(self):
        progress = self.terminal()
        handlers, propagate, level = LOG.handlers[:], LOG.propagate, LOG.level
        with self.assertRaises(KeyboardInterrupt):
            with progress:
                progress((b"file",))
                LOG.warning("warning\n\x1b[2J")
                self.assertIn("WARNING: warning\\n\\x1b[2J", progress.lines[-1])
                self.assertEqual(LOG.handlers, [progress])
                self.assertFalse(LOG.propagate)
                raise KeyboardInterrupt()
        self.assertEqual(LOG.handlers, handlers)
        self.assertEqual(LOG.propagate, propagate)
        self.assertEqual(LOG.level, level)
        self.assertTrue(progress.stream.getvalue().endswith("\n"))

    def test_resize_stops_repainting_and_restores_logging(self):
        progress = self.terminal()
        handlers, propagate = LOG.handlers[:], LOG.propagate
        with progress:
            progress((b"before",))
            text = progress.stream.getvalue()
            with patch("zfs_reader.progress.os.get_terminal_size", return_value=os.terminal_size((40, 12))):
                progress((b"after",))
            self.assertEqual(progress.stream.getvalue(), text + "\n")
            self.assertFalse(progress.enabled)
            self.assertEqual(LOG.handlers, handlers)
            self.assertEqual(LOG.propagate, propagate)

    def test_broken_write_or_flush_does_not_interrupt_recovery(self):
        for operation in ("write", "flush"):
            with self.subTest(operation=operation):
                stream = Tty()
                progress = self.terminal(stream)
                handlers, propagate = LOG.handlers[:], LOG.propagate
                fs, dest = FakeFilesystem(), MemoryOutput()
                with patch.object(stream, operation, side_effect=BrokenPipeError("closed display")):
                    with patch.object(extraction, "os", dest), progress:
                        stats = extraction.extract(fs, dest, selection=Selection(["Fotos"]), progress=progress)
                self.assertFalse(stats.get("errors"))
                self.assertEqual(dest.files[(b"tree", b"Fotos", b"uno.txt")].getvalue(), b"photodata")
                self.assertEqual(LOG.handlers, handlers)
                self.assertEqual(LOG.propagate, propagate)


class ExtractionProgressTests(unittest.TestCase):
    def test_progress_does_not_change_data_manifest_hashes_or_reads(self):
        for paths in (["Fotos"], ["Fotos_backup"], ["Fotos/viaje/dos.txt"], ["escape"], None):
            with self.subTest(paths=paths):
                results = []
                for enabled in (False, True):
                    fs, dest = FakeFilesystem(), MemoryOutput()
                    original = fs.attributes
                    fs.attributes = lambda node: ({**original(node), "symlink": b"/outside"}
                                                   if node.number == 9 else original(node))
                    events = []

                    def started(parts):
                        # The current path is reported before reading any of its data.
                        events.append((parts, fs.payload_reads[:]))

                    with patch.object(extraction, "os", dest), \
                         patch.object(extraction.uuid, "uuid4", return_value=SimpleNamespace(hex="fixed")):
                        stats = extraction.extract(fs, dest, selection=Selection(paths),
                                                   progress=started if enabled else None)
                    files = {key: value.getvalue() if isinstance(value, io.BytesIO) else value
                             for key, value in dest.files.items()}
                    results.append((stats, files, dest.rows, fs.node_reads, fs.payload_reads, fs.zap_reads))
                    if enabled:
                        self.assertEqual([parts for parts, _ in events],
                                         [tuple(bytes.fromhex(row["path_bytes_hex"]).split(b"/"))
                                          if row["path_bytes_hex"] else ()
                                          for row in dest.rows if row["event"] == "entry"])
                        self.assertEqual(events[0], ((), []))
                        if paths == ["Fotos"]:
                            first_file = next(reads for parts, reads in events if parts == (b"Fotos", b"alias"))
                            self.assertEqual(first_file, [])
                self.assertEqual(*results)

    def test_extraction_errors_are_not_swallowed_by_display_context(self):
        progress = RecoveryProgress(io.StringIO())
        fs, dest = FakeFilesystem(), MemoryOutput()
        with self.assertRaises(ResourceLimit), patch.object(extraction, "os", dest), progress:
            extraction.extract(fs, dest, max_dir_entries=1, progress=progress)

    def test_destination_full_still_aborts_without_publishing_file(self):
        progress = RecoveryProgress(Tty())
        fs, dest = FakeFilesystem(), MemoryOutput()
        handlers, propagate = LOG.handlers[:], LOG.propagate
        with patch.dict(os.environ, TERM="xterm"), \
             patch("zfs_reader.progress.os.get_terminal_size", return_value=os.terminal_size((80, 24))), \
             patch.object(extraction, "os", dest), \
             patch.object(dest, "write", side_effect=OSError(errno.ENOSPC, "full")), progress:
            with self.assertRaises(OSError) as caught:
                extraction.extract(fs, dest, selection=Selection(["Fotos/uno.txt"]), progress=progress)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertNotIn((b"tree", b"Fotos", b"uno.txt"), dest.files)
        self.assertIn("Recuperant:", progress.stream.getvalue())
        self.assertEqual(LOG.handlers, handlers)
        self.assertEqual(LOG.propagate, propagate)


class CommandProgressTests(unittest.TestCase):
    def test_json_stdout_and_manifest_are_not_contaminated_by_terminal_display(self):
        flow = support.CommandFlowTests()
        flow.setUp()
        stream = Tty()
        dest = MemoryOutput()
        dest.path, dest.close = "new", lambda *args: None
        with patch.dict(os.environ, TERM="xterm"), \
             patch("zfs_reader.progress.os.get_terminal_size", return_value=os.terminal_size((80, 24))), \
             contextlib.redirect_stderr(stream):
            code, text = flow.run_cli(["extract", "--txg", "100", "--include", "Fotos/viaje",
                                       "--dest", "new"], dest)
        self.assertEqual(code, 0, text)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(rows[-1]["status"], "completed_supported_scope")
        self.assertIn("Recuperant: 'Fotos/viaje/dos.txt'", stream.getvalue())
        self.assertIn("\x1b[4A", stream.getvalue())
        self.assertNotIn("\x1b", text)
        self.assertNotIn("Recuperant:", json.dumps(dest.rows))
        self.assertEqual(dest.files[(b"tree", b"Fotos", b"viaje", b"dos.txt")].getvalue(), b"second photo")

    def test_partial_file_still_returns_partial_status_with_live_display(self):
        flow = support.CommandFlowTests()
        flow.setUp()
        stream, dest = Tty(), MemoryOutput()
        dest.path, dest.close = "new", lambda *args: None
        with patch.dict(os.environ, TERM="xterm"), \
             patch("zfs_reader.progress.os.get_terminal_size", return_value=os.terminal_size((80, 24))), \
             contextlib.redirect_stderr(stream):
            code, text = flow.run_cli(["extract", "--txg", "100", "--include", "Fotos_backup",
                                       "--dest", "new"], dest)
        self.assertEqual(code, 2, text)
        self.assertEqual(json.loads(text.splitlines()[-1])["status"], "partial")
        self.assertTrue(any(row.get("status") == "partial" for row in dest.rows))
        self.assertNotIn((b"tree", b"Fotos_backup", b"broken"), dest.files)
        self.assertIn("Recuperant: 'Fotos_backup/broken'", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
