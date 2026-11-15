"""Lazy, metadata-only terminal picker. curses is loaded only when requested."""
from __future__ import annotations

import logging
import stat
import sys
import textwrap
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from dataclasses import dataclass

from .base import LOG, MAX_ENTRIES, RescueError, ResourceLimit, Unsupported, checked_name, require
from .selection import MAX_DIR_ENTRIES, Selection, relative_parts


MAX_MARKED_BYTES = 64 << 20
ENTRY_CACHE_SIZE = 256


def label(value):
    """Quote/escape disk-controlled text, including terminal controls and bad UTF-8."""
    text = repr(value.decode("utf-8", "surrogateescape") if isinstance(value, bytes) else str(value))
    # Keep Catalan/Latin-1 accents readable. Escape wider/combining characters so
    # each displayed character occupies one cell, without an extra wcwidth dependency.
    return "".join(char if ord(char) < 256 else ascii(char)[1:-1] for char in text)


def path_label(parts):
    return label(b"/".join(parts) or b".")


class Marks:
    """Sorted, non-overlapping paths; an empty list never means 'recover everything'."""
    def __init__(self, max_bytes=MAX_MARKED_BYTES):
        self.paths = []
        self.bytes = 0
        self.max_bytes = max_bytes

    @staticmethod
    def cost(parts):
        return 256 + sum(64 + len(part) for part in parts)

    def state(self, parts):
        previous = bisect_right(self.paths, parts) - 1
        if previous >= 0:
            parent = self.paths[previous]
            if parts[:len(parent)] == parent:
                return "x" if parts == parent else "*"
        following = previous + 1
        if following < len(self.paths) and self.paths[following][:len(parts)] == parts:
            return "-"
        return " "

    def toggle(self, parts):
        require(len(parts) <= 256, "Directory depth limit exceeded")
        for part in parts:
            checked_name(part)
        state = self.state(parts)
        if state == "*":
            return "Inclòs per una carpeta marcada. Desmarca primer aquella carpeta."
        begin = bisect_left(self.paths, parts)
        if state == "x":
            self.bytes -= self.cost(self.paths.pop(begin))
            return "Ruta desmarcada."
        end = begin
        removed = 0
        while end < len(self.paths) and self.paths[end][:len(parts)] == parts:
            removed += self.cost(self.paths[end])
            end += 1
        new_bytes = self.bytes - removed + self.cost(parts)
        if new_bytes > self.max_bytes:
            raise ResourceLimit("Marked path memory budget exceeded; recover a smaller batch")
        self.paths[begin:end] = [parts]
        self.bytes = new_bytes
        return "Ruta marcada. Si és una carpeta, es recuperarà sencera."

    def selection(self):
        require(bool(self.paths), "No paths marked; refusing an empty interactive selection")
        return Selection([b"/".join(parts) or b"." for parts in self.paths])


@dataclass(frozen=True)
class Entry:
    parts: tuple
    number: int
    kind: str
    size: int = 0
    error: str = ""


