"""Bounded read-only KIM SQLite and already-plaintext WeChat cache readers.
No client/library initialization, credentials, decryption, GUI, network or LLM.
A successful read describes the local snapshot, never server-history completeness.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from .common import IMError, canonical, digest, iso_epoch, label, now, readonly

BINDING_KEYS = ('platform', 'account_namespace', 'conversation_id', 'conversation_name', 'source_epoch', 'data_class')
TRANSPORTS = ('kim-sqlite', 'wechat-sqlite', 'wechat-live')
MAX_BODY_BYTES = 1_000_000


def integer(value, code='INVALID_SOURCE_INTEGER', minimum=0, maximum=2**63 - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise IMError(code)
    return value


def local_path(base: Path, value) -> Path:
    text = label(value, 'LOCAL_SOURCE_PATH_REQUIRED')
    if '://' in text or text.startswith(('\\\\', '//')):
        raise IMError('REMOTE_SOURCE_PATH_NOT_SUPPORTED')
    path = Path(text).expanduser()
    path = path if path.is_absolute() else base / path
    if path.is_symlink():
        raise IMError('SOURCE_SYMLINK_NOT_ALLOWED')
    return path.resolve()


def prepare_profile(base: Path, profile: dict) -> dict:
    p = dict(profile)
    if p.get('transport') not in TRANSPORTS or p.get('adapter') != 'database-json':
        raise IMError('DATABASE_PROFILE_UNSUPPORTED')
    if p.get('platform') != ('kim' if p['transport'] == 'kim-sqlite' else 'wechat'):
        raise IMError('DATABASE_PLATFORM_MISMATCH')
    for key in BINDING_KEYS:
        label(p.get(key), 'DATABASE_BINDING_REQUIRED')
    if p['data_class'] not in ('real', 'synthetic'):
        raise IMError('INVALID_DATA_CLASS')
    iso_epoch(p.get('initial_since'))
    p['overlap_seconds'] = integer(p.get('overlap_seconds', 3600), 'INVALID_OVERLAP', 1, 604800)
    p['max_records'] = integer(p.get('max_records', 10000), 'INVALID_RECORD_LIMIT', 1, 20000)
    p['timeout_seconds'] = integer(p.get('timeout_seconds', 8), 'INVALID_QUERY_TIMEOUT', 1, 30)
    account = local_path(base, p.get('account_directory'))
    p['account_directory'] = str(account)
    if p['platform'] == 'kim':
        db = local_path(base, p.get('database'))
        if db.parent != account or db.name != 'user.db':
            raise IMError('ACCOUNT_DIRECTORY_BINDING_MISMATCH')
        if not re.fullmatch(r'[0-9]{1,18}', p['conversation_id']):
            raise IMError('INVALID_KIM_GROUP_ID')
        p['expected_session_id'] = integer(p.get('expected_session_id'), 'KIM_SESSION_BINDING_REQUIRED', 1)
        p['database'] = str(db)
        p['source_kind'] = 'native_local_database'
    else:
        if not p['conversation_id'].endswith('@chatroom') or len(p['conversation_id']) > 128:
            raise IMError('ONLY_REVIEWED_WECHAT_GROUPS_SUPPORTED')
        shards = p.get('shards')
        if not isinstance(shards, list) or not 1 <= len(shards) <= 16:
            raise IMError('EXPLICIT_WECHAT_SHARDS_REQUIRED')
        p['shards'] = []
        for item in shards:
            if not isinstance(item, dict) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', str(item.get('id', ''))):
                raise IMError('INVALID_SHARD_ID')
            path = local_path(base, item.get('path'))
            if path.parent != account:
                raise IMError('ACCOUNT_DIRECTORY_BINDING_MISMATCH')
            p['shards'].append({'id': item['id'], 'path': str(path)})
            if p['transport'] == 'wechat-live':
                key_id = label(item.get('key_id'), 'EXPLICIT_CACHED_KEY_ID_REQUIRED').replace('\\', '/')
                if '..' in key_id.split('/') or key_id.startswith('/'):
                    raise IMError('INVALID_CACHED_KEY_ID')
                p['shards'][-1]['key_id'] = key_id
        if len({s['id'] for s in p['shards']}) != len(shards) or len({s['path'] for s in p['shards']}) != len(shards):
            raise IMError('DUPLICATE_SHARD_BINDING')
        p['source_kind'] = 'plaintext_cache'
        if p['transport'] == 'wechat-live':
            if p.get('key_policy') != 'existing-only':
                raise IMError('EXISTING_KEY_ONLY_POLICY_REQUIRED')
            p['existing_key_file'] = str(local_path(base, p.get('existing_key_file')))
            p['source_kind'] = 'authenticated_local_database'
    return p


def file_identity(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise IMError('SOURCE_DATABASE_UNAVAILABLE')
    st = path.stat()
    return {'path': str(path.resolve()), 'device': st.st_dev, 'inode': st.st_ino}


@contextmanager
def snapshot(path: Path, timeout_seconds: int):
    identity = file_identity(path)
    # Reject encrypted/unsupported input before SQLite, without any key fallback.
    with path.open('rb') as source:
        if source.read(16) != b'SQLite format 3\x00':
            raise IMError('SOURCE_NOT_PLAINTEXT_SQLITE')
    con = readonly(path)
    deadline = time.monotonic() + timeout_seconds
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        con.execute('BEGIN')
        con.execute('SELECT count(*) FROM sqlite_master').fetchone()
        observed_at = now()
        yield con, identity, observed_at
        if file_identity(path) != identity:
            raise IMError('SOURCE_REPLACED_DURING_READ')
    except sqlite3.Error:
        raise IMError('SOURCE_SQLITE_QUERY_FAILED_OR_TIMED_OUT') from None
    finally:
        con.close()


def table_schema(con: sqlite3.Connection, table: str, required: set[str]) -> list:
    # Table names below are constants or derived hex, never caller-supplied SQL.
    if not re.fullmatch(r'[A-Za-z0-9_]+', table):
        raise IMError('INVALID_INTERNAL_TABLE')
    found = con.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
    if found is None or found['type'] != 'table' or 'VIRTUAL TABLE' in (found['sql'] or '').upper():
        raise IMError('SOURCE_TABLE_MISSING_OR_UNSUPPORTED')
    schema = [tuple(r) for r in con.execute('PRAGMA table_info("' + table + '")')]
    if not required <= {r[1] for r in schema}:
        raise IMError('SOURCE_SCHEMA_UNSUPPORTED')
    return schema


def bounded_rows(con, sql: str, params: tuple, limit: int) -> list:
    rows = con.execute(sql, (*params, limit + 1)).fetchall()
    if len(rows) > limit:
        raise IMError('SOURCE_WINDOW_TOO_LARGE_NO_CURSOR_ADVANCE')
    return rows


def source_body(value) -> tuple[str | None, str, list[str]]:
    if value is None:
        return None, digest(b''), ['empty_source_body']
    if not isinstance(value, (str, bytes)):
        return None, digest(type(value).__name__), ['unsupported_source_body']
    raw = value.encode('utf-8') if isinstance(value, str) else value
    if len(raw) > MAX_BODY_BYTES:
        raise IMError('SOURCE_MESSAGE_TOO_LARGE_NO_CURSOR_ADVANCE')
    hashed = digest(raw)
    try:
        text = raw.decode('utf-8', errors='strict')
    except UnicodeError:
        return None, hashed, ['binary_or_compressed_body_not_decoded']
    if '\x00' in text or any(ord(c) < 32 and c not in '\t\r\n' for c in text):
        return None, hashed, ['binary_container_not_decoded']
    return text, hashed, [] if text else ['empty_source_body']


def kim_blocks(value) -> tuple[str | None, list[str]]:
    if not isinstance(value, dict) or not isinstance(value.get('content'), list):
        return None, ['unsupported_kim_content_container']
    if len(value['content']) > 4096:
        raise IMError('TOO_MANY_KIM_CONTENT_ELEMENTS')
    parts, gaps = [], []
    for block in value['content']:
        if not isinstance(block, dict):
            gaps.append('unsupported_kim_element'); continue
        if block.get('type') == 0 and isinstance(block.get('text'), str):
            parts.append(block['text'])
        elif block.get('type') == 2 and isinstance(block.get('replyMemberName'), str):
            parts.append('@' + block['replyMemberName'])
        else:
            gaps.append('unsupported_kim_element')
    return ''.join(parts) or None, sorted(set(gaps))


def read_kim(p: dict, since: float, until: float):
    rows, descriptors = [], []
    path = Path(p['database'])
    with snapshot(path, p['timeout_seconds']) as (con, identity, observed):
        gs = table_schema(con, 'group', {'id', 'name', 'sessionID'})
        ms = table_schema(con, 'message', {'id', 'sender', 'senderName', 'sendTime', 'contentType', 'content', 'sessionID', 'msgIdx'})
        target = con.execute('SELECT id,name,sessionID FROM "group" WHERE id=?', (int(p['conversation_id']),)).fetchall()
        if len(target) != 1 or target[0]['name'] != p['conversation_name'] or target[0]['sessionID'] != p['expected_session_id']:
            raise IMError('KIM_CONVERSATION_BINDING_MISMATCH')
        found = bounded_rows(con,
            'SELECT id,sender,senderName,sendTime,contentType,content,sessionID,msgIdx FROM message '
            'WHERE sessionID=? AND sendTime>=? AND sendTime<? ORDER BY sendTime,id LIMIT ?',
            (p['expected_session_id'], since, until), p['max_records'])
        for r in found:
            mid = integer(r['id'], minimum=1)
            ts = integer(r['sendTime'], minimum=946684800, maximum=4102444799)
            kind = integer(r['contentType'])
            source_text, raw_hash, flags = source_body(r['content'])
            body = None; reply = None; quoted = None
            try:
                obj = json.loads(source_text) if source_text is not None else None
            except (ValueError, RecursionError):
                obj = None; flags.append('invalid_kim_body_json')
            if isinstance(obj, dict) and kind in (4, 13):
                body, more = kim_blocks(obj.get('replyContent') if kind == 13 else obj)
                flags.extend(more)
                if kind == 13:
                    quoted, quote_gaps = kim_blocks(obj.get('replyedContent'))
                    flags.extend('quote_' + x for x in quote_gaps)
                    if isinstance(obj.get('replyedMsgId'), int) and not isinstance(obj['replyedMsgId'], bool):
                        reply = str(obj['replyedMsgId'])
            else:
                flags.append('unsupported_kim_message_type_or_body')
            rows.append({'platform': 'kim', 'group_id': p['conversation_id'], 'group_name': p['conversation_name'],
                'evidence_id': 'kim:' + str(p['expected_session_id']) + ':' + str(mid), 'message_id': mid,
                'message_index': r['msgIdx'], 'timestamp': ts, 'type': str(kind), 'text': body,
                'sender_id': integer(r['sender']), 'sender': r['senderName'] or '未核实发送者', 'sender_verified': False,
                'body_available': bool(body) and not flags, 'reply_to': reply,
                'content_flags': sorted(set(flags)), 'database_extras': {'raw_body_sha256': raw_hash, 'quoted_text': quoted}})
        descriptors.append({'identity': identity, 'schema_sha256': digest([gs, ms])})
    return rows, descriptors, observed


def read_wechat(p: dict, since: float, until: float):
    rows, descriptors, observations = [], [], []
    table = 'Msg_' + hashlib.md5(p['conversation_id'].encode('utf-8')).hexdigest()
    names = {1: '文本', 3: '图片', 34: '语音', 43: '视频', 47: '动画表情', 10000: '系统消息'}
    for shard in p['shards']:
        with snapshot(Path(shard['path']), p['timeout_seconds']) as (con, identity, observed):
            schema = table_schema(con, table, {'local_id', 'server_id', 'local_type', 'sort_seq', 'real_sender_id', 'create_time', 'message_content', 'compress_content'})
            found = bounded_rows(con,
                'SELECT local_id,server_id,local_type,sort_seq,real_sender_id,create_time,message_content,compress_content '
                'FROM "' + table + '" WHERE create_time>=? AND create_time<? ORDER BY create_time,local_id LIMIT ?',
                (since, until), p['max_records'] - len(rows))
            for r in found:
                lid = integer(r['local_id'], minimum=1)
                ts = integer(r['create_time'], minimum=946684800, maximum=4102444799)
                code = integer(r['local_type'])
                kind = names.get(code, names.get(code & 0xffffffff, str(code)))
                text, raw_hash, flags = source_body(r['message_content'])
                _, compressed_hash, _ = source_body(r['compress_content'])
                if kind != '文本':
                    flags.append('nontext_body_not_fully_decoded')
                rows.append({'platform': 'wechat', 'group_id': p['conversation_id'], 'group_name': p['conversation_name'],
                    'evidence_id': 'wechat:' + p['conversation_id'] + ':' + shard['id'] + ':' + str(lid),
                    'source_shard': shard['id'], 'local_id': lid,
                    'server_id': str(r['server_id']) if r['server_id'] else None, 'timestamp': ts, 'type': kind,
                    'text': text, 'sender_raw_id': integer(r['real_sender_id']),
                    'sender': '未核实发送者', 'sender_verified': False, 'body_available': bool(text) and not flags,
                    'content_flags': sorted(set(flags)), 'database_extras': {'raw_type': code, 'raw_body_sha256': raw_hash,
                    'compressed_body_sha256': compressed_hash}})
            descriptors.append({'shard': shard['id'], 'identity': identity, 'schema_sha256': digest(schema)})
            observations.append(observed)
    return rows, descriptors, min(observations, key=iso_epoch)


def read_database(p: dict, since: float, until: float) -> dict:
    if since >= until:
        raise IMError('INVALID_WINDOW')
    rows, descriptors, observed = read_kim(p, since, until) if p['platform'] == 'kim' else read_wechat(p, since, until)
    return {'schema': 'im-hub-database/1', 'binding': {k: p[k] for k in BINDING_KEYS}, 'records': rows,
            'acquisition': {'transport': p['transport'], 'source_kind': p['source_kind'], 'observed_at': observed,
                'source_fingerprint': digest(descriptors), 'database_count': len(descriptors),
                'window_since': since, 'window_until_exclusive': until,
                'snapshot_consistency': 'per_database_read_transaction', 'atomic_across_databases': False,
                'local_window_read_complete': True, 'account_binding': 'explicit_directory_not_active_login_proof',
                'upstream_observed_at': None, 'client_sync_verified': False,
                'client_refreshed': False, 'llm_calls': 0}}
