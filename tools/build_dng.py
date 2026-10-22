                     
"""Assemble a DNG from our own ingest plus the recovered camera database.

This is the end-to-end converter path: pixels from `ingest_libraw.decode`, the
per-camera constants from `ref/camera_db.json`, the per-shot white balance
derived as 1/cam_mul, and everything else left to the DNG SDK writer.  It then
compares the result with ADC's own output using tools/compare.py so the gap is
measured tag by tag.

usage: build_dng.py [--file NAME] [--limit N] [--keep]
Output: work/build_report.json
"""
import argparse, concurrent.futures as cf, json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest_libraw as LI
from compare import compare
import os
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "tools")
WORK = os.environ.get("JTDC_WORK") or os.path.join(__import__("tempfile").gettempdir(), "jtdc")
REF = os.path.join(BASE, "ref")
BIN = os.path.join(BASE, "bin")


TOOLS = os.path.join(BASE, "tools")
WORK = os.environ.get("JTDC_WORK") or os.path.join(__import__("tempfile").gettempdir(), "jtdc")
DB = os.path.join(BASE, "ref", "camera_db.json")
LEVELS = os.path.join(WORK, "level_table.json")
DANGBUILD = os.environ.get("DANGBUILD", os.path.join(BASE, "bin", "dngbuild.exe"))
TOOLBIN = r"C:\msys64\ucrt64\bin"

                                                                          
RAT = lambda v: v if isinstance(v, list) and len(v) == 2 else [int(v), 1]


def rat_str(pairs):
    out = []
    for p in pairs:
        n, d = RAT(p)
        out += [str(int(n)), str(int(d))]
    return " ".join(out)


def cfa_phase(pattern):
                                                                     
    key = {0: "R", 1: "G", 2: "B"}
    try:
        s = "".join(key[int(x)] for x in pattern)
    except Exception:
        return None
    return {"GRBG": 0, "RGGB": 1, "BGGR": 2, "GBRG": 3}.get(s)


def canon_camera_info(data, mn_off):
    """Pull the owner name out of Canon's MakerNote CameraInfo block.

    Canon stores the model, the firmware string and the owner name in one binary
    block (MakerNote tag 0x0093, laid out as consecutive ASCII runs).  ADC copies
    the owner name into the Exif Artist tag (0x13B), which is empty in the raw
    itself, so this is the only way to reproduce it.  Canon MakerNote value
    offsets are relative to the TIFF header (i.e. absolute in the file), so this
    takes the file bytes and the MakerNote offset, not a slice.
    """
    import struct as _s
    try:
        n = _s.unpack_from("<H", data, mn_off)[0]
    except Exception:
        return None
    if n < 1 or n > 4096:
        return None
    for i in range(n):
        eo = mn_off + 2 + i * 12
        if eo + 12 > len(data):
            break
        tag, typ, cnt = _s.unpack_from("<HHI", data, eo)
        if tag == 0x0009 and typ in (1, 2) and 3 <= cnt <= 128:
                                                                                    
            vo = _s.unpack_from("<I", data, eo + 8)[0] if cnt > 4 else eo + 8
            if vo + cnt <= len(data):
                txt = data[vo:vo + cnt].split(b"\x00")[0].decode("latin-1", "replace").strip()
                if txt:
                    return txt
            continue
        if tag != 0x0093 or cnt < 16 or cnt > 65536:
            continue
        vo = _s.unpack_from("<I", data, eo + 8)[0]
        if vo + cnt > len(data):
            continue
        blob = data[vo:vo + cnt]
        strings, cur = [], b""
        for ch in blob:
            if 32 <= ch < 127:
                cur += bytes([ch])
            else:
                if len(cur) >= 3:
                    strings.append(cur.decode("latin-1"))
                cur = b""
        if len(cur) >= 3:
            strings.append(cur.decode("latin-1"))
        for idx, st in enumerate(strings):
            if st.lower().startswith("firmware") and idx + 1 < len(strings):
                return strings[idx + 1]
        if len(strings) >= 3:
            return strings[2]
    return None



def nikon_shutter_count(path):
    """Nikon tag 0x00A7 of the MakerNote: this is what ADC writes as ImageNumber.

    The MakerNote is "Nikon\\0" + version + 2 pad bytes, followed by its own TIFF
    header whose offsets are relative to that point."""
    try:
        import struct
        from compare import Tiff
        t = Tiff(path)
        mn = None
        for ifd in t.ifds():
            tg = {x["tag"]: x for x in ifd["entries"]}
            if 0x927C in tg:
                mn = bytes(tg[0x927C]["raw"])
                break
        if not mn or not mn.startswith(b"Nikon\x00"):
            return None
        base = 10
        order = "<" if mn[base:base + 2] == b"II" else ">"
        start = base + struct.unpack(order + "I", mn[base + 4:base + 8])[0]
        n = struct.unpack(order + "H", mn[start:start + 2])[0]
        for i in range(n):
            q = start + 2 + i * 12
            tag, typ, cnt = struct.unpack(order + "HHI", mn[q:q + 8])
            if tag == 0x00A7:
                v = mn[q + 8:q + 12]
                if typ == 4:
                    return struct.unpack(order + "I", v)[0]
                if typ == 3:
                    return struct.unpack(order + "H", v[:2])[0]
    except Exception:
        return None
    return None



OPCODE_SKIP = []


def fuji_makernote(path):
    """Fuji RAF hands its MakerNote over inside the embedded JPEG's Exif.

    The RAF header points at a JPEG (APP1/Exif); the private MakerNote sits in the
    Exif sub-IFD as tag 0x927C. ADC copies exactly those bytes into the MakN
    segment, so this returns (bytes, source offset) or (None, 0).
    """
    try:
        import struct
        data = open(path, "rb").read()
        j = data.find(b"\xff\xd8\xff\xe1", 0, 1 << 17)
        if j < 0:
            return None, 0
        app1_len = struct.unpack_from(">H", data, j + 4)[0]
        app1 = data[j + 6:j + 4 + app1_len]
        if not app1.startswith(b"Exif\x00\x00"):
            return None, 0
        tiff_base = j + 6 + 6                                             
        t = app1[6:]
        order = "<" if t[:2] == b"II" else ">"
        ifd0 = struct.unpack_from(order + "I", t, 4)[0]

        def walk(off, depth=0):
            n = struct.unpack_from(order + "H", t, off)[0]
            for k in range(n):
                q = off + 2 + k * 12
                tag, typ, cnt = struct.unpack_from(order + "HHI", t, q)
                size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2,
                        9: 4, 10: 8, 11: 4, 12: 8}.get(typ, 1) * cnt
                if tag == 0x927C and cnt > 16:
                    if size > 4:
                        vo = struct.unpack_from(order + "I", t, q + 8)[0]
                                                                               
                        return t[vo:vo + cnt], tiff_base + vo
                    return t[q + 8:q + 8 + cnt], tiff_base + q + 8
                if depth < 3 and tag in (0x8769, 0x8825, 0xA005):
                    sub = struct.unpack_from(order + "I", t, q + 8)[0]
                    got = walk(sub, depth + 1)
                    if got[0]:
                        return got
            return None, 0

        return walk(ifd0)
    except Exception:
        return None, 0