class Picker:
    """Keep one directory, a small metadata cache and bounded marks, not a tree index."""
    def __init__(self, fs, path=".", max_entries=MAX_ENTRIES, max_dir_entries=MAX_DIR_ENTRIES):
        self.fs = fs
        self.max_entries, self.max_dir_entries = max_entries, max_dir_entries
        self.visited = 0
        self.parents = []
        self.marks = Marks()
        self._load(fs.root, ())
        for part in relative_parts(path):
            index = next((i for i, (name, _) in enumerate(self.entries, 1) if name == part), None)
            require(index is not None, f"Selected directory not found: {part!r}")
            self.cursor = index
            entry = self.entry(index)
            require(entry.kind == "directory", "--path must name a real directory, not a file or symlink")
            self.open()

    def _load(self, number, parts, cursor=1):
        node = self.fs.os.node(number)
        require(node.type == 20 and stat.S_ISDIR(self.fs.attributes(node)["mode"]),
                "Invalid directory object")
        errors = []
        entries = self.fs.os.zap(number, errors, max_entries=self.max_dir_entries)
        count = len(entries) + 1
        if self.visited + count > self.max_entries:
            raise ResourceLimit("Interactive traversal limit exceeded; recover a batch or narrow --path")
        # Commit a navigation change only after the new directory is readable.
        entries = sorted(entries.items())
        self.visited += count
        self.number, self.parts = number, parts
        self.entries, self.errors = entries, errors
        self.cache = OrderedDict()
        self.cursor = min(cursor, len(entries))
        self.top = 0

    def __len__(self):
        return len(self.entries) + 1  # '.' can mark the current folder, including an empty one.

    def entry(self, index):
        if index == 0:
            return Entry(self.parts, self.number, "directory")
        if index in self.cache:
            self.cache.move_to_end(index)
            return self.cache[index]
        name, value = self.entries[index - 1]
        parts, number = self.parts + (name,), 0
        try:
            checked_name(name)
            require(isinstance(value, tuple) and len(value) == 1 and isinstance(value[0], int),
                    "Invalid directory ZAP value")
            number = value[0] & ((1 << 48) - 1)
            require(number != 0, "Zero directory object number")
            node = self.fs.os.node(number)
            attrs = self.fs.attributes(node)
            mode = attrs["mode"]
            if stat.S_ISDIR(mode):
                require(node.type == 20, "Invalid directory object")
                kind = "directory"
            elif stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                require(node.type == 19, "Invalid file object")
                kind = "file" if stat.S_ISREG(mode) else "symlink"
            else:
                kind = "special"
            result = Entry(parts, number, kind, attrs["size"])
        except ResourceLimit:
            raise
        except RescueError as exc:
            result = Entry(parts, number, "error", error=str(exc))
        if len(self.cache) >= ENTRY_CACHE_SIZE:
            self.cache.popitem(last=False)
        self.cache[index] = result
        return result

    def move(self, count):
        self.cursor = max(0, min(len(self) - 1, self.cursor + count))

    def open(self):
        entry = self.entry(self.cursor)
        if entry.kind != "directory":
            return "Només s'obren carpetes. Els fitxers i enllaços es poden marcar, no obrir."
        if self.cursor == 0:
            return "Ja ets en aquesta carpeta. Espai la marca sencera."
        require(len(entry.parts) <= 256, "Directory depth limit exceeded")
        require(entry.number not in [self.number, *(number for number, _ in self.parents)],
                "Cycle in directory tree")
        previous = self.number, self.cursor
        self._load(entry.number, entry.parts)
        self.parents.append(previous)
        return ""

    def back(self):
        if not self.parents:
            return "Ja ets a l'arrel del dataset."
        number, cursor = self.parents[-1]
        self._load(number, self.parts[:-1], cursor)
        self.parents.pop()
        return ""

    def toggle(self):
        entry = self.entry(self.cursor)
        require(entry.kind in ("directory", "file", "symlink"),
                entry.error or "Special files cannot be selected")
        return self.marks.toggle(entry.parts)


def ensure_terminal():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Unsupported("select requires an interactive terminal (TTY); use browse/extract for redirected I/O")
    try:
        import curses
    except ImportError as exc:
        raise Unsupported("select requires Python curses on POSIX; install your OS's Python curses package") from exc
    return curses


def _put(screen, terminal, row, text, highlight=False):
    height, width = screen.getmaxyx()
    if 0 <= row < height and width > 1:
        # Leave the last column unused: curses may otherwise wrap/raise at bottom-right.
        try:
            limit = width - 1
            visible = text[:limit]
            if len(text) > limit and limit >= 3:
                visible = text[:limit - 3] + "..."
            screen.addstr(row, 0, visible, terminal.A_REVERSE if highlight else 0)
        except terminal.error:
            pass  # A resize can race a draw; the next frame uses the new dimensions.


def _confirm_exit(screen, terminal):
    """Ask before abandoning the current selection; only lowercase s confirms."""
    screen.erase()
    _put(screen, terminal, 0, "Segur que vols sortir?")
    _put(screen, terminal, 1, "s: sí | qualsevol altra tecla: no")
    screen.refresh()
    return screen.get_wch() == "s"


