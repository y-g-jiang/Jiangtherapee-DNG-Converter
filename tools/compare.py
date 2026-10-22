                     
"""Byte-level and structural comparison of two DNG/TIFF files.

The acceptance test for the ADC replication is "same as ADC". Because ADC
stamps the conversion time into the file, exact byte equality is only
meaningful modulo those timestamps; this tool separates that case from real
differences.

Python API:
    result = compare(path_a, path_b)

CLI:
    compare.py <a.dng> <b.dng> [--json out.json] [--verbose]
"""
import json, os, re, struct, sys, hashlib
import os
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "tools")
WORK = os.path.join(BASE, "work")
REF = os.path.join(BASE, "ref")
BIN = os.path.join(BASE, "bin")


                                                                            
                                                                            
                                                                            
TIME_TAGS = {
    0x0132: "DateTime",
    0xC71B: "PreviewDateTime",
}
                                                                            
FINGERPRINT_TAGS = {0xC65D: "RawDataUniqueID", 0xC719: "PreviewSettingsDigest",
                    0xC7A7: "DepthFar(?)"}

TYPES = {1: ("BYTE", 1), 2: ("ASCII", 1), 3: ("SHORT", 2), 4: ("LONG", 4),
         5: ("RATIONAL", 8), 6: ("SBYTE", 1), 7: ("UNDEFINED", 1), 8: ("SSHORT", 2),
         9: ("SLONG", 4), 10: ("SRATIONAL", 8), 11: ("FLOAT", 4), 12: ("DOUBLE", 8),
         13: ("IFD", 4)}

IMAGE_TAGS = {0x0111: "StripOffsets", 0x0117: "StripByteCounts",
              0x0144: "TileOffsets", 0x0145: "TileByteCounts"}


class Tiff:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        if len(d) < 8 or d[:2] not in (b"II", b"MM"):
            raise ValueError("not a TIFF: %s" % path)
        self.endian = "<" if d[:2] == b"II" else ">"
        self.magic = self.u16(2)
        self.ifd0 = self.u32(4)

    def u16(self, o):
        return struct.unpack_from(self.endian + "H", self.data, o)[0]

    def u32(self, o):
        return struct.unpack_from(self.endian + "I", self.data, o)[0]

    def ifds(self):
        """walk IFD0, its SubIFDs, Exif/GPS/Interop, and the IFD0 chain"""
        out = []
        seen = set()
        queue = [(self.ifd0, "ifd0")]
        while queue:
            off, ctx = queue.pop(0)
            if not off or off in seen or off + 2 > len(self.data):
                continue
            seen.add(off)
            n = self.u16(off)
            entries = []
            for i in range(n):
                eo = off + 2 + i * 12
                if eo + 12 > len(self.data):
                    break
                tag, typ, cnt = struct.unpack_from(self.endian + "HHI", self.data, eo)
                tsize = TYPES.get(typ, ("?", 1))[1] * cnt
                if tsize <= 4:
                    raw = self.data[eo + 8:eo + 8 + tsize]
                    valoff = None
                else:
                    valoff = self.u32(eo + 8)
                    raw = self.data[valoff:valoff + tsize]
                entries.append(dict(tag=tag, type=typ, count=cnt, raw=raw,
                                    off=valoff, entry_off=eo))
            nxt = self.u32(off + 2 + n * 12) if off + 2 + n * 12 + 4 <= len(self.data) else 0
            out.append(dict(offset=off, ctx=ctx, entries=entries, next=nxt))
            for e in entries:
                if e["tag"] in (0x014A,):           
                    for k in range(e["count"]):
                        queue.append((struct.unpack_from(self.endian + "I", e["raw"], k * 4)[0],
                                      "subifd", ))
                if e["tag"] == 0x8769 and e["raw"]:
                    queue.append((struct.unpack_from(self.endian + "I", e["raw"], 0)[0], "exif"))
                if e["tag"] == 0x8825 and e["raw"]:
                    queue.append((struct.unpack_from(self.endian + "I", e["raw"], 0)[0], "gps"))
                if e["tag"] == 0xA005 and e["raw"]:
                    queue.append((struct.unpack_from(self.endian + "I", e["raw"], 0)[0], "interop"))
            if nxt:
                queue.append((nxt, ctx + "+next"))
        return out


