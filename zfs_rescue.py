#!/usr/bin/env python3
"""Command-line entry point; keep the zfs_reader folder alongside this file."""
from zfs_reader.commands import cli

if __name__ == "__main__":
    raise SystemExit(cli())