def _details(screen, terminal, parts, dataset, selected, dest):
    """Read-only full paths, including names which do not fit in the main list."""
    fields = [("Ruta", path_label(parts)), ("Dataset", label(dataset)), ("TXG", str(selected["txg"])),
              ("root-id", label(selected["root_id"])), ("Destí nou", label(dest))]
    top = 0
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        lines = [line for title, value in fields
                 for line in textwrap.wrap(title + ": " + value, max(1, width - 2), replace_whitespace=False)]
        rows = max(1, height - 4)
        top = max(0, min(top, len(lines) - rows))
        _put(screen, terminal, 0, "RUTES COMPLETES — no s'obre el contingut de cap fitxer")
        for row, line in enumerate(lines[top:top + rows], 1):
            _put(screen, terminal, row, line)
        _put(screen, terminal, height - 2, "j/k o PgUp/PgDn: desplaçar | Enter/n: tornar | q: sortir sense copiar")
        screen.refresh()
        key = screen.get_wch()
        if key in ("q", "Q"):
            return True
        if key == "\x1b":
            if _confirm_exit(screen, terminal):
                return True
            continue
        if key in ("\n", "\r", terminal.KEY_ENTER, "n", "N", "i"):
            return False
        top += {terminal.KEY_UP: -1, "k": -1, terminal.KEY_DOWN: 1, "j": 1,
                terminal.KEY_PPAGE: -rows, terminal.KEY_NPAGE: rows}.get(key, 0)


