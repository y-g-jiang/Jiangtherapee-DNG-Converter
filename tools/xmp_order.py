import os
                     
"""Reorder a DNG's XMP packet into the order Adobe DNG Converter uses.

The XMP toolkit serialises properties and child elements in the order they were
first set, and that order is an artefact of ADC's code path, which the DNG SDK
gives a caller no control over. Our packet already has the same content *and the
same length* as ADC's; only the order differs.

The target order is not universal (on some bodies `dc:format` sits before the aux
group, on others after `stRef`), so it is recovered per conversion group into
ref/xmp_props.json (`__ns_order__`, `__prop_order__`, `__block_order__`) and this
tool permutes three things into that order, in place:

  * the namespace declarations,
  * the Description's attributes,
  * the Description's child elements,

each by permuting chunks that carry their own surrounding whitespace, so the byte
count cannot change.

usage: xmp_reorder.py <file.dng> [--camera NAME] [--group SUB] [--dry]
"""
import json
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compare import Tiff

TABLE = None
ATTR_RE = re.compile(r'(\s*)([A-Za-z][\w]*:[\w]+="[^"]*")')


def load_order(camera, group):
    global TABLE
    if TABLE is None:
        try:
            TABLE = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ref", "xmp_props.json"), encoding="utf-8"))
        except Exception:
            TABLE = {}
    if not camera:
        return [], [], []
    rec = TABLE.get(camera, {}).get(group or "default", {})
    return (rec.get("__ns_order__") or [],
            rec.get("__prop_order__") or [],
            rec.get("__block_order__") or [])


def reorder_attrs(region, prop_order, ns_order):
    chunks = ATTR_RE.findall(region)
    if not chunks or "".join(w + a for w, a in chunks) != region:
        return region
    ranked = []
    for idx, (w, a) in enumerate(chunks):
        name = a.split("=", 1)[0]
        if name == "rdf:about":
            key = (-1, 0, idx)
        elif name.startswith("xmlns:"):
            pref = name.split(":", 1)[1]
            key = (0, ns_order.index(pref) if pref in ns_order else 99, idx)
        else:
            key = (1, prop_order.index(name) if name in prop_order else 99, idx)
        ranked.append((key, w, a))
    ranked.sort(key=lambda t: t[0])
    return "".join(w + a for _k, w, a in ranked)


def block_spans(region, block_order):
    spans = []
    for name in block_order:
        pos = region.find("<" + name)
        if pos < 0:
            continue
                                                                                 
        start = pos
        while start > 0 and region[start - 1] in " \t\r\n":
            start -= 1
        selfclose = region.find("/>", pos)
        close = region.find("</" + name + ">", pos)
        if close < 0 or (0 <= selfclose < close):
            end = selfclose + 2
        else:
            end = close + len("</" + name + ">")
        spans.append((name, start, end))
    spans.sort(key=lambda t: t[1])
                   
    keep, last = [], -1
    for name, s, e in spans:
        if s >= last:
            keep.append((name, s, e))
            last = e
    return keep


def reorder_blocks(region, block_order):
    spans = block_spans(region, block_order)
    if len(spans) < 2:
        return region
    chunks, cursor = [], 0
    for name, s, e in spans:
        if s < cursor:
            return region
        chunks.append((name, region[cursor:e]))
        cursor = e
    tail = region[cursor:]
    if "".join(c for _n, c in chunks) + tail != region:
        return region
    chunks.sort(key=lambda c: block_order.index(c[0]) if c[0] in block_order else 99)
    rebuilt = "".join(c for _n, c in chunks) + tail
    return rebuilt if len(rebuilt) == len(region) else region


def reorder_packet(text, ns_order, prop_order, block_order):
    m = re.search(r"<rdf:Description\b([^>]*)>", text)
    if not m:
        return text
    close = text.rfind("</rdf:Description>")
    if close < 0:
        return text
    attrs = reorder_attrs(m.group(1), prop_order, ns_order)
    children = reorder_blocks(text[m.end(1) + 1:close], block_order)
    out = text[:m.start(1)] + attrs + ">" + children + text[close:]
    return out if len(out) == len(text) else text


def main():
    path = sys.argv[1]
    dry = "--dry" in sys.argv
    camera = group = None
    if "--camera" in sys.argv:
        camera = sys.argv[sys.argv.index("--camera") + 1]
    if "--group" in sys.argv:
        group = sys.argv[sys.argv.index("--group") + 1]
    return reorder_file(path, camera, group, dry)


def reorder_file(path, camera=None, group=None, dry=False):
    ns_order, prop_order, block_order = load_order(camera, group)
    if not ns_order and not prop_order:
        print("no order table for %s/%s" % (camera, group))
        return 1
    data = bytearray(open(path, "rb").read())
    t = Tiff(path)
    tg = {e["tag"]: e for e in t.ifds()[0]["entries"]}
    if 0x02BC not in tg:
        print("no XMP")
        return 1
    e = tg[0x02BC]
    off, cnt = e["off"], e["count"]
    text = bytes(data[off:off + cnt]).decode("utf-8", "replace")
    new = reorder_packet(text, ns_order, prop_order, block_order)
    if len(new) != len(text):
        print("length changed: %d vs %d" % (len(new), len(text)))
        return 2
    if new == text:
        print("unchanged")
        return 0
    if dry:
        print("would reorder (dry run)")
        return 0
    data[off:off + cnt] = new.encode("utf-8")
    open(path, "wb").write(data)
    print("reordered %d bytes of XMP" % cnt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
