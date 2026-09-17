"""Recoverable single-writer import into ChatLab; the sidecar is provenance only.
ChatLab owns the message bodies. No IM database and no MyMind ledger is written.
"""
from __future__ import annotations
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from . import __version__
from .adapters import normalize
from .common import (ROOT, IMError, binding_spec, canonical, check_home, db_path, digest,
                     iso_epoch, load_json, now, read_blob, readonly, stream_key, write_new, writer)

PORTABLE_ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else ROOT
PACKAGED_BACKEND = PORTABLE_ROOT / 'backend/node_modules/chatlab-cli'
CHATLAB = Path(os.environ.get('IM_HUB_CHATLAB_DIR', str(PACKAGED_BACKEND if PACKAGED_BACKEND.is_dir() else ROOT / 'node_modules/chatlab-cli'))).expanduser().resolve()
PACKAGED_NODE = PORTABLE_ROOT / 'backend/node.exe'
NODE = Path(os.environ.get('IM_HUB_NODE') or (str(PACKAGED_NODE) if PACKAGED_NODE.is_file() else shutil.which('node')) or 'node')
GUARD = Path(__file__).with_name('offline_guard.mjs')

def backend_info() -> dict:
    package = CHATLAB / 'package.json'
    meta = load_json(read_blob(package)) if package.is_file() else {}
    version = meta.get('version')
    return {'installed_version': version, 'required_version': '0.37.1',
            'node_available': NODE.is_file(), 'offline_guard_available': GUARD.is_file(),
            'ready': meta.get('name') == 'chatlab-cli' and version == '0.37.1'
                     and NODE.is_file() and GUARD.is_file() and (CHATLAB / 'bin/chatlab.mjs').is_file()}

