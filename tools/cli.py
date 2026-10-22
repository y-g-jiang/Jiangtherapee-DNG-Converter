"""Convert supported camera RAW files without calling Adobe DNG Converter."""
from pathlib import Path
import argparse, json, os, tempfile
from concurrent.futures import ThreadPoolExecutor
from jdc_api import convert_one, load_database
RAW_EXTS = {'.cr2','.cr3','.nef','.arw','.raf','.rw2','.orf','.pef','.srw','.dng','.3fr','.iiq','.nrw','.srf'}
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('inputs', nargs='+'); ap.add_argument('-o', '--outdir', required=True)
    ap.add_argument('--jobs', type=int, default=1); ap.add_argument('--limit', type=int); ap.add_argument('--camera-db')
    ap.add_argument('--scratch', default=os.environ.get('JTDC_WORK') or str(Path(tempfile.gettempdir()) / 'jtdc'))
    a = ap.parse_args(); paths = []
    for item in a.inputs:
        p = Path(item).resolve(strict=True)
        paths.extend(sorted(x for x in p.rglob('*') if x.is_file() and x.suffix.lower() in RAW_EXTS) if p.is_dir() else [p])
    paths = list(dict.fromkeys(paths))
    if a.limit is not None:
        if a.limit <= 0: ap.error('--limit must be positive')
        paths = paths[:a.limit]
    if not paths: ap.error('no RAW files found')
    targets = [Path(a.outdir).resolve() / (p.stem + '.dng') for p in paths]
    if len({str(p).casefold() for p in targets}) != len(targets): ap.error('duplicate output names; use separate folders')
    if any(t.exists() or t == p for p,t in zip(paths,targets)): ap.error('output already exists; choose another output folder')
    db = load_database(a.camera_db)
    def run(pair):
        p,target = pair
        try:
            print(json.dumps(convert_one(p,target,scratch_root=a.scratch,database=db),ensure_ascii=False),flush=True); return True
        except Exception as exc:
            print(json.dumps(dict(source=str(p),error=str(exc)),ensure_ascii=False),flush=True); return False
    with ThreadPoolExecutor(max_workers=max(1,a.jobs)) as pool: results = list(pool.map(run,zip(paths,targets)))
    return 0 if all(results) else 1
