                     
"""Decode raw files through the audited LibRaw 0.22.2 adapter and prove the
samples against ADC's own DNG.

The adapter (srhq_joraw2.dll: LibRaw 0.22.2 + the Nikon HE patch, already built
in this environment) exposes a flat C ABI returning the unpacked uint16 raw
buffer.  This tool reads that buffer (whole frame, not just the visible window)
and compares it sample-by-sample with the CFA image ADC wrote for the same file
(`build/dngw.exe dumpraw`).  Nothing is assumed: every file gets a measured
verdict.

Verdicts
  PIXEL_EXACT                        identical geometry, every sample equal
  PIXEL_EXACT_EXCEPT_DEFECTS         equal except dead pixels ADC interpolated
  PIXEL_EXACT_EXCEPT_FEW             equal except a handful of samples
  PIXEL_OFFSET[_EXCEPT_*]            ADC's window sits inside the frame at a found offset
  PIXEL_DIFF / GEOMETRY_MISMATCH     real disagreement (n_diff reported)
  LIBRAW_FAILED                      the decoder could not open it

usage:
  ingest_libraw.py [--ext nef --ext rw2 ...] [--file NAME] [--limit N] [--jobs N]
  ingest_libraw.py --one --file NAME          # one record as JSON (crash-safe driver)
Output: work/libraw_ingest.json
"""
import argparse, concurrent.futures as cf, ctypes, json, os, subprocess, sys
import os
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "tools")
WORK = os.path.join(BASE, "work")
REF = os.path.join(BASE, "ref")
BIN = os.path.join(BASE, "bin")


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WORK = os.path.join(BASE, "work")
DNGW = os.path.join(BASE, "bin", "dngw.exe")
TOOLBIN = r"C:\msys64\ucrt64\bin"
DLL_CANDIDATES = [p for p in [os.environ.get('JDC_LIBRAW_DLL'), os.path.join(BIN, 'srhq_joraw2.dll')] if p]


class RawInfoV1(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32), ("struct_size", ctypes.c_uint32),
        ("raw_width", ctypes.c_uint32), ("raw_height", ctypes.c_uint32),
        ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
        ("top_margin", ctypes.c_uint32), ("left_margin", ctypes.c_uint32),
        ("flip", ctypes.c_uint32), ("filters", ctypes.c_uint32),
        ("colors", ctypes.c_uint32), ("maximum", ctypes.c_uint32),
        ("data_maximum", ctypes.c_uint32), ("raw_bps", ctypes.c_uint32),
        ("black_common", ctypes.c_uint32),
        ("repeat_rows", ctypes.c_uint32), ("repeat_cols", ctypes.c_uint32),
        ("repeat_count", ctypes.c_uint32),
        ("repeat_origin_y", ctypes.c_uint32), ("repeat_origin_x", ctypes.c_uint32),
        ("site_color_indices", ctypes.c_int32 * 4),
        ("cblack", ctypes.c_float * 4), ("black_site_base", ctypes.c_float * 4),
        ("black_site", ctypes.c_float * 4),
        ("cam_mul", ctypes.c_float * 4), ("pre_mul", ctypes.c_float * 4),
        ("cam_xyz", ctypes.c_float * 12), ("cmatrix", ctypes.c_float * 12),
        ("rgb_cam", ctypes.c_float * 12),
        ("cfa_pattern", ctypes.c_char * 5),
        ("camera_make", ctypes.c_char * 64), ("camera_model", ctypes.c_char * 64),
        ("unique_camera_model", ctypes.c_char * 64),
        ("libraw_version", ctypes.c_char * 32), ("build_id", ctypes.c_char * 64),
    ]


_DLL = None


def dll():
    global _DLL
    if _DLL is not None:
        return _DLL
    last = None
    for path in DLL_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            d = ctypes.CDLL(path)
            d.srhq_joraw2_abi_version.restype = ctypes.c_uint32
            d.srhq_joraw2_build_id.restype = ctypes.c_char_p
            d.srhq_joraw2_libraw_version.restype = ctypes.c_char_p
            d.srhq_joraw2_create.restype = ctypes.c_void_p
            d.srhq_joraw2_destroy.argtypes = [ctypes.c_void_p]
            d.srhq_joraw2_last_error.argtypes = [ctypes.c_void_p]
            d.srhq_joraw2_last_error.restype = ctypes.c_char_p
            d.srhq_joraw2_open_buffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
            d.srhq_joraw2_open_buffer.restype = ctypes.c_int
            d.srhq_joraw2_unpack.argtypes = [ctypes.c_void_p]
            d.srhq_joraw2_unpack.restype = ctypes.c_int
            d.srhq_joraw2_get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(RawInfoV1)]
            d.srhq_joraw2_get_info.restype = ctypes.c_int
            d.srhq_joraw2_visible_u16_pointer.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
            d.srhq_joraw2_visible_u16_pointer.restype = ctypes.POINTER(ctypes.c_uint16)
            if int(d.srhq_joraw2_abi_version()) != 1:
                continue
            _DLL = (d, path, d.srhq_joraw2_build_id().decode(),
                    d.srhq_joraw2_libraw_version().decode())
            return _DLL
        except OSError as exc:
            last = exc
    raise RuntimeError("no usable LibRaw adapter DLL (%s)" % last)


