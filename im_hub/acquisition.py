"""Recoverable explicit database acquisition, independent from the query path.
One collector lease per home; checkpoint advances only after validated ChatLab import.
Pending immutable batches are resumed before taking another source snapshot.
"""
from __future__ import annotations
import json
import sqlite3
import uuid
from pathlib import Path
from .adapters import normalize, spec_for
from .common import IMError, MAX_BYTES, canonical, check_home, digest, iso_epoch, load_json, now, read_blob, readonly, stamp, write_new, writer
from .database_readers import prepare_profile, read_database
from .store import ingest

DDL = '''
CREATE TABLE IF NOT EXISTS sources(
 source_id TEXT PRIMARY KEY, profile_sha256 TEXT NOT NULL,
 source_fingerprint TEXT, scan_until REAL, last_success_at TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS runs(
 run_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, completed_at TEXT, input_sha256 TEXT,
 manifest_json TEXT, error_code TEXT);
CREATE INDEX IF NOT EXISTS runs_source ON runs(source_id,status);
'''


def collection_status(home: Path, source_name=None) -> dict:
    check_home(home)
    path = home / 'collection.sqlite3'
    if not path.is_file():
        return {'items': [], 'query_only': True, 'collection_performed': False}
    con = readonly(path)
    try:
        items = []
        for source in con.execute('SELECT * FROM sources ORDER BY source_id'):
            if source_name and source_name != source['source_id']:
                continue
            counts = dict(con.execute('SELECT status,count(*) FROM runs WHERE source_id=? GROUP BY status', (source['source_id'],)))
            items.append({'source': source['source_id'], 'last_success_at': source['last_success_at'],
                          'local_scan_until': stamp(source['scan_until']) if source['scan_until'] is not None else None,
                          'last_error': source['last_error'], 'runs_by_status': counts,
                          'pending_runs': sum(counts.get(k, 0) for k in ('staged', 'import_failed')),
                          'client_complete_through': None})
        return {'items': items, 'query_only': True, 'collection_performed': False}
    finally:
        con.close()


def _source(con, source_name):
    return con.execute('SELECT * FROM sources WHERE source_id=?', (source_name,)).fetchone()


def _manifest_path(home: Path, run_id: str) -> Path:
    # DB-derived IDs cannot be used as filesystem traversal.
    if len(run_id) != 32 or any(c not in '0123456789abcdef' for c in run_id):
        raise IMError('ACQUISITION_RUN_ID_INVALID')
    return home / 'acquisitions' / run_id / 'source.json'


def _finish(home, con, source_name, run, backend):
    manifest = json.loads(run['manifest_json'])
    path = _manifest_path(home, run['run_id'])
    try:
        if digest(read_blob(path)) != run['input_sha256']:
            raise IMError('ACQUISITION_PAYLOAD_CHANGED')
        extra = {'backend': backend} if backend is not None else {}
        result = ingest(home, path, manifest['spec'], manifest['observed_at'],
                        since=manifest['since'], until=manifest['until'],
                        expected_sha256=run['input_sha256'], **extra)
        # This transaction is intentionally after the message-store commit. A crash
        # in between is recovered by replaying the same immutable input on retry.
        with con:
            previous = _source(con, source_name)['scan_until']
            checkpoint = max(previous or manifest['until'], manifest['until'])
            con.execute('UPDATE sources SET source_fingerprint=?,scan_until=?,last_success_at=?,last_error=NULL WHERE source_id=?',
                        (manifest['source_fingerprint'], checkpoint, now(), source_name))
            con.execute("UPDATE runs SET status='committed',completed_at=?,error_code=NULL WHERE run_id=?", (now(), run['run_id']))
        return {**result, 'configured_source': source_name, 'run_id': run['run_id'],
                'transport': manifest['transport'], 'client_access_performed': manifest['transport'] in ('kim-sqlite', 'wechat-live'),
                'client_refreshed': False, 'database_read_performed': True,
                'local_scan_until': stamp(checkpoint), 'client_complete_through': None,
                'llm_calls': 0, 'dry_run': False}
    except Exception as exc:
        code = exc.code if isinstance(exc, IMError) else 'ACQUISITION_IMPORT_FAILED'
        with con:
            con.execute("UPDATE runs SET status='import_failed',error_code=? WHERE run_id=?", (code, run['run_id']))
            con.execute('UPDATE sources SET last_error=? WHERE source_id=?', (code, source_name))
        raise


