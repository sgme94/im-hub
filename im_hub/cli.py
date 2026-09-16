"""A bounded JSON CLI; mutation commands are explicit and never implicit in queries."""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from . import __version__
from .adapters import ADAPTERS, spec_for
from .analysis import export_packet, validate_candidates
from .collection import capabilities, collect
from .acquisition import collection_status
from .common import DEFAULT_HOME, IMError, initialize, iso_epoch
from .query import query_messages, source_status
from .store import backend_info, ingest

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise IMError('INVALID_ARGUMENTS_USE_HELP')

def epoch(value):
    return iso_epoch(value) if value is not None else None

def filters(p, query=False):
    p.add_argument('--platform', choices=('wechat', 'kim', 'wecom', 'qq'))
    p.add_argument('--stream')
    p.add_argument('--include-synthetic', action='store_true')
    p.add_argument('--max-age-seconds', type=int)
    if query:
        p.add_argument('keyword', nargs='?', default='')
        p.add_argument('--conversation')
        p.add_argument('--account')
        p.add_argument('--since', help='ISO time with explicit timezone, inclusive')
        p.add_argument('--until', help='ISO time with explicit timezone, exclusive')
        p.add_argument('--limit', type=int, default=50)
        p.add_argument('--cursor')
        p.add_argument('--full', action='store_true')

def query_args(a):
    return {'keyword': a.keyword, 'platform': a.platform, 'stream': a.stream,
            'conversation': a.conversation, 'account': a.account,
            'since': epoch(a.since), 'until': epoch(a.until), 'limit': a.limit,
            'cursor': a.cursor, 'include_synthetic': a.include_synthetic,
            'full': a.full, 'max_age': a.max_age_seconds}

