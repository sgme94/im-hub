"""Read-only, fixed-SQL query facade. Never calls a collector or ChatLab CLI."""
from __future__ import annotations
import base64
import json
from pathlib import Path
from .common import IMError, canonical, check_home, db_path, digest, iso_epoch, now, readonly

MAX_LIMIT = 200

def revision(home: Path) -> int:
    con = readonly(home / 'index.sqlite3')
    try:
        return con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
    finally:
        con.close()

def source_status(home: Path, platform=None, stream=None, include_synthetic=False, max_age=None) -> dict:
    check_home(home)
    if max_age is not None and (not isinstance(max_age, int) or max_age < 0):
        raise IMError('INVALID_MAX_AGE')
    con = readonly(home / 'index.sqlite3')
    try:
        items = []
        for row in con.execute('SELECT * FROM streams ORDER BY stream_id'):
            spec = json.loads(row['spec_json'])
            sid = row['stream_id']
            if platform and platform != spec['platform'] or stream and stream != sid:
                continue
            if not include_synthetic and spec['data_class'] == 'synthetic':
                continue
            batches = [dict(x) for x in con.execute('SELECT * FROM batches WHERE stream_id=? ORDER BY revision,batch_id', (sid,))]
            committed = [x for x in batches if x['status'] == 'committed']
            count = con.execute('SELECT count(*) FROM records WHERE stream_id=?', (sid,)).fetchone()[0]
            latest_observation = max((b['observed_at'] for b in committed), key=iso_epoch, default=None)
            cache_observation = None
            if committed and all(json.loads(b['manifest_json'])['coverage'].get('source_kind') == 'plaintext_cache' for b in committed):
                cache_observation, latest_observation = latest_observation, None
            age = max(0, iso_epoch(now()) - iso_epoch(latest_observation)) if latest_observation else None
            freshness = 'unknown' if age is None or max_age is None else ('fresh' if age <= max_age else 'stale')
            summaries = [{'batch_id': b['batch_id'], 'observed_at': b['observed_at'],
                          'imported_at': b['imported_at'], **json.loads(b['manifest_json'])['coverage']} for b in committed]
            items.append({'stream_id': sid, **{k: spec[k] for k in
                ('platform', 'account_namespace', 'conversation_id', 'conversation_name', 'adapter', 'source_epoch', 'data_class', 'collection_mode')},
                'conversation_type': spec.get('conversation_type', 'group'),
                'stored_records': count, 'source_observed_at': latest_observation,
                'cache_observed_at': cache_observation,
                'freshness': freshness, 'freshness_age_seconds': round(age, 3) if age is not None else None,
                'freshness_max_age_seconds': max_age, 'history_completeness': 'partial', 'complete_through': None,
                'failed_or_pending_batches': sum(x['status'] != 'committed' for x in batches), 'batches': summaries})
        if stream and not items:
            raise IMError('STREAM_NOT_FOUND_OR_FILTERED')
        return {'items': items, 'revision': con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0],
                'query_only': True, 'source_refreshed': False,
                'freshness_policy': 'caller_supplied_max_age_not_mymind_business_status'}
    finally:
        con.close()

def _decode_cursor(value):
    try:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError()
        result = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        if not isinstance(result, dict) or not isinstance(result.get('after_ms'), int) or not isinstance(result.get('after_key'), str):
            raise ValueError()
        return result
    except (ValueError, UnicodeError, TypeError):
        raise IMError('INVALID_CURSOR') from None

