from __future__ import annotations

from collections import Counter

from .blocks import BP, Uberblock, label_offsets
from .base import Corrupt, LABEL_SIZE, LOG, RescueError, UBER_MAGIC, Unsupported, parse_nvlist, require, u64, unpack, verify_label

def scan(sources):
    configs = []
    stats = Counter()
    for source in sources:
        valid = []
        for index, offset in enumerate(label_offsets(source)):
            try:
                raw = source.read(offset + (16 << 10), 112 << 10)
                verify_label(raw, offset + (16 << 10))
                config = parse_nvlist(raw[:-40])
                require(all(k in config for k in ("pool_guid", "guid", "top_guid", "name", "vdev_tree")),
                        "Incomplete pool label")
                valid.append(config)
                stats["valid_config_labels"] += 1
            except RescueError as exc:
                stats["invalid_config_labels"] += 1
                LOG.warning("%s label %d configuration: %s", source.path, index, exc)
        if not valid:
            raise Corrupt(f"No verified pool configuration on {source.path}. Check partition offset/length with probe.")
        identities = {(x["pool_guid"], x["guid"], x["top_guid"]) for x in valid}
        require(len(identities) == 1, "Conflicting pool/member identities across labels; inspect images first")
        configs.append(max(valid, key=lambda x: x.get("txg", 0)))
    first = configs[0]
    require(len({x["guid"] for x in configs}) == len(configs), "Inputs are copies of the same mirror member")
    require(all(x["pool_guid"] == first["pool_guid"] and x["top_guid"] == first["top_guid"]
                for x in configs), "Sources do not belong to the same pool and top-level vdev")
    trees = []
    for config in configs:
        tree = config["vdev_tree"]
        if tree.get("type") == "root":
            children = tree.get("children", [])
            require(len(children) == 1, "Only a single top-level mirror is supported")
            tree = children[0]
        if tree.get("type") != "mirror" or len(tree.get("children", [])) != 2:
            raise Unsupported("Expected a two-leaf mirror; no RAIDZ, replacing, spare or indirect topology")
        if config.get("vdev_children", 1) != 1:
            raise Unsupported("Pool has multiple top-level vdevs (including possible special/log vdevs)")
        leaves = tree["children"]
        require(all(x.get("type") in ("disk", "file") and not x.get("children") for x in leaves),
                "Unsupported mirror child topology")
        require(tree.get("guid") == config["top_guid"] and config["guid"] in {x["guid"] for x in leaves},
                "Leaf/top GUID mismatch")
        require(isinstance(tree.get("ashift"), int) and 9 <= tree["ashift"] <= 16,
                "Invalid/missing ashift")
        trees.append(tree)
    identity = lambda x: (x.get("id"), x["guid"], x["ashift"], tuple(sorted(c["guid"] for c in x["children"])))
    require(all(identity(x) == identity(trees[0]) for x in trees), "Conflicting mirror configurations")
    tree = trees[0]
    require(isinstance(tree.get("id"), int), "Missing top-level vdev ID")
    ubsize = 1 << min(max(tree["ashift"], 10), 13)
    candidates = {}
    for source in sources:
        for label, base in enumerate(label_offsets(source)):
            for relative in range(128 << 10, LABEL_SIZE, ubsize):
                offset = base + relative
                try:
                    data = source.read(offset, ubsize)
                    if not any(data[:8]):
                        continue
                    endian = next((e for e in ("<", ">") if u64(data, 0, e) == UBER_MAGIC), None)
                    require(endian is not None, "Invalid uberblock magic")
                    require(verify_label(data, offset) == endian, "Uberblock/checksum byte order mismatch")
                    _, version, txg, _, timestamp = unpack(endian + "5Q", data)
                    require((1 <= version <= 28 or version == 5000) and txg > 0, "Invalid uberblock version/TXG")
                    root = BP(data[40:168], endian)
                    require(not root.hole and root.level == 0 and ((root.prop >> 48) & 255) == 11,
                            "Invalid MOS root BP")
                    key = (txg, root.raw, endian)
                    candidates.setdefault(key, Uberblock(txg, timestamp, root, source.path, label, offset))
                    stats["valid_uberblock_copies"] += 1
                except RescueError as exc:
                    stats["invalid_uberblock_slots"] += 1
                    LOG.debug("%s uberblock %d: %s", source.path, offset, exc)
    require(bool(candidates), "No checksum-verified uberblocks found")
    info = {"pool": first["name"], "pool_guid": first["pool_guid"], "top_guid": tree["guid"],
            "vdev_id": tree["id"], "ashift": tree["ashift"], "scan": dict(stats),
            "members": [{"path": s.path, "offset": s.offset, "length": s.size,
                         "guid": c["guid"]} for s, c in zip(sources, configs)]}
    return info, sorted(candidates.values(), key=lambda x: (x.txg, x.timestamp), reverse=True)
