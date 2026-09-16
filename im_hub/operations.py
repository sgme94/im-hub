"""Product operations: explicit batch runs, complete exports, integrity and backups.
No timers are installed, no source client is launched, no cloud upload is performed.
"""
from __future__ import annotations
import json
import os
import shutil
import sqlite3
import stat
import time
import uuid
import zipfile
from contextlib import ExitStack, nullcontext
from pathlib import Path, PurePosixPath
from .common import IMError, canonical, check_home, digest, initialize, load_json, now, read_blob, readonly, write_new, writer
from .query import query_messages, source_status

MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_ARCHIVE_FILES = 50000
BACKUP_DIRS = {'batches', 'acquisitions', 'desktop', 'chatlab'}
ROOT_FILES = {'im-hub.json', 'index.sqlite3', 'collection.sqlite3', 'desktop.sqlite3'}


def config_check(config: Path) -> dict:
    data = load_json(read_blob(config))
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('sources'), dict) or not 1 <= len(data['sources']) <= 64:
        raise IMError('SOURCE_CONFIG_VERSION_UNSUPPORTED')
    from .database_readers import prepare_profile
    from .desktop_sources import prepare_desktop_profile
    import re
    items = []
    for name, p in data['sources'].items():
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', name) or not isinstance(p, dict):
            raise IMError('INVALID_SOURCE_NAME_OR_PROFILE')
        if not isinstance(p.get('enabled'), bool):
            raise IMError('SOURCE_ENABLED_BOOLEAN_REQUIRED')
        kind = p.get('transport')
        if kind in ('kim-sqlite', 'wechat-sqlite', 'wechat-live'):
            prepare_profile(config.resolve().parent, p)
        elif kind in ('tim-export', 'tim-ui', 'wecom-clipboard', 'wecom-ui'):
            prepare_desktop_profile(config.resolve().parent, p)
        elif kind == 'file':
            from .adapters import spec_for
            spec_for(p.get('platform'), p.get('account_namespace'), p.get('conversation_id'), p.get('conversation_name'),
                     p.get('adapter'), p.get('source_epoch'), p.get('data_class'), p.get('binding'), p.get('source_account'))
            if not all(isinstance(p.get(k), str) and p[k] for k in ('input', 'manifest')):
                raise IMError('SOURCE_PROFILE_INCOMPLETE')
        else:
            raise IMError('UNSUPPORTED_SOURCE_TRANSPORT')
        items.append({'source': name, 'enabled': p.get('enabled') is True, 'platform': p.get('platform'), 'transport': kind,
                      'ui_required': kind in ('tim-ui', 'wecom-ui'), 'manual_copy_required': kind == 'wecom-clipboard'})
    return {'valid': True, 'sources': items, 'source_access_performed': False, 'configuration_sha256': digest(read_blob(config))}


def run_sources(home: Path, config: Path, sources=None, dry_run=False, allow_ui=False, reconcile=False, collect_fn=None) -> dict:
    from .collection import collect
    fn = collect_fn or collect
    summary = config_check(config)
    selected = set(sources or [x['source'] for x in summary['sources'] if x['enabled']])
    known = {x['source'] for x in summary['sources']}
    if not selected or not selected <= known:
        raise IMError('REQUESTED_SOURCES_NOT_CONFIGURED')
    results = []
    # One ordered pass; not a scheduler. The outer lock prevents overlapping runs
    # through this entry. Each collector retains its own transactional write lease.
    with (nullcontext() if dry_run else writer(home, lock_name='.run.lock')):
        for item in summary['sources']:
            name = item['source']
            if name not in selected:
                continue
            if not item['enabled']:
                results.append({'source': name, 'status': 'disabled'}); continue
            if item['manual_copy_required']:
                results.append({'source': name, 'status': 'needs_input', 'error': 'EXPLICIT_COPY_BASELINE_REQUIRED'}); continue
            if item['ui_required'] and not allow_ui:
                results.append({'source': name, 'status': 'needs_input', 'error': 'UI_CONSENT_REQUIRED'}); continue
            try:
                args = {'dry_run': dry_run, 'allow_ui': allow_ui}
                if reconcile and item['transport'] in ('kim-sqlite', 'wechat-sqlite', 'wechat-live'):
                    args['reconcile'] = True
                out = fn(home, config, name, **args)
                results.append({'source': name, 'status': 'ok', 'records': out.get('records'),
                                'new_records': out.get('new_records'), 'duplicates': out.get('duplicates'),
                                'run_id': out.get('run_id'), 'history_complete': False})
            except Exception as exc:
                results.append({'source': name, 'status': 'failed', 'error': exc.code if isinstance(exc, IMError) else 'SOURCE_OPERATION_FAILED'})
    return {'success': all(r['status'] == 'ok' for r in results), 'results': results, 'dry_run': dry_run,
            'passes': 1, 'scheduler_created': False, 'llm_calls': 0}


