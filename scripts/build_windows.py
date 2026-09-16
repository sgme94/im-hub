"""Build a create-only Windows console bundle from explicit reviewed dependencies.
No network downloads, source/client discovery, data copies, installs or publishing.
The caller supplies the fixed ChatLab node_modules, Node executable and license inputs.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from im_hub import __version__

HEAD = 'd844578bab17d68f8f960dea4675781645c170ff'
SOURCE_HASH = 'c50d542ccf5820f03721f52c8a0c51508819627271d64d8136e7cf4db55ba0b4'
NODE_HASH = 'ae1a50511be58e987483fdbc12125407443926d2d394669ade2352776e920dd3'
FONT_SUFFIXES = {'.ttf', '.otf', '.woff', '.woff2', '.ttc'}


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--node-modules', required=True, type=Path)
    p.add_argument('--node', required=True, type=Path)
    p.add_argument('--license-inputs', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    a = p.parse_args()
    if os.name != 'nt' or sys.maxsize <= 2**32:
        raise SystemExit('WINDOWS_X64_BUILD_REQUIRED')
    target = a.output.resolve()
    if target.exists(): raise SystemExit('NEW_BUILD_DIRECTORY_REQUIRED')
    modules = a.node_modules.resolve(); licenses = a.license_inputs.resolve()
    package = json.loads((modules / 'chatlab-cli/package.json').read_text('utf-8'))
    if package.get('name') != 'chatlab-cli' or package.get('version') != '0.37.1':
        raise SystemExit('FIXED_CHATLAB_BACKEND_REQUIRED')
    if sha(a.node) != NODE_HASH: raise SystemExit('NODE_BINARY_HASH_MISMATCH')
    upstream = licenses / ('ChatLab-source-' + HEAD + '.zip')
    if sha(upstream) != SOURCE_HASH: raise SystemExit('CHATLAB_SOURCE_HASH_MISMATCH')
    with zipfile.ZipFile(upstream) as z:
        if any(Path(n).suffix.lower() in FONT_SUFFIXES for n in z.namelist()):
            raise SystemExit('FONTS_NOT_ALLOWED_IN_DELIVERY')
    dependency_files = [f for f in modules.rglob('*') if f.is_file()]
    if any(f.suffix.lower() in FONT_SUFFIXES or f.is_symlink() or '.local' in f.parts for f in dependency_files):
        raise SystemExit('UNSAFE_DEPENDENCY_BUNDLE_CONTENT')
    target.mkdir(parents=True)
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--noupx', '--onedir', '--console',
               '--name', 'im-hub', '--distpath', str(target / 'app'), '--workpath', str(target / 'work'),
               '--specpath', str(target / 'spec'), '--paths', str(ROOT),
               '--collect-submodules', 'im_hub', '--collect-all', 'uiautomation',
               '--hidden-import', 'win32clipboard', '--hidden-import', 'win32process', '--hidden-import', 'win32security',
               '--add-data', str(ROOT / 'im_hub/offline_guard.mjs') + ';im_hub', str(ROOT / 'scripts/frozen_entry.py')]
    with (target / 'pyinstaller.log').open('wb') as log:
        done = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=600)
    if done.returncode: raise SystemExit('PYINSTALLER_FAILED_SEE_BUILD_LOG')
    app = target / 'app/im-hub'
    backend = app / 'backend'; backend.mkdir()
    shutil.copy2(a.node, backend / 'node.exe')
    shutil.copytree(modules, backend / 'node_modules', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.cache'))
    legal = app / 'licenses'; legal.mkdir()
    for name in ('NODE-LICENSE', 'CHATLAB-LICENSE'):
        shutil.copy2(licenses / name, legal / name)
    shutil.copy2(upstream, legal / upstream.name)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file(): shutil.copy2(python_license, legal / 'PYTHON-LICENSE.txt')
    python_dependencies = []
    for name in ('pywin32', 'uiautomation', 'comtypes', 'pyinstaller', 'cryptography', 'cffi', 'pycparser'):
        distribution = importlib.metadata.distribution(name)
        copied = []
        for file in distribution.files or []:
            if any(part.lower().startswith(('license', 'copying', 'notice')) for part in Path(str(file)).parts):
                src = Path(distribution.locate_file(file))
                if src.is_file():
                    dest = legal / 'python' / name / str(file).replace('..', '_')
                    dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dest)
                    copied.append(dest.relative_to(app).as_posix())
        python_dependencies.append({'name': name, 'version': distribution.version, 'notice_files': copied})
    node_dependencies = []
    for path in modules.rglob('package.json'):
        if path.parent == modules or path.stat().st_size > 2 * 1024**2: continue
        try: meta = json.loads(path.read_text('utf-8'))
        except (ValueError, UnicodeError): continue
        if isinstance(meta.get('name'), str) and isinstance(meta.get('version'), str):
            node_dependencies.append({'name': meta['name'], 'version': meta['version'], 'license': meta.get('license'),
                                      'package_path': path.relative_to(modules).as_posix()})
    manifest = {'schema': 'im-hub-portable-build/1', 'version': __version__, 'platform': 'windows-x64',
                'binary_signed': False, 'frontend_started': False, 'node_version': '22.22.2',
                'node_sha256': NODE_HASH, 'chatlab_version': '0.37.1', 'chatlab_source_commit': HEAD,
                'chatlab_source_sha256': SOURCE_HASH, 'python_dependencies': python_dependencies,
                'node_dependencies': node_dependencies, 'contains_user_data': False}
    (app / 'DEPENDENCIES.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    for f in app.rglob('*'):
        if f.is_file() and f.suffix.lower() in FONT_SUFFIXES:
            raise SystemExit('FONTS_NOT_ALLOWED_IN_DELIVERY')
    print(json.dumps({'built': True, 'directory': str(app), 'version': __version__,
                      'bytes': sum(f.stat().st_size for f in app.rglob('*') if f.is_file()), 'signed': False}))


if __name__ == '__main__': main()