def values(raw, typ, endian="<"):
    if typ in (1, 7, 6):
        return list(raw)
    if typ == 2:
        return raw.split(b"\x00")[0].decode("latin-1")
    fmt = {3: "H", 8: "h", 4: "I", 9: "i", 11: "f", 12: "d", 13: "I"}.get(typ)
    if fmt:
        n = len(raw) // struct.calcsize(fmt)
        return list(struct.unpack(endian + "%d%s" % (n, fmt), raw[:n * struct.calcsize(fmt)]))
    if typ in (5, 10):
        f = "ii" if typ == 10 else "II"
        return [list(struct.unpack_from(endian + f, raw, k * 8)) for k in range(len(raw) // 8)]
    return list(raw)


def _zero(match):
    """length-preserving replacement: digits/hex -> '0', others kept"""
    s = match.group(0)
    return re.sub(r"[0-9a-fA-F]", "0", s)


ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2})?")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
                                                                       
                                                                           
                                                                      
                                                  


def canonical_xmp(raw):
    """Length-insensitive canonical form of an XMP packet."""
    s = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) else raw
    s = ISO_TIME.sub("<TIME>", s)
    s = UUID.sub("<UUID>", s)
                                                                           
    s = re.sub(r"xmp\.(?:did|iid):<UUID>", "<UUID>", s)
    s = s.replace("\r\n", "\n")
    return s


def canonical_identical(A, B):
    """Structurally identical apart from ADC's clock and per-run UUID?

    Compares the IFD tree, every tag value (with DateTime / PreviewDateTime
    blanked and the XMP packet canonicalised) and every strip/tile payload.

    Pure layout tags (strip/tile offsets, SubIFD/Exif/GPS pointers) are allowed
    to differ, but only by one uniform shift, and that shift must equal the
    change in the XMP packet length -- i.e. the layout is identical, it merely
    moves because ADC's timestamp string can be 22 or 25 characters long.
    """
    ia, ib = A.ifds(), B.ifds()
    if len(ia) != len(ib):
        return False, "ifd count %d vs %d" % (len(ia), len(ib))
    LAYOUT_TAGS = {0x0111: "StripOffsets", 0x0144: "TileOffsets",
                   0x014A: "SubIFDs", 0x8769: "ExifIFD", 0x8825: "GPSIFD",
                   0xA005: "InteropIFD"}
    deltas = []
    xmp_delta = None
    for k, (x, y) in enumerate(zip(ia, ib)):
        if x["ctx"] != y["ctx"]:
            return False, "ifd %d context %s vs %s" % (k, x["ctx"], y["ctx"])
        ta = {e["tag"]: e for e in x["entries"]}
        tb = {e["tag"]: e for e in y["entries"]}
        if list(ta) != list(tb):
            return False, "ifd %d tag list differs" % k
        if 0x2BC in ta and 0x2BC in tb:
            d = tb[0x2BC]["count"] - ta[0x2BC]["count"]
            if xmp_delta is None:
                xmp_delta = d
        for tag in ta:
            ea, eb = ta[tag], tb[tag]
            if tag in LAYOUT_TAGS:
                if ea["count"] != eb["count"] or ea["type"] != eb["type"]:
                    return False, "ifd %d layout tag %s shape differs" % (k, LAYOUT_TAGS[tag])
                va = values(ea["raw"], ea["type"], "<")
                vb = values(eb["raw"], eb["type"], "<")
                deltas.extend([vb[i] - va[i] for i in range(len(va))])
                continue
            if tag == 0x2BC:
                if canonical_xmp(ea["raw"]) == canonical_xmp(eb["raw"]):
                    continue
                return False, "ifd %d XMP differs" % k
            if ea["type"] != eb["type"] or ea["count"] != eb["count"]:
                return False, "ifd %d tag 0x%04X shape %s/%s vs %s/%s" % (
                    k, tag, ea["type"], ea["count"], eb["type"], eb["count"])
            if ea["raw"] == eb["raw"]:
                continue
            if tag in TIME_TAGS:
                continue
            return False, "ifd %d tag 0x%04X value differs" % (k, tag)
                  
        for okey, ckey in ((0x0111, 0x0117), (0x0144, 0x0145)):
            if okey in ta:
                oa = values(ta[okey]["raw"], ta[okey]["type"], A.endian)
                ca = values(ta[ckey]["raw"], ta[ckey]["type"], A.endian)
                ob = values(tb[okey]["raw"], tb[okey]["type"], B.endian)
                cb = values(tb[ckey]["raw"], tb[ckey]["type"], B.endian)
                if len(oa) != len(ob):
                    return False, "ifd %d block count %d vs %d" % (k, len(oa), len(ob))
                if ca != cb:
                    return False, "ifd %d block byte counts differ" % k
                for i in range(len(oa)):
                    if A.data[oa[i]:oa[i] + ca[i]] != B.data[ob[i]:ob[i] + cb[i]]:
                        return False, "ifd %d block %d payload differs" % (k, i)
    if deltas:
        uniq = set(deltas)
        if len(uniq) != 1:
            return False, "layout shifted by several amounts: %s" % sorted(uniq)[:6]
        shift = uniq.pop()
        if xmp_delta is not None and shift != xmp_delta:
            return False, "layout shift %d != XMP length delta %d" % (shift, xmp_delta)
    return True, ""


