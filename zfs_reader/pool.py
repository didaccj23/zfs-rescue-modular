from __future__ import annotations

import os
import stat

from .blocks import BP, BlockReader
from .base import MAX_ENTRIES, Unsupported, checked_name, require, u64, unpack
from .objects import ObjectSet, scalar

class PoolView:
    def __init__(self, sources, info, uber, sector_recovery=False):
        self.info, self.uber = info, uber
        self.reader = BlockReader(sources, info["vdev_id"], uber.txg, sector_recovery)
        self.mos = ObjectSet(self.reader.read(uber.root), self.reader, 1)
        self.rootdir = scalar(self.mos.zap(1), b"root_dataset")

    def directory(self, number):
        dn = self.mos.node(number)
        require(dn.bonustype == 12 and len(dn.bonus) >= 256, "Invalid DSL directory")
        return u64(dn.bonus, 8, dn.endian), u64(dn.bonus, 32, dn.endian)

    def dataset(self, name):
        path, sep, snapshot = name.partition("@")
        parts = os.fsencode(path).split(b"/")
        require(parts and parts[0] == os.fsencode(self.info["pool"]), "Dataset must include the pool name")
        directory = self.rootdir
        for part in parts[1:]:
            checked_name(part)
            _, childmap = self.directory(directory)
            directory = scalar(self.mos.zap(childmap), part)
        obj, _ = self.directory(directory)
        dn = self.mos.node(obj)
        require(dn.bonustype == 16 and len(dn.bonus) >= 320, "Invalid DSL dataset")
        if sep:
            checked_name(os.fsencode(snapshot))
            snapmap = u64(dn.bonus, 32, dn.endian)
            obj = scalar(self.mos.zap(snapmap), os.fsencode(snapshot))
            dn = self.mos.node(obj)
            require(dn.bonustype == 16 and len(dn.bonus) >= 320, "Invalid snapshot dataset")
        bp = BP(dn.bonus[128:256], dn.endian)
        return Filesystem(ObjectSet(self.reader.read(bp), self.reader, 2))

    def datasets(self):
        stack = [(self.rootdir, os.fsencode(self.info["pool"]), 0)]
        seen = set()
        while stack:
            number, path, depth = stack.pop()
            require(depth <= 256 and number not in seen and len(seen) < MAX_ENTRIES,
                    "Cycle/excessive DSL dataset tree")
            seen.add(number)
            head, childmap = self.directory(number)
            yield {"dataset": os.fsdecode(path), "dsl_object": head}
            if head:
                dn = self.mos.node(head)
                require(dn.bonustype == 16, "Invalid DSL head dataset")
                snapmap = u64(dn.bonus, 32, dn.endian)
                if snapmap:
                    for name, value in self.mos.zap(snapmap).items():
                        require(len(value) == 1, "Invalid snapshot map value")
                        yield {"dataset": os.fsdecode(path + b"@" + checked_name(name)), "dsl_object": value[0]}
            if childmap:
                for name, value in self.mos.zap(childmap).items():
                    require(len(value) == 1, "Invalid child dataset map value")
                    stack.append((value[0], path + b"/" + checked_name(name), depth + 1))


class Filesystem:
    def __init__(self, objset):
        self.os = objset
        self.master = objset.zap(1)
        self.root = scalar(self.master, b"ROOT")
        self.registry = self.layouts = None
        require(self.os.node(self.root).type == 20, "Dataset root is not a directory")

    def _load_sa(self):
        if self.registry is not None:
            return
        master = self.os.zap(scalar(self.master, b"SA_ATTRS"))
        registry = self.os.zap(scalar(master, b"REGISTRY"))
        layouts = self.os.zap(scalar(master, b"LAYOUTS"))
        result = {}
        for name, values in registry.items():
            require(len(values) == 1, "Invalid SA registration")
            v = values[0]
            attrid, length, swap = v & 65535, (v >> 24) & 65535, (v >> 16) & 255
            require(attrid not in result, "Duplicate SA attribute ID")
            result[attrid] = name, length, swap
        self.registry = result
        self.layouts = layouts

    def _sa_buffer(self, data, endian):
        if len(data) >= 8 and data[:8] == bytes(8):
            return {}  # empty bonus; attributes may live in the spill
        require(unpack(endian + "I", data)[0] == 0x2F505A, "Invalid SA magic")
        info = unpack(endian + "H", data, 4)[0]
        if info == 0:
            return {}  # ZPL's implicit empty SA layout (see SA_LAYOUT_NUM)
        layout, headsize = info & 1023, (info >> 10) * 8
        attrs = self.layouts.get(str(layout).encode())
        require(attrs is not None, f"Missing SA layout {layout}")
        require(8 <= headsize <= len(data), "Invalid SA header size")
        result = {}
        pos, varpos = headsize, 6
        nvars = 0
        for attr in attrs:
            require(attr in self.registry, "Unregistered SA attribute")
            name, length, swap = self.registry[attr]
            if length == 0:
                require(varpos + 2 <= headsize, "Missing variable SA length")
                length = unpack(endian + "H", data, varpos)[0]
                varpos += 2
                nvars += 1
            require(pos + length <= len(data) and name not in result, "Invalid SA attribute bounds")
            raw = data[pos:pos + length]
            result[name] = (raw, endian)
            pos = (pos + length + 7) & ~7
        require(((6 + 2 * nvars + 7) & ~7) == headsize, "SA layout/header size disagreement")
        return result


    def attributes(self, dn):
        if dn.bonustype == 17:
            require(len(dn.bonus) >= 264, "Truncated legacy znode")
            result = {"mode": u64(dn.bonus, 72, dn.endian), "size": u64(dn.bonus, 80, dn.endian),
                      "links": u64(dn.bonus, 96, dn.endian), "uid": u64(dn.bonus, 128, dn.endian),
                      "gid": u64(dn.bonus, 136, dn.endian),
                      "mtime": unpack(dn.endian + "2Q", dn.bonus, 16)}
            if stat.S_ISLNK(result["mode"]) and 264 + result["size"] <= len(dn.bonus_capacity):
                result["symlink"] = dn.bonus_capacity[264:264 + result["size"]]
            return result
        if dn.bonustype != 44:
            raise Unsupported(f"Unsupported file bonus type {dn.bonustype}")
        self._load_sa()
        values = self._sa_buffer(dn.bonus, dn.endian) if dn.bonus else {}
        if dn.spill:
            spill = self.os.reader.read(dn.spill)
            extra = self._sa_buffer(spill.data, spill.endian)
            require(not values.keys() & extra.keys(), "Duplicate bonus/spill SA attributes")
            values.update(extra)
        result = {}
        for key in ("MODE", "SIZE", "LINKS", "UID", "GID"):
            name = ("ZPL_" + key).encode()
            require(name in values and len(values[name][0]) == 8, f"Missing/invalid {name!r}")
            raw, endian = values[name]
            result[key.lower()] = u64(raw, 0, endian)
        if b"ZPL_MTIME" in values:
            raw, endian = values[b"ZPL_MTIME"]
            require(len(raw) == 16, "Invalid modification time")
            result["mtime"] = unpack(endian + "2Q", raw)
        if b"ZPL_SYMLINK" in values:
            result["symlink"] = values[b"ZPL_SYMLINK"][0]
        return result