def collect_database(home: Path, base: Path, source_name: str, profile: dict,
                     dry_run=False, backend=None, until=None, reconcile=False) -> dict:
    p = prepare_profile(base, profile)
    profile_hash = digest(p)
    spec = spec_for(p['platform'], p['account_namespace'], p['conversation_id'], p['conversation_name'],
                    'database-json', p['source_epoch'], p['data_class'])
    end = float(int(iso_epoch(now()))) if until is None else iso_epoch(until)
    if end > iso_epoch(now()):
        raise IMError('COLLECTION_UNTIL_IN_FUTURE')
    initial = iso_epoch(p['initial_since'])
    check_home(home)
    if not (home / 'im-hub.json').is_file():
        raise IMError('LEGACY_HOME_READ_ONLY_USE_NEW_HOME_FOR_WRITES')

    def bounds(old):
        if end <= initial:
            raise IMError('INVALID_COLLECTION_WINDOW')
        if old is not None and old['profile_sha256'] != profile_hash:
            raise IMError('SOURCE_PROFILE_CHANGED_USE_REVIEWED_NEW_SOURCE')
        if old is not None and old['scan_until'] is not None and end < old['scan_until']:
            raise IMError('COLLECTION_CLOCK_REGRESSION')
        # A cache has no proven upstream refresh watermark; re-scan its configured
        # range on every call rather than skipping newly copied older history.
        start = initial
        if p['platform'] == 'kim' and not reconcile and old is not None and old['scan_until'] is not None:
            start = max(initial, old['scan_until'] - p['overlap_seconds'])
        return start

    def read(old):
        start = bounds(old)
        if p['transport'] == 'wechat-live':
            from .wechat_live import read_live
            packet = read_live(p, start, end, home)
        else:
            packet = read_database(p, start, end)
        fingerprint = packet['acquisition']['source_fingerprint']
        if old is not None and old['source_fingerprint'] is not None and old['source_fingerprint'] != fingerprint:
            raise IMError('SOURCE_IDENTITY_OR_SCHEMA_CHANGED_REBIND_REQUIRED')
        blob = canonical(packet).encode('utf-8')
        if len(blob) > MAX_BYTES:
            raise IMError('ACQUISITION_BATCH_TOO_LARGE')
        rows, coverage = normalize(blob, spec, start, end)
        return start, packet, blob, rows, coverage

    if dry_run:
        old = None
        path = home / 'collection.sqlite3'
        if path.is_file():
            con = readonly(path)
            try:
                old = _source(con, source_name)
                pending = con.execute("SELECT 1 FROM runs WHERE source_id=? AND status IN ('staged','import_failed')", (source_name,)).fetchone()
                if pending:
                    raise IMError('PENDING_ACQUISITION_RESUME_BEFORE_NEW_READ')
            finally:
                con.close()
        start, packet, blob, rows, coverage = read(old)
        return {'configured_source': source_name, 'transport': p['transport'], 'dry_run': True,
                'written': False, 'records': len(rows), 'coverage': coverage,
                'requested_since': stamp(start), 'requested_until_exclusive': stamp(end),
                'database_read_performed': True, 'client_refreshed': False, 'llm_calls': 0}

    with writer(home, lock_name='.collection.lock'):
        con = sqlite3.connect(home / 'collection.sqlite3', timeout=5)
        con.row_factory = sqlite3.Row
        try:
            con.executescript(DDL)
            old = _source(con, source_name)
            if old is not None and old['profile_sha256'] != profile_hash:
                raise IMError('SOURCE_PROFILE_CHANGED_USE_REVIEWED_NEW_SOURCE')
            pending = con.execute("SELECT * FROM runs WHERE source_id=? AND status IN ('staged','import_failed') ORDER BY created_at LIMIT 2", (source_name,)).fetchall()
            if len(pending) > 1:
                raise IMError('MULTIPLE_PENDING_ACQUISITIONS_REQUIRE_REVIEW')
            if pending:
                pending_end = json.loads(pending[0]['manifest_json'])['until']
                if until is not None and end < pending_end:
                    raise IMError('PENDING_ACQUISITION_EXCEEDS_REQUESTED_END')
                result = _finish(home, con, source_name, pending[0], backend)
                return {**result, 'resumed_pending': True, 'database_read_performed': False,
                        'client_access_performed': False}
            with con:
                con.execute('INSERT OR IGNORE INTO sources VALUES(?,?,NULL,NULL,NULL,NULL)', (source_name, profile_hash))
            run_id = uuid.uuid4().hex
            try:
                start, packet, blob, rows, coverage = read(old)
            except Exception as exc:
                code = exc.code if isinstance(exc, IMError) else 'DATABASE_READ_FAILED'
                with con:
                    con.execute('UPDATE sources SET last_error=? WHERE source_id=?', (code, source_name))
                    con.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', (run_id, source_name, 'read_failed', now(), now(), None, None, code))
                raise
            folder = home / 'acquisitions' / run_id
            folder.mkdir(parents=True, mode=0o700)
            write_new(folder / 'source.json', blob)
            manifest = {'spec': spec, 'since': start, 'until': end, 'observed_at': packet['acquisition']['observed_at'],
                        'source_fingerprint': packet['acquisition']['source_fingerprint'], 'transport': p['transport']}
            with con:
                con.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', (run_id, source_name, 'staged', now(), None, digest(blob), canonical(manifest), None))
            run = con.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
            return {**_finish(home, con, source_name, run, backend), 'resumed_pending': False,
                    'reconcile_requested': reconcile, 'cache_full_window_rescan': p['transport'] == 'wechat-sqlite'}
        finally:
            con.close()
