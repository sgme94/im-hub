"""Explicit configured-file collection. No GUI, network, model or shell commands.

The source exporter must finish an immutable file and an observation manifest first.
This module does NOT claim to navigate TIM or WeCom or refresh a client database.
"""
from __future__ import annotations
import re
from pathlib import Path
from .adapters import spec_for
from .common import IMError, iso_epoch, label, load_json, read_blob
from .store import ingest


def capabilities() -> dict:
    return {
        'cli': 'im-hub', 'frontend': False, 'llm_required': False,
        'llm_calls_in_core': 0, 'message_sending': False,
        'configured_file_collection': True, 'automatic_client_collection': False,
        'platforms': {
            'wechat': {'input': ['normalized-v2'], 'upstream': 'reviewed local database reader', 'upstream_ui_required': False},
            'kim': {'input': ['normalized-v2'], 'upstream': 'reviewed read-only SQLite reader', 'upstream_ui_required': False},
            'qq': {'input': ['tim-txt', 'qce-json'], 'upstream': 'TIM official export; QCE real login not accepted', 'tim_export_ui_required': True},
            'wecom': {'input': ['wecom-native', 'wecom-json'], 'upstream': 'normal selected-message clipboard copy', 'upstream_ui_required': True},
        },
        'desktop_driver': {'implemented': False, 'planned': 'bounded deterministic state machine', 'llm_required': False},
        'analysis': {'current': 'deterministic keyword triage', 'semantic_llm': 'optional external consumer, not implemented'},
    }


def _path(base: Path, value: object) -> Path:
    value = label(value, 'LOCAL_SOURCE_PATH_REQUIRED')
    if '://' in value:
        raise IMError('REMOTE_SOURCE_URL_NOT_SUPPORTED')
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else base / candidate).resolve()


def collect(home: Path, config: Path, source_name: str, dry_run=False, backend=None) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', source_name):
        raise IMError('INVALID_SOURCE_NAME')
    config = config.resolve()
    document = load_json(read_blob(config))
    if not isinstance(document, dict) or document.get('version') != 1:
        raise IMError('SOURCE_CONFIG_VERSION_UNSUPPORTED')
    sources = document.get('sources')
    if not isinstance(sources, dict) or len(sources) > 64 or source_name not in sources:
        raise IMError('CONFIGURED_SOURCE_NOT_FOUND')
    profile = sources[source_name]
    if not isinstance(profile, dict) or profile.get('enabled') is not True:
        raise IMError('SOURCE_NOT_ENABLED')
    if profile.get('transport') != 'file':
        raise IMError('CLIENT_DRIVER_NOT_IMPLEMENTED')
    required = ('platform', 'adapter', 'account_namespace', 'conversation_id',
                'conversation_name', 'source_epoch', 'data_class', 'input', 'manifest')
    if any(key not in profile for key in required):
        raise IMError('SOURCE_PROFILE_INCOMPLETE')
    input_path = _path(config.parent, profile['input'])
    manifest_path = _path(config.parent, profile['manifest'])
    manifest = load_json(read_blob(manifest_path))
    if not isinstance(manifest, dict) or manifest.get('source_id') != source_name:
        raise IMError('SOURCE_MANIFEST_BINDING_MISMATCH')
    source_hash = manifest.get('sha256')
    if not isinstance(source_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', source_hash):
        raise IMError('MANIFEST_SOURCE_HASH_REQUIRED')
    observed_at = manifest.get('observed_at')
    iso_epoch(observed_at)  # Never replace source observation with the import clock.
    spec = spec_for(profile['platform'], profile['account_namespace'], profile['conversation_id'],
                    profile['conversation_name'], profile['adapter'], profile['source_epoch'],
                    profile['data_class'], profile.get('binding'), profile.get('source_account'))
    extra = {'backend': backend} if backend is not None else {}
    result = ingest(home, input_path, spec, observed_at,
                    since=iso_epoch(manifest['since']) if 'since' in manifest else None,
                    until=iso_epoch(manifest['until']) if 'until' in manifest else None,
                    expected_sha256=source_hash, dry_run=dry_run, **extra)
    return {**result, 'configured_source': source_name, 'transport': 'file',
            'client_access_performed': False, 'client_refreshed': False, 'llm_calls': 0}
