"""Four-platform deterministic adapters; input snapshots are not live refreshes.
Existing experimental parsers are reused, not overwritten. Undocumented IDs stay provisional.
"""
from __future__ import annotations
import json
import importlib
from pathlib import Path
from .common import IMError, MAX_RECORDS, canonical, digest, iso_epoch, label, load_json, stamp, stream_key

ADAPTERS = ('normalized-v2', 'database-json', 'tim-txt', 'tim-sequence-json', 'wecom-json', 'wecom-native', 'qce-json')

def existing_module(folder: str, module: str):
    reviewed = {
        ('ui_boundary_20260915', 'desktop_readers'): 'desktop',
        ('qq_qce_chatlab_20260915', 'qce_compat'): 'qce',
    }
    codec = reviewed.get((folder, module))
    if codec is None:
        raise IMError('UNREVIEWED_CODEC')
    return importlib.import_module('.codecs.' + codec, __package__)

def spec_for(platform: str, account: str, conversation: str, name: str, adapter: str,
             epoch: str, data_class: str, binding=None, source_account=None) -> dict:
    if platform not in ('wechat', 'kim', 'wecom', 'qq') or adapter not in ADAPTERS:
        raise IMError('UNSUPPORTED_ADAPTER')
    allowed = {'normalized-v2': ('wechat', 'kim'), 'database-json': ('wechat', 'kim'), 'tim-txt': ('qq',), 'tim-sequence-json': ('qq',),
               'wecom-json': ('wecom',), 'wecom-native': ('wecom',), 'qce-json': ('qq',)}
    if platform not in allowed[adapter] or data_class not in ('real', 'synthetic'):
        raise IMError('ADAPTER_PLATFORM_MISMATCH')
    if adapter.startswith('wecom'):
        label(binding, 'EXPLICIT_WECOM_BINDING_REQUIRED')
    if adapter == 'qce-json':
        label(source_account, 'QCE_SOURCE_ACCOUNT_REQUIRED')
    return {'platform': platform, 'account_namespace': label(account),
            'conversation_id': label(conversation), 'conversation_name': label(name),
            'adapter': adapter, 'source_epoch': label(epoch), 'data_class': data_class,
            'binding': binding, 'source_account': source_account,
            'collection_mode': 'explicit_database_snapshot' if adapter == 'database-json' else 'existing_file_or_native_payload',
            'adapter_version': '1', 'full_history_verified': False}

def _record(spec, identity, quality, ts, text, kind, sender, sender_name, sender_verified,
            locator, native_id=None, flags=None, reply=None, extras=None, event_ms=None):
    if isinstance(ts, bool) or not isinstance(ts, int) or not 946684800 <= ts < 4102444800:
        raise IMError('INVALID_MESSAGE_TIMESTAMP')
    if text is not None and (not isinstance(text, str) or len(text) > 200000):
        raise IMError('INVALID_OR_OVERSIZED_MESSAGE_BODY')
    if not isinstance(kind, int) or isinstance(kind, bool):
        raise IMError('INVALID_MESSAGE_TYPE')
    sid = stream_key(spec)
    mid = digest([sid, label(str(identity))])
    content_flags = list(flags or [])
    if text == '':
        text = None
        content_flags.append('empty_source_text')
    if sender == 'unresolved':
        sender = 'unresolved-per-message:' + mid
    result = {'message_key': mid, 'stream_id': sid, 'platform': spec['platform'],
              'account_namespace': spec['account_namespace'], 'conversation_id': spec['conversation_id'],
              'conversation_name': spec['conversation_name'], 'data_class': spec['data_class'],
              'identity_quality': quality, 'native_global_id_verified': False,
              'native_message_id': native_id, 'source_identity': str(identity),
              'event_ms': event_ms if event_ms is not None else ts * 1000,
              'event_time': stamp((event_ms if event_ms is not None else ts * 1000) / 1000),
              'timestamp': ts, 'text': text, 'type': kind,
              'sender_ref': str(sender), 'sender_name': str(sender_name),
              'sender_verified': bool(sender_verified), 'reply_to_source_id': reply,
              'body_state': 'parsed' if not content_flags and text is not None else 'partial',
              'content_flags': content_flags, 'source_locator': locator,
              'evidence_ref': 'immsg:' + mid, 'extras': extras or {}}
    result['semantic_sha256'] = digest({k: result[k] for k in
        ('message_key', 'event_ms', 'text', 'type', 'sender_ref', 'reply_to_source_id', 'body_state', 'content_flags', 'extras')})
    return result

