"""TIM full-export reconciliation and explicit native clipboard/UI acquisition.
UI drivers are opt-in and require a reviewed local profile. No LLM, sending or login.
No source credential or process memory is read. Pending imports resume without UI.
"""
from __future__ import annotations
import json
import re
import sqlite3
import uuid
from pathlib import Path
from .common import IMError, MAX_BYTES, canonical, check_home, digest, iso_epoch, label, load_json, now, read_blob, readonly, write_new, writer
from .database_readers import local_path, integer, BINDING_KEYS
from .adapters import spec_for
from .store import ingest
from .codecs.desktop import parse_tim

TRANSPORTS = ('tim-export', 'tim-ui', 'wecom-clipboard', 'wecom-ui')
DDL = '''
CREATE TABLE IF NOT EXISTS sources(source_id TEXT PRIMARY KEY,profile_sha256 TEXT NOT NULL,
 sequence_json TEXT, last_success_at TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,status TEXT NOT NULL,
 created_at TEXT NOT NULL,completed_at TEXT,manifest_json TEXT NOT NULL,error_code TEXT);
'''


def prepare_desktop_profile(base: Path, profile: dict) -> dict:
    p = dict(profile)
    transport = p.get('transport')
    if transport not in TRANSPORTS: raise IMError('UNSUPPORTED_DESKTOP_TRANSPORT')
    for key in BINDING_KEYS: label(p.get(key), 'DESKTOP_BINDING_REQUIRED')
    if p['data_class'] not in ('real', 'synthetic'): raise IMError('INVALID_DATA_CLASS')
    if p['platform'] != ('qq' if transport.startswith('tim-') else 'wecom'):
        raise IMError('DESKTOP_PLATFORM_MISMATCH')
    if p['platform'] == 'wecom': label(p.get('binding'), 'WECOM_BINDING_REQUIRED')
    if p['platform'] == 'qq' and p.get('export_mode') != 'full-history-append-only':
        raise IMError('TIM_FULL_EXPORT_CONTRACT_REQUIRED')
    p['max_records'] = integer(p.get('max_records', 20000), 'INVALID_RECORD_LIMIT', 1, 200000)
    if transport == 'tim-export':
        p['input'] = str(local_path(base, p.get('input')))
        p['manifest'] = str(local_path(base, p.get('manifest')))
    if transport.endswith('-ui'):
        ui = p.get('desktop')
        if not isinstance(ui, dict) or ui.get('profile_reviewed') is not True:
            raise IMError('REVIEWED_DESKTOP_PROFILE_REQUIRED')
        from .windows_desktop import validate_profile
        validate_profile(ui, p['platform'], p['conversation_name'])
    if 'since' in p: iso_epoch(p['since'])
    return p


def profile_fingerprint(p: dict) -> str:
    # Client version changes do not rebind accounts, sources, or message identity.
    scoped = {k: v for k, v in p.items() if k not in ('client_version', 'observed_client_version')}
    if isinstance(scoped.get('desktop'), dict):
        scoped['desktop'] = {k: v for k, v in scoped['desktop'].items()
                             if k not in ('client_version', 'observed_client_version')}
    return digest(scoped)


def reconcile_tim(raw: bytes, p: dict, previous: list[str] | None) -> tuple[dict, list[str]]:
    try: parsed = parse_tim(raw, p['conversation_name'])
    except (ValueError, UnicodeError): raise IMError('TIM_EXPORT_PARSE_OR_GROUP_MISMATCH') from None
    if len(parsed) > p['max_records']: raise IMError('TIM_EXPORT_RECORD_LIMIT')
    signatures = [digest([r['timestamp'], r['sender_label_and_account'], r['text'], r['content_flags']]) for r in parsed]
    if previous is not None and (len(signatures) < len(previous) or signatures[:len(previous)] != previous):
        raise IMError('TIM_HISTORY_NOT_EXACT_APPEND_REVIEW_REQUIRED')
    # Stable occurrence identity, conditional on exact whole-history prefix equality.
    # Two identical same-second messages remain two different ordinals.
    records = [{'ordinal': i, 'timestamp': r['timestamp'], 'sender': r['sender_label_and_account'],
                'text': r['text'], 'flags': r['content_flags']} for i, r in enumerate(parsed)]
    return {'schema': 'im-hub-tim-sequence/1', 'binding': {k: p[k] for k in BINDING_KEYS},
            'raw_sha256': digest(raw), 'records': records, 'identity_scope': 'full_export_exact_prefix_ordinal'}, signatures


