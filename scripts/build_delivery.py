"""Build a wheel+installer ZIP from an explicitly allowlisted source tree.
Only source/docs/examples/tests are included; no local config, chats or databases.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
VERSION='0.4.0rc1'
ALLOWED_ROOTS={'im_hub','tests','scripts','docs','examples','packaging'}
ALLOWED_SUFFIXES={'.py','.md','.json','.toml','.mjs','.ps1'}
ROOT_FILES={'README.md','AGENTS.md','SECURITY.md','THIRD_PARTY_NOTICES.md','pyproject.toml','.gitignore'}


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def sources():
    result=[]
    for rel in ROOT_FILES:
        p=ROOT/rel
        if p.is_file():result.append(p)
    for directory in sorted(ALLOWED_ROOTS):
        for p in (ROOT/directory).rglob('*'):
            if p.is_symlink():raise RuntimeError('Symlink in distributable tree')
            if p.is_file() and p.suffix in ALLOWED_SUFFIXES and not any(x.startswith('.') or x=='__pycache__' for x in p.relative_to(ROOT).parts):
                result.append(p)
    return sorted(set(result))

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=ROOT/'dist');a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
    deliver=out/f'im-hub-{VERSION}-windows.zip'
    if deliver.exists():raise SystemExit('Delivery already exists; use a fresh output directory')
    files=sources()
    sourcehashes={f.relative_to(ROOT).as_posix():digest(f) for f in files}
    # pip build isolation may acquire setuptools. No user message data is involved.
    subprocess.run([sys.executable,'-m','pip','wheel',str(ROOT),'--no-deps','--wheel-dir',str(out)],check=True)
    wheels=list(out.glob(f'im_hub-{VERSION}-*.whl'))
    if len(wheels)!=1:raise SystemExit('Expected exactly one versioned wheel')
    wheel=wheels[0]
    with zipfile.ZipFile(wheel) as z:
        if z.testzip() is not None:raise SystemExit('Wheel CRC validation failed')
        for name in z.namelist():
            if any(x in name for x in ('/.local/','/private/','__pycache__')) or name.endswith(('.db','.bin','.sqlite3')):
                raise SystemExit('Unexpected private file in wheel')
    with tempfile.TemporaryDirectory(prefix='im-hub-bundle-') as temporary:
        base=Path(temporary);shutil.copyfile(wheel,base/wheel.name)
        shutil.copyfile(ROOT/'packaging/install.ps1',base/'install.ps1')
        for directory in ('docs','examples'):
            for f in files:
                rel=f.relative_to(ROOT)
                if rel.parts[0]==directory:
                    dest=base/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(f,dest)
        shutil.copyfile(ROOT/'README.md',base/'README.md')
        manifest={'version':VERSION,'release_channel':'candidate','production_desktop_accepted':False,
                  'files':{f.relative_to(base).as_posix():digest(f) for f in sorted(base.rglob('*')) if f.is_file()}}
        (base/'FILES.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
        with zipfile.ZipFile(deliver,'x',zipfile.ZIP_DEFLATED) as z:
            for f in sorted(base.rglob('*')):
                if f.is_file():z.write(f,f.relative_to(base).as_posix())
    sourcezip=out/f'im-hub-{VERSION}-source.zip'
    with zipfile.ZipFile(sourcezip,'x',zipfile.ZIP_DEFLATED) as z:
        for f in files:z.write(f,f.relative_to(ROOT).as_posix())
    if sourcehashes!={f.relative_to(ROOT).as_posix():digest(f) for f in files}:raise SystemExit('Source changed during build')
    products={f.name:{'sha256':digest(f),'bytes':f.stat().st_size} for f in (wheel,deliver,sourcezip)}
    (out/'SHA256SUMS.txt').write_text(''.join(meta['sha256']+'  '+name+'\n' for name,meta in products.items()),'utf-8')
    (out/'BUILD.json').write_text(json.dumps({'version':VERSION,'source_sha256':sourcehashes,'products':products},ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'version':VERSION,'artifacts':products,'output':str(out),'private_data_included':False},ensure_ascii=False))


if __name__=='__main__':main()
