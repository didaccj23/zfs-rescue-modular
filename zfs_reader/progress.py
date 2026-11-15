"""Best-effort five-line terminal progress; never part of the recovery manifest."""
from __future__ import annotations

from collections import deque
import logging
import os
import sys

from .base import LOG


class RecoveryProgress(logging.Handler):
    """Reuse at most five physical lines, with no cursor controls in redirected I/O."""
    def __init__(self, stream=None):
        super().__init__()
        self.stream = stream if stream is not None else sys.stderr
        self.lines = deque(maxlen=5)
        self.drawn = False
        self.enabled = False
        self.saved_logging = None
        self.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    def _size(self):
        return os.get_terminal_size(self.stream.fileno())

    def __enter__(self):
        try:
            if self.stream.isatty() and os.environ.get("TERM", "dumb") != "dumb":
                self.size = self._size()
                self.enabled = self.size.columns >= 20 and self.size.lines >= 6
        except (OSError, ValueError):
            pass
        if self.enabled:
            # Otherwise INFO/WARNING lines could move the cursor out of our region.
            # Restore the exact logger configuration even on interruption/failure.
            self.saved_logging = LOG.handlers[:], LOG.propagate
            LOG.handlers = [self]
            LOG.propagate = False
        return self

    def __call__(self, parts):
        if not self.enabled:
            return
        path = b"/".join(parts).decode("utf-8", "surrogateescape") or "."
        self._show("Recuperant: " + ascii(path))

    def emit(self, record):
        # Keep disk-controlled log text on one physical line as well.
        self._show(ascii(self.format(record))[1:-1])

    def _show(self, text):
        if not self.enabled:
            return
        try:
            # Terminal reflow after resizing invalidates relative cursor positions.
            # Stop repainting rather than risk overwriting earlier terminal output.
            if self._size() != self.size:
                self._finish()
                return
            width = self.size.columns - 1  # Never wrap into an extra physical row.
            self.lines.append(text if len(text) <= width else text[:width - 3] + "...")
            rows = list(self.lines) + [""] * (5 - len(self.lines))
            prefix = "\r\x1b[4A" if self.drawn else ""
            frame = prefix + "\n".join("\r\x1b[2K" + line for line in rows)
            # No final newline: the cursor remains on row five, without scrolling.
            self.drawn = True
            self.stream.write(frame)
            self.stream.flush()
        except (OSError, ValueError, UnicodeError):
            # A closed/broken display must not interrupt writes to the healthy disk.
            self._finish()

    def _finish(self):
        if self.saved_logging is not None:
            LOG.handlers, LOG.propagate = self.saved_logging
            self.saved_logging = None
        if self.drawn:
            try:
                self.stream.write("\n")
                self.stream.flush()
            except (OSError, ValueError, UnicodeError):
                pass
            self.drawn = False
        self.enabled = False

    def __exit__(self, exc_type, exc_value, traceback):
        self._finish()
