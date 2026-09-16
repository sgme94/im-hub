"""Bounded reader for already-copied WeCom messages. Python 3.11+.

Observed profile: WXWork.exe 5.0.10.6025, 'WeWork Message' archive version 19.
No client database, process-memory, network, message sending, or unsafe object deserialization.
Field 12 time and text were checked against one real message. Other source ID field
semantics remain provisional; composite keys are NOT documented global WeCom IDs.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 100
PROFILE = 'wxwork-5.0.10.6025-clipboard-v19-observed'
MAGIC = 'wecom-native-reader-state-v1\n'

class DecodeError(ValueError):
    pass

class IdentityConflict(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for i in range(10):
        if pos >= len(data):
            raise DecodeError('TRUNCATED_VARINT')
        byte = data[pos]; pos += 1
        if i == 9 and byte > 1:
            raise DecodeError('VARINT_OVERFLOW')
        value |= (byte & 127) << (7 * i)
        if byte < 128:
            return value, pos
    raise DecodeError('VARINT_OVERFLOW')


def wire_fields(data: bytes) -> dict[int, list[tuple[int, int | bytes]]]:
    if len(data) > MAX_BYTES:
        raise DecodeError('PAYLOAD_TOO_LARGE')
    result: dict[int, list[tuple[int, int | bytes]]] = {}
    pos = 0; count = 0
    while pos < len(data):
        tag, pos = varint(data, pos); field, wire = tag >> 3, tag & 7
        if not 1 <= field < 2**29:
            raise DecodeError('INVALID_FIELD_NUMBER')
        if wire == 0:
            value, pos = varint(data, pos)
        elif wire in (1, 2, 5):
            if wire == 2:
                length, pos = varint(data, pos)
            else:
                length = 8 if wire == 1 else 4
            if length > MAX_BYTES or pos + length > len(data):
                raise DecodeError('TRUNCATED_LENGTH_DELIMITED_OR_FIXED_FIELD')
            value = data[pos:pos + length]; pos += length
        else:
            raise DecodeError('UNSUPPORTED_WIRE_TYPE')
        result.setdefault(field, []).append((wire, value))
        count += 1
        if count > 4096:
            raise DecodeError('TOO_MANY_FIELDS')
    return result


def one(fields: dict, number: int, wire: int, default: Any = None) -> Any:
    values = fields.get(number, [])
    if not values:
        return default
    if len(values) != 1 or values[0][0] != wire:
        raise DecodeError(f'AMBIGUOUS_OR_WRONG_TYPE_FIELD_{number}')
    return values[0][1]


def archive_records(blob: bytes) -> list[bytes]:
    if len(blob) > MAX_BYTES:
        raise DecodeError('ARCHIVE_TOO_LARGE')
    match = re.match(rb'22 serialization::archive 19 0 0 ([0-9]{1,4}) 0 ', blob)
    if not match:
        raise DecodeError('UNSUPPORTED_ARCHIVE_HEADER')
    count = int(match[1])
    if not 1 <= count <= MAX_RECORDS:
        raise DecodeError('RECORD_COUNT_OUT_OF_BOUNDS')
    pos = match.end(); result = []
    for index in range(count):
        if index:
            if blob[pos:pos + 1] != b' ':
                raise DecodeError('MISSING_RECORD_SEPARATOR')
            pos += 1
        length_match = re.match(rb'([0-9]{1,8}) ', blob[pos:pos + 10])
        if not length_match:
            raise DecodeError('INVALID_RECORD_LENGTH')
        length = int(length_match[1]); pos += length_match.end()
        if not 1 <= length <= MAX_BYTES or pos + length > len(blob):
            raise DecodeError('TRUNCATED_ARCHIVE_RECORD')
        result.append(blob[pos:pos + length]); pos += length
    if blob[pos:] not in (b'', b'\n', b'\r\n', b'\0', b'\n\0'):
        raise DecodeError('UNEXPECTED_ARCHIVE_TRAILER')
    return result


def rich_text(data: bytes) -> tuple[str, list[dict], bool]:
    """Only the observed text element: 8 -> repeated 1 -> 2 -> UTF-8 field 1.
    Unknown element types are preserved as incomplete, never guessed from printable bytes.
    """
    fields = wire_fields(data); text_parts = []; unknown = []
    for index, (wire, raw) in enumerate(fields.get(1, [])):
        if wire != 2 or not isinstance(raw, bytes):
            raise DecodeError('INVALID_RICH_ELEMENT')
        element = wire_fields(raw); kind = one(element, 1, 0)
        content = one(element, 2, 2)
        if kind == 0 and isinstance(content, bytes):
            leaf = one(wire_fields(content), 1, 2)
            if not isinstance(leaf, bytes):
                raise DecodeError('MISSING_TEXT_LEAF')
            try:
                text_parts.append(leaf.decode('utf-8', errors='strict'))
            except UnicodeError as exc:
                raise DecodeError('INVALID_TEXT_UTF8') from exc
        else:
            unknown.append({'element_index': index, 'type_code': kind,
                            'sha256': digest(raw), 'body_available': False})
    for number, values in fields.items():
        if number != 1:
            unknown.append({'unmapped_container_field': number, 'occurrences': len(values),
                            'body_available': False})
    return ''.join(text_parts), unknown, bool(text_parts) and not unknown


def normalize(blob: bytes, account_namespace: str, group_label: str,
              expected_field6: str | None = None) -> list[dict]:
    if not account_namespace.strip() or len(account_namespace) > 200:
        raise DecodeError('EXPLICIT_ACCOUNT_NAMESPACE_REQUIRED')
    result = []
    for index, payload in enumerate(archive_records(blob)):
        fields = wire_fields(payload)
        source_fields = {f'field_{n}': str(one(fields, n, 0, 0)) for n in (1, 2, 4, 6)}
        record_mode = one(fields, 3, 0, 0); type_code = one(fields, 7, 0)
        timestamp_raw = one(fields, 12, 0, 0)
        timestamp_valid = isinstance(timestamp_raw, int) and 946684800 <= timestamp_raw < 4102444800
        if expected_field6 is not None and source_fields['field_6'] != str(expected_field6):
            raise DecodeError('CONVERSATION_BINDING_MISMATCH')
        body = one(fields, 8, 2, b''); text = ''; unknown = []; complete = False
        if body and type_code == 2:
            text, unknown, complete = rich_text(body)
        elif body:
            unknown = [{'message_type_code': type_code, 'sha256': digest(body), 'body_available': False}]
        metadata_available = record_mode == 1 and timestamp_valid and all(v != '0' for v in source_fields.values())
        identity = {'profile': PROFILE, 'account_namespace': account_namespace,
                    'source_fields': source_fields}
        record = {
            'platform': 'wecom', 'transport': 'windows_native_selected_copy',
            'profile': PROFILE, 'account_namespace': account_namespace,
            'group_label': group_label, 'group_label_source': 'explicit_ui_observation_not_clipboard_text',
            'source_id_fields': source_fields, 'source_id_semantics': 'provisional_single_sample_binding',
            'native_global_message_id_verified': False,
            'provisional_source_key': digest(canonical(identity).encode()) if metadata_available else None,
            'record_mode_raw': record_mode, 'message_type_code': type_code,
            'timestamp_field12_raw': timestamp_raw,
            'timestamp_utc': datetime.fromtimestamp(timestamp_raw, timezone.utc).isoformat() if timestamp_valid else None,
            'timestamp_mapping': 'field12_seconds_observed_one_real_ui_match',
            'text': text, 'content_complete': complete, 'unparsed_elements': unknown,
            'metadata_fields_available': metadata_available,
            'payload_sha256': digest(payload), 'archive_sha256': digest(blob),
            'archive_record_index': index,
        }
        result.append(record)
    return result