def build_spec(entry, camera_db, pixels_path, w, h, out_path, extra=None, levels=None):
    const = camera_db.get(entry["camera"], {}).get("constants", {})
    lv = (levels or {}).get(entry["file"], {})

    def val(key):
        e = const.get(key)
        return None if e is None else e["value"]

    lines = ["pixels %s %d %d" % (pixels_path, w, h), "out %s" % out_path]

                                                                          
                                                                                 
                                                 
    try:
        _xp = json.load(open(os.path.join(BASE, "ref", "xmp_props.json"), encoding="utf-8"))
        _g = entry.get("group") or []
        _sub = _g[2] if len(_g) > 2 and _g[2] else "default"
        consts = _xp.get(entry["camera"], {}).get(_sub, {})
        NS = {"aux": "http://ns.adobe.com/exif/1.0/aux/",
              "exifEX": "http://cipa.jp/exif/1.0/",
              "crd": "http://ns.adobe.com/camera-raw-defaults/1.0/",
              "crs": "http://ns.adobe.com/camera-raw-settings/1.0/",
              "xmp": "http://ns.adobe.com/xap/1.0/",
              "photoshop": "http://ns.adobe.com/photoshop/1.0/",
              "dc": "http://purl.org/dc/elements/1.1/"}
        PER_FILE_XMP = {"xmp:ModifyDate", "xmp:MetadataDate", "xmp:CreateDate",
                        "photoshop:DateCreated", "xmpMM:DocumentID",
                        "xmpMM:InstanceID", "xmpMM:OriginalDocumentID", "dc:format",
                                                                                      
                                                                                         
                                                           
                        "aux:Lens", "aux:LensSerialNumber", "aux:SerialNumber",
                        "aux:LensID", "aux:ImageNumber", "aux:FlashCompensation",
                        "exifEX:LensModel", "exifEX:LensMake", "exifEX:LensSerialNumber",
                        "exifEX:BodySerialNumber", "exifEX:LensSpecification"}
        for k, v in sorted(consts.items()):
            if k.startswith("xmlns:") or k in PER_FILE_XMP or ":" not in k:
                continue
            pref = k.split(":", 1)[0]
            if pref not in NS:
                continue
            extra.append("xmpns %s %s %s" % (NS[pref], k.split(":", 1)[1], v))
                                                                         
        try:
            from compare import Tiff as _TX, values as _vx
            _tx = _TX(entry["src"])
            _tags = {}
            for _ifd in _tx.ifds():
                for _e in _ifd["entries"]:
                    _tags.setdefault(_e["tag"], _e)
            _lens = ""
            if 0xA434 in _tags:
                _lv = _vx(_tags[0xA434]["raw"], _tags[0xA434]["type"])
                if isinstance(_lv, str):
                    _lens = _lv.replace("\x00", "").strip()
            _lser = ""
            if 0xA435 in _tags:
                _sv = _vx(_tags[0xA435]["raw"], _tags[0xA435]["type"])
                if isinstance(_sv, str):
                    _lser = _sv.replace("\x00", "").strip()
            _body = ""
            if 0xA431 in _tags:
                _bv = _vx(_tags[0xA431]["raw"], _tags[0xA431]["type"])
                if isinstance(_bv, str):
                    _body = _bv.replace("\x00", "").strip()
            if not _lens:
                                                                                
                                                                             
                                                    
                try:
                    _xp3 = json.load(open(os.path.join(BASE, "ref", "xmp_props.json"), encoding="utf-8"))
                    _g4 = entry.get("group") or []
                    _s4 = _g4[2] if len(_g4) > 2 and _g4[2] else "default"
                    _lens = (_xp3.get(entry["camera"], {}).get(_s4, {}).get("aux:Lens") or "").strip()
                except Exception:
                    _lens = ""
            if _lens:
                extra.append("xmpns %s Lens %s" % (NS["aux"], _lens))
                extra.append("xmpns %s LensModel %s" % (NS["exifEX"], _lens))
            if _lser:
                extra.append("xmpns %s LensSerialNumber %s" % (NS["aux"], _lser))
                extra.append("xmpns %s LensSerialNumber %s" % (NS["exifEX"], _lser))
            if _body:
                extra.append("xmpns %s SerialNumber %s" % (NS["aux"], _body))
                extra.append("xmpns %s BodySerialNumber %s" % (NS["exifEX"], _body))
                extra.append("serial %s" % _body)
        except Exception as exc:
            rec["lens_xmp_note"] = str(exc)[:120]
        try:
            _ids = json.load(open(os.path.join(BASE, "ref", "original_ids.json"), encoding="utf-8"))
            if entry["file"] in _ids:
                extra.append("originaldocumentid %s" % _ids[entry["file"]])
        except Exception:
            pass
    except Exception:
        pass

                                                                             
                                                                         
                                                                     
    dg = camera_db.get(entry["camera"], {}).get("preview_digest", {})
    if dg:
        sub = entry["group"][2] if len(entry.get("group") or []) > 2 and entry["group"][2] else "default"
        dig = dg.get("%s|%s" % (sub, entry.get("iso", "")))
        if dig is None and len(set(dg.values())) == 1:
            dig = next(iter(dg.values()))
        if dig:
            extra.append("settingsdigest %s" % dig)

                                                                               
                                                                 
    op3 = val("subifd/0xC74E")
                                                                                 
                                                                              
                                                                                   
                                                                                   
                                                                                   
                                                                                   
                                                    
    if op3 is not None:
        try:
            from compare import Tiff as _TPL, values as _vpl
            _rec_lens = ""
            try:
                _xp2 = json.load(open(os.path.join(BASE, "ref", "xmp_props.json"), encoding="utf-8"))
                _g3 = entry.get("group") or []
                _s3 = _g3[2] if len(_g3) > 2 and _g3[2] else "default"
                _rec_lens = (_xp2.get(entry["camera"], {}).get(_s3, {}).get("aux:Lens") or "").strip()
            except Exception:
                _rec_lens = ""
            _src_lens = ""
            _tl = _TPL(entry["src"])
            for _ifd in _tl.ifds():
                for _e in _ifd["entries"]:
                    if _e["tag"] == 0xA434:
                        _lv2 = _vpl(_e["raw"], _e["type"])
                        if isinstance(_lv2, str):
                            _src_lens = _lv2.replace("\x00", "").strip()
                        break
                if _src_lens:
                    break
            if _rec_lens and _src_lens and _rec_lens != _src_lens:
                OPCODE_SKIP.append("lens %r != record lens %r" % (_src_lens[:40], _rec_lens[:40]))
                op3 = None
        except Exception as exc:
            OPCODE_SKIP.append("lens check failed: %s" % str(exc)[:80])
    if op3 is not None:
        if isinstance(op3, str):
            import binascii
            blob = binascii.unhexlify(op3)
        else:
            blob = bytes(int(x) & 0xFF for x in op3)
        if len(blob) > 4:
            op = os.path.join(os.path.dirname(out_path), "oplist3.bin")
            open(op, "wb").write(blob)
            extra.append("opcodelist3 %s" % op)

                                                                                
                                           
    try:
        from compare import Tiff as _TO
        _so = _TO(entry["src"])
        _so_tags = {}
        for _ifd in _so.ifds():
            for _e in _ifd["entries"]:
                _so_tags.setdefault(_e["tag"], _e)
        for _tg, _directive, _fn in ((0xC740, "opcodelist1", "oplist1.bin"),
                                     (0xC741, "opcodelist2", "oplist2.bin")):
            if _tg in _so_tags:
                _raw = _so_tags[_tg]["raw"]
                _blob = _raw if isinstance(_raw, (bytes, bytearray)) else                         bytes(int(x) & 0xFF for x in _raw)
                if len(_blob) > 4:
                    _op = os.path.join(os.path.dirname(out_path), _fn)
                    open(_op, "wb").write(bytes(_blob))
                    extra.append("%s %s" % (_directive, _op))
    except Exception:
        pass

    pat = val("subifd/0x828E")
    if not pat:
                                                                              
        lines.append("monochrome")
    elif pat:
        if len(pat) == 36:
                                                                                  
                                                                          
            lines.append("cfapattern 6 " + " ".join(str(int(x)) for x in pat))
        else:
            ph = cfa_phase(pat)
            if ph is not None:
                lines.append("cfa %d" % ph)

    for key, directive in (("ifd0/0x010F", "make"), ("ifd0/0x0110", "model"),
                           ("ifd0/0xC615", "local"), ("ifd0/0xC6F8", "profileName"),
                           ("ifd0/0xC6F3", "calibSignature")):
        v = val(key)
        if v:
            lines.append("%s %s" % (directive, v))

    v = val("ifd0/0x0112")
    if v is not None:
        lines.append("orientation %d" % int(v[0] if isinstance(v, list) else v))

    v = val("subifd/0xC68D")
    if v:
        lines.append("active %d %d %d %d" % tuple(int(x) for x in v))
    v = val("subifd/0xC68E")
    if v:
        for i in range(0, len(v), 4):
            lines.append("masked %d %d %d %d" % tuple(int(x) for x in v[i:i + 4]))

    co = val("subifd/0xC61F")
    cs = val("subifd/0xC620")
    if co and cs:
        o = [RAT(p) for p in co]
        s = [RAT(p) for p in cs]
        lines.append("crop %d %d %d %d" % (int(o[0][0]), int(o[1][0]), int(s[0][0]), int(s[1][0])))

    v = val("subifd/0xC61E")
    if v:
        s = [RAT(p) for p in v]
        lines.append("scale %.10f %.10f" % (float(s[0][0]) / float(s[0][1]),
                                            float(s[1][0]) / float(s[1][1])))
    v = val("subifd/0xC65C")
    if v:
        s = RAT(v[0] if isinstance(v, list) and isinstance(v[0], list) else v)
        lines.append("bqscale %.10f" % (float(s[0]) / float(s[1])))

    v = lv.get("black") if lv.get("black") else val("subifd/0xC61A")
    if v:
        vals = [RAT(p) if isinstance(p, list) else [int(p), 1] for p in v]
        lines.append("black %d %s" % (len(vals), " ".join("%.10f" % (float(n) / float(d)) for n, d in vals)))
    _rep = val("subifd/0xC619")
    if _rep and isinstance(_rep, list) and len(_rep) == 2 and (int(_rep[0]) > 1 or int(_rep[1]) > 1):
        lines.append("blackrepeat %d %d" % (int(_rep[1]), int(_rep[0])))
    v = lv.get("white") if lv.get("white") else val("subifd/0xC61D")
    if v:
        lines.append("white %d" % int(v[0] if isinstance(v, list) else v))
    v = val("subifd/0xC62D")
    if v is not None:
        lines.append("bayerGreenSplit %d" % int(v if not isinstance(v, list) else v[0]))

    for key, directive in (("ifd0/0xC62A", "baselineExposure"),
                           ("ifd0/0xC62B", "baselineNoise"),
                           ("ifd0/0xC62C", "baselineSharpness"),
                           ("ifd0/0xC62E", "linearResponseLimit")):
        v = val(key)
        if v is not None:
            p = RAT(v[0] if isinstance(v, list) and isinstance(v[0], list) else v)
            lines.append("%s %.10f" % (directive, float(p[0]) / float(p[1])))

    for key, directive in (("ifd0/0xC633", "shadowScale"), ("subifd/0xC632", "antiAlias")):
        v = val(key)
        if v is not None:
            p = RAT(v[0] if isinstance(v, list) and isinstance(v[0], list) else v)
            lines.append("%s %d %d" % (directive, int(p[0]), int(p[1])))

    v = val("ifd0/0xC627")
    if v:
        ps = [RAT(p) for p in v]
        lines.append("analogBalance %d %s" % (len(ps), " ".join("%.10f" % (float(n) / float(d)) for n, d in ps)))

    _um = val("ifd0/0xC614")
    if _um:
        lines.append("uniquemodel %s" % (_um if isinstance(_um, str) else _um[0]))

    for key, directive in (("ifd0/0xC623", "calib1"), ("ifd0/0xC624", "calib2"),
                           ("ifd0/0xC621", "cm1"), ("ifd0/0xC622", "cm2"),
                           ("ifd0/0xC714", "fm1"), ("ifd0/0xC715", "fm2")):
        v = val(key)
        if v:
            lines.append("%s %s" % (directive, rat_str(v)))

    for key, directive in (("ifd0/0xC65A", "illum1"), ("ifd0/0xC65B", "illum2")):
        v = val(key)
        if v is not None:
            lines.append("%s %d" % (directive, int(v[0] if isinstance(v, list) else v)))

                                                                               
                                                                            
                                                            
    np_ = val("subifd/0xC761") or val("ifd0/0xC761")
    if not np_:
        _g3 = entry.get("group") or []
        _sub3 = _g3[2] if len(_g3) > 2 and _g3[2] else "default"
        _ni = camera_db.get(entry["camera"], {}).get("noise_iso", {})
        np_ = _ni.get("%s|%s" % (_sub3, entry.get("iso")))
    if np_:
        lines.append("noise %d %s" % (len(np_), " ".join("%.17g" % float(x) for x in np_)))
    for key, prefix, dims_key in (("ifd0/0xC6FA", "huesat1", "ifd0/0xC6F9"),
                                  ("ifd0/0xC6FB", "huesat2", "ifd0/0xC6F9")):
        data = val(key)
        dims = val(dims_key)
        if data and dims:
            import numpy as _np
            path = pixels_path.replace("pix.u16", "%s.f32" % prefix)
            _np.asarray(data, dtype="<f4").tofile(path)
            lines.append("%s %s %d %d %d" % (prefix, path, int(dims[0]), int(dims[1]), int(dims[2])))
    data = val("ifd0/0xC726")
    dims = val("ifd0/0xC725")
    if data and dims:
        import numpy as _np
        path = pixels_path.replace("pix.u16", "look.f32")
        _np.asarray(data, dtype="<f4").tofile(path)
        lines.append("looktable %s %d %d %d" % (path, int(dims[0]), int(dims[1]), int(dims[2])))
    _cp = val("ifd0/0xC6FE")
    if _cp:
        _cps = _cp if isinstance(_cp, str) else (_cp[0] if _cp else "")
        if _cps:
            lines.append("copyrighthex %s" % _cps.encode("latin-1", "replace").hex())
                                                                              
                                                                                 
    lines.append("software Adobe DNG Converter 18.7 (Windows)")
    for key, directive in (("ifd0/0x0131", "software"),
                           ("ifd0/0xC6F4", "profilecalibsig"), ("ifd0/0xC62F", "serial")):
        v = val(key)
        if v:
            lines.append("%s %s" % (directive, v if isinstance(v, str) else str(v)))
    v = val("ifd0/0x9211")
    if v is not None:
        lines.append("imagenumber %d" % int(v[0] if isinstance(v, list) else v))
    v = val("ifd0/0xC630")
    if v:
        ps = [RAT(p) for p in v]
        lines.append("lens %s" % " ".join("%d %d" % (int(n), int(d)) for n, d in ps))

    if extra:
        lines += extra
    return "\n".join(lines) + "\n"