def build_parser():
    p = Parser(prog='im-hub', description='Local IM evidence hub. Query never refreshes clients. JSON stdout; no server or scheduler.')
    p.add_argument('--home', type=Path, default=DEFAULT_HOME)
    p.add_argument('--version', action='version', version=__version__)
    commands = p.add_subparsers(dest='command', required=True, parser_class=Parser)
    commands.add_parser('init', help='Create a dedicated private ChatLab/provenance home')
    commands.add_parser('doctor', help='Check pinned local dependency; never install or log in')
    commands.add_parser('capabilities', help='Report implemented and pending capabilities; no client access')
    c = commands.add_parser('collect', help='Explicit configured file or read-only database acquisition; no client UI automation')
    c.add_argument('--config', required=True, type=Path)
    c.add_argument('--source', required=True)
    c.add_argument('--dry-run', action='store_true')
    c.add_argument('--allow-ui', action='store_true', help='Explicit opt-in for a reviewed desktop profile')
    c.add_argument('--after-sequence', type=int, help='WeCom native clipboard baseline before a normal selected-message copy')
    c.add_argument('--until', help='Database-only exclusive end time, with explicit timezone')
    c.add_argument('--reconcile', action='store_true', help='Database-only full configured-window scan for older late arrivals')
    cs = commands.add_parser('collection-status', help='Read acquisition checkpoints, pending runs and errors without collection')
    cs.add_argument('--source')
    ds = commands.add_parser('desktop-status', help='Read TIM/native clipboard collection states')
    ds.add_argument('--source')
    commands.add_parser('clipboard-status', help='Read only the current clipboard sequence, not contents')
    commands.add_parser('selftest', help='Run disposable synthetic four-platform import/query/backup checks against the actual local backend')
    capture = commands.add_parser('capture', help='Wait for one normal WeCom selected-message copy; no automatic UI or clipboard history')
    capture.add_argument('--config', required=True, type=Path)
    capture.add_argument('--source', required=True)
    capture.add_argument('--wait-seconds', type=int, default=60)
    config = commands.add_parser('config-check', help='Validate profiles without opening a client or data source')
    config.add_argument('--config', required=True, type=Path)
    run = commands.add_parser('run', help='Ordered foreground collection passes; does not install a scheduler')
    run.add_argument('--config', required=True, type=Path)
    run.add_argument('--source', action='append')
    run.add_argument('--cycles', type=int, default=1)
    run.add_argument('--interval', type=int, default=60)
    run.add_argument('--dry-run', action='store_true')
    run.add_argument('--allow-ui', action='store_true')
    run.add_argument('--reconcile', action='store_true')
    soak = commands.add_parser('soak', help='Explicit deadline-limited serial collection segment; no installed service or schedule')
    soak.add_argument('--config', required=True, type=Path)
    soak.add_argument('--until', required=True, help='Absolute authorization end, ISO time with timezone')
    soak.add_argument('--duration-seconds', type=int, default=3300)
    soak.add_argument('--interval', type=int, default=60)
    soak.add_argument('--cycles', type=int)
    soak.add_argument('--allow-ui', action='store_true')
    soak.add_argument('--activate-clients', action='store_true', help='Explicitly switch between uniquely matched already-running IM windows before the deadline')
    commands.add_parser('soak-status', help='Read latest bounded-run report without collecting or activating windows')
    commands.add_parser('verify', help='Verify stored messages, provenance and SQLite integrity without collecting')
    h = commands.add_parser('health', help='Inspect integrity, freshness and collection errors')
    h.add_argument('--config', type=Path)
    h.add_argument('--max-age-seconds', type=int, default=3600)
    b = commands.add_parser('backup', help='Create a new private ZIP snapshot outside the data home')
    b.add_argument('--output', required=True, type=Path)
    r = commands.add_parser('restore', help='Verify and restore a backup only into a new directory')
    r.add_argument('--input', required=True, type=Path)
    r.add_argument('--destination', required=True, type=Path)
    for name in ('export-all', 'report'):
        sub = commands.add_parser(name, help='Export the whole bounded query, not just the first page')
        filters(sub, query=True)
        sub.add_argument('--max-records', type=int, default=10000)
    for name in ('sources', 'status', 'coverage'):
        filters(commands.add_parser(name, help='Read source coverage/freshness; no refresh'))
    for name in ('query', 'export', 'analyze'):
        filters(commands.add_parser(name, help='Read imported messages' if name == 'query' else 'Write a local evidence packet; not a business event'), query=True)
    e = commands.add_parser('evidence', help='Read one known message by opaque evidence ID')
    e.add_argument('id')
    e.add_argument('--include-synthetic', action='store_true')
    i = commands.add_parser('ingest', help='Explicitly normalize an existing export/native payload and import it into ChatLab')
    i.add_argument('--input', required=True, type=Path)
    i.add_argument('--adapter', required=True, choices=ADAPTERS)
    i.add_argument('--platform', required=True, choices=('wechat', 'kim', 'wecom', 'qq'))
    i.add_argument('--account', required=True, help='Reviewed local account namespace, not a password or token')
    i.add_argument('--conversation', required=True)
    i.add_argument('--name', required=True)
    i.add_argument('--source-epoch', required=True, help='Reviewed account/database generation; change after rebuild or account change')
    i.add_argument('--observed-at', required=True, help='Original source observation time, not this import time')
    i.add_argument('--binding', help='Explicit reviewed WeCom field_6 binding; not an official group ID')
    i.add_argument('--source-account', help='QCE chatInfo.selfUin for exact source-account check')
    i.add_argument('--data-class', choices=('real', 'synthetic'), default='real')
    i.add_argument('--since'); i.add_argument('--until')
    i.add_argument('--expected-sha256')
    i.add_argument('--allow-new-snapshot', action='store_true', help='Accept that a new TIM export may overlap; no cross-export dedup claim')
    i.add_argument('--dry-run', action='store_true')
    v = commands.add_parser('validate-candidates', help='Validate evidence links only; never change MyMind business status')
    v.add_argument('--input', required=True, type=Path)
    return p