def normalize(blob: bytes, spec: dict, since=None, until=None) -> tuple[list[dict], dict]:
    adapter, platform = spec['adapter'], spec['platform']
    if adapter == 'tim-sequence-json':
        from .tim_sequence import normalize_sequence
        return normalize_sequence(blob, spec, since, until)
    rows = []
    recognized = 0
    acquisition = None
    if adapter in ('normalized-v2', 'database-json'):
        if adapter == 'database-json':
            envelope = load_json(blob)
            keys = ('platform', 'account_namespace', 'conversation_id', 'conversation_name', 'source_epoch', 'data_class')
            if not isinstance(envelope, dict) or envelope.get('schema') != 'im-hub-database/1' or envelope.get('binding') != {k: spec[k] for k in keys}:
                raise IMError('DATABASE_ENVELOPE_BINDING_MISMATCH')
            source = envelope.get('records')
            acquisition = envelope.get('acquisition')
            kind = 'plaintext_cache' if platform == 'wechat' else 'native_local_database'
            transport = 'wechat-sqlite' if platform == 'wechat' else 'kim-sqlite'
            if not isinstance(acquisition, dict) or acquisition.get('source_kind') != kind or acquisition.get('transport') != transport or acquisition.get('local_window_read_complete') is not True:
                raise IMError('DATABASE_ACQUISITION_METADATA_INVALID')
            iso_epoch(acquisition.get('observed_at'))
            if acquisition.get('window_since') != since or acquisition.get('window_until_exclusive') != until:
                raise IMError('DATABASE_ENVELOPE_WINDOW_MISMATCH')
        else:
            try:
                source = [json.loads(line) for line in blob.decode('utf-8-sig').splitlines() if line.strip()]
            except (ValueError, UnicodeError, RecursionError):
                raise IMError('INVALID_NORMALIZED_JSONL') from None
        if not isinstance(source, list) or len(source) > MAX_RECORDS:
            raise IMError('MESSAGE_COUNT_BOUND')
        selected = [r for r in source if isinstance(r, dict) and r.get('platform') == platform and str(r.get('group_id')) == spec['conversation_id']]
        if acquisition is not None and len(selected) != len(source):
            raise IMError('DATABASE_RECORD_OUTSIDE_BOUND_SOURCE')
        if not selected and acquisition is None:
            raise IMError('CONVERSATION_NOT_PRESENT_IN_INPUT')
        for r in selected:
            if r.get('group_name') != spec['conversation_name']:
                raise IMError('CONVERSATION_NAME_MISMATCH')
            ty = str(r['type'])
            if platform == 'wechat':
                kind = {'文本': 0, '图片': 1, '语音': 2, '视频': 3, '动画表情': 5, '系统消息': 80}.get(ty, 99)
                sender = str(r.get('source_shard', '')) + ':' + str(r.get('sender_raw_id', 'unresolved'))
                native = r.get('server_id') or None
            else:
                kind = {'4': 0, '13': 25, '3': 4}.get(ty, 99)
                sender = str(r.get('sender_id', 'unresolved'))
                native = str(r['message_id']) if r.get('message_id') is not None else None
            flags = [] if r.get('body_available') is True and kind == 0 else ['nontext_or_body_not_fully_decoded']
            extras = {'source_type': ty}
            if acquisition is not None:
                given_flags = r.get('content_flags', [])
                given_extras = r.get('database_extras', {})
                if not isinstance(given_flags, list) or any(not isinstance(f, str) for f in given_flags) or not isinstance(given_extras, dict):
                    raise IMError('DATABASE_RECORD_METADATA_INVALID')
                flags = sorted(set(flags + given_flags))
                extras['database'] = given_extras
            rows.append(_record(spec, r['evidence_id'], 'local_source_ref_epoch_scoped', r['timestamp'], r.get('text'), kind,
                sender, r.get('sender') or '未核实发送者', r.get('sender_verified') is True,
                {'evidence_id': r['evidence_id'], 'source_shard': r.get('source_shard'), 'local_id': r.get('local_id')},
                native_id=native, flags=flags, reply=r.get('reply_to'), extras=extras))
            if acquisition is not None:
                rows[-1]['source_kind'] = acquisition['source_kind']
                rows[-1]['upstream_observed_at'] = None
                rows[-1]['source_time_basis'] = 'cache_read_not_client_sync' if platform == 'wechat' else 'local_database_read_not_server_sync'
        recognized = len(selected)
    elif adapter == 'tim-txt':
        parser = existing_module('ui_boundary_20260915', 'desktop_readers')
        try:
            source = parser.parse_tim(blob, spec['conversation_name'])
        except (ValueError, UnicodeError) as exc:
            raise IMError('TIM_PARSE_OR_GROUP_VALIDATION_FAILED') from exc
        recognized = len(source)
        for r in source:
            sender = r['sender_label_and_account']
            flags = r['content_flags']
            rows.append(_record(spec, r['evidence_id'], 'snapshot_ordinal_not_cross_export', r['timestamp'], r['text'],
                0 if r['text'] and not flags else 99, digest(sender), sender, False,
                {'source_ordinal': r['source_ordinal'], 'source_line': r['source_line']}, flags=flags))
    elif adapter in ('wecom-json', 'wecom-native'):
        if adapter == 'wecom-json':
            source = load_json(blob)
        else:
            parser = existing_module('ui_boundary_20260915', 'desktop_readers')
            try:
                source = parser.enriched_wecom(blob, spec['account_namespace'], spec['conversation_name'], spec['binding'])
            except (ValueError, UnicodeError) as exc:
                raise IMError('WECOM_NATIVE_PARSE_FAILED') from exc
        if not isinstance(source, list) or len(source) > MAX_RECORDS:
            raise IMError('INVALID_WECOM_RECORDS')
        for r in source:
            if not isinstance(r, dict) or r.get('group_label') != spec['conversation_name']:
                raise IMError('WECOM_GROUP_MISMATCH')
            fields = r.get('source_id_fields', {})
            if fields.get('field_6') != spec['binding'] or not r.get('metadata_fields_available'):
                raise IMError('WECOM_BINDING_OR_METADATA_FAILED')
            text = r.get('display_text_with_mentions', r.get('text'))
            complete = r.get('supplemental_body_complete', r.get('content_complete', False))
            flags = [] if complete else ['unknown_native_elements']
            rows.append(_record(spec, digest(fields), 'provisional_native_fields', r['timestamp_field12_raw'], text, 0 if complete else 99,
                'unresolved', '未核实发送者', False,
                {'archive_sha256': r.get('archive_sha256'), 'archive_record_index': r.get('archive_record_index')},
                flags=flags, extras={'source_id_fields': fields, 'source_type': r.get('message_type_code'),
                'mentions': r.get('mentions_observed', []), 'sender_mapping': 'unverified'}))
        recognized = len(source)
    elif adapter == 'qce-json':
        parser = existing_module('qq_qce_chatlab_20260915', 'qce_compat')
        try:
            payload, _ = parser.adapt(load_json(blob), spec['conversation_id'], spec['source_account'])
        except ValueError as exc:
            raise IMError('QCE_COMPAT_VALIDATION_FAILED') from exc
        source = payload['messages']
        for i, r in enumerate(source):
            content, sender = r['content'], r['sender']
            kind = {'text': 0, 'image': 1, 'voice': 2, 'video': 3, 'file': 4, 'reply': 25, 'system': 80}.get(r['type'], 99)
            flags = [] if kind == 0 and not content.get('resources') else ['media_or_rich_content_not_fully_decoded']
            rows.append(_record(spec, r['id'], 'export_native_id_client_history_unverified', r['timestamp'] // 1000,
                content.get('text'), kind, sender.get('uin') or sender.get('uid'),
                sender.get('groupCard') or sender.get('nickname') or sender.get('name') or '未核实发送者', False,
                {'source_ordinal': i}, native_id=r['id'], flags=flags,
                reply=(content.get('reply') or {}).get('referencedMessageId'), event_ms=r['timestamp']))
        recognized = len(source)
    if len(rows) > MAX_RECORDS:
        raise IMError('MESSAGE_COUNT_BOUND')
    if len({r['message_key'] for r in rows}) != len(rows):
        raise IMError('DUPLICATE_SOURCE_ID_WITHIN_BATCH')
    selected = [r for r in rows if (since is None or r['event_ms'] >= since * 1000) and (until is None or r['event_ms'] < until * 1000)]
    if acquisition is not None and len(selected) != len(rows):
        raise IMError('DATABASE_RECORD_OUTSIDE_DECLARED_WINDOW')
    selected.sort(key=lambda r: (r['event_ms'], r['message_key']))
    coverage = {'scope': 'sampled_history' if platform == 'wecom' else 'input_local_snapshot',
                'source_records_recognized': recognized, 'selected_records': len(selected),
                'requested_since': stamp(since) if since is not None else None,
                'requested_until_exclusive': stamp(until) if until is not None else None,
                'observed_event_min': selected[0]['event_time'] if selected else None,
                'observed_event_max': selected[-1]['event_time'] if selected else None,
                'complete_through': None, 'history_completeness': 'partial',
                'cross_snapshot_dedup': 'unverified' if adapter == 'tim-txt' else 'identity_profile_scoped',
                'gaps': ['server_history_not_verified', 'attachments_not_fully_decoded']}
    if platform == 'wecom':
        coverage['gaps'] += ['continuous_history_not_collected', 'native_id_and_sender_semantics_provisional']
    if adapter == 'qce-json':
        coverage['gaps'] += ['real_QQNT_login_and_history_not_accepted_in_20260915_experiment']
    if acquisition is not None:
        coverage.update({'scope': 'configured_local_database_window', 'source_kind': acquisition['source_kind'],
                         'transport': acquisition['transport'], 'local_window_read_complete': True,
                         'snapshot_consistency': 'per_database_read_transaction', 'atomic_across_databases': False,
                         'database_count': acquisition.get('database_count'), 'client_sync_verified': False,
                         'upstream_observed_at': None})
        if acquisition['source_kind'] == 'plaintext_cache':
            coverage['gaps'] += ['upstream_cache_refresh_time_unknown', 'encrypted_client_refresh_not_implemented']
    return selected, coverage
