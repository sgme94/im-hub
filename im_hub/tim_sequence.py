"""Normalize a reviewed TIM full-export sequence, never a fuzzy deduplication.
The desktop collector validates exact prefix continuity before creating this packet.
Sequence IDs are local to the explicitly bound source epoch, not Tencent native IDs.
"""
from __future__ import annotations
from .common import IMError, MAX_RECORDS, digest, load_json

BINDING = ('platform', 'account_namespace', 'conversation_id', 'conversation_name', 'source_epoch', 'data_class')


def normalize_sequence(blob, spec, since=None, until=None):
    from .adapters import _record
    obj = load_json(blob)
    if not isinstance(obj, dict) or obj.get('schema') != 'im-hub-tim-sequence/1' or obj.get('binding') != {k: spec[k] for k in BINDING}:
        raise IMError('TIM_SEQUENCE_BINDING_MISMATCH')
    source = obj.get('records')
    if not isinstance(source, list) or len(source) > MAX_RECORDS:
        raise IMError('TIM_SEQUENCE_RECORD_LIMIT')
    output = []
    for ordinal, row in enumerate(source):
        if not isinstance(row, dict) or isinstance(row.get('ordinal'), bool) or row.get('ordinal') != ordinal:
            raise IMError('TIM_SEQUENCE_ORDINAL_INVALID')
        flags = row.get('flags')
        if not isinstance(flags, list) or any(not isinstance(f, str) for f in flags):
            raise IMError('TIM_SEQUENCE_FLAGS_INVALID')
        sender = row.get('sender')
        if not isinstance(sender, str) or not sender:
            raise IMError('TIM_SEQUENCE_SENDER_MISSING')
        output.append(_record(spec, 'sequence:' + str(ordinal), 'verified_export_prefix_ordinal_not_native',
            row.get('timestamp'), row.get('text'), 0 if row.get('text') and not flags else 99,
            digest(sender), sender, False, {'source_ordinal': ordinal, 'export_sha256': obj.get('raw_sha256')}, flags=flags))
    selected = [r for r in output if (since is None or r['timestamp'] >= since) and (until is None or r['timestamp'] < until)]
    selected.sort(key=lambda r: (r['event_ms'], r['message_key']))
    return selected, {'scope': 'verified_prefix_export_snapshot', 'selected_records': len(selected),
        'source_records_recognized': len(source), 'history_completeness': 'partial', 'complete_through': None,
        'cross_snapshot_dedup': 'strict_prefix_same_timeline_only',
        'gaps': ['server_history_not_verified', 'TXT_media_and_reply_semantics_may_be_lossy',
                 'non_prefix_exports_require_new_reviewed_timeline']}