def main(argv=None):
    # The installed console entry has no implicit `python -X utf8` on Windows.
    # Keep the JSON wire format stable even when redirected under a legacy codepage.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='strict')
    try:
        from .soak import parent_gate
        parent_gate()
        a = build_parser().parse_args(argv)
        home = a.home.resolve()
        if a.command == 'init':
            data = initialize(home)
        elif a.command == 'capabilities':
            data = capabilities()
        elif a.command == 'collect':
            data = collect(home, a.config, a.source, a.dry_run, until=a.until, reconcile=a.reconcile,
                           allow_ui=a.allow_ui, after_sequence=a.after_sequence)
        elif a.command == 'desktop-status':
            from .desktop_sources import desktop_status
            data = desktop_status(home, a.source)
        elif a.command == 'clipboard-status':
            from .windows_desktop import clipboard_status
            data = clipboard_status()
        elif a.command == 'selftest':
            from .selftest import selftest
            data = selftest()
        elif a.command == 'capture':
            from .windows_desktop import wait_and_capture
            data = wait_and_capture(home, a.config, a.source, a.wait_seconds)
        elif a.command == 'config-check':
            from .operations import config_check
            data = config_check(a.config)
        elif a.command == 'run':
            from .operations import run_cycles
            data = run_cycles(home, a.config, a.source, a.cycles, a.interval, a.dry_run, a.allow_ui, a.reconcile)
        elif a.command == 'soak':
            from .soak import run_soak
            data = run_soak(home,a.config,a.until,a.interval,a.duration_seconds,a.allow_ui,a.activate_clients,a.cycles)
        elif a.command == 'soak-status':
            from .soak import soak_status
            data = soak_status(home)
        elif a.command == 'verify':
            from .operations import verify
            data = verify(home)
        elif a.command == 'health':
            from .operations import health
            data = health(home, a.config, a.max_age_seconds)
        elif a.command == 'backup':
            from .operations import backup
            data = backup(home, a.output)
        elif a.command == 'restore':
            from .operations import restore
            data = restore(a.input, a.destination)
        elif a.command in ('export-all', 'report'):
            from .operations import export_all, report
            data = (report if a.command == 'report' else export_all)(home, query_args(a), a.max_records)
        elif a.command == 'collection-status':
            data = collection_status(home, a.source)
        elif a.command == 'doctor':
            data = {'version': __version__, 'backend': backend_info(), 'home_initialized': any((home / name).is_file() for name in ('im-hub.json','im-unified.json')),
                    'llm_required': False, 'frontend': False, 'new_live_collection_implemented': True,
                    'live_database_platforms': ['kim', 'wechat'], 'plaintext_cache_platforms': ['wechat'],
                    'source_client_version_required': False, 'source_version_policy': 'capability_probe_not_version_whitelist',
                    'automatic_ui': True, 'automatic_ui_requires_calibrated_profile_and_explicit_consent': True, 'scheduler_enabled': False,
                    'send_supported': False, 'source_modes': ['KIM native SQLite', 'WeChat authenticated encrypted DB/WAL (existing keys only)', 'WeChat plaintext SQLite cache', 'normalized-v2 file', 'TIM TXT file', 'WeCom native payload/enriched JSON', 'QCE JSON file']}
        elif a.command in ('sources', 'status', 'coverage'):
            data = source_status(home, a.platform, a.stream, a.include_synthetic, a.max_age_seconds)
        elif a.command == 'query':
            data = query_messages(home, **query_args(a))
        elif a.command in ('export', 'analyze'):
            data = export_packet(home, query_args(a), analyze=a.command == 'analyze')
        elif a.command == 'evidence':
            mid = a.id.removeprefix('immsg:')
            if not re.fullmatch(r'[0-9a-f]{64}', mid):
                raise IMError('INVALID_MESSAGE_ID')
            data = query_messages(home, message_key=mid, limit=1, full=True, include_synthetic=a.include_synthetic)
            if not data['count']:
                raise IMError('MESSAGE_NOT_FOUND')
        elif a.command == 'ingest':
            spec = spec_for(a.platform, a.account, a.conversation, a.name, a.adapter,
                            a.source_epoch, a.data_class, a.binding, a.source_account)
            data = ingest(home, a.input, spec, a.observed_at, epoch(a.since), epoch(a.until),
                          a.expected_sha256, a.allow_new_snapshot, a.dry_run)
        elif a.command == 'validate-candidates':
            data = validate_candidates(home, a.input)
        else:
            raise IMError('UNKNOWN_COMMAND')
        print(json.dumps({'ok': True, 'command': a.command, 'data': data}, ensure_ascii=False, allow_nan=False))
        if a.command == 'soak':
            return 0 if data['status']=='segment_complete' and not data['paused_sources'] and data.get('last_cycle_all_succeeded') else 4
        return 4 if a.command == 'run' and not data['success'] else 0
    except IMError as exc:
        print(json.dumps({'ok': False, 'error': {'code': exc.code}, 'source_refreshed': False}))
        return 3 if exc.code in ('WRITER_BUSY', 'DATASET_CHANGED_RETRY_QUERY', 'CURSOR_STALE_RESTART_QUERY') else 2
    except Exception as exc:
        # Exception text can contain source bodies/paths. Only the type reaches stdout.
        print(json.dumps({'ok': False, 'error': {'code': 'LOCAL_OPERATION_FAILED', 'type': type(exc).__name__}}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({'ok': False, 'error': {'code': 'INTERRUPTED_RECONCILE_BATCH_STATUS'}}))
        return 130

if __name__ == '__main__':
    raise SystemExit(main())