def query_messages(home: Path, keyword='', platform=None, stream=None, conversation=None,
                   account=None, since=None, until=None, limit=50, cursor=None,
                   include_synthetic=False, full=False, max_age=None, message_key=None, conversation_type=None) -> dict:
    check_home(home)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise IMError('LIMIT_MUST_BE_1_TO_200')
    if not isinstance(keyword, str) or len(keyword) > 500 or '\x00' in keyword:
        raise IMError('INVALID_KEYWORD')
    if since is not None and until is not None and since >= until:
        raise IMError('INVALID_WINDOW')
    scope = {'keyword': keyword, 'platform': platform, 'stream': stream, 'conversation': conversation,
             'account': account, 'since': since, 'until': until, 'include_synthetic': include_synthetic, 'message_key': message_key}
    if conversation_type is not None:
        if conversation_type not in ('group', 'direct'):
            raise IMError('INVALID_CONVERSATION_TYPE_FILTER')
        scope['conversation_type'] = conversation_type
    scope_hash = digest(scope)
    rev = revision(home)
    after_ms, after_key = -1, ''
    if cursor:
        state = _decode_cursor(cursor)
        if state.get('scope') != scope_hash:
            raise IMError('CURSOR_SCOPE_MISMATCH')
        if state.get('revision') != rev:
            raise IMError('CURSOR_STALE_RESTART_QUERY')
        after_ms, after_key = state['after_ms'], state['after_key']
    status = source_status(home, platform, stream, include_synthetic, max_age)
    sources = [s for s in status['items'] if (not conversation or s['conversation_id'] == conversation)
               and (not account or s['account_namespace'] == account)
               and (not conversation_type or s['conversation_type'] == conversation_type)]
    collected = []
    idx = readonly(home / 'index.sqlite3')
    try:
        for src in sources:
            sid = src['stream_id']
            if not src['stored_records']:
                continue
            con = readonly(db_path(home, sid))
            try:
                cols = {r['name'] for r in con.execute('PRAGMA table_info(message)')}
                if not {'platform_message_id', 'content', 'ts', 'type'} <= cols:
                    raise IMError('CHATLAB_SCHEMA_CHANGED')
                con.execute('ATTACH DATABASE ? AS provenance', ((home / 'index.sqlite3').resolve().as_uri() + '?mode=ro',))
                physical = con.execute('SELECT count(*),count(DISTINCT p.message_key) FROM message m JOIN provenance.records p ON p.message_key=m.platform_message_id WHERE p.stream_id=?', (sid,)).fetchone()
                if physical[0] != src['stored_records'] or physical[1] != src['stored_records']:
                    raise IMError('CHATLAB_RECORD_PROVENANCE_DRIFT')
                conditions = ['p.stream_id=?', '(p.event_ms>? OR (p.event_ms=? AND p.message_key>?))']
                args = [sid, after_ms, after_ms, after_key]
                if keyword:
                    conditions.append("instr(lower(coalesce(m.content,'')),lower(?))>0")
                    args.append(keyword)
                if since is not None:
                    conditions.append('p.event_ms>=?'); args.append(since * 1000)
                if until is not None:
                    conditions.append('p.event_ms<?'); args.append(until * 1000)
                if message_key:
                    conditions.append('p.message_key=?'); args.append(message_key)
                sql = 'SELECT p.message_key,p.metadata_json,p.semantic_sha256,p.event_ms,m.content,m.ts,m.type FROM message m JOIN provenance.records p ON p.message_key=m.platform_message_id WHERE ' + ' AND '.join(conditions) + ' ORDER BY p.event_ms,p.message_key LIMIT ?'
                for row in con.execute(sql, [*args, limit + 1]):
                    item = json.loads(row['metadata_json'])
                    if row['ts'] != item['timestamp'] or row['type'] != item['type']:
                        raise IMError('CHATLAB_PROVENANCE_DRIFT')
                    text = row['content']
                    proof = {**item, 'text': text}
                    actual_hash = digest({k: proof[k] for k in ('message_key', 'event_ms', 'text', 'type', 'sender_ref', 'reply_to_source_id', 'body_state', 'content_flags', 'extras')})
                    if actual_hash != row['semantic_sha256']:
                        raise IMError('CHATLAB_BODY_PROVENANCE_DRIFT')
                    item['text_truncated'] = bool(not full and text and len(text) > 2000)
                    item['text'] = text if full or text is None else text[:2000]
                    collected.append(item)
            finally:
                con.close()
    finally:
        idx.close()
    if revision(home) != rev:
        raise IMError('DATASET_CHANGED_RETRY_QUERY')
    collected.sort(key=lambda r: (r['event_ms'], r['message_key']))
    more = len(collected) > limit
    items = collected[:limit]
    if len({r['message_key'] for r in collected}) != len(collected):
        raise IMError('DUPLICATE_PHYSICAL_MESSAGE_ID')
    next_cursor = None
    if more:
        last = items[-1]
        next_cursor = base64.urlsafe_b64encode(canonical({'scope': scope_hash, 'revision': rev,
            'after_ms': last['event_ms'], 'after_key': last['message_key']}).encode()).decode()
    return {'items': items, 'count': len(items), 'has_more': more, 'next_cursor': next_cursor,
            'revision': rev, 'coverage': sources, 'source_refreshed': False, 'query_only': True,
            'empty_result_means': 'no_matching_imported_records_not_proof_of_no_client_messages',
            'warnings': ['partial_source_coverage', 'unverified_sender_or_message_identity_may_exist'],
            'system_messages_included': True}