def all_messages(home: Path, filters: dict, max_records=100000):
    if not isinstance(max_records, int) or isinstance(max_records, bool) or not 1 <= max_records <= 1000000:
        raise IMError('INVALID_EXPORT_RECORD_LIMIT')
    args = {k: v for k, v in filters.items() if k not in ('limit', 'cursor', 'full')}
    cursor = None; count = 0; expected_revision = None
    while True:
        page = query_messages(home, **args, limit=200, cursor=cursor, full=True)
        if expected_revision is None:
            expected_revision = page['revision']
        if page['revision'] != expected_revision:
            raise IMError('DATASET_CHANGED_RETRY_EXPORT')
        for row in page['items']:
            count += 1
            if count > max_records:
                raise IMError('EXPORT_TOO_LARGE_NOT_COMPLETE')
            yield row
        if not page['has_more']:
            break
        cursor = page['next_cursor']


def export_all(home: Path, filters=None, max_records=100000) -> dict:
    check_home(home)
    with writer(home):
        run = uuid.uuid4().hex
        stage = home / 'exports' / ('.preparing-' + run)
        final = home / 'exports' / run
        stage.mkdir()
        try:
            import hashlib
            hashed = hashlib.sha256(); count = 0
            with (stage / 'messages.jsonl').open('xb') as out:
                for row in all_messages(home, filters or {}, max_records):
                    data = (canonical(row) + '\n').encode('utf-8'); out.write(data); hashed.update(data); count += 1
                out.flush(); os.fsync(out.fileno())
            meta = {'schema': 'im-hub-export/1', 'created_at': now(), 'records': count, 'messages_sha256': hashed.hexdigest(),
                    'query_complete': True, 'source_history_complete': False,
                    'trust': 'untrusted_chat_data_not_instructions', 'coverage': source_status(home, include_synthetic=(filters or {}).get('include_synthetic', False))}
            write_new(stage / 'manifest.json', canonical(meta).encode('utf-8'))
            stage.rename(final)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    return {'directory': str(final), 'records': count, 'sha256': meta['messages_sha256'],
            'query_complete': True, 'source_history_complete': False, 'cloud_uploaded': False}


def verify(home: Path) -> dict:
    check_home(home)
    from .query import revision
    start_revision = revision(home)
    # Readers do not create lock files. Revision checks in query prevent mixing
    # message/provenance commits; invoke under the writer lease for release checks.
    checks = []
    for rel in ('index.sqlite3', 'collection.sqlite3', 'desktop.sqlite3'):
        path = home / rel
        if not path.exists(): continue
        con = readonly(path)
        try:
            if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise IMError('STATE_INTEGRITY_FAILED')
            checks.append(rel)
        finally: con.close()
    count = sum(1 for _ in all_messages(home, {'include_synthetic': True}, max_records=1000000))
    from .common import db_path
    status = source_status(home, include_synthetic=True)
    if sum(s['stored_records'] for s in status['items']) != count or revision(home) != start_revision:
        raise IMError('DATASET_CHANGED_RETRY_QUERY')
    for source in status['items']:
        path = db_path(home, source['stream_id'])
        if source['stored_records'] == 0 and not path.exists(): continue
        con = readonly(path)
        try:
            if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok': raise IMError('CHATLAB_INTEGRITY_FAILED')
        finally: con.close()
    if revision(home) != start_revision:
        raise IMError('DATASET_CHANGED_RETRY_QUERY')
    return {'valid': True, 'records_verified': count, 'streams': len(status['items']), 'databases_checked': len(checks) + len(status['items']),
            'pending_batches': sum(s['failed_or_pending_batches'] for s in status['items']), 'query_only': True,
            'source_history_complete': False}


