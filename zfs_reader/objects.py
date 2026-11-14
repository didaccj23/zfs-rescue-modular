from __future__ import annotations

import struct
from collections import OrderedDict

from .blocks import BP
from .base import MAX_BLOCK, MAX_ENTRIES, RescueError, require, u64, unpack

class Dnode:
    def __init__(self, raw, endian, reader):
        require(len(raw) >= 512, "Short dnode")
        self.endian, self.reader = endian, reader
        self.type, self.indshift, self.levels, self.nptr, self.bonustype = raw[:5]
        self.flags = raw[7]
        self.blocksize = unpack(endian + "H", raw, 8)[0] * 512
        bonuslen = unpack(endian + "H", raw, 10)[0]
        self.slots = raw[12] + 1
        length = self.slots * 512
        require(self.type != 0 and self.slots <= 32 and length <= len(raw), "Free/invalid dnode")
        require(self.nptr <= 3 and self.levels <= 8, "Unsupported dnode geometry")
        require(self.blocksize <= MAX_BLOCK, "Oversized dnode data block")
        if self.levels:
            require(self.nptr > 0 and self.blocksize > 0, "Data dnode without block pointers/size")
        if self.levels > 1:
            require(9 <= self.indshift <= 24, "Invalid indirect block shift")
        self.maxblkid = u64(raw, 16, endian)
        bonusstart = 64 + self.nptr * 128
        bonusend = length - (128 if self.flags & 4 else 0)
        require(bonusstart + bonuslen <= bonusend, "Bonus overlaps spill/end of dnode")
        self.bonus = raw[bonusstart:bonusstart + bonuslen]
        self.bonus_capacity = raw[bonusstart:bonusend]
        self.pointers = [BP(raw[64 + i * 128:192 + i * 128], endian) for i in range(self.nptr)]
        self.spill = BP(raw[length - 128:length], endian) if self.flags & 4 else None

    def pointer(self, blockid):
        require(blockid >= 0, "Negative block id")
        if not self.levels or blockid > self.maxblkid:
            return None  # legitimate sparse tail or empty object
        bits = self.indshift - 7 if self.levels > 1 else 0
        rootindex = blockid >> (bits * (self.levels - 1))
        require(rootindex < len(self.pointers), "Block ID beyond dnode address space")
        bp = self.pointers[rootindex]
        for level in range(self.levels - 1, 0, -1):
            if bp.hole:
                return None
            require(bp.level == level and not bp.embedded, "Wrong indirect BP level")
            indirect = self.reader.read(bp)
            require(len(indirect.data) == 1 << self.indshift, "Wrong indirect block size")
            index = (blockid >> (bits * (level - 1))) & ((1 << bits) - 1)
            bp = BP(indirect.data[index * 128:(index + 1) * 128], indirect.endian)
        if bp.hole:
            return None
        require(bp.level == 0 and bp.lsize <= self.blocksize, "Invalid leaf BP")
        return bp

    def block(self, blockid, metadata=True):
        bp = self.pointer(blockid)
        if bp is None:
            require(not metadata, f"Hole in metadata object at block {blockid}")
            return None
        return self.reader.read(bp)

    def read_range(self, start, count):
        require(self.blocksize > 0 and 0 <= count <= MAX_BLOCK, "Invalid object read size")
        result = bytearray()
        while count:
            blockid, inside = divmod(start, self.blocksize)
            n = min(count, self.blocksize - inside)
            block = self.block(blockid)
            require(inside + n <= len(block.data), "Truncated object block")
            result.extend(block.data[inside:inside + n])
            start += n
            count -= n
        return bytes(result)


class ObjectSet:
    def __init__(self, block, reader, expected_type):
        require(len(block.data) >= 1024 and u64(block.data, 704, block.endian) == expected_type,
                "Wrong/truncated object set type")
        self.reader = reader
        self.meta = Dnode(block.data[:512], block.endian, reader)
        require(self.meta.type == 10 and self.meta.slots == 1, "Invalid meta dnode")
        self.nodes = OrderedDict()

    def node(self, number):
        require(0 < number < (1 << 48), "Invalid object number")
        if number in self.nodes:
            self.nodes.move_to_end(number)
            return self.nodes[number]
        blockid, position = divmod(number * 512, self.meta.blocksize)
        block = self.meta.block(blockid)
        require(position + 512 <= len(block.data), "Object outside dnode block")
        cursor = 0
        while cursor < position:
            cursor += (block.data[cursor + 12] + 1) * 512
        require(cursor == position, "Object number points inside a large dnode")
        node = Dnode(block.data[position:], block.endian, self.reader)
        if len(self.nodes) >= 4096:
            self.nodes.popitem(last=False)
        self.nodes[number] = node
        return node

    def zap(self, number, errors=None):
        return read_zap(self.node(number), errors)


def scalar(values, name):
    value = values.get(name)
    require(isinstance(value, tuple) and len(value) == 1, f"Missing/non-scalar ZAP key {name!r}")
    return value[0]


