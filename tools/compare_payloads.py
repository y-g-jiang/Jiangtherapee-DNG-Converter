                     
"""Compare the *image payloads* of two DNGs, independent of layout.

The payloads are the bytes a reader actually renders: the CFA strip/tile data,
the uncompressed thumbnail and the JPEG preview. Byte-identity of those is a
much stronger and more meaningful statement than a byte-offset diff, because two
files that differ by one metadata tag shift every following byte.

usage: compare_payloads.py <ours.dng> <reference.dng>
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compare import Tiff, values
import os
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "tools")
WORK = os.path.join(BASE, "work")
REF = os.path.join(BASE, "ref")
BIN = os.path.join(BASE, "bin")



def payloads(path):
    t = Tiff(path)
    out = {}
    for n, ifd in enumerate(t.ifds()):
        tg = {e["tag"]: e for e in ifd["entries"]}
        parts = []
        for off_tag, cnt_tag in ((0x0111, 0x0117), (0x0144, 0x0145)):
            if off_tag in tg and cnt_tag in tg:
                offs = values(tg[off_tag]["raw"], tg[off_tag]["type"])
                cnts = values(tg[cnt_tag]["raw"], tg[cnt_tag]["type"])
                for x, y in zip(offs, cnts):
                    parts.append(t.data[x:x + y])
        if parts:
            w = values(tg[0x0100]["raw"], tg[0x0100]["type"])[0] if 0x0100 in tg else 0
            h = values(tg[0x0101]["raw"], tg[0x0101]["type"])[0] if 0x0101 in tg else 0
            out[(ifd["ctx"], n, w, h)] = b"".join(parts)
    return out


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: compare_payloads.py <ours.dng> <reference.dng>")
    ours = payloads(sys.argv[1])
    ref = payloads(sys.argv[2])
    total = 0
    same = 0
    for key in sorted(ref, key=lambda k: -len(ref[k])):
        a = ours.get(key)
        b = ref[key]
        total += len(b)
        ok = a is not None and a == b
        same += len(b) if ok else 0
        state = "IDENTICAL" if ok else ("MISSING" if a is None else "DIFFERS")
        print("%-34s %9d bytes  %s" % (str(key), len(b), state))
                                                                      
    for key in sorted(set(ours) - set(ref)):
        print("%-34s %9d bytes  ONLY-IN-OURS" % (str(key), len(ours[key])))
    print()
    print("payload bytes identical: %d / %d = %.4f%%" % (same, total, 100.0 * same / max(1, total)))
    return 0 if same == total else 1


if __name__ == "__main__":
    sys.exit(main())