def decode(path):
    """Return (full_raw_uint16_array, info_dict) for a raw file."""
    import numpy as np
    d, dll_path, build_id, libraw_version = dll()
    handle = d.srhq_joraw2_create()
    if not handle:
        raise MemoryError("handle")
    mapping = np.memmap(path, dtype=np.uint8, mode="r")
    try:
        rc = d.srhq_joraw2_open_buffer(handle, ctypes.c_void_p(int(mapping.ctypes.data)),
                                       mapping.nbytes)
        if rc != 0:
            raise RuntimeError(d.srhq_joraw2_last_error(handle).decode("latin-1", "replace"))
        rc = d.srhq_joraw2_unpack(handle)
        if rc != 0:
            raise RuntimeError(d.srhq_joraw2_last_error(handle).decode("latin-1", "replace"))
        info = RawInfoV1()
        info.struct_size = ctypes.sizeof(RawInfoV1)
        rc = d.srhq_joraw2_get_info(handle, ctypes.byref(info))
        if rc != 0:
            raise RuntimeError(d.srhq_joraw2_last_error(handle).decode("latin-1", "replace"))
        pitch = ctypes.c_size_t(0)
        ptr = d.srhq_joraw2_visible_u16_pointer(handle, ctypes.byref(pitch))
        if not ptr:
            raise RuntimeError("no raw image pointer")
        base = ctypes.cast(ptr, ctypes.c_void_p).value - 2 * (info.top_margin * pitch.value
                                                             + info.left_margin)
        full = np.ctypeslib.as_array(ctypes.cast(base, ctypes.POINTER(ctypes.c_uint16)),
                                     shape=(info.raw_height * pitch.value,))
        raw = full.reshape(info.raw_height, pitch.value)[:, :info.raw_width].copy()
        meta = dict(raw_width=int(info.raw_width), raw_height=int(info.raw_height),
                    width=int(info.width), height=int(info.height),
                    top_margin=int(info.top_margin), left_margin=int(info.left_margin),
                    flip=int(info.flip), filters=int(info.filters), colors=int(info.colors),
                    maximum=int(info.maximum), data_maximum=int(info.data_maximum),
                    raw_bps=int(info.raw_bps), black=int(info.black_common),
                    cfa=info.cfa_pattern.decode(errors="replace"),
                    cam_mul=[round(float(x), 6) for x in info.cam_mul],
                    cam_xyz=[round(float(x), 6) for x in info.cam_xyz],
                    make=info.camera_make.decode(errors="replace"),
                    model=info.camera_model.decode(errors="replace"),
                    unique_model=info.unique_camera_model.decode(errors="replace"),
                    libraw=libraw_version, build=build_id)
        return raw, meta
    finally:
        d.srhq_joraw2_destroy(handle)
        del mapping


def adc_cfa(path, out):
    import numpy as np
    env = dict(os.environ)
    env["PATH"] = TOOLBIN + ";" + env.get("PATH", "")
    r = subprocess.run([DNGW, "dumpraw", path, out], capture_output=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("latin-1", "replace")[-200:])
    w, h, p = (int(x) for x in r.stdout.split())
    return np.fromfile(out, dtype="<u2").reshape(h, w * p), (w, h, p)