def desktop_status(home: Path, source=None):
    check_home(home)
    path = home / 'desktop.sqlite3'
    if not path.exists(): return {'items': [], 'query_only': True}
    con = readonly(path)
    try:
        result = []
        for row in con.execute('SELECT * FROM sources ORDER BY source_id'):
            if source and source != row['source_id']: continue
            counts = dict(con.execute('SELECT status,count(*) FROM runs WHERE source_id=? GROUP BY status', (row['source_id'],)))
            result.append({'source': row['source_id'], 'last_success_at': row['last_success_at'], 'last_error': row['last_error'],
                           'runs_by_status': counts, 'history_complete': False})
        return {'items': result, 'query_only': True}
    finally: con.close()


def _finish(home, con, run, backend):
    meta = json.loads(run['manifest_json']); rid = run['run_id']
    if not re.fullmatch(r'[0-9a-f]{32}', rid): raise IMError('INVALID_DESKTOP_RUN_ID')
    path = home / 'desktop' / rid / 'source.json'
    extra = {'backend': backend} if backend is not None else {}
    try:
        result = ingest(home, path, meta['spec'], meta['observed_at'], since=meta.get('since'),
                        expected_sha256=meta['sha256'], **extra)
        with con:
            con.execute('UPDATE sources SET sequence_json=?,last_success_at=?,last_error=NULL WHERE source_id=?',
                        (canonical(meta['sequence']) if meta['sequence'] is not None else None, now(), run['source_id']))
            con.execute("UPDATE runs SET status='committed',completed_at=?,error_code=NULL WHERE run_id=?", (now(), rid))
        return {**result, 'run_id': rid, 'history_complete': False, 'llm_calls': 0,
                'acquisition': meta['acquisition'], 'configured_source': run['source_id']}
    except Exception as exc:
        code = exc.code if isinstance(exc, IMError) else 'DESKTOP_IMPORT_FAILED'
        with con:
            con.execute("UPDATE runs SET status='import_failed',error_code=? WHERE run_id=?", (code, rid))
            con.execute('UPDATE sources SET last_error=? WHERE source_id=?', (code, run['source_id']))
        raise


