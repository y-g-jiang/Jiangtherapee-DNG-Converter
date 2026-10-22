"""Bounded standalone JDC ingress for CLI and JDN. No Adobe process."""
from pathlib import Path
import gzip, hashlib, json, os, shutil, tempfile
BASE = Path(__file__).resolve().parent.parent
SOFTWARE = 'Jiangtherapee DNG Converter 1.0 (JDC)'

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''): h.update(b)
    return h.hexdigest()

def load_database(path=None):
    p = Path(path) if path else BASE / 'ref/camera_db.json.gz'
    if not p.exists() and path is None: p = BASE / 'ref/camera_db.json'
    with (gzip.open if p.suffix == '.gz' else open)(p, 'rt', encoding='utf-8') as f:
        return json.load(f)

def convert_one(source, target, *, scratch_root, database=None, cancelled=None):
    import build_dng as B
    import ingest_libraw as LI
    source, target = Path(source).resolve(strict=True), Path(target).resolve()
    if target == source or target.exists(): raise FileExistsError(target)
    if cancelled and cancelled(): raise InterruptedError('JDC cancelled')
    db = database if database is not None else load_database()
    norm = lambda s: ''.join(c for c in s.lower() if c.isalnum())
    aliases = {norm(k): k for k in db}
    aliases.update(json.loads((BASE / 'ref/model_aliases.json').read_text(encoding='utf-8')))
    raw, meta = LI.decode(str(source))
    del raw
    camera = next((aliases[norm(s)] for s in [str(meta.get('make', '')) + ' ' + str(meta.get('model', '')), str(meta.get('model', ''))] if norm(s) in aliases), None)
    if not camera or camera not in db:
        raise ValueError('JDC has no verified camera record for this file; use an ADC-converted DNG')
    entry = dict(file='input' + source.suffix.lower(), src=str(source), camera=camera, ext=source.suffix.lower().lstrip('.'), reference='', group=[], iso=None)
    scratch_root = Path(scratch_root).resolve(); scratch_root.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='jdc-', dir=scratch_root) as temp:
        rec = B.one(entry, db, keep=True, standalone=True, work=temp)
        if rec.get('status') != 'STANDALONE': raise RuntimeError('JDC conversion failed: ' + str(rec))
        built = Path(rec['built'])
        if cancelled and cancelled(): raise InterruptedError('JDC cancelled')
        created = False
        try:
            with target.open('xb') as out:
                created = True
                with built.open('rb') as inp: shutil.copyfileobj(inp, out, 1024 * 1024)
            if sha256(target) != sha256(built): raise IOError('JDC output SHA mismatch')
        except BaseException:
            if created: target.unlink(missing_ok=True)
            raise
    rec.pop('built', None)
    return dict(schema='JDC_STANDALONE_V1', source=str(source), source_sha256=sha256(source), output=str(target), output_sha256=sha256(target), software=SOFTWARE, adobe_process_used=False, temporary_files_removed=True, conversion=rec)
