"""Bounded primitives. No client automation, network or business-ledger writes."""
from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = Path(os.environ.get('IM_HUB_HOME', str(Path.home() / '.im-hub'))).expanduser()
MAX_BYTES = 50 * 1024 * 1024
MAX_RECORDS = 200000
SCHEMA_VERSION = 1

class IMError(Exception):
    """A safe, machine-readable error; never includes chat bodies or credentials."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value) -> str:
    data = value if isinstance(value, bytes) else canonical(value).encode('utf-8')
    return hashlib.sha256(data).hexdigest()

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def iso_epoch(value: str) -> float:
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise IMError('TIMEZONE_AWARE_ISO_TIME_REQUIRED') from None

def stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

def label(value, code='INVALID_IDENTIFIER') -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or '\x00' in value:
        raise IMError(code)
    return value

def read_blob(path: Path) -> bytes:
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise IMError('SOURCE_MISSING_OR_TOO_LARGE')
    with path.open('rb') as f:
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise IMError('SOURCE_TOO_LARGE')
    return data

def load_json(data: bytes):
    try:
        return json.loads(data.decode('utf-8-sig'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise IMError('INVALID_JSON') from None

def write_new(path: Path, data: bytes):
    with path.open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

def readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise IMError('DATABASE_NOT_AVAILABLE')
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    con.execute('PRAGMA trusted_schema=OFF')
    return con

def check_home(home: Path):
    if not home.is_dir() or home.is_symlink():
        raise IMError('HOME_NOT_INITIALIZED_OR_UNSAFE')
    # Explicit --home can read the already-verified legacy state without copying it.
    candidates = [('im-hub.json', 'im-hub-private'), ('im-unified.json', 'im-unified-private')]
    for filename, kind in candidates:
        marker_path = home / filename
        if marker_path.is_file():
            if load_json(read_blob(marker_path)) != {'kind': kind, 'schema_version': SCHEMA_VERSION}:
                raise IMError('HOME_SCHEMA_MISMATCH')
            return
    raise IMError('HOME_NOT_INITIALIZED_RUN_INIT')

DDL = '''
CREATE TABLE meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT INTO meta VALUES('revision', 0);
CREATE TABLE streams(stream_id TEXT PRIMARY KEY, spec_json TEXT NOT NULL);
CREATE TABLE batches(batch_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL,
 source_sha256 TEXT NOT NULL, status TEXT NOT NULL, observed_at TEXT NOT NULL,
 imported_at TEXT, revision INTEGER, manifest_json TEXT NOT NULL);
CREATE TABLE records(message_key TEXT PRIMARY KEY, stream_id TEXT NOT NULL,
 semantic_sha256 TEXT NOT NULL, event_ms INTEGER NOT NULL, batch_id TEXT NOT NULL,
 metadata_json TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE INDEX record_stream_time ON records(stream_id,event_ms,message_key);
CREATE INDEX batch_stream ON batches(stream_id,status,revision);
'''

def initialize(home: Path) -> dict:
    if (home / 'im-hub.json').exists():
        check_home(home)
        return {'initialized': True, 'already_exists': True}
    if home.exists():
        raise IMError('INIT_REQUIRES_NEW_DEDICATED_DIRECTORY')
    home.mkdir(parents=True, mode=0o700)
    if os.name == 'nt':
        principal = os.environ.get('USERDOMAIN', '') + '\\' + os.environ.get('USERNAME', '')
        p = subprocess.run(['icacls.exe', str(home), '/inheritance:r', '/grant:r',
                            principal + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'],
                           capture_output=True, timeout=20)
        if p.returncode:
            raise IMError('PRIVATE_DIRECTORY_ACL_FAILED')
    for sub in ('batches', 'exports', 'candidates', 'chatlab', 'runtime'):
        (home / sub).mkdir(mode=0o700)
    con = sqlite3.connect(home / 'index.sqlite3')
    try:
        con.executescript(DDL)
        con.commit()
    finally:
        con.close()
    write_new(home / 'im-hub.json', canonical({'kind': 'im-hub-private', 'schema_version': SCHEMA_VERSION}).encode())
    return {'initialized': True, 'already_exists': False, 'raw_data_public': False}

@contextmanager
def writer(home: Path, lock_name: str = '.writer.lock'):
    check_home(home)
    if not (home / 'im-hub.json').is_file():
        raise IMError('LEGACY_HOME_READ_ONLY_USE_NEW_HOME_FOR_WRITES')
    if lock_name not in ('.writer.lock', '.collection.lock', '.desktop.lock', '.run.lock', '.soak.lock'):
        raise IMError('INVALID_INTERNAL_LOCK')
    f = (home / lock_name).open('a+b')
    locked = False
    try:
        f.seek(0, 2)
        if f.tell() == 0:
            f.write(b'0'); f.flush()
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise IMError('WRITER_BUSY') from None
        yield
    finally:
        if locked:
            f.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()

def binding_spec(spec: dict) -> dict:
    # Both representations describe the same observed native message identity.
    # Preserve the original wecom-json namespace for already imported streams.
    return {**spec, 'adapter': 'wecom-json' if spec['adapter'] == 'wecom-native' else spec['adapter']}

def stream_key(spec: dict) -> str:
    bound = binding_spec(spec)
    return 'im-' + digest({k: bound[k] for k in ('platform', 'account_namespace', 'conversation_id', 'adapter', 'source_epoch', 'data_class')})[:32]

def db_path(home: Path, stream_id: str) -> Path:
    if not re.fullmatch(r'im-[0-9a-f]{32}', stream_id):
        raise IMError('INVALID_STREAM_ID')
    return home / 'chatlab/data/databases' / (stream_id + '.db')
