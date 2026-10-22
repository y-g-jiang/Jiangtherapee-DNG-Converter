import csv, json, math, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE = os.environ.get("JDC_PTC_TABLE") or os.path.join(BASE, "ref", "ptc_model.csv")
MODEL_MAP = os.environ.get("JDC_PTC_MODELS") or os.path.join(BASE, "ref", "models.csv")
P = ("a_read_var", "b_shot", "c_prnu_var")

REBEL = {"rebel sl2": "200d", "rebel sl3": "250d", "rebel t5i": "700d", "rebel t6i": "750d",
         "rebel t6s": "760d", "rebel t7i": "800d", "rebel t8i": "850d", "rebel t7": "2000d",
         "rebel t6": "1300d", "rebel t5": "1200d", "rebel t100": "4000d"}



def norm(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def load():
    rows = []
    if not os.path.exists(TABLE):
        return rows
    with open(TABLE, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                r["iso"] = float(r["iso_manufacturer"])
                for k in P:
                    r[k] = float(r[k])
            except Exception:
                continue
            rows.append(r)
    return rows


def alias_map():
    m = {}
    if os.path.exists(MODEL_MAP):
        with open(MODEL_MAP, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                m[norm((r.get("brand") or "") + " " + (r.get("model") or ""))] = (r.get("brand"), r.get("model"))
                m[norm(r.get("name") or "")] = (r.get("brand"), r.get("model"))
    return m


def index(rows):
    by = {}
    for r in rows:
        by.setdefault(norm(r["model"]), []).append(r)
        by.setdefault(norm(r["brand"] + r["model"]), []).append(r)
    return by


def series_for(idx, al, make, model, *, allow_fuzzy=False):
    for key in (norm(make) + norm(model), norm(model)):
        if key in idx:
            return idx[key]
    for key in (norm(make) + norm(model), norm(model)):
        if key in al:
            k = norm(al[key][1])
            if k in idx:
                return idx[k]
    low = model.lower()
    import re as _re
    m_ilce = _re.match(r"ilce-(\d)([rsc]*)m?(\d)?", low)
    if m_ilce:
        num, suf, mk = m_ilce.group(1), (m_ilce.group(2) or "").upper(), m_ilce.group(3)
        roman = {"1": "", "2": " II", "3": " III", "4": " IV", "5": " V"}.get(mk or "", "")
        candidates = ("a" + num + suf + roman,) if not allow_fuzzy else ("a" + num + suf + roman, "a" + num + suf)
        for cand in candidates:
            if norm(cand) in idx:
                return idx[norm(cand)]
    for tok in ("lumix", "dc", "dmc"):
        low = low.replace(tok, " ")
    low = " ".join(low.split())
    if norm(low) in idx:
        return idx[norm(low)]
    for k, v in REBEL.items():
        if k in low and norm("eos " + v) in idx:
            return idx[norm("eos " + v)]
    t = norm(model)
    if allow_fuzzy and len(t) > 4:
        for k, v in idx.items():
            if len(k) > 4 and (k.endswith(t) or t.endswith(k)):
                return v
        import difflib
        keys = [k for k in idx if len(k) > 4 and not k.startswith(norm(make))]
        near = difflib.get_close_matches(t, list(idx), n=1, cutoff=0.86)
        if near:
            return idx[near[0]]
    return None


def interp(series, iso):
    pts = sorted(((r["iso"], r) for r in series), key=lambda item: item[0])
    for x, r in pts:
        if abs(x - iso) < 1e-9:
            return dict(r, mode="exact")
    if len(pts) == 1:
        return dict(pts[0][1], mode="single")
    lo = hi = None
    for x, r in pts:
        if x <= iso:
            lo = (x, r)
        if x >= iso and hi is None:
            hi = (x, r)
    if lo is None:
        lo, hi = pts[0], pts[1]
    if hi is None:
        lo, hi = pts[-2], pts[-1]
    t = (math.log(iso) - math.log(lo[0])) / (math.log(hi[0]) - math.log(lo[0]))
    out = dict(lo[1])
    out["iso"] = iso
    for k in P:
        a, b = lo[1][k], hi[1][k]
        out[k] = math.exp(math.log(a) + t * (math.log(b) - math.log(a))) if a > 0 and b > 0 else a + t * (b - a)
    out["mode"] = "interp"
    out["between"] = (lo[0], hi[0])
    return out


def lookup(idx, al, make, model, iso):
    s = series_for(idx, al, make, model)
    return interp(s, float(iso)) if s else None


def bind_camera(make, model, iso):
    """Production camera/ISO binding; no fuzzy camera match or ISO extrapolation."""
    iso = float(iso)
    if not math.isfinite(iso) or iso <= 0:
        raise ValueError('PTC requires a finite positive capture ISO')
    rows = load()
    if not rows:
        raise FileNotFoundError('PTC table missing: ' + TABLE)
    series = series_for(index(rows), alias_map(), make or '', model or '', allow_fuzzy=False)
    if not series:
        # Remove known naming prefixes only; do not infer sensor equivalence.
        def canonical(value):
            value = norm(value)
            for prefix in ('nikoncorporation', 'panasonic', 'olympusimagingcorp',
                           'olympus', 'fujifilm', 'canon', 'nikon', 'sony'):
                if value.startswith(prefix):
                    value = value[len(prefix):]; break
            if value.startswith('lumix'): value = value[5:]
            if norm(make).startswith('panasonic'):
                for prefix in ('dmc', 'dc'):
                    if value.startswith(prefix): value = value[len(prefix):]; break
            return value
        brand = norm(make).replace('corporation', '').replace('imagingcorp', '')
        series = [r for r in rows if norm(r['brand']) == brand and canonical(r['model']) == canonical(model)]
    if not series:
        return None
    sensors = {str(r['sensor_id']) for r in series}
    if len(sensors) != 1:
        raise ValueError('Ambiguous PTC camera identity; choose an explicit PTC model')
    unique = {}
    for row in series:
        x = float(row['iso'])
        if x in unique and any(float(row[k]) != float(unique[x][k]) for k in P):
            raise ValueError('Conflicting PTC rows at the same camera/ISO')
        unique[x] = row
    if iso < min(unique) or iso > max(unique):
        raise ValueError('Capture ISO lies outside the recorded PTC range; select an explicit calibration')
    row = interp(list(unique.values()), iso)
    a, b, c = (float(row[k]) for k in P)
    if not all(math.isfinite(v) for v in (a, b, c)) or b <= 0:
        raise ValueError('Invalid PTC fit coefficients')
    # Source signal is DN255. x=DN255/255 gives Var(x)=a/255^2+(b/255)x+c*x^2.
    result = dict(schema='JDC_PTC_BINDING_V1', sensor_id=next(iter(sensors)),
        capture_make=make, capture_model=model, capture_iso=iso,
        matched_camera=row['brand'].strip() + ' ' + row['model'].strip(),
        source_table=TABLE, source_axis_max=255.0,
        source_coefficients={k: row[k] for k in P},
        normalized_coefficients=dict(read_variance=a / (255.0**2), shot=b / 255.0, prnu_variance=c),
        fullwell_e=255.0 / b, mode=row['mode'],
        iso_extrapolation=False, fuzzy_camera_match=False,
        readout_mode='unrecorded_by_dxo',
        fullwell_scope='effective saturation electrons implied by the recorded normalized shot slope',
        catalog_read_applied=False, prnu_applied=False, read_source='matched dark frame')
    if 'between' in row: result['between'] = list(row['between'])
    return result


def from_raw(path):
    sys.path.insert(0, os.path.join(BASE, "tools"))
    import ingest_libraw as LI
    from compare import Tiff, values
    _raw, meta = LI.decode(path)
    iso = None
    try:
        t = Tiff(path)
        for ifd in t.ifds():
            for e in ifd["entries"]:
                if e["tag"] == 0x8827:
                    v = values(e["raw"], e["type"])
                    if isinstance(v, list) and v:
                        iso = int(v[0])
                    break
            if iso:
                break
    except Exception:
        pass
    return meta.get("make") or "", meta.get("model") or "", iso


def main():
    rows = load()
    idx = index(rows)
    al = alias_map()
    print("ptc_model rows %d, sensors %d" % (len(rows), len({r["sensor_id"] for r in rows})))
    args = sys.argv[1:]
    if args and args[0] == "--coverage":
        from jdc_api import load_database
        db = load_database()
        hit, miss = 0, []
        for cam in db:
            make = cam.split(" ")[0]
            model = cam[len(make):].strip()
            if series_for(idx, al, make, model):
                hit += 1
            else:
                miss.append(cam)
        print("camera_db coverage: %d / %d" % (hit, len(db)))
        for m in miss:
            print("  missing:", m)
        return 0
    for path in args:
        if os.path.exists(path):
            make, model, iso = from_raw(path)
        else:
            make, model, iso = "", path, None
        if iso is None:
            print("%s: no ISO" % path)
            continue
        r = lookup(idx, al, make, model, iso)
        if not r:
            print("%-50s %-20s ISO %-5s -> no entry" % (os.path.basename(path)[:50], model[:20], iso))
            continue
        extra = " between %g-%g" % r["between"] if r["mode"] == "interp" else ""
        print("%-50s %-20s ISO %-5s a=%.6e b=%.6e c=%.6e [%s%s]"
              % (os.path.basename(path)[:50], model[:20], iso, r["a_read_var"], r["b_shot"],
                 r["c_prnu_var"], r["mode"], extra))
    return 0


if __name__ == "__main__":
    sys.exit(main())
