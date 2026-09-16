"""Read already exported/copied data; never drives GUI, decrypts or sends.
QQ TXT identities are snapshot-scoped. WeCom fields remain empirically mapped.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ = timezone(timedelta(hours=8))
HEADER = re.compile(r'^(\d{4}-\d{2}-\d{2}) (上午|下午) (\d{1,2}):(\d{2}):(\d{2}) (.+)$', re.M)
MAX_FILE = 30_000_000

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def parse_tim(data: bytes, expected_group: str) -> list[dict]:
    if not data or len(data) > MAX_FILE:
        raise ValueError('INVALID_FILE_SIZE')
    text = data.decode('utf-8-sig', errors='strict').replace('\r\n', '\n')
    matches = list(HEADER.finditer(text))
    if not matches:
        raise ValueError('NO_RECOGNIZED_MESSAGE_HEADERS')
    group = re.findall(r'^消息对象:(.+)$', text[:matches[0].start()], re.M)
    if group != [expected_group]:
        raise ValueError('EXPORT_GROUP_MISMATCH')
    digest = sha(data)
    result = []
    for i, m in enumerate(matches):
        date, part, hour, minute, second, sender = m.groups()
        h = int(hour)
        if not 1 <= h <= 12:
            raise ValueError('INVALID_12_HOUR_TIME')
        h = h % 12 + (12 if part == '下午' else 0)
        dt = datetime.fromisoformat(date).replace(hour=h, minute=int(minute), second=int(second), tzinfo=TZ)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip('\n')
        flags = []
        if not body:
            flags.append('empty_export_body_not_proof_of_no_content')
        if re.search(r'\[(?:图片|语音|视频|文件|表情)\]', body):
            flags.append('media_placeholder_not_decoded')
        result.append({
            'platform': 'qq', 'client': 'TIM', 'group_name': expected_group,
            'source_kind': 'official_client_txt_export',
            'source_file_sha256': digest, 'source_ordinal': i,
            'source_line': text.count('\n', 0, m.start()) + 1,
            'evidence_id': f'tim-export:{digest}:{i}',
            'identity_scope': 'export_snapshot_ordinal_not_global_msgid',
            'sender_label_and_account': sender,
            'timestamp': int(dt.timestamp()), 'time': dt.isoformat(),
            'text': body, 'content_flags': flags,
            'native_global_message_id_verified': False,
        })
    return result

def selected_window(rows: list[dict], start: int, end: int) -> list[dict]:
    if start >= end:
        raise ValueError('INVALID_WINDOW')
    return [row for row in rows if start <= row['timestamp'] < end]


def enriched_wecom(blob: bytes, namespace: str, group: str, binding: str) -> list[dict]:
    """Supplement the existing parser with an independently UI-matched type5 mention.
    This does not validate all undocumented field semantics or all message types.
    """
    from .wecom_wire import normalize, archive_records, wire_fields, one
    normalized = normalize(blob, namespace, group, binding)
    for row, payload in zip(normalized, archive_records(blob), strict=True):
        fields = wire_fields(payload)
        parts, mentions, unknown = [], [], []
        if row['message_type_code'] == 2:
            rich = wire_fields(one(fields, 8, 2, b''))
            for wt, raw in rich.get(1, []):
                ef = wire_fields(raw)
                kind = one(ef, 1, 0)
                content = one(ef, 2, 2)
                if wt != 2 or not isinstance(content, bytes):
                    unknown.append(kind)
                    continue
                child = wire_fields(content)
                if kind == 0:
                    parts.append(one(child, 1, 2).decode('utf-8', errors='strict'))
                elif kind == 5:
                    label = one(child, 2, 2).decode('utf-8', errors='strict')
                    uid = one(child, 1, 0)
                    mentions.append({'label': label, 'id_raw': str(uid), 'mapping': 'observed_type5_not_official_schema'})
                    parts.append('@' + label)
                else:
                    unknown.append(kind)
            unknown += ['container_field_' + str(n) for n in rich if n != 1]
        else:
            unknown.append('message_type_' + str(row['message_type_code']))
        row.update({'display_text_with_mentions': ''.join(parts), 'mentions_observed': mentions,
                    'supplemental_unknown': unknown,
                    'supplemental_body_complete': bool(parts) and not unknown,
                    'supplemental_parser': 'observed_kind0_text_kind5_mention_v1'})
    return normalized