def normalize(data, path=None):
    """Blank out the fields ADC derives from wall-clock time or randomness.

    Verified empirically: two consecutive ADC runs on the same input differ in
    exactly these bytes and nowhere else (see tests/test_normalization.py).

    * TIFF DateTime (0x0132) and PreviewDateTime (0xC71B)
    * inside the XMP packet: ISO-8601 stamps and xmp.iid/xmp.did UUIDs

    Replacements are length preserving, so the file layout stays valid and the
    rest of the file can still be compared byte for byte.
    """
    t = Tiff.__new__(Tiff)
    t.data = data
    t.path = path or "<mem>"
    d = bytearray(data)
    endian = "<" if d[:2] == b"II" else ">"
    t.endian = endian
    try:
        ifd0 = struct.unpack_from(endian + "I", d, 4)[0]
        t.ifd0 = ifd0
    except Exception:
        return bytes(d)
    try:
        entries = t.ifds()
    except Exception:
        return bytes(d)
    for ifd in entries:
        for e in ifd["entries"]:
            if e["tag"] in (0x0132, 0xC71B):
                off = e["off"] if e["off"] else e["entry_off"] + 8
                for k in range(len(e["raw"])):
                    d[off + k] = 0x30       
            elif e["tag"] == 0x2BC:
                off = e["off"] if e["off"] else e["entry_off"] + 8
                raw = bytes(d[off:off + e["count"]])
                new = ISO_TIME.sub(_zero, raw.decode("latin-1")).encode("latin-1")
                new = UUID.sub(_zero, new.decode("latin-1")).encode("latin-1")
                d[off:off + e["count"]] = new
    return bytes(d)