def _allowed(name: str) -> bool:
    p = PurePosixPath(name)
    if not name or '\\' in name or ':' in name or p.is_absolute() or '..' in p.parts or str(p) != name:
        return False
    # Windows aliases and device names can escape a lexical ZIP whitelist.
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CLOCK$', 'CONIN$', 'CONOUT$'} | {f'{prefix}{i}' for prefix in ('COM', 'LPT') for i in range(1, 10)}
    if any(part.endswith((' ', '.')) or part.split('.')[0].upper() in reserved or
           any(ord(c) < 32 or c in '<>"|?*' for c in part) for part in p.parts):
        return False
    if name in ROOT_FILES: return True
    if p.parts[0] in ('batches', 'acquisitions', 'desktop'):
        return len(p.parts) >= 2 and p.suffix in ('.json', '.jsonl', '.bin')
    if p.parts[:3] == ('chatlab', 'data', 'databases'):
        return len(p.parts) == 4 and p.suffix == '.db'
    return False


def _backup_db(source: Path, destination: Path):
    src = readonly(source); dst = sqlite3.connect(destination); deadline = time.monotonic() + 30
    def progress(status, remaining, total):
        if time.monotonic() > deadline: raise IMError('BACKUP_TIMEOUT')
    try:
        src.backup(dst, pages=128, progress=progress, sleep=0.05)
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok': raise IMError('BACKUP_DB_INVALID')
    finally: dst.close(); src.close()


def backup(home: Path, output: Path) -> dict:
    check_home(home); output = output.resolve()
    if output.exists() or output.suffix.lower() != '.zip' or output.is_relative_to(home.resolve()):
        raise IMError('BACKUP_REQUIRES_NEW_ZIP_OUTSIDE_HOME')
    if not output.parent.is_dir(): raise IMError('BACKUP_PARENT_MISSING')
    stage = home / ('backup-stage-' + uuid.uuid4().hex)
    temp = output.with_name('.' + output.name + '.' + uuid.uuid4().hex + '.tmp')
    entries = []; total = 0
    try:
        with ExitStack() as locks:
            # Order matches orchestrator -> source collector -> importer.
            for key in ('.run.lock', '.desktop.lock', '.collection.lock', '.writer.lock'):
                locks.enter_context(writer(home, lock_name=key))
            verified = verify(home)
            stage.mkdir(mode=0o700)
            candidates = [home / x for x in ROOT_FILES if (home / x).is_file()]
            for directory in BACKUP_DIRS:
                base = home / directory
                if not base.exists(): continue
                for p in base.rglob('*'):
                    if p.is_symlink(): raise IMError('BACKUP_SYMLINK_REJECTED')
                    if p.is_file() and _allowed(p.relative_to(home).as_posix()): candidates.append(p)
            if len(candidates) > MAX_ARCHIVE_FILES: raise IMError('BACKUP_FILE_LIMIT')
            with zipfile.ZipFile(temp, 'x', compression=zipfile.ZIP_DEFLATED) as z:
                for source in sorted(candidates):
                    if source.is_symlink(): raise IMError('BACKUP_SYMLINK_REJECTED')
                    rel = source.relative_to(home).as_posix()
                    if not _allowed(rel): raise IMError('BACKUP_PATH_NOT_ALLOWED')
                    target = source
                    if source.suffix in ('.sqlite3', '.db'):
                        target = stage / uuid.uuid4().hex; _backup_db(source, target)
                    size = target.stat().st_size; total += size
                    if total > MAX_ARCHIVE_BYTES: raise IMError('BACKUP_SIZE_LIMIT')
                    h = __import__('hashlib').sha256()
                    with target.open('rb') as stream, z.open(rel, 'w') as out:
                        while chunk := stream.read(1024 * 1024): h.update(chunk); out.write(chunk)
                    entries.append({'path': rel, 'bytes': size, 'sha256': h.hexdigest()})
                manifest = {'schema': 'im-hub-backup/1', 'created_at': now(), 'entries': entries,
                            'records_verified': verified['records_verified'], 'encrypted': False,
                            'excluded': ['machine_config', 'credentials', 'runtime_logs', 'exports', 'candidates']}
                z.writestr('BACKUP.json', canonical(manifest).encode('utf-8'))
            # Same-user source protections do not automatically protect an external directory.
            if os.name == 'nt':
                import subprocess
                principal = os.environ.get('USERDOMAIN', '') + '\\' + os.environ.get('USERNAME', '')
                p = subprocess.run(['icacls.exe', str(temp), '/inheritance:r', '/grant:r', principal + ':F', '*S-1-5-18:F'], capture_output=True, timeout=20)
                if p.returncode: raise IMError('BACKUP_ACL_FAILED')
            else: temp.chmod(0o600)
            # Hard-link creation is atomic and refuses an existing destination.
            os.link(temp, output); temp.unlink()
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if temp.exists(): temp.unlink()
    return {'file': str(output), 'files': len(entries), 'bytes_uncompressed': total,
            'records_verified': manifest['records_verified'], 'encrypted': False, 'cloud_uploaded': False}