def collect_desktop(home: Path, base: Path, source_name: str, profile: dict, dry_run=False,
                    backend=None, allow_ui=False, after_sequence=None, driver=None):
    p = prepare_desktop_profile(base, profile)
    check_home(home)
    ui = p['transport'].endswith('-ui')
    if ui and not allow_ui: raise IMError('UI_CONSENT_REQUIRED')
    key = profile_fingerprint(p)
    if dry_run:
        return {'configured_source': source_name, 'transport': p['transport'], 'dry_run': True, 'written': False,
                'profile_valid': True, 'client_access_performed': False, 'requirements': ['actual_client_identity_scope_and_payload_capabilities'] +
                (['interactive_unlocked_desktop', 'no_simultaneous_user_input'] if ui else []), 'llm_calls': 0}
    with writer(home, lock_name='.desktop.lock'):
        con = sqlite3.connect(home / 'desktop.sqlite3'); con.row_factory = sqlite3.Row
        try:
            con.executescript(DDL)
            prior = con.execute('SELECT * FROM sources WHERE source_id=?', (source_name,)).fetchone()
            if prior and prior['profile_sha256'] != key:
                if prior['profile_sha256'] != digest(p):
                    raise IMError('DESKTOP_PROFILE_CHANGED_REBIND_REQUIRED')
                # Exact old profile migration: no identity or scope widening.
                with con:
                    con.execute('UPDATE sources SET profile_sha256=? WHERE source_id=?', (key, source_name))
            pending = con.execute("SELECT * FROM runs WHERE source_id=? AND status IN ('staged','import_failed') ORDER BY created_at", (source_name,)).fetchall()
            if len(pending) > 1: raise IMError('MULTIPLE_PENDING_DESKTOP_RUNS')
            if pending:
                return {**_finish(home, con, pending[0], backend), 'resumed_pending': True, 'ui_performed': False}
            with con: con.execute('INSERT OR IGNORE INTO sources VALUES(?,?,NULL,NULL,NULL)', (source_name, key))
            rid = uuid.uuid4().hex; folder = home / 'desktop' / rid; folder.mkdir(parents=True, mode=0o700)
            try:
                raw_blobs = []
                if p['transport'] == 'tim-export':
                    meta = load_json(read_blob(Path(p['manifest'])))
                    if not isinstance(meta, dict) or meta.get('source_id') != source_name: raise IMError('SOURCE_MANIFEST_BINDING_MISMATCH')
                    observed = meta.get('observed_at'); iso_epoch(observed)
                    raw = read_blob(Path(p['input']))
                    if meta.get('sha256') != digest(raw): raise IMError('SOURCE_HASH_MISMATCH')
                    if digest(read_blob(Path(p['input']))) != digest(raw): raise IMError('SOURCE_CHANGED_DURING_READ')
                    raw_blobs = [raw]
                    acquisition = {'transport': 'tim-export', 'ui_performed': False, 'export_observation_from_manifest': True}
                elif p['transport'] == 'wecom-clipboard':
                    if after_sequence is None: raise IMError('EXPLICIT_COPY_BASELINE_REQUIRED')
                    from .windows_desktop import NativeWindows
                    native = driver or NativeWindows()
                    raw, acquisition = native.capture(after_sequence)
                    observed = acquisition['captured_at']; raw_blobs = [raw]
                else:
                    from .windows_desktop import acquire_ui
                    raw_blobs, acquisition = acquire_ui(p, folder, driver=driver)
                    observed = acquisition['captured_at']
                if not raw_blobs: raise IMError('NO_NATIVE_DATA_COLLECTED')
                if sum(map(len, raw_blobs)) > MAX_BYTES: raise IMError('DESKTOP_CAPTURE_SIZE_LIMIT')
                if p['platform'] == 'qq':
                    previous = json.loads(prior['sequence_json']) if prior and prior['sequence_json'] is not None else None
                    packet, sequence = reconcile_tim(raw_blobs[0], p, previous)
                    adapter = 'tim-sequence-json'; payload = canonical(packet).encode('utf-8')
                else:
                    from .codecs.desktop import enriched_wecom
                    rows = {}
                    for blob in raw_blobs:
                        for row in enriched_wecom(blob, p['account_namespace'], p['conversation_name'], p['binding']):
                            if not row['metadata_fields_available']: raise IMError('WECOM_SOURCE_METADATA_MISSING')
                            native_key = digest(row['source_id_fields'])
                            # Different encodings/observations of the same native message can differ
                            # in archive metadata; compare only the observed semantic fields.
                            semantic = {k: row.get(k) for k in ('source_id_fields', 'timestamp_field12_raw', 'display_text_with_mentions', 'message_type_code', 'supplemental_unknown')}
                            if native_key in rows and rows[native_key][0] != semantic: raise IMError('WECOM_NATIVE_ID_CONFLICT')
                            rows[native_key] = (semantic, row)
                    if len(rows) > p['max_records']: raise IMError('WECOM_CAPTURE_RECORD_LIMIT')
                    payload = canonical([x[1] for x in rows.values()]).encode('utf-8'); sequence = None; adapter = 'wecom-json'
                if len(payload) > MAX_BYTES: raise IMError('DESKTOP_CAPTURE_SIZE_LIMIT')
                # Reject wrong-group or malformed data before keeping any original payload.
                for i, blob in enumerate(raw_blobs): write_new(folder / (str(i) + '.bin'), blob)
                spec = spec_for(p['platform'], p['account_namespace'], p['conversation_id'], p['conversation_name'],
                                adapter, p['source_epoch'], p['data_class'], p.get('binding'))
                write_new(folder / 'source.json', payload)
                manifest = {'spec': spec, 'observed_at': observed, 'sha256': digest(payload), 'sequence': sequence,
                            'since': iso_epoch(p['since']) if 'since' in p else None,
                            'acquisition': acquisition, 'history_complete': False}
                write_new(folder / 'manifest.json', canonical(manifest).encode('utf-8'))
                with con:
                    con.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)', (rid, source_name, 'staged', now(), None, canonical(manifest), None))
            except Exception as exc:
                code = exc.code if isinstance(exc, IMError) else 'DESKTOP_ACQUISITION_FAILED'
                with con:
                    con.execute('UPDATE sources SET last_error=? WHERE source_id=?', (code, source_name))
                    con.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)', (rid, source_name, 'read_failed', now(), now(), '{}', code))
                raise
            run = con.execute('SELECT * FROM runs WHERE run_id=?', (rid,)).fetchone()
            return {**_finish(home, con, run, backend), 'resumed_pending': False, 'ui_performed': ui}
        finally: con.close()