def _same_shape_verdict(rec, img, adc):
    """Classify img vs adc when the geometry already matches."""
    import numpy as np
    mask = img != adc
    n_diff = int(mask.sum())
    if n_diff == 0:
        rec["status"] = "PIXEL_EXACT"
        rec["equal_pct"] = 100.0
        return True
    flat = np.nonzero(mask.ravel())[0]
    w = img.shape[1]
    d = img.astype(np.int32) - adc.astype(np.int32)
    rec["diff"] = dict(mean_abs=float(abs(d).mean()), max_abs=int(abs(d).max()),
                       n_diff=n_diff, first_row=int(flat[0] // w),
                       first_col=int(flat[0] % w),
                       last_row=int(flat[-1] // w), last_col=int(flat[-1] % w))
    rec["equal_pct"] = round(100.0 * (1.0 - n_diff / float(img.size)), 6)
    zeros_ours = bool((img[mask] == 0).all())
    rec["all_ours_zero"] = zeros_ours
    small = n_diff < 0.001 * img.size
    if zeros_ours and small:
        rec["status"] = "PIXEL_EXACT_EXCEPT_DEFECTS"
        rec["defect_pixels"] = n_diff
        return True
    if small:
        rec["status"] = "PIXEL_EXACT_EXCEPT_FEW"
        return True
    rec["status"] = "PIXEL_DIFF"
    return False


def _locate_and_verify(rec, img, adc):
    import numpy as np
    h2, w2 = adc.shape
    if img.shape[0] < h2 or img.shape[1] < w2:
        rec["status"] = "GEOMETRY_MISMATCH"
        return False
    dh = img.shape[0] - h2
    dw = img.shape[1] - w2
    probes = [r for r in (h2 // 4, h2 // 2, (3 * h2) // 4) if r < h2]
    tol = max(8, w2 // 4000)
    cands = []
    for dy in range(0, min(dh, 64) + 1):
        for dx in range(0, min(dw, 300) + 1):
            ok = True
            for r in probes:
                if int((img[dy + r, dx:dx + w2] != adc[r]).sum()) > tol:
                    ok = False
                    break
            if ok:
                cands.append((dy, dx))
    for dy, dx in cands:
        window = img[dy:dy + h2, dx:dx + w2]
        mask = window != adc
        n_diff = int(mask.sum())
        if n_diff == 0 or (n_diff < 0.001 * window.size and (window[mask] == 0).all()):
            probe = dict(rec)
            probe.pop("diff", None)
            _same_shape_verdict(probe, window, adc)
            rec.update(probe)
            rec["offset"] = [dx, dy]
            rec["crop"] = [dw, dh]
            if rec["status"].startswith("PIXEL_EXACT"):
                rec["status"] = "PIXEL_OFFSET" + rec["status"][len("PIXEL_EXACT"):]
            return True
    rec["status"] = "GEOMETRY_MISMATCH"
    rec["offset_candidates"] = cands[:8]
    return False


def compare(entry):
    import numpy as np
    rec = dict(file=entry["file"], camera=entry["camera"], ext=entry["ext"])
    base = os.path.join(WORK, "lr", os.path.splitext(entry["file"])[0])
    os.makedirs(base, exist_ok=True)
    try:
        raw, meta = decode(entry["src"])
    except Exception as exc:
        rec["status"] = "LIBRAW_FAILED"
        rec["error"] = "%s: %s" % (type(exc).__name__, exc)
        return rec
    rec.update(meta)
    try:
        adc, dims = adc_cfa(entry["reference"], os.path.join(base, "adc.u16"))
    except Exception as exc:
        rec["status"] = "DUMPRAW_FAILED"
        rec["error"] = str(exc)
        return rec
    rec["adc_dims"] = list(dims)
    rec["our_dims"] = [int(raw.shape[1]), int(raw.shape[0])]

                                                                            
                                                                              
                                                                              
                                                               
    bps = meta.get("raw_bps") or 0
    hi = (1 << bps) - 1 if 8 <= bps <= 16 else 65535
    variants = [("raw", raw)]
    if int(raw.max()) > hi:
        variants.append(("clipped", raw.clip(0, hi)))

    def _rank(status):
        if status == "PIXEL_EXACT" or status == "PIXEL_OFFSET":
            return 0
        if status.endswith("_EXCEPT_DEFECTS"):
            return 1
        if status.endswith("_EXCEPT_FEW"):
            return 2
        if status in ("PIXEL_DIFF", "PIXEL_OFFSET_DIFF"):
            return 3
        return 4

    best = None
    for name, img in variants:
        cand = dict(rec)
        cand.pop("diff", None)
        if img.shape == adc.shape:
            _same_shape_verdict(cand, img, adc)
        else:
            _locate_and_verify(cand, img, adc)
        if name == "clipped":
            cand["clipped_samples"] = int((raw > hi).sum())
        if best is None or _rank(cand["status"]) < _rank(best["status"]):
            best = cand
        if _rank(best["status"]) == 0:
            break
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ext", action="append")
    ap.add_argument("--file", action="append")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--one", action="store_true",
                    help="process a single --file and print its record as JSON")
    a = ap.parse_args()
    man = json.load(open(os.path.join(WORK, "manifest.json"), encoding="utf-8"))
    if a.one:
        entry = [e for e in man["entries"] if e["file"] == a.file[0]][0]
        print("@@JSON@@" + json.dumps(compare(entry), default=str))
        return
    entries = man["entries"]
    if a.ext:
        entries = [e for e in entries if e["ext"] in a.ext]
    if a.file:
        entries = [e for e in entries if e["file"] in a.file]
    if a.limit:
        entries = entries[:a.limit]
    d, dll_path, build_id, libraw_version = dll()
    print("LibRaw ingest: %d files  (%s, %s)" % (len(entries), libraw_version, build_id), flush=True)
    rows = []
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for rec in ex.map(compare, entries):
            rows.append(rec)
            dd = rec.get("diff") or {}
            print("%-32s %-56s %s %s" % (rec.get("status"), rec["file"],
                                         rec.get("our_dims", ""),
                                         rec.get("defect_pixels") or dd.get("n_diff", "")),
                  flush=True)
    out = os.path.join(WORK, "libraw_ingest.json")
    json.dump(rows, open(out, "w", encoding="utf-8"), indent=1, default=str)
    from collections import Counter
    print()
    print(dict(Counter(r.get("status") for r in rows)))
    print("wrote", out)


if __name__ == "__main__":
    main()