def restore(archive: Path, destination: Path) -> dict:
    destination = destination.resolve()
    if destination.exists() or not destination.parent.is_dir(): raise IMError('RESTORE_REQUIRES_NEW_DIRECTORY')
    stage = destination.with_name('.restore-' + uuid.uuid4().hex)
    try:
        with zipfile.ZipFile(archive, 'r') as z:
            infos = z.infolist(); names = [x.filename for x in infos]
            if len(infos) > MAX_ARCHIVE_FILES + 1 or len({n.casefold() for n in names}) != len(names) or 'BACKUP.json' not in names:
                raise IMError('INVALID_BACKUP_INDEX')
            if sum(x.file_size for x in infos) > MAX_ARCHIVE_BYTES: raise IMError('RESTORE_SIZE_LIMIT')
            info = z.getinfo('BACKUP.json')
            if info.file_size > 20 * 1024**2: raise IMError('BACKUP_MANIFEST_TOO_LARGE')
            manifest = load_json(z.read('BACKUP.json'))
            if not isinstance(manifest, dict) or manifest.get('schema') != 'im-hub-backup/1' or not isinstance(manifest.get('entries'), list):
                raise IMError('INVALID_BACKUP_MANIFEST')
            import re
            if any(not isinstance(x, dict) or not isinstance(x.get('path'), str) or
                   isinstance(x.get('bytes'), bool) or not isinstance(x.get('bytes'), int) or x['bytes'] < 0 or
                   not re.fullmatch(r'[0-9a-f]{64}', str(x.get('sha256', ''))) for x in manifest['entries']):
                raise IMError('INVALID_BACKUP_MANIFEST_ENTRY')
            expected = {x['path']: x for x in manifest['entries']}
            if len(expected) != len(manifest['entries']) or set(expected) != set(names) - {'BACKUP.json'} or not {'im-hub.json','index.sqlite3'} <= set(expected):
                raise IMError('BACKUP_INDEX_MISMATCH')
            for entry in infos:
                if entry.filename == 'BACKUP.json': continue
                mode = entry.external_attr >> 16
                if not _allowed(entry.filename) or stat.S_ISLNK(mode) or entry.is_dir() or entry.flag_bits & 1:
                    raise IMError('UNSAFE_BACKUP_MEMBER')
                if expected[entry.filename]['bytes'] != entry.file_size: raise IMError('BACKUP_SIZE_MISMATCH')
            initialize(stage)
            # Replace only newly created empty staging metadata, never user data.
            (stage / 'im-hub.json').unlink(); (stage / 'index.sqlite3').unlink()
            for name, entry in expected.items():
                target = stage.joinpath(*PurePosixPath(name).parts); target.parent.mkdir(parents=True, exist_ok=True)
                h = __import__('hashlib').sha256(); seen = 0
                with z.open(name) as inp, target.open('xb') as out:
                    while chunk := inp.read(1024 * 1024):
                        seen += len(chunk)
                        if seen > entry['bytes']: raise IMError('BACKUP_SIZE_MISMATCH')
                        h.update(chunk); out.write(chunk)
                if seen != entry['bytes'] or h.hexdigest() != entry['sha256']: raise IMError('BACKUP_HASH_MISMATCH')
        valid = verify(stage)
        if valid['records_verified'] != manifest['records_verified']: raise IMError('RESTORE_RECORD_COUNT_MISMATCH')
        if destination.exists(): raise IMError('RESTORE_DESTINATION_APPEARED')
        stage.rename(destination)
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError):
        raise IMError('INVALID_BACKUP_ARCHIVE') from None
    finally:
        if stage.exists(): shutil.rmtree(stage, ignore_errors=True)
    return {'directory': str(destination), 'records_verified': valid['records_verified'], 'valid': True,
            'source_configuration_restored': False, 'new_directory_only': True}