def _loop(screen, terminal, picker, dataset, selected, dest):
    screen.keypad(True)
    try:
        terminal.curs_set(0)
    except terminal.error:
        pass
    message, reviewing, review_cursor, review_top = "", False, 0, 0
    kinds = {"directory": "CARPETA", "file": "FITXER", "symlink": "ENLLAÇ",
             "special": "ESPECIAL", "error": "ERROR"}
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 18 or width < 80:
            _put(screen, terminal, 0, "Amplia el terminal a 80x18 com a mínim. q: sortir sense copiar")
            screen.refresh()
            key = screen.get_wch()
            if key in ("q", "Q"):
                return None
            if key == "\x1b" and _confirm_exit(screen, terminal):
                return None
            continue
        rows = height - 12
        _put(screen, terminal, 0, "REVISAR I CONFIRMAR" if reviewing else "RECUPERACIÓ ZFS — selector de metadades")
        _put(screen, terminal, 1, f"Dataset: {label(dataset)} | TXG: {selected['txg']}")
        _put(screen, terminal, 2, f"root-id: {label(selected['root_id'])}")
        _put(screen, terminal, 3, "Destí nou: " + label(dest))
        if reviewing:
            count = len(picker.marks.paths)
            review_top = max(0, min(review_top, review_cursor))
            review_top = max(review_top, review_cursor - rows + 1)
            _put(screen, terminal, 4, f"{count} rutes. Les carpetes marcades es recuperaran SENCERES.")
            _put(screen, terminal, 5, "s: CONFIRMAR i copiar | n: tornar al selector | q: sortir sense copiar")
            _put(screen, terminal, 6, "Amunt/avall o j/k | PgUp/PgDn | Inici/Fi | i: ruta i destí complets")
            _put(screen, terminal, 7, "RUTES SELECCIONADES (no es segueixen enllaços simbòlics)")
            for row, parts in enumerate(picker.marks.paths[review_top:review_top + rows], 8):
                suffix = " [TOT EL DATASET]" if not parts else ""
                _put(screen, terminal, row, path_label(parts) + suffix, row - 8 + review_top == review_cursor)
            _put(screen, terminal, height - 4, f"Ruta {review_cursor + 1}/{count}. Encara no s'ha creat el destí.")
            _put(screen, terminal, height - 3, "Mides de carpetes no calculades: comprova l'espai lliure del destí.")
            _put(screen, terminal, height - 2, "Llistar metadades NO verifica el contingut dels fitxers.")
        else:
            picker.top = max(0, min(picker.top, picker.cursor))
            picker.top = max(picker.top, picker.cursor - rows + 1)
            _put(screen, terminal, 4, "Carpeta: " + path_label(picker.parts))
            _put(screen, terminal, 5, "Enter/dreta/l: obrir CARPETA | Espai: marcar/desmarcar | esquerra/h: enrere")
            _put(screen, terminal, 6, "j/k o fletxes | PgUp/PgDn | r: recuperar | q: sortir | i: ruta completa")
            _put(screen, terminal, 7, "MARCA  TIPUS       BYTES LÒGICS  NOM")
            for index in range(picker.top, min(len(picker), picker.top + rows)):
                entry = picker.entry(index)
                mark = picker.marks.state(entry.parts)
                size = str(entry.size) if entry.kind == "file" else "-"
                name = label(entry.parts[-1]) if index else "'.' (carpeta actual, sencera)"
                text = f"[{mark}]    {kinds[entry.kind]:<8} {size:>13}  {name}"
                _put(screen, terminal, 8 + index - picker.top, text, index == picker.cursor)
            current = picker.entry(picker.cursor)
            warning = f"ATENCIÓ: {len(picker.errors)} errors al directori | " if picker.errors else ""
            _put(screen, terminal, height - 4,
                 warning + f"{len(picker.marks.paths)} rutes marcades | entrada {picker.cursor + 1}/{len(picker)}")
            _put(screen, terminal, height - 3, "[x] marcada | [*] inclosa per carpeta sencera | [-] selecció parcial")
            detail = current.error or (picker.errors[0] if picker.errors else "")
            _put(screen, terminal, height - 2, message or ("Error: " + label(detail) if detail else
                 "Espai marca carpetes SENCERES o fitxers individuals. No s'obre cap fitxer."))
            _put(screen, terminal, height - 1, "Ruta: " + path_label(current.parts))
        screen.refresh()
        key = screen.get_wch()
        if key in ("q", "Q"):
            return None
        if key == "\x1b":
            if _confirm_exit(screen, terminal):
                return None
            continue
        if key == terminal.KEY_RESIZE:
            continue
        if key == "i":
            parts = picker.marks.paths[review_cursor] if reviewing else picker.entry(picker.cursor).parts
            if _details(screen, terminal, parts, dataset, selected, dest):
                return None
            continue
        if reviewing:
            if key in ("s", "S"):
                return picker.marks.selection()
            if key in ("n", "N", "\n", "\r", terminal.KEY_ENTER):
                reviewing, message = False, ""
                continue
            delta = {terminal.KEY_UP: -1, "k": -1, terminal.KEY_DOWN: 1, "j": 1,
                     terminal.KEY_PPAGE: -rows, terminal.KEY_NPAGE: rows}.get(key, 0)
            review_cursor = max(0, min(len(picker.marks.paths) - 1, review_cursor + delta))
            if key == terminal.KEY_HOME:
                review_cursor = 0
            elif key == terminal.KEY_END:
                review_cursor = len(picker.marks.paths) - 1
            continue
        message = ""
        try:
            delta = {terminal.KEY_UP: -1, "k": -1, terminal.KEY_DOWN: 1, "j": 1,
                     terminal.KEY_PPAGE: -rows, terminal.KEY_NPAGE: rows}.get(key, 0)
            if delta:
                picker.move(delta)
            elif key == terminal.KEY_HOME:
                picker.cursor = 0
            elif key == terminal.KEY_END:
                picker.cursor = len(picker) - 1
            elif key in ("\n", "\r", terminal.KEY_ENTER, terminal.KEY_RIGHT, "l",
                         terminal.KEY_LEFT, terminal.KEY_BACKSPACE, "\b", "\x7f", "h"):
                _put(screen, terminal, height - 2, "Llegint només metadades…")
                screen.refresh()
                message = (picker.back() if key in (terminal.KEY_LEFT, terminal.KEY_BACKSPACE, "\b", "\x7f", "h")
                           else picker.open())
            elif key == " ":
                message = picker.toggle()
            elif key in ("r", "R"):
                if picker.marks.paths:
                    reviewing, review_cursor, review_top = True, 0, 0
                else:
                    message = "No hi ha res marcat. Prem Espai sobre una carpeta o un fitxer."
        except ResourceLimit:
            raise
        except RescueError as exc:
            message = "Error: " + label(str(exc))


def choose(fs, dataset, selected, dest, path=".", max_entries=MAX_ENTRIES,
           max_dir_entries=MAX_DIR_ENTRIES, terminal=None):
    terminal = terminal if terminal is not None else ensure_terminal()
    picker = Picker(fs, path, max_entries, max_dir_entries)
    level = LOG.level
    try:
        # Replica-recovery INFO messages must not overwrite the curses screen.
        LOG.setLevel(max(LOG.getEffectiveLevel(), logging.WARNING))
        return terminal.wrapper(lambda screen: _loop(screen, terminal, picker, dataset, selected, dest))
    except terminal.error as exc:
        raise Unsupported(f"Cannot use this terminal: {exc}; check TERM or use browse/extract") from exc
    finally:
        LOG.setLevel(level)
