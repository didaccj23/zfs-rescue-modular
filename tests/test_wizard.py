"""Portable setup tests: temporary images and explicit doubles, never physical disks."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from zfs_reader import commands, extract as extraction, wizard
from zfs_reader.base import Unsupported
from tests.test_selection import MemoryOutput, ReadOnlyImage
from tests.test_tui import Terminal
from tests.test_zfs_rescue import Fixture


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.first = self.parent / "member one.img"
        self.second = self.parent / "member two.img"
        self.map = self.parent / "member one.map"
        for path in (self.first, self.second, self.map):
            path.write_bytes(b"test")
        self.dest = self.parent / "new rescue"

    def args(self, **kwargs):
        values = dict(source=None, offset=None, length=None, mapfile=None, dataset=None, dest=None)
        values.update(kwargs)
        return SimpleNamespace(**values)

    def configure(self, answers, args=None):
        args = args if args is not None else self.args()
        output = io.StringIO()
        with patch.object(wizard.sys.stdin, "isatty", return_value=True):
            with contextlib.redirect_stdout(output), patch("builtins.input", side_effect=answers) as asked:
                # redirect_stdout is intentionally not a TTY unless explicitly adapted.
                with patch.object(output, "isatty", return_value=True, create=True):
                    wizard.configure(args, commands.integer)
        return args, output.getvalue(), asked

    def test_two_partitions_paths_with_spaces_defaults_and_no_writes(self):
        before = [path.read_bytes() for path in (self.first, self.second)]
        args, text, _ = self.configure([
            str(self.first), "", "", str(self.second), "", "",
            "", str(self.dest), "s",
        ])
        self.assertEqual(args.source, [str(self.first), str(self.second)])
        self.assertEqual(args.offset, [0, 0])
        self.assertEqual(args.length, [None, None])
        self.assertEqual(args.mapfile, ["-", "-"])
        self.assertEqual(args.dataset, "vol1/DATA")
        self.assertEqual(args.dest, str(self.dest))
        self.assertIn("member one.img", text)
        self.assertFalse(self.dest.exists())
        self.assertEqual(before, [path.read_bytes() for path in (self.first, self.second)])

    def test_single_member_and_custom_dataset(self):
        args, text, _ = self.configure([
            str(self.first), "1", "", "", "tank/Other", str(self.dest), "s",
        ])
        self.assertEqual(args.source, [str(self.first)])
        self.assertEqual(args.length, [None])
        self.assertEqual(args.dataset, "tank/Other")
        self.assertIn("Un sol membre", text)

    def test_whole_disk_bounds_map_and_mixed_source_types(self):
        args, _, _ = self.configure([
            str(self.first), "2", "0x100000", "8388608", str(self.map),
            str(self.second), "1", "", "", str(self.dest), "s",
        ])
        self.assertEqual(args.offset, [1048576, 0])
        self.assertEqual(args.length, [8388608, None])
        self.assertEqual(args.mapfile, [str(self.map), "-"])

    def test_invalid_paths_choices_numbers_and_destination_are_reprompted(self):
        args, text, _ = self.configure([
            "", str(self.parent / "missing"), str(self.parent), str(self.first),
            "wrong", "2", "-1", "one", "513", "512", "0", "4096",
            str(self.parent), "", "", "",  # invalid map directory, no map, no member 2, default dataset
            "", str(self.parent / "missing/child"), str(self.first), str(self.dest), "s",
        ])
        self.assertEqual(args.offset, [512])
        self.assertEqual(args.length, [4096])
        self.assertEqual(args.dest, str(self.dest))
        self.assertIn("ja existeix", text)
        self.assertIn("carpeta pare", text)
        self.assertFalse(self.dest.exists())

    def test_existing_empty_destination_is_rejected(self):
        existing = self.parent / "empty"
        existing.mkdir()
        args, text, _ = self.configure(
            [str(existing), str(self.dest), "s"],
            self.args(source=[str(self.first)], dataset="vol1/DATA"))
        self.assertEqual(args.dest, str(self.dest))
        self.assertIn("ja existeix", text)
        self.assertFalse(self.dest.exists())

    def test_duplicate_path_is_reprompted_without_opening_it(self):
        args, text, _ = self.configure([
            str(self.first), "", "", str(self.first), str(self.second), "", "",
            "", str(self.dest), "s",
        ])
        self.assertEqual(args.source, [str(self.first), str(self.second)])
        self.assertIn("ja s'ha indicat", text)
        self.assertFalse(self.dest.exists())

    def test_expand_home_in_source_paths(self):
        with patch.object(wizard.os.path, "expanduser", side_effect=lambda path:
                          str(self.first) if path == "~/member.img" else path):
            args, _, _ = self.configure(["~/member.img", "", "", "", "", str(self.dest), "s"])
        self.assertEqual(args.source, [str(self.first)])

    def test_explicit_arguments_preserved_and_only_confirmation_asked(self):
        args = self.args(source=[str(self.first)], offset=[512], length=[4096],
                         mapfile=[str(self.map)], dataset="tank/data", dest=str(self.dest))
        original = vars(args).copy()
        _, _, asked = self.configure(["s"], args)
        self.assertEqual(vars(args), original)
        self.assertEqual(asked.call_count, 1)

    def test_cancellation_q_eof_no_and_default_create_nothing(self):
        for answer in ("q", EOFError(), "n", ""):
            with self.subTest(answer=answer), self.assertRaises(wizard.SetupCancelled):
                self.configure([answer], self.args(source=[str(self.first)],
                               dataset="vol1/DATA", dest=str(self.dest)))
            self.assertFalse(self.dest.exists())

    def test_keyboard_interrupt_propagates(self):
        with self.assertRaises(KeyboardInterrupt):
            self.configure([KeyboardInterrupt()])
        self.assertFalse(self.dest.exists())

    def test_non_terminal_rejected_without_asking(self):
        for input_tty, output_tty in ((False, True), (True, False)):
            with self.subTest(input_tty=input_tty, output_tty=output_tty):
                with patch.object(wizard.sys.stdin, "isatty", return_value=input_tty):
                    with patch.object(wizard.sys.stdout, "isatty", return_value=output_tty):
                        with patch("builtins.input") as asked, self.assertRaises(Unsupported):
                            wizard.configure(self.args(), commands.integer)
                        asked.assert_not_called()


class WizardCommandTests(unittest.TestCase):
    def run_cli(self, argv, answers, *, posix=True, **extra):
        output = io.StringIO()
        source_module = ModuleType("zfs_reader.sources")
        source_module.Source = Mock()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"zfs_reader.sources": source_module}))
            stack.enter_context(patch.object(commands, "os",
                                             SimpleNamespace(name="posix" if posix else "nt", fsdecode=os.fsdecode)))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(patch.object(output, "isatty", return_value=True, create=True))
            stack.enter_context(patch.object(sys.stdin, "isatty", return_value=True))
            asked = stack.enter_context(patch("builtins.input", side_effect=answers))
            destination = stack.enter_context(patch.object(commands, "Destination"))
            terminal = stack.enter_context(patch.object(commands, "ensure_terminal"))
            for key, value in extra.items():
                stack.enter_context(patch.object(commands, key, value))
            code = commands.cli(argv)
        return code, output.getvalue(), source_module.Source, destination, terminal, asked

    def test_no_arguments_starts_setup_and_q_cancels_before_opening(self):
        code, text, source, dest, terminal, _ = self.run_cli([], ["q"])
        self.assertEqual(code, 0)
        self.assertIn("Assistent de rescat ZFS", text)
        self.assertIn('"event": "cancelled"', text)
        source.assert_not_called()
        dest.assert_not_called()
        terminal.assert_not_called()

    def test_eof_cancels_and_ctrl_c_returns_130_without_creating_destination(self):
        for answer, expected in ((EOFError(), 0), (KeyboardInterrupt(), 130)):
            with self.subTest(answer=answer):
                code, _, source, dest, _, _ = self.run_cli([], [answer])
                self.assertEqual(code, expected)
                source.assert_not_called()
                dest.assert_not_called()

    def test_windows_rejected_before_questions_or_sources(self):
        code, _, source, dest, _, asked = self.run_cli([], [], posix=False)
        self.assertEqual(code, 1)
        source.assert_not_called()
        dest.assert_not_called()
        asked.assert_not_called()

    def test_explicit_interactive_select_preserves_flags(self):
        argv = ["select", "-i", "--source", "given", "--dataset", "tank/data", "--dest", "new",
                "--txg", "100", "--root-id", "known"]
        code, _, source, dest, _, asked = self.run_cli(argv, ["n"])
        self.assertEqual(code, 0)
        self.assertEqual(asked.call_count, 1)
        source.assert_not_called()
        dest.assert_not_called()

    def test_invalid_flags_fail_before_prompts_or_opening(self):
        for argv in (["scan", "-i"], ["--offset", "512"], ["--recursive"],
                     ["--include", "Fotos", "--txg", "100"], ["--max-dir-entries", "0"],
                     ["select"], ["scan"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with patch("builtins.input") as asked, self.assertRaises(SystemExit) as caught:
                    commands.cli(argv)
                self.assertEqual(caught.exception.code, 2)
                asked.assert_not_called()

    def test_help_describes_default_setup_and_interactive_flag(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            commands.cli(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("without arguments", output.getvalue())
        self.assertIn("--interactive", output.getvalue())

    def test_synthetic_default_setup_to_picker_and_extraction(self):
        # Full reader/picker pipeline; only POSIX I/O is replaced on Windows.
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            fixture = Fixture(directory)
            fixture.damage(0, fixture.payload_offsets["hello"])
            before = [hashlib.sha256(path.read_bytes()).digest() for path in fixture.paths]
            source_module = ModuleType("zfs_reader.sources")
            opened = []

            def source(path, offset, length, mapfile):
                self.assertIsNone(mapfile)
                image = ReadOnlyImage(path, offset, length)
                opened.append(image)
                stack.callback(image.close)
                return image

            # ReadOnlyImage exposes file but not stat; production Source already has st.
            def with_stat(*args):
                image = source(*args)
                info = os.fstat(image.file.fileno())
                image.st = SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino,
                                           st_rdev=getattr(info, "st_rdev", 0))
                return image

            source_module.Source = with_stat
            dest_path = Path(directory) / "recovered"
            destination = MemoryOutput()
            destination.path = str(dest_path)
            destination.close = lambda *args: None
            terminal = Terminal(["j", "j", " ", "r", "s"])  # alias -> empty -> folder
            output = io.StringIO()
            stack.enter_context(patch.dict(sys.modules, {"zfs_reader.sources": source_module}))
            stack.enter_context(patch.object(commands, "os", SimpleNamespace(name="posix", fsdecode=os.fsdecode)))
            stack.enter_context(patch.object(commands, "ensure_terminal", return_value=terminal))
            dest = stack.enter_context(patch.object(commands, "Destination", return_value=destination))
            stack.enter_context(patch.object(extraction, "os", destination))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(patch.object(output, "isatty", return_value=True, create=True))
            stack.enter_context(patch.object(sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch("builtins.input", side_effect=[
                str(fixture.paths[0]), "", "", str(fixture.paths[1]), "", "",
                "tank/rescue", str(dest_path), "s",
            ]))
            self.assertEqual(commands.cli([]), 0, output.getvalue())
            dest.assert_called_once()
            self.assertTrue(terminal.restored)
            self.assertTrue(all(image.file.closed for image in opened))
            self.assertEqual(destination.rows[0]["selection_method"], "terminal")
            self.assertEqual(destination.rows[-1]["status"], "completed_supported_scope")
            self.assertEqual(destination.files[(b"tree", b"folder", b"hello.txt")].getvalue(), b"A" * 512)
            self.assertEqual(before, [hashlib.sha256(path.read_bytes()).digest() for path in fixture.paths])
            self.assertFalse(dest_path.exists())
            summary = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(summary["status"], "completed_supported_scope")


if __name__ == "__main__":
    unittest.main()