def read_zap(node, errors=None):
    """Read live entries only. A supplied errors list permits surviving fat-ZAP leaves."""
    head = node.block(0)
    data, endian = head.data, head.endian
    require(len(data) == node.blocksize and len(data) >= 512, "Invalid ZAP block size")
    typ = u64(data, 0, endian)
    values = {}

    def add(name, value):
        require(name and b"\0" not in name and name not in values, "Invalid/duplicate ZAP name")
        require(len(values) < MAX_ENTRIES, "ZAP entry limit exceeded")
        values[name] = value

    if typ == (1 << 63) + 3:
        require(node.maxblkid == 0, "Micro ZAP with additional blocks")
        for off in range(64, len(data), 64):
            rawname = data[off + 14:off + 64]
            if rawname[0] == 0:
                continue
            require(b"\0" in rawname, "Unterminated micro ZAP name")
            add(rawname.split(b"\0", 1)[0], (u64(data, off, endian),))
        return values
    require(typ == (1 << 63) + 1 and u64(data, 8, endian) == 0x2F52AB2AB,
            "Not a micro/fat ZAP header")
    table, nblocks, shift, nextblk, copied = unpack(endian + "5Q", data, 16)
    nleaves, nentries = unpack(endian + "2Q", data, 64)
    require(shift <= 24 and nleaves <= MAX_ENTRIES and nentries <= MAX_ENTRIES, "Fat ZAP limits exceeded")
    # ZAP_FLAG_UINT64_KEY requires different name decoding; never guess.
    require(not (u64(data, 96, endian) & 2), "ZAP with uint64 keys unsupported")
    count = 1 << shift
    leaves = set()
    if nblocks == 0:
        require(table == 0 and count * 8 <= len(data) // 2, "Invalid embedded ZAP pointer table")
        pointers = data[len(data) // 2:len(data) // 2 + count * 8]
        leaves.update(x[0] for x in struct.iter_unpack(endian + "Q", pointers))
    else:
        require(nblocks * node.blocksize == count * 8 and nblocks <= 262144,
                "Invalid external ZAP pointer table")
        require(table > 0 and table + nblocks - 1 <= node.maxblkid, "ZAP pointer table outside object")
        for blockid in range(table, table + nblocks):
            try:
                block = node.block(blockid)
                require(len(block.data) == node.blocksize, "Truncated ZAP pointer table")
                leaves.update(x[0] for x in struct.iter_unpack(block.endian + "Q", block.data))
            except RescueError as exc:
                if errors is None:
                    raise
                errors.append(f"ZAP pointer table block {blockid}: {exc}")
    # During table growth the old table remains authoritative (zap_table_load).
    require(0 not in leaves and all(x <= node.maxblkid for x in leaves), "Invalid ZAP leaf pointer")
    if errors is None or not errors:
        require(len(leaves) == nleaves, "ZAP leaf count mismatch")
    for blockid in sorted(leaves):
        try:
            block = node.block(blockid)
            for name, value in parse_zap_leaf(block).items():
                add(name, value)
        except RescueError as exc:
            if errors is None:
                raise
            errors.append(f"ZAP leaf {blockid}: {exc}")
    if errors is None or not errors:
        require(len(values) == nentries, "ZAP entry count mismatch")
    return values


def parse_zap_leaf(block):
    data, endian = block.data, block.endian
    size = len(data)
    require(size >= 512 and size & (size - 1) == 0, "Invalid ZAP leaf size")
    require(u64(data, 0, endian) == 1 << 63 and unpack(endian + "I", data, 24)[0] == 0x2AB1EAF,
            "Invalid ZAP leaf header")
    expected = unpack(endian + "H", data, 30)[0]
    nhash = size // 32
    start = 48 + 2 * nhash
    nchunks = (size - start) // 24

    def chunk(index, typ):
        require(0 <= index < nchunks, "ZAP chunk out of bounds")
        raw = data[start + index * 24:start + (index + 1) * 24]
        require(raw[0] == typ, "Wrong ZAP chunk type")
        return raw

    def array(index, length):
        require(0 < length <= size, "Invalid ZAP array length")
        result = bytearray()
        seen = set()
        while len(result) < length:
            require(index not in seen, "Cycle in ZAP array")
            seen.add(index)
            raw = chunk(index, 251)
            result.extend(raw[1:22][:length - len(result)])
            index = unpack(endian + "H", raw, 22)[0]
        require(index == 65535, "Overlong ZAP array chain")
        return bytes(result)

    seen, values = set(), {}
    for (index,) in struct.iter_unpack(endian + "H", data[48:start]):
        while index != 65535:
            require(index not in seen, "Cycle/duplicate in ZAP hash chain")
            seen.add(index)
            entry = chunk(index, 252)
            intlen = entry[1]
            nxt, name_chunk, name_len, value_chunk, value_len = unpack(endian + "5H", entry, 2)
            require(intlen in (1, 2, 4, 8), "Invalid ZAP integer size")
            name = array(name_chunk, name_len)
            require(name[-1:] == b"\0" and b"\0" not in name[:-1] and name[:-1] not in values,
                    "Invalid/duplicate fat ZAP name")
            raw = array(value_chunk, value_len * intlen)
            # Values in leaf array chunks always use big-endian byte encoding.
            values[name[:-1]] = tuple(int.from_bytes(raw[i:i + intlen], "big")
                                      for i in range(0, len(raw), intlen))
            index = nxt
    require(len(seen) == expected, "ZAP leaf entry count mismatch")
    return values
