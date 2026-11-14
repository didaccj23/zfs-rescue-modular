from __future__ import annotations

import dataclasses
import datetime
import hashlib
import struct
from collections import Counter, OrderedDict

from .base import Corrupt, DATA_START, LABEL_SIZE, LOG, MAX_BLOCK, ReadError, Unsupported, checksum, decompress, require

@dataclasses.dataclass(frozen=True)
class BP:
    raw: bytes
    endian: str

    def __post_init__(self):
        require(len(self.raw) == 128 and self.endian in ("<", ">"), "Invalid BP")

    @property
    def words(self):
        return struct.unpack(self.endian + "16Q", self.raw)

    @property
    def prop(self):
        return self.words[6]

    @property
    def embedded(self):
        return bool(self.prop & (1 << 39))

    @property
    def hole(self):
        return not self.embedded and self.words[0] == self.words[1] == 0

    @property
    def order(self):
        return "<" if self.prop >> 63 else ">"

    @property
    def level(self):
        return (self.prop >> 56) & 31

    @property
    def lsize(self):
        return ((self.prop & ((1 << 25) - 1)) + 1 if self.embedded
                else ((self.prop & 65535) + 1) * 512)

    @property
    def psize(self):
        return (((self.prop >> 25) & 127) + 1 if self.embedded
                else (((self.prop >> 16) & 65535) + 1) * 512)

    @property
    def compression(self):
        return (self.prop >> 32) & 127

    @property
    def algorithm(self):
        return (self.prop >> 40) & 255

    def payload(self):
        require(self.embedded and self.algorithm == 0, "Unsupported embedded BP type")
        require(self.psize <= 112, "Oversized embedded BP")
        return b"".join(struct.pack("<Q", word) for i, word in enumerate(self.words)
                        if i not in (6, 10))[:self.psize]

    def dvas(self):
        for i in range(3):
            w0, w1 = self.words[2 * i:2 * i + 2]
            if w0 or w1:
                yield w0 >> 32, (w1 & ((1 << 63) - 1)) * 512, bool(w1 >> 63), (w0 & 0xFFFFFF) * 512


@dataclasses.dataclass
class Block:
    data: bytes
    endian: str


class BlockReader:
    def __init__(self, sources, vdev_id, txg, sector_recovery=False, cache_bytes=64 << 20):
        self.sources, self.vdev_id, self.txg = sources, vdev_id, txg
        self.sector_recovery = sector_recovery
        self.cache_bytes, self.cache_size = cache_bytes, 0
        self.cache = OrderedDict()
        self.stats = Counter()

    def _remember(self, key, block):
        if len(block.data) <= self.cache_bytes:
            while self.cache and self.cache_size + len(block.data) > self.cache_bytes:
                _, old = self.cache.popitem(last=False)
                self.cache_size -= len(old.data)
            self.cache[key] = block
            self.cache_size += len(block.data)
        return block

    def read(self, bp):
        require(not bp.hole, "A metadata block unexpectedly points to a hole")
        key = (bp.raw, bp.endian)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        require(0 < bp.lsize <= MAX_BLOCK, "Oversized BP")
        require(bp.words[10] <= self.txg, "BP born after selected TXG")
        if bp.prop & (1 << 61):
            raise Unsupported("Encrypted/authenticated BP; native encryption is not supported")
        if bp.embedded:
            self.stats["embedded_blocks"] += 1
            return self._remember(key, Block(decompress(bp.payload(), bp.compression, bp.lsize), bp.order))
        # Reject unknown/OFF checksums before considering any replica.
        if bp.algorithm not in (6, 7, 8, 11):
            raise Unsupported(f"Unsupported/unverifiable checksum {bp.algorithm}")

        def decode(raw):
            require(checksum(raw, bp.algorithm, bp.order) == bp.words[12:16], "Block checksum mismatch")
            return Block(decompress(raw, bp.compression, bp.lsize), bp.order)

        errors = []
        for vdev, offset, gang, asize in bp.dvas():
            if vdev != self.vdev_id:
                errors.append(f"Missing top-level vdev {vdev}")
                continue
            if gang:
                errors.append("Gang blocks unsupported")
                continue
            require(asize >= bp.psize, "DVA allocation smaller than physical block")
            physical = DATA_START + offset
            for index, source in enumerate(self.sources):
                try:
                    require(physical + bp.psize <= source.label_size - 2 * LABEL_SIZE,
                            "DVA overlaps end labels")
                    block = decode(source.read(physical, bp.psize))
                    self.stats["verified_blocks"] += 1
                    if index or errors:
                        self.stats["replica_fallbacks"] += 1
                        LOG.info("Recovered verified block from %s at %d", source.path, physical)
                    return self._remember(key, block)
                except (ReadError, Corrupt) as exc:
                    self.stats["rejected_replicas"] += 1
                    errors.append(str(exc))
            # Optional: combine readable sectors after whole-copy failures.
            # Never brute-force silent corruption; the entire result must verify.
            if self.sector_recovery and len(self.sources) == 2:
                try:
                    pieces = []
                    for relative in range(0, bp.psize, 512):
                        for source in self.sources:
                            try:
                                pieces.append(source.read(physical + relative, 512))
                                break
                            except ReadError:
                                pass
                        else:
                            raise ReadError(f"Both copies lack sector {physical + relative}")
                    block = decode(b"".join(pieces))
                    self.stats["sector_assemblies"] += 1
                    return self._remember(key, block)
                except (ReadError, Corrupt) as exc:
                    errors.append(str(exc))
        raise ReadError("No verified replica: " + "; ".join(errors[-8:]))


@dataclasses.dataclass
class Uberblock:
    txg: int
    timestamp: int
    root: BP
    source: str
    label: int
    offset: int

    def public(self):
        try:
            utc = datetime.datetime.fromtimestamp(self.timestamp, datetime.timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            utc = None
        return {"txg": self.txg, "timestamp": self.timestamp,
                "time_utc": utc,
                "root_id": hashlib.sha256(self.root.raw).hexdigest()[:16],
                "source": self.source, "label": self.label, "vdev_offset": self.offset}


def label_offsets(source):
    return (0, LABEL_SIZE, source.label_size - 2 * LABEL_SIZE, source.label_size - LABEL_SIZE)
