from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import stat
import sys
import uuid
import zlib
from collections import Counter

from .base import Corrupt, LOG, MAX_BLOCK, MAX_ENTRIES, RescueError, ResourceLimit, require, u64, unpack
from .browse import browse
from .extract import Destination, extract
from .pool import PoolView
from .progress import RecoveryProgress
from .scan import scan
from .selection import MAX_DIR_ENTRIES, Selection
from .tui import choose, ensure_terminal
from .wizard import SetupCancelled, configure

def probe_gpt(source, sector_size):
    """Read both GPTs with header/table CRC validation. Never modify either."""
    require(source.offset == 0, "probe expects a whole image/device with offset zero")
    total = source.size
    results, failures = [], []
    for lba in (1, total // sector_size - 1):
        try:
            raw = source.read(lba * sector_size, sector_size)
            require(raw[:8] == b"EFI PART", "No GPT signature")
            version, headsize, crc = unpack("<III", raw, 8)
            require(version == 0x10000 and 92 <= headsize <= sector_size, "Invalid GPT header")
            checked = bytearray(raw[:headsize])
            checked[16:20] = bytes(4)
            require(zlib.crc32(checked) == crc, "GPT header CRC mismatch")
            current, backup, first, last = unpack("<4Q", raw, 24)
            require(current == lba and first <= last and last * sector_size < total, "Invalid GPT bounds")
            array_lba = u64(raw, 72)
            count, entrysize, tablecrc = unpack("<III", raw, 80)
            require(0 < count <= 16384 and 128 <= entrysize <= 4096 and count * entrysize <= MAX_BLOCK,
                    "Invalid GPT entry geometry")
            array = source.read(array_lba * sector_size, count * entrysize)
            require(zlib.crc32(array) == tablecrc, "GPT partition array CRC mismatch")
            partitions = []
            for index in range(count):
                entry = array[index * entrysize:(index + 1) * entrysize]
                if not any(entry[:16]):
                    continue
                begin, end = unpack("<2Q", entry, 32)
                require(first <= begin <= end <= last, "GPT partition outside usable space")
                partitions.append({"index": index + 1, "type_guid": str(uuid.UUID(bytes_le=entry[:16])),
                                   "name": entry[56:128].decode("utf-16le", "replace").rstrip("\0"),
                                   "offset": begin * sector_size, "length": (end - begin + 1) * sector_size})
            results.append({"header_lba": lba, "sector_size": sector_size, "partitions": partitions})
        except RescueError as exc:
            failures.append({"header_lba": lba, "error": str(exc)})
    return {"source": source.path, "valid_gpt_copies": results, "failed_gpt_copies": failures}


def integer(text):
    try:
        value = int(text, 0)
        if value < 0:
            raise ValueError
        return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use a nonnegative byte count/integer (decimal or 0x...)") from exc


def cli(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline ZFS mirror rescue. Run without arguments for terminal setup and file selection.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=("probe", "scan", "list", "check", "browse", "select", "extract"),
                        help="select: interactive folder/file picker, then confirmed extraction")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="select: ask for missing sources, dataset and destination (default without a command)")
    parser.add_argument("--source", action="append", help="Mirror image or leaf partition; repeat for member 2")
    parser.add_argument("--offset", action="append", type=integer, help="Vdev start in bytes, one per source; default all zero")
    parser.add_argument("--length", action="append", type=integer, help="Vdev/partition length, one per source; default to end")
    parser.add_argument("--mapfile", action="append", help="ddrescue map, one per source; '-' means no map")
    parser.add_argument("--dataset", help="Exact pool/dataset or pool/dataset@snapshot")
    parser.add_argument("--txg", type=integer, help="Only try this exact TXG; never silently choose another")
    parser.add_argument("--max-txg", type=integer, help="Only try TXGs <= this value")
    parser.add_argument("--root-id", help="Select a root_id from scan if a TXG has conflicting roots")
    parser.add_argument("--dest", help="select/extract: NEW directory on healthy storage; parent must exist")
    parser.add_argument("--path", help="browse/select: dataset-relative starting directory (default '.')")
    parser.add_argument("--recursive", action="store_true", help="browse: descend into subdirectories; metadata only")
    parser.add_argument("--directories-only", action="store_true", help="browse: display only directories")
    parser.add_argument("--human", action="store_true", help="browse: readable text instead of JSONL")
    parser.add_argument("--include", action="append",
                        help="extract: exact dataset-relative path and descendants; repeat to select more; requires --txg")
    parser.add_argument("--sector-recovery", action="store_true", help="Try readable 512-byte sectors after whole-copy failures")
    parser.add_argument("--sector-size", type=int, choices=(512, 4096), default=512, help="GPT logical sector size for probe")
    parser.add_argument("--max-file-size", type=integer, default=16 << 40, help="Maximum extracted file bytes (default 16 TiB)")
    parser.add_argument("--max-entries", type=integer, default=MAX_ENTRIES, help="Traversal safety bound")
    parser.add_argument("--max-dir-entries", type=integer, default=MAX_DIR_ENTRIES,
                        help="Maximum entries/leaves in a visited directory (default 100000, maximum 2000000)")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args(argv)
    args.interactive = args.interactive or args.command is None
    args.command = args.command or "select"
    logging.basicConfig(level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s: %(message)s")
    if args.interactive and args.command != "select":
        parser.error("--interactive is only used by select")
    if not args.source and not args.interactive:
        parser.error("--source is required (or run without arguments for terminal setup)")
    if args.source is not None and not 1 <= len(args.source) <= 2:
        parser.error("Supply one or two sources")
    for key in ("offset", "length", "mapfile"):
        value = getattr(args, key)
        if value is not None and (not args.source or len(value) != len(args.source)):
            parser.error(f"--{key} must be supplied once per source")
    if args.command in ("check", "browse", "select", "extract") and not args.dataset and not args.interactive:
        parser.error("--dataset is required for check/browse/select/extract")
    if args.command in ("select", "extract") and not args.dest and not args.interactive:
        parser.error("--dest is required for select/extract")
    if args.dest and args.command not in ("select", "extract"):
        parser.error("--dest is only used by select/extract")
    if args.path is not None and args.command not in ("browse", "select"):
        parser.error("--path is only used by browse/select")
    if args.command != "browse" and (args.recursive or args.directories_only or args.human):
        parser.error("--recursive, --directories-only and --human are only used by browse")
    if args.include is not None:
        if args.command != "extract":
            parser.error("--include is only used by extract")
        if args.txg is None:
            parser.error("Selective extraction requires --txg from browse/check; do not change dataset state silently")
    if not 1 <= args.max_dir_entries <= MAX_ENTRIES or args.max_entries < 1:
        parser.error("--max-dir-entries must be 1..2000000; --max-entries must be positive")

    def output(record):
        if args.command != "browse" or not args.human:
            print(json.dumps(record, ensure_ascii=True))
        elif record["event"] == "pool":
            print("Pool: " + json.dumps(record["pool"], ensure_ascii=True))
        elif record["event"] == "selected":
            print(f"TXG: {record['txg']}  root-id: {record['root_id']}")
            print("TYPE       LOGICAL BYTES  DATASET-RELATIVE PATH (quoted)")
        elif record["event"] == "entry":
            size = str(record["logical_size"]) if record["logical_size"] is not None else "-"
            print(f"{record['kind']:<10} {size:>13}  {json.dumps(record['path'], ensure_ascii=True)}")
        else:
            print(json.dumps(record, ensure_ascii=True))

    sources, destination = [], None
    try:
        selection = Selection(args.include)
        # Keep --help and argument validation usable without importing POSIX-only fcntl.
        if os.name != "posix":
            raise OSError("Reading/extraction requires POSIX (preferably Linux), not native Windows")
        if args.interactive:
            configure(args, integer)
        terminal = ensure_terminal() if args.command == "select" else None
        from .sources import Source

        for i, path in enumerate(args.source):
            source = Source(path, args.offset[i] if args.offset else 0,
                            args.length[i] if args.length else None,
                            args.mapfile[i] if args.mapfile and args.mapfile[i] != "-" else None)
            sources.append(source)
        require(len({(s.st.st_dev, s.st.st_ino, s.st.st_rdev) for s in sources}) == len(sources),
                "The same source was supplied more than once")
        if args.command == "probe":
            for source in sources:
                print(json.dumps(probe_gpt(source, args.sector_size), ensure_ascii=True))
            return 0
        info, candidates = scan(sources)
        output({"event": "pool", **info})
        candidates = [ub for ub in candidates if (args.txg is None or ub.txg == args.txg)
                      and (args.max_txg is None or ub.txg <= args.max_txg)
                      and (args.root_id is None or ub.public()["root_id"] == args.root_id)]
        require(candidates, "No verified uberblock matches the requested TXG/root filters")
        if args.command == "scan":
            for ub in candidates:
                print(json.dumps({"event": "uberblock", **ub.public()}))
            return 0
        view, filesystem = None, None
        # Conflicting same-TXG roots require an explicit selection.
        by_txg = Counter(ub.txg for ub in candidates)
        if any(n > 1 for n in by_txg.values()):
            raise Corrupt("Conflicting roots within a TXG; select --txg and --root-id from scan")
        for ub in candidates:
            try:
                attempted = PoolView(sources, info, ub, args.sector_recovery)
                if args.command in ("check", "browse", "select", "extract"):
                    filesystem = attempted.dataset(args.dataset)
                    attrs = filesystem.attributes(filesystem.os.node(filesystem.root))
                    require(stat.S_ISDIR(attrs["mode"]), "Dataset root mode is not directory")
                    filesystem.os.zap(filesystem.root, max_entries=args.max_dir_entries)
                view = attempted
                break
            except ResourceLimit:
                raise
            except RescueError as exc:
                LOG.warning("Reject TXG %d: %s", ub.txg, exc)
        require(view is not None, "No requested TXG has a readable MOS/target root with supported formats")
        selected = {"event": "selected", **view.uber.public(), "dataset": args.dataset,
                    "validation": "MOS and target root only; not a full dataset integrity test"}
        output(selected)
        if args.command == "list":
            for dataset in view.datasets():
                print(json.dumps(dataset, ensure_ascii=True))
            return 0
        if args.command == "check":
            print(json.dumps({"event": "check", "status": "root_readable", "root_object": filesystem.root,
                              "note": "File contents and descendant directories have not all been read."}))
            return 0
        if args.command == "browse":
            stats = browse(filesystem, lambda **row: output(row),
                           path=args.path if args.path is not None else ".",
                           recursive=args.recursive, directories_only=args.directories_only,
                           max_entries=args.max_entries, max_dir_entries=args.max_dir_entries)
            output({"event": "summary", "status": "partial" if stats.get("errors") else "metadata_listed",
                    "counts": stats,
                    "note": "Metadata only, NOT verified file contents. Logical bytes count each listed file path; "
                            "not a prediction of recoverable or physical bytes. Child datasets are separate."})
            return 2 if stats.get("errors") else 0
        selective = args.command == "select" or args.include is not None
        if args.command == "select":
            selection = choose(filesystem, args.dataset, selected, args.dest,
                               path=args.path if args.path is not None else ".",
                               max_entries=args.max_entries, max_dir_entries=args.max_dir_entries,
                               terminal=terminal)
            if selection is None:
                output({"event": "cancelled", "status": "cancelled", "note": "No destination was created."})
                return 0
            output({"event": "selection_confirmed", "selected_paths": len(selection.paths),
                    "txg": view.uber.txg, "destination": args.dest})
        # Reject misspellings, missing paths and symlink traversal before creating output.
        if selective:
            selection.validate(filesystem, args.max_dir_entries)
        destination = Destination(args.dest, sources)
        destination.emit(event="run", version=1, pool=info, selected=selected,
                          started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                          scope="selected_paths" if selective else "whole_dataset",
                          selection_method="terminal" if args.command == "select" else "paths" if selective else "all",
                          included_paths=[{"path": os.fsdecode(b"/".join(parts)) or ".",
                                           "path_bytes_hex": b"/".join(parts).hex()}
                                          for parts in selection.paths],
                          limitations="One dataset/TXG; no child datasets, ACL/xattr restore or ZIL replay. See README_CA.md.")
        sys.stdout.flush()  # Keep earlier JSON records above the live terminal region.
        with RecoveryProgress() as progress:
            stats = extract(filesystem, destination, args.max_file_size, args.max_entries,
                            selection, args.max_dir_entries, progress=progress)
        result = {"event": "summary", "status": "partial" if stats.get("errors") else "completed_supported_scope",
                  "counts": stats, "reader": dict(view.reader.stats),
                  "scope": "selected_paths" if selective else "whole_dataset",
                  "sources": [{"path": s.path, "stats": dict(s.stats)} for s in sources],
                  "destination": destination.path}
        destination.emit(**result)
        print(json.dumps(result, ensure_ascii=True))
        return 2 if stats.get("errors") else 0
    except SetupCancelled:
        output({"event": "cancelled", "status": "cancelled", "note": "No sources were opened or destination created."})
        return 0
    except (RescueError, OSError, ValueError) as exc:
        LOG.error("Stopped: %s", exc)
        if destination:
            try:
                destination.emit(event="aborted", error=str(exc))
            except OSError:
                pass
        return 1
    except KeyboardInterrupt:
        LOG.warning("Interrupted; output is incomplete. Original sources were only opened O_RDONLY.")
        if destination:
            destination.emit(event="aborted", error="KeyboardInterrupt")
        return 130
    finally:
        close_failed = False
        try:
            if destination:
                try:
                    destination.close()
                except OSError as exc:
                    close_failed = True
                    LOG.error("Output/report could not be flushed; this run is incomplete: %s", exc)
        finally:
            for source in sources:
                source.close()
        if close_failed:
            return 1