def run_chatlab(home: Path, args: list[str]) -> dict:
    info = backend_info()
    if not info['ready']:
        raise IMError('PINNED_CHATLAB_BACKEND_NOT_READY')
    # Import-only internal entry, never exposed as an arbitrary CLI proxy.
    if not args or args[0] not in ('validate', 'import'):
        raise IMError('BACKEND_COMMAND_DENIED')
    env = {k: v for k, v in os.environ.items() if k.upper() in
           {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE'}}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    for rel in ('home', 'appdata', 'localappdata', 'data', 'temp'):
        (home / 'chatlab' / rel).mkdir(exist_ok=True)
    env.update({'HOME': str(home / 'chatlab/home'), 'USERPROFILE': str(home / 'chatlab/home'),
                'APPDATA': str(home / 'chatlab/appdata'), 'LOCALAPPDATA': str(home / 'chatlab/localappdata'),
                'TEMP': str(home / 'chatlab/temp'), 'TMP': str(home / 'chatlab/temp'),
                'CHATLAB_DATA_DIR': str(home / 'chatlab/data'), 'TZ': 'UTC', 'NO_COLOR': '1'})
    tag = uuid.uuid4().hex
    out, err = home / 'runtime' / (tag + '.stdout.json'), home / 'runtime' / (tag + '.stderr.log')
    try:
        with out.open('xb') as stdout, err.open('xb') as stderr:
            proc = subprocess.run([str(NODE), '--import', GUARD.as_uri(),
                                   str(CHATLAB / 'bin/chatlab.mjs'), *args],
                                  cwd=CHATLAB, env=env, stdout=stdout, stderr=stderr, timeout=120)
    except subprocess.TimeoutExpired:
        raise IMError('CHATLAB_IMPORT_OUTCOME_REQUIRES_RECONCILIATION') from None
    response = load_json(read_blob(out)) if out.stat().st_size else {}
    if proc.returncode or not isinstance(response, dict) or response.get('ok') is not True:
        raise IMError('CHATLAB_COMMAND_FAILED_PRIVATE_LOG_RETAINED')
    if args[0] == 'validate' and response.get('data', {}).get('valid') is not True:
        raise IMError('CHATLAB_FORMAT_VALIDATION_FAILED')
    return response

def payload_for(spec: dict, rows: list[dict], observed_at: str) -> dict:
    members, messages = {}, []
    for r in rows:
        sender = 'sender-' + digest([r['stream_id'], r['sender_ref']])[:32]
        members.setdefault(sender, {'platformId': sender, 'accountName': r['sender_name']})
        messages.append({'platformMessageId': r['message_key'], 'sender': sender,
                         'accountName': r['sender_name'], 'timestamp': r['timestamp'],
                         'type': r['type'], 'content': r['text']})
    return {'chatlab': {'version': '0.0.2', 'exportedAt': int(iso_epoch(observed_at)),
                       'generator': 'im-hub/' + __version__,
                       'description': 'Local evidence projection; source identities and coverage are in the im-hub sidecar.'},
            'meta': {'name': spec['conversation_name'],
                     'platform': 'weixin' if spec['platform'] == 'wechat' else spec['platform'],
                     **({'type': 'private'} if spec.get('conversation_type') == 'direct'
                        else {'type': 'group', 'groupId': stream_key(spec)})},
            'members': list(members.values()), 'messages': messages}

def validate_readback(home: Path, sid: str, rows: list[dict]) -> dict:
    if not rows:
        return {'checked': 0, 'quick_check': 'not_applicable_empty_batch'}
    con = readonly(db_path(home, sid))
    try:
        columns = {r['name'] for r in con.execute('PRAGMA table_info(message)')}
        if not {'platform_message_id', 'ts', 'content', 'type'} <= columns:
            raise IMError('CHATLAB_SCHEMA_CHANGED')
        expected = {r['message_key']: r for r in rows}
        stored = {}
        keys = list(expected)
        for pos in range(0, len(keys), 400):
            chunk = keys[pos:pos + 400]
            sql = 'SELECT platform_message_id,ts,content,type FROM message WHERE platform_message_id IN (' + ','.join('?' for _ in chunk) + ')'
            for found in con.execute(sql, chunk):
                key = found['platform_message_id']
                if key in stored:
                    raise IMError('CHATLAB_DUPLICATE_PHYSICAL_ID')
                stored[key] = dict(found)
        if set(stored) != set(expected):
            raise IMError('CHATLAB_MESSAGE_ID_ROUNDTRIP_FAILED')
        for key, r in expected.items():
            found = stored[key]
            if found['ts'] != r['timestamp'] or found['content'] != r['text'] or found['type'] != r['type']:
                raise IMError('CHATLAB_BODY_TIME_TYPE_ROUNDTRIP_FAILED')
        quick = con.execute('PRAGMA quick_check').fetchone()[0]
        if quick != 'ok':
            raise IMError('CHATLAB_INTEGRITY_FAILED')
        return {'checked': len(rows), 'quick_check': quick}
    finally:
        con.close()

def ingest(home: Path, source: Path, spec: dict, observed_at: str, since=None, until=None,
           expected_sha256=None, allow_new_snapshot=False, dry_run=False, backend=run_chatlab) -> dict:
    source = source.resolve()
    observed = iso_epoch(observed_at)
    if observed > iso_epoch(now()) + 300:
        raise IMError('OBSERVED_AT_IN_FUTURE')
    if since is not None and until is not None and since >= until:
        raise IMError('INVALID_WINDOW')
    blob = read_blob(source)
    raw_hash = digest(blob)
    if expected_sha256 is not None and expected_sha256 != raw_hash:
        raise IMError('SOURCE_HASH_MISMATCH')
    rows, coverage = normalize(blob, spec, since, until)
    if rows and max(r['event_ms'] for r in rows) > (observed + 300) * 1000:
        raise IMError('MESSAGE_NEWER_THAN_SOURCE_OBSERVATION')
    if digest(read_blob(source)) != raw_hash:
        raise IMError('SOURCE_CHANGED_DURING_READ')
    sid = stream_key(spec)
    batch_id = digest([sid, raw_hash, since, until, spec['adapter_version']])
    # observed_at is intentionally excluded from batch identity. Replaying an old file
    # must never make its data fresh again. Its first accepted observation is retained.
    base = {'stream_id': sid, 'batch_id': batch_id, 'source_sha256': raw_hash,
            'source_observed_at': observed_at, 'coverage': coverage,
            'spec': spec, 'records': len(rows), 'source_path': str(source)}
    safe = {k: base[k] for k in ('stream_id', 'batch_id', 'source_sha256', 'source_observed_at', 'coverage', 'records')}
    if dry_run:
        return {**safe, 'dry_run': True, 'written': False}
    check_home(home)
    with writer(home):
        con = sqlite3.connect(home / 'index.sqlite3', timeout=5)
        con.row_factory = sqlite3.Row
        try:
            previous = con.execute('SELECT spec_json FROM streams WHERE stream_id=?', (sid,)).fetchone()
            if previous and binding_spec(json.loads(previous['spec_json'])) != binding_spec(spec):
                raise IMError('STREAM_BINDING_CHANGED_REVIEW_REQUIRED')
            batch = con.execute('SELECT * FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
            if batch:
                base = json.loads(batch['manifest_json'])
                safe['source_observed_at'] = base['source_observed_at']
            if batch and batch['status'] == 'committed':
                verified = validate_readback(home, sid, rows)
                return {**safe, 'new_records': 0, 'duplicates': len(rows), 'replayed_batch': True,
                        'revision': batch['revision'], 'readback': verified}
            existing = {}
            for r in con.execute('SELECT message_key,semantic_sha256 FROM records WHERE stream_id=?', (sid,)):
                existing[r['message_key']] = r['semantic_sha256']
            for r in rows:
                if r['message_key'] in existing and existing[r['message_key']] != r['semantic_sha256']:
                    raise IMError('IDENTITY_CONTENT_CONFLICT')
            other = con.execute("SELECT 1 FROM batches WHERE stream_id=? AND source_sha256<>? AND status='committed' LIMIT 1", (sid, raw_hash)).fetchone()
            if spec['adapter'] == 'tim-txt' and other and not allow_new_snapshot:
                raise IMError('TIM_CROSS_EXPORT_IDENTITY_UNVERIFIED')
            if spec['adapter'] == 'tim-txt' and other:
                coverage['gaps'].append('cross_export_duplicates_may_remain_explicitly_accepted')
            folder = home / 'batches' / batch_id
            if folder.exists():
                saved = load_json(read_blob(folder / 'manifest.json'))
                if saved['source_sha256'] != raw_hash or binding_spec(saved['spec']) != binding_spec(spec):
                    raise IMError('BATCH_MANIFEST_CONFLICT')
                base = saved
                payload = payload_for(spec, rows, saved['source_observed_at'])
                if read_blob(folder / 'messages.jsonl') != ''.join(canonical(r) + '\n' for r in rows).encode('utf-8'):
                    raise IMError('STAGED_MESSAGES_TAMPERED')
                staged_payload = load_json(read_blob(folder / 'chatlab.json'))
                # Generator version is descriptive metadata, not message semantics.
                # Recover older prepared imports without rewriting their immutable file.
                generator = staged_payload.get('chatlab', {}).get('generator') if isinstance(staged_payload, dict) else None
                if generator in ('im-hub/0.2.0', 'im-hub/0.3.0', 'im-hub/' + __version__):
                    payload['chatlab']['generator'] = generator
                if staged_payload != payload:
                    raise IMError('STAGED_CHATLAB_PAYLOAD_TAMPERED')
            else:
                staging = home / 'batches' / ('.preparing-' + uuid.uuid4().hex)
                staging.mkdir(mode=0o700)
                write_new(staging / 'messages.jsonl', ''.join(canonical(r) + '\n' for r in rows).encode('utf-8'))
                write_new(staging / 'chatlab.json', canonical(payload_for(spec, rows, base['source_observed_at'])).encode('utf-8'))
                write_new(staging / 'manifest.json', canonical(base).encode('utf-8'))
                staging.rename(folder)
            with con:
                con.execute('INSERT OR IGNORE INTO streams VALUES(?,?)', (sid, canonical(spec)))
                con.execute('INSERT OR IGNORE INTO batches VALUES(?,?,?,?,?,?,?,?)',
                            (batch_id, sid, raw_hash, 'staged', base['source_observed_at'], None, None, canonical(base)))
            try:
                if rows:
                    backend(home, ['validate', str(folder / 'chatlab.json'), '--json'])
                    backend(home, ['import', str(folder / 'chatlab.json'), '--session-id', sid, '--json'])
                verification = validate_readback(home, sid, rows)
                if digest(read_blob(source)) != raw_hash:
                    raise IMError('SOURCE_CHANGED_BEFORE_COMMIT')
                with con:
                    revision = con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0] + 1
                    added = 0
                    for r in rows:
                        if r['message_key'] in existing:
                            continue
                        metadata = {k: v for k, v in r.items() if k not in ('text', 'semantic_sha256')}
                        metadata.update({'batch_id': batch_id, 'source_sha256': raw_hash, 'source_observed_at': base['source_observed_at']})
                        if r.get('source_kind') == 'plaintext_cache':
                            metadata['cache_observed_at'] = base['source_observed_at']
                            metadata['source_observed_at'] = None
                        con.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?)',
                                    (r['message_key'], sid, r['semantic_sha256'], r['event_ms'], batch_id, canonical(metadata), revision))
                        added += 1
                    con.execute("UPDATE meta SET value=? WHERE key='revision'", (revision,))
                    con.execute("UPDATE batches SET status='committed',imported_at=?,revision=? WHERE batch_id=?", (now(), revision, batch_id))
                return {**safe, 'source_observed_at': base['source_observed_at'], 'new_records': added,
                        'duplicates': len(rows) - added, 'replayed_batch': False, 'revision': revision, 'readback': verification}
            except Exception:
                with con:
                    con.execute("UPDATE batches SET status='failed' WHERE batch_id=? AND status<>'committed'", (batch_id,))
                raise
        finally:
            con.close()