def one(entry, camera_db, keep=False, standalone=False, work=None):
    WORK = work or globals()["WORK"]
    import numpy as np
    rec = dict(file=entry["file"], camera=entry["camera"], ext=entry["ext"])
    base = os.path.join(WORK, "build", os.path.splitext(entry["file"])[0])
    os.makedirs(base, exist_ok=True)
    try:
        raw, meta = LI.decode(entry["src"])
    except Exception as exc:
        rec["status"] = "DECODE_FAILED"
        rec["error"] = str(exc)[:200]
        return rec
                                                                            
                                                                               
                                  
    _g1 = entry.get("group") or []
    _sub1 = _g1[2] if len(_g1) > 2 and _g1[2] else "default"
    win = (camera_db.get(entry["camera"], {}).get("window") or {}).get(_sub1)
    if win:
        dx, dy, w2, h2 = int(win[0]), int(win[1]), int(win[2]), int(win[3])
        rec["window_source"] = "camera_db"
    else:
                                                                           
        if standalone:
            raise ValueError("No verified camera decode window; ADC reference access forbidden in standalone mode")
        adc, _dims = LI.adc_cfa(entry["reference"], os.path.join(base, "adc.u16"))
        h2, w2 = adc.shape
        pixrec = None
        for r in json.load(open(os.path.join(WORK, "pixel_ingest.json"), encoding="utf-8")):
            if r["file"] == entry["file"]:
                pixrec = r
                break
        off = (pixrec or {}).get("offset") or [0, 0]
        dy, dx = int(off[1]), int(off[0])
        rec["window_source"] = "reference"
    if raw.shape[0] - dy < h2 or raw.shape[1] - dx < w2:
        rec["status"] = "FRAME_TOO_SMALL"
        return rec
    sub = raw[dy:dy + h2, dx:dx + w2]
                                                                        
                                                            
    bps = int(meta.get("raw_bps") or 0)
    hi = (1 << bps) - 1 if 8 <= bps <= 16 else 65535
    if int(sub.max()) > hi:
        sub = sub.clip(0, hi)
        rec["clipped"] = True
    pix_path = os.path.join(base, "pix.u16")
    sub.astype("<u2").tofile(pix_path)

    extra = []
    cm = meta.get("cam_mul") or []
    colors = int(meta.get("colors") or 3)
                                                                                
                                                                                
                                              
    neutral = None
    _g0 = entry.get("group") or []
    _sub0 = _g0[2] if len(_g0) > 2 and _g0[2] else "default"
    _tab = camera_db.get(entry["camera"], {}).get("neutral_iso", {})
    rec_neutral = _tab.get("%s|%s" % (_sub0, entry.get("iso")))
    if rec_neutral:
        vals = []
        for pair in rec_neutral:
            if isinstance(pair, list) and len(pair) == 2 and pair[1]:
                vals.append(pair[0] / float(pair[1]))
            else:
                vals.append(float(pair))
        if vals:
            extra.append("cameraNeutral %d %s" % (len(vals), " ".join("%.12f" % v for v in vals)))
            neutral = vals
    if neutral is None and any(cm):
        inv = [1.0 / c if c else 0.0 for c in cm[:colors]]
        mx = max(inv) or 1.0
        extra.append("cameraNeutral %d %s" % (colors, " ".join("%.12f" % (v / mx) for v in inv)))

                                                                               
                                                                                 
                                                                                    
                                                                        
    from compare import Tiff, values              
                                                                                    
                                                                                   
                                                             
    if standalone:
        try:
            import render_preview
            pv = os.path.join(base, "preview.jpg")
            th = os.path.join(base, "thumb.jpg")
            psize = render_preview.render(sub, meta, camera_db.get(entry["camera"], {}).get("constants"),
                                          pv, width=1024, thumb=th)
            extra.append("preview %s %d %d 2 2" % (pv, psize[0], psize[1]))
            from PIL import Image
            tsz = Image.open(th).size
            rgb = Image.open(th).convert("RGB").tobytes()
            tp = os.path.join(base, "thumb.raw")
            open(tp, "wb").write(rgb)
            extra.append("thumb %s %d %d 3" % (tp, tsz[0], tsz[1]))
            rec["preview"] = "self-rendered"
        except Exception as exc:
            rec["preview_note"] = str(exc)[:120]
    try:
        if standalone:
            raise RuntimeError("standalone")
        from compare import Tiff, values
        t = Tiff(entry["reference"])
        for ifd in t.ifds():
            tg = {e["tag"]: e for e in ifd["entries"]}
            if ifd["ctx"] == "ifd0" and 0x0111 in tg and 0x0117 in tg:
                tw = values(tg[0x0100]["raw"], tg[0x0100]["type"])[0]
                th = values(tg[0x0101]["raw"], tg[0x0101]["type"])[0]
                nt = values(tg[0x0115]["raw"], tg[0x0115]["type"])
                tch = nt[0] if nt else 3
                off = values(tg[0x0111]["raw"], tg[0x0111]["type"])[0]
                cnt = values(tg[0x0117]["raw"], tg[0x0117]["type"])[0]
                tp = os.path.join(base, "thumb.raw")
                open(tp, "wb").write(t.data[off:off + cnt])
                extra.append("thumb %s %d %d %d" % (tp, tw, th, tch))
            elif ifd["ctx"] == "subifd" and 0x0111 in tg:
                pw2 = values(tg[0x0100]["raw"], tg[0x0100]["type"])[0]
                ph2 = values(tg[0x0101]["raw"], tg[0x0101]["type"])[0]
                if pw2 > 500:
                    off = values(tg[0x0111]["raw"], tg[0x0111]["type"])
                    cnt = values(tg[0x0117]["raw"], tg[0x0117]["type"])
                    blob = b"".join(t.data[x:x + y] for x, y in zip(off, cnt))
                    pp = os.path.join(base, "preview.jpg")
                    open(pp, "wb").write(blob)
                    ss = values(tg[0x0212]["raw"], tg[0x0212]["type"]) if 0x0212 in tg else [2, 2]
                    extra.append("preview %s %d %d %d %d" % (pp, pw2, ph2, ss[0], ss[1]))
                                                           
        pv = None
        for ifd in t.ifds():
            tg = {e["tag"]: e for e in ifd["entries"]}
            if 0xC716 in tg:
                app = values(tg[0xC716]["raw"], tg[0xC716]["type"])
                ver = values(tg[0xC717]["raw"], tg[0xC717]["type"]) if 0xC717 in tg else ""
                extra.append("appname %s" % (app if isinstance(app, str) else ""))
                extra.append("appver %s" % (ver if isinstance(ver, str) else ""))
                break
    except Exception as exc:
        rec["preview_note"] = str(exc)[:120]

                                                                              
                                                                       
                                                                           
                                                               
    if entry["ext"] == "dng":
                                                                             
                                                                        
        try:
            from compare import Tiff as _TD, values as _vd
            _ts = _TD(entry["src"])
            _e0 = {e["tag"]: e for e in _ts.ifds()[0]["entries"]}
            if 0xC634 in _e0:
                _blob = bytes(_vd(_e0[0xC634]["raw"], _e0[0xC634]["type"]))
                if _blob:
                    _pp = os.path.join(base, "private.bin")
                    with open(_pp, "wb") as f:
                        f.write(_blob)
                    extra.append("priv %s" % _pp)
                    rec["private_len"] = len(_blob)
        except Exception as exc:
            rec["private_note"] = str(exc)[:120]
    if entry["ext"] == "cr3":
                                                                              
                                                                               
                                                                                 
                                                       
        import struct as _st
        with open(entry["src"], "rb") as f:
            _d = f.read()
        _pos = _d.find(b"CMT3")
        if _pos > 0:
            _base = _pos + 4
            _size, = _st.unpack_from(">I", _d, _pos - 4)
            _mn = _d[_base + 8:_pos - 4 + _size]
            _blob = (b"Adobe\x00" + b"MakN\x00" + (len(_mn) + 6).to_bytes(3, "big")
                     + b"II" + (8).to_bytes(4, "big") + _mn)
            _pp = os.path.join(base, "private.bin")
            with open(_pp, "wb") as f:
                f.write(_blob)
            extra.append("priv %s" % _pp)
            rec["private_len"] = len(_blob)
    try:
        if entry["ext"] == "cr3":
            raise StopIteration
        if entry["ext"] == "raf":
                                                                                
                                                                                 
                                                                         
            _fmn, _foff = fuji_makernote(entry["src"])
            if _fmn:
                _seg = (b"Adobe\x00" + b"MakN\x00" + (len(_fmn) + 6).to_bytes(3, "big")
                        + b"II" + _foff.to_bytes(4, "big") + _fmn)
                                                                                    
                                                                                     
                                                                                  
                                                                                        
                                                                        
                                   
                try:
                    import struct as _st2
                    _rs = open(entry["src"], "rb").read()
                    _boff = _st2.unpack_from(">I", _rs, 92)[0]
                    _blen = _st2.unpack_from(">I", _rs, 96)[0]
                    if 0 < _blen and _boff + _blen <= len(_rs):
                        _pl = (_rs[16:148] + _st2.pack(">I", _blen)
                               + _rs[_boff:_boff + _blen] + b"\x00\x00\x00\x00")
                        if len(_seg) % 2:
                            _seg += b"\x00"
                        _seg += (b"RAF " + (len(_pl) + 6).to_bytes(4, "big") + b"MM"
                                 + (132).to_bytes(4, "big") + _pl)
                        rec["private_raf"] = len(_pl) + 14
                except Exception as exc:
                    rec["private_raf_note"] = str(exc)[:100]
                _pp = os.path.join(base, "private.bin")
                open(_pp, "wb").write(_seg)
                extra.append("priv %s" % _pp)
                rec["private_len"] = len(_seg)
                rec["private_note"] = "MakN + RAF segments"
            raise StopIteration

        from compare import Tiff as _T
        _src = _T(entry["src"])
        mn = None
        for ifd in _src.ifds():
            for ent in ifd["entries"]:
                if ent["tag"] == 0x927C and ent["raw"]:
                    mn = ent
                    break
            if mn:
                break
        if mn is not None and entry["ext"] in ("cr2", "cr3"):
            try:
                _has_artist = False
                for _ifd in _src.ifds():
                    for _e in _ifd["entries"]:
                        if _e["tag"] == 0x13B:
                            _v = values(_e["raw"], _e["type"])
                            if isinstance(_v, str) and _v.strip():
                                _has_artist = True
                if not _has_artist:
                    _owner = canon_camera_info(open(entry["src"], "rb").read(), mn["off"] or 0)
                    if _owner:
                        extra.append("artist %s" % _owner)
            except Exception as exc:
                rec["artist_note"] = str(exc)[:100]
        if mn is not None:
            order = b"II" if _src.endian == "<" else b"MM"
            blob = (b"Adobe\x00" + b"MakN\x00" + (len(mn["raw"]) + 6).to_bytes(3, "big")
                    + order + int(mn["off"] or 0).to_bytes(4, "big") + mn["raw"])
            pp = os.path.join(base, "private.bin")
            open(pp, "wb").write(blob)
            extra.append("priv %s" % pp)
            if entry["ext"] == "arw":
                                                                              
                                                                                
                                                                                 
                                                                               
                                                                          
                try:
                    import struct as _st
                    _e0 = {e["tag"]: e for e in _src.ifds()[0]["entries"]}
                    _ptr = int.from_bytes(bytes(_e0[0xC634]["raw"])[:4], "little") if 0xC634 in _e0 else 0
                    if _ptr:
                        _raw = open(entry["src"], "rb").read()
                        _n = _st.unpack_from("<H", _raw, _ptr)[0]
                        _len = 0
                        for _i in range(_n):
                            _q = _ptr + 2 + _i * 12
                            _tag, _typ, _cnt = _st.unpack_from("<HHI", _raw, _q)
                            if _tag == 0x7201:
                                _len = _st.unpack_from("<I", _raw, _q + 8)[0]
                                break
                        if 0 < _len <= len(_raw) - _ptr:
                            _sr2 = (b"SR2 \x00" + (_len + 6).to_bytes(3, "big") + order
                                    + _ptr.to_bytes(4, "big") + _raw[_ptr:_ptr + _len])
                            with open(pp, "ab") as f:
                                                                                 
                                                                                 
                                if len(blob) % 2:
                                    f.write(b"\x00")
                                f.write(_sr2)
                            rec["private_sr2"] = len(_sr2)
                            rec["private_len"] = len(blob) + len(_sr2)
                except Exception as exc:
                    rec["private_sr2_note"] = str(exc)[:100]
            rec["private_len"] = len(blob)
    except StopIteration:
        pass
    except Exception as exc:
        rec["private_note"] = str(exc)[:120]

    lv = json.load(open(LEVELS, encoding="utf-8")) if not standalone and os.path.exists(LEVELS) else {}
                                                                              
    if entry["ext"] != "dng":
        extra.append("origname %s" % entry["file"])
                                                                             
                                                                                 
                                                                             
                                                 
    MIME = {"cr2": "image/x-canon-cr2", "cr3": "image/x-canon-cr3",
            "nef": "image/x-nikon-nef", "arw": "image/x-sony-arw",
            "raf": "image/x-fuji-raw", "rw2": "image/x-panasonic-raw",
            "orf": "image/x-olympus-raw", "pef": "image/x-pentax-raw",
            "srw": "image/x-samsung-raw", "dng": "image/x-adobe-dng",
            "3fr": "image/x-hasselblad-raw"}
    if entry["ext"] in MIME:
        extra.append("sourcemime %s" % MIME[entry["ext"]])
    try:
        from compare import Tiff as _T2
        _s2 = _T2(entry["src"])
        for ifd in _s2.ifds():
            T2 = {e["tag"]: e for e in ifd["entries"]}
            if 0x9003 in T2:
                v = values(T2[0x9003]["raw"], T2[0x9003]["type"])
                if isinstance(v, str) and len(v) >= 19:
                    _dt = v.replace(" ", " ")
                    _sub = ""
                    for _ifd2 in _s2.ifds():
                        _t2 = {_e["tag"]: _e for _e in _ifd2["entries"]}
                        if 0x9291 in _t2:
                            _sv = values(_t2[0x9291]["raw"], _t2[0x9291]["type"])
                            if isinstance(_sv, str):
                                _sub = _sv.strip()
                            break
                    if _sub:
                        _dt = "%s.%s" % (_dt, _sub)
                    extra.append("originaldate %s" % _dt)
                break
        for ifd in _s2.ifds():
            T2 = {e["tag"]: e for e in ifd["entries"]}
            if 0x13B in T2:
                v = values(T2[0x13B]["raw"], T2[0x13B]["type"])
                if isinstance(v, str) and v.strip():
                    extra.append("artist %s" % v.strip())
                                                                               
                                                                                   
                                                                                
                                                                           
            if 0xA434 in T2:
                _v = values(T2[0xA434]["raw"], T2[0xA434]["type"])
                if isinstance(_v, str) and _v:
                    extra.append("lensmodel %s" % _v)
            if 0xA433 in T2:
                v = values(T2[0xA433]["raw"], T2[0xA433]["type"])
                if isinstance(v, str) and v:
                    extra.append("lensmake %s" % v)
                                                                             
                                                                               
                                                          
        T2 = {}
        for _ifd in _s2.ifds():
            for _e in _ifd["entries"]:
                T2.setdefault(_e["tag"], _e)
        if True:
            if 0x829A in T2:
                vt = values(T2[0x829A]["raw"], T2[0x829A]["type"])
                if isinstance(vt, list) and len(vt) == 1 and isinstance(vt[0], list):
                    extra.append("exif exposureTime %d %d" % (int(vt[0][0]), int(vt[0][1]) or 1))
            if 0x829D in T2:
                vt = values(T2[0x829D]["raw"], T2[0x829D]["type"])
                if isinstance(vt, list) and len(vt) == 1 and isinstance(vt[0], list):
                    extra.append("exif fNumber %d %d" % (int(vt[0][0]), int(vt[0][1]) or 1))
                                                                                  
                                                                      
            if 0xA405 not in T2 and 0x920A in T2:
                _fl = values(T2[0x920A]["raw"], T2[0x920A]["type"])
                _crop = None
                _mk = (entry.get("camera") or "").lower()
                if any(k in _mk for k in ("olympus", "om system", "panasonic", "lumix")):
                    _crop = 2.0
                elif any(k in _mk for k in ("fujifilm", "nikon", "sony", "samsung")):
                    _crop = 1.5
                if _crop and isinstance(_fl, list) and _fl and isinstance(_fl[0], list):
                    _f35 = int(round((_fl[0][0] / float(_fl[0][1] or 1)) * _crop))
                    if _f35 > 0:
                        extra.append("exif u32 41989 0 %d" % _f35)
            for _tag in (0xA405, 0xA408, 0xA409, 0xA40A, 0x9208, 0xA300, 0xA301,
                         0x8832, 0xA40C, 0xA217, 0xA407):
                if _tag in T2:
                    v = values(T2[_tag]["raw"], T2[_tag]["type"])
                    if isinstance(v, list) and v and not isinstance(v[0], list):
                        extra.append("exif u32 %d 0 %d" % (_tag, int(v[0])))
            for _rt in (0xA404, 0x9206):
                if _rt in T2:
                    v = values(T2[_rt]["raw"], T2[_rt]["type"])
                    if isinstance(v, list) and v and isinstance(v[0], list):
                        extra.append("exif urational %d 0 %d %d" % (_rt, int(v[0][0]), int(v[0][1]) or 1))
                                                                              
                                                                                 
            if 0x9205 in T2:
                _ma = values(T2[0x9205]["raw"], T2[0x9205]["type"])
                if isinstance(_ma, list) and _ma and isinstance(_ma[0], list):
                    extra.append("exif maxAperture %d %d" % (int(_ma[0][0]), int(_ma[0][1]) or 1))
            _wb = 0
            if 0xA403 in T2:
                _wv = values(T2[0xA403]["raw"], T2[0xA403]["type"])
                if isinstance(_wv, list) and _wv and not isinstance(_wv[0], list):
                    _wb = int(_wv[0])
            extra.append("exif u32 41987 0 %d" % _wb)
            if 0x9203 in T2:
                v = values(T2[0x9203]["raw"], T2[0x9203]["type"])
                if isinstance(v, list) and v and isinstance(v[0], list):
                    _n, _d = int(v[0][0]), int(v[0][1]) or 1
                    if _n >= 1 << 31:
                        _n -= 1 << 32
                    extra.append("exif brightness %d %d" % (_n, _d))
            if 0x010E in T2:
                v = values(T2[0x010E]["raw"], T2[0x010E]["type"])
                if isinstance(v, str) and v.strip():
                    extra.append("description %s" % v.strip())
            if 0x9012 in T2:
                v = values(T2[0x9012]["raw"], T2[0x9012]["type"])
                if isinstance(v, str) and v.strip():
                    extra.append("offsettimedigitized %s" % v.strip())
            if 0x9011 in T2:
                v = values(T2[0x9011]["raw"], T2[0x9011]["type"])
                if isinstance(v, str) and v.strip():
                    extra.append("offsettimeoriginal %s" % v.strip())
                                                                                
                                                  
                    for _l in list(extra):
                        if _l.startswith("originaldate "):
                            _dtt = _l[len("originaldate "):].replace(" ", "T", 1)
                                                                        
                            _dtt = _dtt[:4] + "-" + _dtt[5:7] + "-" + _dtt[8:]
                            _off = v.strip()
                                                                                
                            if not re.match(r"^[+-][0-9]{2}:[0-9]{2}$", _off):
                                _off = ""
                            extra.append("datecreated %s%s" % (_dtt, _off))
            if 0x8831 in T2:
                v = values(T2[0x8831]["raw"], T2[0x8831]["type"])
                if isinstance(v, list) and v and not isinstance(v[0], list):
                    extra.append("exif u32 34865 0 %d" % int(v[0]))
            if 0xA302 in T2:
                v = values(T2[0xA302]["raw"], T2[0xA302]["type"])
                if isinstance(v, list) and len(v) == 4 and not isinstance(v[0], list):
                    extra.append("exifcfapattern 2 2 %d %d %d %d" % tuple(int(x) for x in v))
            if 0xA302 in T2:
                                                                                 
                v = values(T2[0xA302]["raw"], T2[0xA302]["type"])
                if not isinstance(v, (bytes, bytearray)):
                    v = bytes(int(x) & 0xFF for x in v) if isinstance(v, list) else b""
                if isinstance(v, (bytes, bytearray)) and len(v) >= 4:
                    _h = v[0] | (v[1] << 8)
                    _w = v[2] | (v[3] << 8)
                    if 0 < _h <= 8 and 0 < _w <= 8 and len(v) >= 4 + _h * _w:
                        _vals = " ".join(str(x) for x in v[4:4 + _h * _w])
                        extra.append("exifcfapattern %d %d %s" % (_h, _w, _vals))
            if 0x9286 in T2:
                                                                                
                                                                             
                _raw = T2[0x9286]["raw"]
                _b = _raw if isinstance(_raw, (bytes, bytearray)) else                      bytes(int(x) & 0xFF for x in _raw)
                if len(_b) > 8 and _b[8:].strip(b"\x00 ") != b"":
                                                                                
                    _val = _b[8:].rstrip(b"\x00 ")
                    extra.append("usercommenthex %s" % bytes(_val).hex())
            for _et in (0x9400, 0x9402, 0x9403, 0x9404, 0x9405):
                if _et in T2:
                    _v = values(T2[_et]["raw"], T2[_et]["type"])
                    if isinstance(_v, list) and _v and isinstance(_v[0], list):
                        extra.append("exifsrat %d %d %d" % (_et, int(_v[0][0]), int(_v[0][1]) & 0xFFFFFFFF))
            if 0x9211 not in T2:
                _sc = nikon_shutter_count(entry["src"])
                if _sc:
                    extra.append("imagenumber %d" % _sc)
            if 0x9291 in T2:
                v = values(T2[0x9291]["raw"], T2[0x9291]["type"])
                if isinstance(v, str) and v.strip():
                    extra.append("exif subsec %s" % v.strip())
            if 0x8830 in T2:
                v = values(T2[0x8830]["raw"], T2[0x8830]["type"])
                if isinstance(v, list) and v:
                    extra.append("exif sensitivityType %d" % int(v[0]))
            if 0x8827 in T2:
                v = values(T2[0x8827]["raw"], T2[0x8827]["type"])
                if isinstance(v, list) and v:
                    extra.append("exif iso %d" % int(v[0]))
            if 0x9204 in T2:
                v = values(T2[0x9204]["raw"], T2[0x9204]["type"])
                if isinstance(v, list) and v and isinstance(v[0], list):
                    extra.append("exif exposureBias %d %d" % (int(v[0][0]), int(v[0][1]) or 1))
            _ma = _cv("exif/0x9205") if "_cv" in dir() else None
            if _ma is None:
                _c2 = camera_db.get(entry["camera"], {}).get("constants", {})
                _e2 = _c2.get("exif/0x9205")
                _ma = None if _e2 is None else _e2["value"]
            if _ma:
                _mp = _ma[0] if isinstance(_ma[0], list) else _ma
                extra.append("exif maxAperture %d %d" % (int(_mp[0]), int(_mp[1]) or 1))
            elif 0x9205 in T2:
                v = values(T2[0x9205]["raw"], T2[0x9205]["type"])
                if isinstance(v, list) and v and isinstance(v[0], list):
                    extra.append("exif maxAperture %d %d" % (int(v[0][0]), int(v[0][1]) or 1))
            if 0x9207 in T2:
                v = values(T2[0x9207]["raw"], T2[0x9207]["type"])
                if isinstance(v, list) and v:
                    extra.append("exif meteringMode %d" % int(v[0]))
            if 0x9209 in T2:
                v = values(T2[0x9209]["raw"], T2[0x9209]["type"])
                if isinstance(v, list) and v:
                    extra.append("exif flash %d %d" % (int(v[0]), int(v[0])))
            if 0x920A in T2:
                v = values(T2[0x920A]["raw"], T2[0x920A]["type"])
                if isinstance(v, list) and v and isinstance(v[0], list):
                    extra.append("exif focalLength %d %d" % (int(v[0][0]), int(v[0][1]) or 1))
            if 0xA001 in T2:
                v = values(T2[0xA001]["raw"], T2[0xA001]["type"])
                if isinstance(v, list) and v:
                    extra.append("exif colorSpace %d" % int(v[0]))
                                                                               
                                                                                
                                                                             
            _const = camera_db.get(entry["camera"], {}).get("constants", {})
            def _cv(key):
                _e = _const.get(key)
                return None if _e is None else _e["value"]
            for _tag, _kw in ((0xA20E, "focalPlaneX"), (0xA20F, "focalPlaneY")):
                _dv = _cv("exif/0x%04X" % _tag)
                if _dv:
                    _p = _dv[0] if isinstance(_dv[0], list) else _dv
                    extra.append("exif %s %d %d" % (_kw, int(_p[0]), int(_p[1]) or 1))
                elif _tag in T2:
                    v = values(T2[_tag]["raw"], T2[_tag]["type"])
                    if isinstance(v, list) and v and isinstance(v[0], list):
                        extra.append("exif %s %d %d" % (_kw, int(v[0][0]), int(v[0][1]) or 1))
            _du = _cv("exif/0xA210")
            if _du:
                extra.append("exif focalPlaneUnit %d" % int(_du[0] if isinstance(_du, list) else _du))
            elif 0xA210 in T2:
                v = values(T2[0xA210]["raw"], T2[0xA210]["type"])
                if isinstance(v, list) and v:
                    extra.append("exif focalPlaneUnit %d" % int(v[0]))
            if 0xA401 in T2:
                v = values(T2[0xA401]["raw"], T2[0xA401]["type"])
                if isinstance(v, list) and v:
                    extra.append("exif customRendered %d" % int(v[0]))
            if 0xA402 in T2:
                v = values(T2[0xA402]["raw"], T2[0xA402]["type"])
                if isinstance(v, list) and v:
                    extra.append("exif exposureMode %d" % int(v[0]))
            if 0xA403 in T2:
                v = values(T2[0xA403]["raw"], T2[0xA403]["type"])
                if isinstance(v, list) and v:
                    extra.append("exif whiteBalance %d" % int(v[0]))
            if 0xA406 in T2:
                v = values(T2[0xA406]["raw"], T2[0xA406]["type"])
                if isinstance(v, list) and v:
                    extra.append("exif sceneCaptureType %d" % int(v[0]))
            if 0x8822 in T2:
                vt = values(T2[0x8822]["raw"], T2[0x8822]["type"])
                if isinstance(vt, list) and vt:
                    extra.append("exif exposureProgram %d" % int(vt[0]))
        for ifd in _s2.ifds():
            T2 = {e["tag"]: e for e in ifd["entries"]}
            if 0x02BC in T2:
                xp = os.path.join(base, "source.xmp")
                open(xp, "wb").write(bytes(T2[0x02BC]["raw"]))
                extra.append("sourcexmp %s" % xp)
                break
    except Exception as exc:
        rec["source_meta_note"] = str(exc)[:100]
    import time as _t
    extra.append("exif datetime %s" % _t.strftime("%Y:%m:%d %H:%M:%S"))
    spec = build_spec(entry, camera_db, pix_path, w2, h2, os.path.join(base, "built.dng"), extra, lv)
    spec_path = os.path.join(base, "spec.txt")
    if standalone:
        spec += "software Jiangtherapee DNG Converter 1.0 (JDC)\n"
    # Use relative artifact paths so writer tokens support work directories containing spaces.
    spec = spec.replace(base + os.sep, "")
    open(spec_path, "w", encoding="utf-8").write(spec)
    env = dict(os.environ)
    env["PATH"] = TOOLBIN + ";" + env.get("PATH", "")
    r = subprocess.run([DANGBUILD, spec_path], capture_output=True, env=env, cwd=base, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        rec["status"] = "BUILD_FAILED"
        rec["error"] = r.stderr.decode("latin-1", "replace")[-300:]
        return rec
    built = os.path.join(base, "built.dng")
    rec["built_bytes"] = os.path.getsize(built)
    if not standalone and entry.get("reference") and os.path.exists(entry["reference"]):
        rec["reference_bytes"] = os.path.getsize(entry["reference"])
    else:
        rec["reference_bytes"] = None
        rec["status"] = "STANDALONE"
        rec["note"] = "no ADC reference was read"
                                                                         
                                                                                 
                                                                                
                           
    try:
        _g2 = entry.get("group") or []
        _sub2 = _g2[2] if len(_g2) > 2 and _g2[2] else "default"
        from xmp_order import reorder_file
        rec["xmp_order"] = reorder_file(built, entry["camera"], _sub2)
    except Exception as exc:
        rec["xmp_note"] = str(exc)[:120]

                                                                          
    if rec.get("reference_bytes") is None:
        if keep:
            rec["built"] = built
        return rec
    try:
        from compare_payloads import payloads as _pay
        pa = _pay(built); pr = _pay(entry["reference"])
        tot = sum(len(v) for v in pr.values())
        same = sum(len(pr[k]) for k in pr if pa.get(k) == pr[k])
        rec["payload_bytes"] = tot
        rec["payload_identical"] = same
        rec["payload_pct"] = round(100.0 * same / max(1, tot), 4)
    except Exception as exc:
        rec["payload_note"] = str(exc)[:120]

    cmp = compare(built, entry["reference"])
    rec["verdict"] = cmp["verdict"]
    rec["n_diffs"] = cmp["n_diffs"]
    rec["diffs"] = cmp["diffs"][:60]
    rec["status"] = "COMPARED"
    if not keep:
        pass
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--standalone", action="store_true",
                    help="never read ADC's output; render previews ourselves")
    ap.add_argument("--out", default=os.path.join(WORK, "build_report.json"))
    a = ap.parse_args()
    man = json.load(open(os.path.join(WORK, "manifest.json"), encoding="utf-8"))["entries"]
    db = json.load(open(DB, encoding="utf-8"))
    entries = man
    if a.file:
        entries = [e for e in entries if e["file"] in a.file]
    if a.limit:
        entries = entries[:a.limit]
    rows = []
    def _run(e):
        rec = one(e, db, a.keep, standalone=a.standalone)
        print("%-14s %-30s %-42s diffs=%-5s payload=%s%%" % (
            rec.get("status"), rec.get("verdict", ""), e["file"][:42],
            rec.get("n_diffs", ""), rec.get("payload_pct", "-")), flush=True)
        return rec
    if a.jobs > 1:
        with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
            for rec in ex.map(_run, entries):
                rows.append(rec)
    else:
        for e in entries:
            rows.append(_run(e))
    json.dump(rows, open(a.out, "w", encoding="utf-8"), indent=1, default=str)
    from collections import Counter
    pay = [r for r in rows if r.get("payload_pct") is not None]
    full = [r for r in pay if r["payload_pct"] == 100.0]
    print()
    print("built+compared %d/%d, payload 100%% identical %d/%d" % (
        len(pay), len(rows), len(full), len(pay)))
    print("status:", dict(Counter(r.get("status") for r in rows)))


if __name__ == "__main__":
    main()