def health(home: Path, config=None, max_age=3600):
    from .acquisition import collection_status
    from .desktop_sources import desktop_status
    verified = verify(home)
    coverage = source_status(home, max_age=max_age)
    acquisition, desktop = collection_status(home), desktop_status(home)
    warnings = []
    if verified['pending_batches']:
        warnings.append('IMPORT_PENDING_OR_FAILED')
    for item in coverage['items']:
        if item['freshness'] != 'fresh':
            warnings.append('SOURCE_' + item['freshness'].upper())
    for item in acquisition['items'] + desktop['items']:
        if item.get('last_error') or item.get('pending_runs') or any(item.get('runs_by_status', {}).get(s) for s in ('staged', 'import_failed')):
            warnings.append('COLLECTION_ATTENTION')
    checked_config = config_check(config) if config is not None else None
    return {'status': 'degraded' if warnings else 'ok', 'warnings': sorted(set(warnings)),
            'integrity': verified, 'coverage': coverage, 'acquisition': acquisition,
            'desktop': desktop, 'config': checked_config, 'source_refreshed': False}


def run_cycles(home, config, sources=None, cycles=1, interval=60, dry_run=False, allow_ui=False, reconcile=False):
    if isinstance(cycles, bool) or not isinstance(cycles, int) or not 1 <= cycles <= 60:
        raise IMError('CYCLES_MUST_BE_1_TO_60')
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 3600:
        raise IMError('INTERVAL_MUST_BE_1_TO_3600')
    if dry_run and cycles != 1:
        raise IMError('DRY_RUN_REQUIRES_ONE_CYCLE')
    summaries = []
    for i in range(cycles):
        summaries.append(run_sources(home, config, sources, dry_run, allow_ui, reconcile))
        if i + 1 < cycles:
            time.sleep(interval)
    return {'success': all(s['success'] for s in summaries), 'cycles_completed': len(summaries),
            'runs': summaries, 'foreground_execution': True, 'scheduler_created': False, 'llm_calls': 0}


def report(home: Path, filters=None, max_records=10000):
    result = export_all(home, filters, max_records)
    folder = Path(result['directory'])
    # Messages stay quoted and HTML-escaped. No source link, script or instruction
    # is opened/executed; Markdown is a local evidence report, not a state update.
    import html
    lines = ['# IM 消息证据报告', '', '这是确定性消息汇编，不是模型语义结论或项目完成证明。', '',
             f"记录数：{result['records']}。当前查询已完整导出；来源历史仍可能不完整。", '', '## 来源覆盖', '']
    meta = load_json(read_blob(folder / 'manifest.json'))
    for source in meta['coverage']['items']:
        name = html.escape(source['conversation_name']).replace('\n', ' ')
        lines.append(f"- {source['platform']} / {name}：{source['stored_records']} 条，时效 {source['freshness']}。")
    lines += ['', '## 消息时间线', '']
    with (folder / 'messages.jsonl').open('r', encoding='utf-8') as stream:
        for raw in stream:
            row = json.loads(raw)
            sender = html.escape(row['sender_name']).replace('\n', ' ')
            lines += [f"### {row['event_time']} · {row['platform']} · {sender}", '',
                      f"证据：`{row['evidence_ref']}`；正文状态：`{row['body_state']}`。", '']
            lines += ['> ' + html.escape(line) for line in (row.get('text') or '[正文不可用]').splitlines()]
            lines.append('')
    data = '\n'.join(lines).encode('utf-8')
    if len(data) > 100 * 1024**2:
        raise IMError('REPORT_TOO_LARGE_USE_JSONL_EXPORT')
    write_new(folder / 'report.md', data)
    return {**result, 'report': str(folder / 'report.md'), 'llm_calls': 0,
            'semantic_claims_verified': False, 'business_state_committed': False}