def compare(a_path, b_path, max_diffs=400):
    A, B = Tiff(a_path), Tiff(b_path)
    res = dict(a=a_path, b=b_path, a_bytes=len(A.data), b_bytes=len(B.data),
               diffs=[], payload=[], verdict=None)

    exact = A.data == B.data
    res["byte_identical"] = exact
    na, nb = normalize(A.data, a_path), normalize(B.data, b_path)
    res["normalized_identical"] = (na == nb)
    if not res["normalized_identical"] and len(na) == len(nb):
        for k in range(len(na)):
            if na[k] != nb[k]:
                res["first_normalized_diff"] = dict(offset=k, a=na[k], b=nb[k])
                break
    try:
        ok_can, why_not = canonical_identical(A, B)
    except Exception as exc:                                                   
        ok_can, why_not = False, "%s: %s" % (type(exc).__name__, exc)
    res["canonical_identical"] = ok_can
    res["canonical_reason"] = why_not

    ia, ib = A.ifds(), B.ifds()
    res["ifd_count"] = [len(ia), len(ib)]
                   
    for k in range(max(len(ia), len(ib))):
        if k >= len(ia) or k >= len(ib):
            res["diffs"].append(dict(kind="ifd_missing", index=k))
            continue
        x, y = ia[k], ib[k]
        if x["offset"] != y["offset"]:
            res["diffs"].append(dict(kind="ifd_offset", index=k, ctx=x["ctx"],
                                     a=x["offset"], b=y["offset"]))
        ta = {e["tag"]: e for e in x["entries"]}
        tb = {e["tag"]: e for e in y["entries"]}
        if x["entries"] and y["entries"] and [e["tag"] for e in x["entries"]] != \
                [e["tag"] for e in y["entries"]]:
            only_a = [t for t in ta if t not in tb]
            only_b = [t for t in tb if t not in ta]
            same_set = set(ta) == set(tb)
            res["diffs"].append(dict(kind="tag_order" if same_set else "tag_set",
                                     index=k, ctx=x["ctx"],
                                     only_a=[hex(t) for t in only_a],
                                     only_b=[hex(t) for t in only_b]))
        for tag in sorted(set(ta) | set(tb)):
            ea, eb = ta.get(tag), tb.get(tag)
            if ea is None or eb is None:
                res["diffs"].append(dict(kind="tag_missing", index=k, ctx=x["ctx"],
                                         tag=hex(tag), side="a" if ea is None else "b"))
                continue
            if ea["type"] != eb["type"] or ea["count"] != eb["count"]:
                res["diffs"].append(dict(kind="tag_shape", index=k, ctx=x["ctx"],
                                         tag=hex(tag),
                                         a=[ea["type"], ea["count"]], b=[eb["type"], eb["count"]]))
                continue
            if ea["raw"] != eb["raw"]:
                kind = "time" if tag in TIME_TAGS else "value"
                res["diffs"].append(dict(kind=kind, tag=hex(tag), index=k, ctx=x["ctx"],
                                         a=values(ea["raw"], ea["type"])[:16],
                                         b=values(eb["raw"], eb["type"])[:16]))
            elif ea["off"] is not None and eb["off"] is not None and ea["off"] != eb["off"]:
                res["diffs"].append(dict(kind="data_offset", index=k, ctx=x["ctx"],
                                         tag=hex(tag), a=ea["off"], b=eb["off"]))

                                         
    for k in range(min(len(ia), len(ib))):
        ta = {e["tag"]: e for e in ia[k]["entries"]}
        tb = {e["tag"]: e for e in ib[k]["entries"]}
        for okey, ckey in ((0x0111, 0x0117), (0x0144, 0x0145)):
            if okey in ta and okey in tb and ckey in ta and ckey in tb:
                oa = values(ta[okey]["raw"], ta[okey]["type"], A.endian)
                ca = values(ta[ckey]["raw"], ta[ckey]["type"], A.endian)
                ob = values(tb[okey]["raw"], tb[okey]["type"], B.endian)
                cb = values(tb[ckey]["raw"], tb[ckey]["type"], B.endian)
                n = min(len(oa), len(ob))
                same = 0
                first_bad = None
                for i in range(n):
                    pa = A.data[oa[i]:oa[i] + ca[i]]
                    pb = B.data[ob[i]:ob[i] + cb[i]]
                    if pa == pb and ca[i] == cb[i]:
                        same += 1
                    elif first_bad is None:
                        first_bad = (i, hashlib.sha256(pa).hexdigest()[:16],
                                     hashlib.sha256(pb).hexdigest()[:16], ca[i], cb[i])
                res["payload"].append(dict(ifd=k, kind="tile" if okey == 0x0144 else "strip",
                                           blocks=[len(oa), len(ob)], matching=same,
                                           first_diff=first_bad))

             
    real = [d for d in res["diffs"] if d["kind"] != "time"]
    payload_ok = all(p["matching"] == p["blocks"][0] == p["blocks"][1] for p in res["payload"])
    if exact:
        res["verdict"] = "IDENTICAL"
    elif res["normalized_identical"]:
        res["verdict"] = "IDENTICAL_NORMALIZED"
    elif res["canonical_identical"]:
                                                                          
                                                
        res["verdict"] = "IDENTICAL_CANONICAL"
    elif not real and payload_ok and res["a_bytes"] == res["b_bytes"]:
        res["verdict"] = "CONTAINER"
    elif payload_ok:
        res["verdict"] = "CONTAINER"
    else:
        res["verdict"] = "PIXELS"
    res["diffs"] = res["diffs"][:max_diffs]
                                                                             
                                                                                
                                                    
    accepted = res["normalized_identical"] or res["canonical_identical"]
    res["n_diffs"] = 0 if accepted else len(real)
    return res


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        raise SystemExit(__doc__)
    r = compare(args[0], args[1])
    print("%s\n  vs %s" % (r["a"], r["b"]))
    print("  bytes %d / %d" % (r["a_bytes"], r["b_bytes"]))
    print("  verdict: %s   real_diffs=%d" % (r["verdict"], r["n_diffs"]))
    for p in r["payload"]:
        print("  payload ifd%d %s blocks=%s matching=%d first_diff=%s" %
              (p["ifd"], p["kind"], p["blocks"], p["matching"], p["first_diff"]))
    shown = 0
    for d in r["diffs"]:
        if d["kind"] == "time":
            continue
        print("  DIFF", json.dumps(d, default=str)[:300])
        shown += 1
        if shown > 40:
            print("  ...")
            break
    for a in sys.argv[1:]:
        if a.startswith("--json="):
            with open(a.split("=", 1)[1], "w", encoding="utf-8") as f:
                json.dump(r, f, indent=1, default=str)


if __name__ == "__main__":
    main()
