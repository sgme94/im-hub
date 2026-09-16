"""Product contract tests use synthetic data; no actual IM client or LLM calls."""
from __future__ import annotations
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from im_hub.common import IMError, canonical, digest, initialize, readonly, writer
from im_hub.adapters import normalize, spec_for
from im_hub.collection import collect
from im_hub.desktop_sources import collect_desktop, desktop_status, prepare_desktop_profile, reconcile_tim
from im_hub.operations import _allowed, backup, config_check, export_all, health, report, restore, run_cycles, run_sources, verify
from im_hub.cli import main
from im_hub.windows_desktop import validate_profile, selector
from test_core import fake_backend, normalized, spec, OBSERVED
from im_hub.store import ingest


def vi(number):
    out = bytearray()
    while number > 127:
        out.append((number & 127) | 128); number >>= 7
    out.append(number); return bytes(out)


def f(n, value):
    return vi(n << 3) + vi(value) if isinstance(value, int) else vi(n << 3 | 2) + vi(len(value)) + value


def native_message(identity=1, binding=66, body='[合成] 请确认', timestamp=1704153600):
    element = f(1, 0) + f(2, f(1, body.encode('utf-8')))
    payload = b''.join(f(n, v) for n, v in ((1, identity), (2, 2), (3, 1), (4, 4), (6, binding), (7, 2), (8, f(1, element)), (12, timestamp)))
    return b'22 serialization::archive 19 0 0 1 0 ' + str(len(payload)).encode() + b' ' + payload


def tim_export(messages=('收到',), prefix=''):
    rows = ['消息对象:测试群', prefix]
    for text in messages:
        rows += ['2024-01-02 上午 08:00:00 Fixture(1)', text]
    return '\n'.join(rows).encode('utf-8-sig')


def S(name, type='ButtonControl'):
    return {'name': name, 'control_type': type}


def ui_profile():
    return {'profile_reviewed': True, 'client_version': '3.5.1.22171',
            'window': S('测试群', 'WindowControl'), 'conversation_header': S('测试群', 'TextControl'),
            'navigation': [], 'save_dialog': S('导出消息记录', 'WindowControl'),
            'filename_field': S('文件名', 'EditControl'), 'filetype_field': S('类型', 'ComboBoxControl'),
            'save_button': S('保存'), 'expected_filename': '测试群.txt', 'expected_filetype': '文本文件 (*.txt)'}


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='im-hub-product-synthetic-')
        self.root = Path(self.tmp.name); self.home = self.root / 'home'; initialize(self.home)
        self.source = self.root / 'tim.txt'; self.meta = self.root / 'manifest.json'
        self.p = {'enabled': True, 'platform': 'qq', 'transport': 'tim-export', 'adapter': 'tim-sequence-json',
                  'account_namespace': 'fixture', 'conversation_id': 'fixture-group', 'conversation_name': '测试群',
                  'source_epoch': 'fixture-epoch', 'data_class': 'synthetic', 'input': str(self.source),
                  'manifest': str(self.meta), 'export_mode': 'full-history-append-only'}
        self.config = self.root / 'sources.json'
        self.set_export(('收到',))
        self.save_config()

    def tearDown(self): self.tmp.cleanup()

    def set_export(self, texts, prefix=''):
        blob = tim_export(texts, prefix); self.source.write_bytes(blob)
        self.meta.write_text(canonical({'source_id': 'tim', 'observed_at': '2024-01-02T12:00:00+08:00', 'sha256': digest(blob)}), 'utf-8')

    def save_config(self, extra=None):
        self.config.write_text(canonical({'version': 1, 'sources': {'tim': self.p, **(extra or {})}}), 'utf-8')

    def collect(self, **kw):
        return collect(self.home, self.config, 'tim', backend=fake_backend, **kw)

    def existing(self, count=1):
        path = self.root / 'messages.jsonl'
        path.write_text(''.join(canonical(normalized(str(i + 1))) + '\n' for i in range(count)), 'utf-8')
        return ingest(self.home, path, spec(), OBSERVED, backend=fake_backend)

    def hashes(self):
        return {p.relative_to(self.home).as_posix(): digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}

    def test_tim_initial_replay(self):
        a = self.collect(); b = self.collect()
        self.assertEqual((a['new_records'], b['new_records'], b['duplicates']), (1, 0, 1))

    def test_tim_header_changes_do_not_duplicate(self):
        self.collect(); self.set_export(('收到',), '导出备注:新的导出')
        self.assertEqual(self.collect()['new_records'], 0)

    def test_tim_exact_prefix_appends_same_second_same_text(self):
        self.collect(); self.set_export(('收到', '收到'))
        result = self.collect()
        self.assertEqual((result['new_records'], result['duplicates']), (1, 1))
        self.assertEqual(verify(self.home)['records_verified'], 2)

    def test_tim_edited_history_rejected(self):
        self.collect(); self.set_export(('改过了',))
        with self.assertRaisesRegex(IMError, 'NOT_EXACT_APPEND'): self.collect()
        self.assertEqual(verify(self.home)['records_verified'], 1)
        self.assertTrue(desktop_status(self.home)['items'][0]['last_error'])

    def test_tim_shortened_history_rejected(self):
        self.set_export(('a', 'b')); self.collect(); self.set_export(('b',))
        with self.assertRaisesRegex(IMError, 'NOT_EXACT_APPEND'): self.collect()

    def test_tim_changed_group_stops(self):
        self.source.write_bytes(tim_export().replace('测试群'.encode(), '别的群'.encode()))
        self.meta.write_text(canonical({'source_id': 'tim', 'observed_at': '2024-01-02T12:00:00+08:00', 'sha256': digest(self.source.read_bytes())}), 'utf-8')
        with self.assertRaisesRegex(IMError, 'GROUP_MISMATCH'): self.collect()
        self.assertFalse(list((self.home / 'desktop').rglob('*.bin')))

    def test_tim_manifest_wrong_source_rejected(self):
        self.meta.write_text(canonical({'source_id': 'wrong'}), 'utf-8')
        with self.assertRaisesRegex(IMError, 'MANIFEST_BINDING'): self.collect()

    def test_tim_hash_mismatch_rejected(self):
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(IMError, 'SOURCE_HASH_MISMATCH'): self.collect()

    def test_tim_requires_explicit_full_export_contract(self):
        del self.p['export_mode']; self.save_config()
        with self.assertRaisesRegex(IMError, 'FULL_EXPORT_CONTRACT'): self.collect()

    def test_tim_profile_mutation_rejected(self):
        self.collect(); self.p['source_epoch'] = 'different'; self.save_config()
        with self.assertRaisesRegex(IMError, 'PROFILE_CHANGED'): self.collect()

    def test_tim_since_filter_preserves_sequence(self):
        self.p['since'] = '2024-01-02T09:00:00+08:00'; self.save_config()
        self.assertEqual(self.collect()['records'], 0)

    def test_desktop_pending_resumes_before_reacquisition(self):
        def fail(home, args):
            result = fake_backend(home, args)
            if args[0] == 'import': raise IMError('INJECTED_FAILURE_AFTER_IMPORT')
            return result
        with self.assertRaises(IMError): collect(self.home, self.config, 'tim', backend=fail)
        self.set_export(('收到', '新增'))
        a = self.collect()
        self.assertTrue(a['resumed_pending']); self.assertEqual(a['records'], 1)
        b = self.collect(); self.assertEqual(b['new_records'], 1)

    def test_desktop_pending_tampered_stops(self):
        with self.assertRaises(IMError): collect(self.home, self.config, 'tim', backend=lambda *x: (_ for _ in ()).throw(IMError('FAIL')))
        source = next((self.home / 'desktop').glob('*/source.json')); source.write_text('{}', 'utf-8')
        with self.assertRaisesRegex(IMError, 'SOURCE_HASH_MISMATCH'): self.collect()

    def test_pending_v030_import_recovers_after_upgrade(self):
        p = self.root / 'legacy-pending.jsonl'
        p.write_text(canonical(normalized()) + '\n', 'utf-8')
        with patch('im_hub.store.__version__', '0.3.0'):
            with self.assertRaises(IMError):
                ingest(self.home, p, spec(), OBSERVED, backend=lambda *x: (_ for _ in ()).throw(IMError('FAIL')))
        result = ingest(self.home, p, spec(), OBSERVED, backend=fake_backend)
        self.assertEqual(result['new_records'], 1)
        self.assertEqual(verify(self.home)['records_verified'], 1)

    def test_capture_wait_is_bounded_without_client_access(self):
        from im_hub.windows_desktop import wait_and_capture
        for limit in (0, 121, True):
            with self.assertRaisesRegex(IMError, 'CAPTURE_WAIT'):
                wait_and_capture(self.home, self.config, 'tim', limit)

    def test_selftest_cli_dispatch(self):
        result = {'passed': True, 'synthetic_only': True, 'llm_calls': 0}
        output = io.StringIO()
        with patch('im_hub.selftest.selftest', return_value=result), redirect_stdout(output):
            code = main(['selftest'])
        self.assertEqual(code, 0); self.assertTrue(json.loads(output.getvalue())['data']['passed'])

    def test_desktop_dry_run_no_writes(self):
        before = self.hashes(); self.collect(dry_run=True); self.assertEqual(before, self.hashes())

    def test_native_clipboard_integration_with_mock_owner(self):
        p = {**self.p, 'platform': 'wecom', 'transport': 'wecom-clipboard', 'adapter': 'wecom-native', 'binding': '66'}
        class Fake:
            def capture(self, baseline):
                if baseline != 1: raise IMError('INVALID_BASELINE_SEQUENCE')
                return native_message(), {'captured_at': '2024-01-02T12:00:00+08:00', 'clipboard_owner_verified': True}
        a = collect_desktop(self.home, self.root, 'wecom', p, backend=fake_backend, after_sequence=1, driver=Fake())
        b = collect_desktop(self.home, self.root, 'wecom', p, backend=fake_backend, after_sequence=1, driver=Fake())
        self.assertEqual((a['new_records'], b['new_records']), (1, 0))
        self.assertFalse(a['history_complete'])

    def test_native_wrong_binding_never_persisted(self):
        p = {**self.p, 'platform': 'wecom', 'transport': 'wecom-clipboard', 'adapter': 'wecom-native', 'binding': '77'}
        class Fake:
            def capture(self, baseline): return native_message(), {'captured_at': OBSERVED}
        with self.assertRaises(ValueError): collect_desktop(self.home, self.root, 'wecom', p, backend=fake_backend, after_sequence=1, driver=Fake())
        self.assertFalse(list((self.home / 'desktop').rglob('*.bin')))

    def test_native_explicit_baseline_required(self):
        p = {**self.p, 'platform': 'wecom', 'transport': 'wecom-clipboard', 'adapter': 'wecom-native', 'binding': '66'}
        with self.assertRaisesRegex(IMError, 'BASELINE_REQUIRED'):
            collect_desktop(self.home, self.root, 'wecom', p, backend=fake_backend)

    def test_ui_consent_before_actions(self):
        self.p.update({'transport': 'tim-ui', 'desktop': ui_profile()}); self.save_config()
        with self.assertRaisesRegex(IMError, 'UI_CONSENT_REQUIRED'): self.collect()

    def test_ui_declared_version_is_not_auto_adopted(self):
        ui = ui_profile(); ui['client_version'] = '999'
        with self.assertRaisesRegex(IMError, 'VERSION_NOT_REVIEWED'): validate_profile(ui, 'qq', '测试群')

    def test_ui_no_arbitrary_actions_or_send(self):
        for bad in ['send', 'shell', 'type', 'delete']:
            ui = ui_profile(); ui['navigation'] = [{'action': bad, 'selector': S('发送')}]
            with self.assertRaises(IMError): validate_profile(ui, 'qq', '测试群')

    def test_ui_selector_cannot_match_anything(self):
        with self.assertRaises(IMError): selector({'control_type': 'ButtonControl'})
        with self.assertRaises(IMError): selector({'control_type': 'ButtonControl', 'x': 1})

    def test_ui_header_exact_binding(self):
        ui = ui_profile(); ui['conversation_header']['name'] = 'other'
        with self.assertRaises(IMError): validate_profile(ui, 'qq', '测试群')

    def test_config_static_check_does_not_read_source(self):
        self.source.unlink(); result = config_check(self.config)
        self.assertTrue(result['valid']); self.assertFalse(result['source_access_performed'])

    def test_run_partial_failure_does_not_block_other_source(self):
        p = {**self.p}; self.save_config({'second': p})
        called = []
        def fn(home, cfg, name, **kw):
            called.append(name)
            if name == 'tim': raise IMError('SOURCE_UNAVAILABLE')
            return {'records': 2, 'new_records': 2}
        result = run_sources(self.home, self.config, collect_fn=fn)
        self.assertFalse(result['success']); self.assertEqual(called, ['second', 'tim'])
        self.assertEqual(sum(x['status'] == 'ok' for x in result['results']), 1)

    def test_run_manual_clipboard_is_not_read_implicitly(self):
        p = {**self.p, 'platform': 'wecom', 'transport': 'wecom-clipboard', 'adapter': 'wecom-native', 'binding': '66'}
        self.save_config({'wecom': p})
        result = run_sources(self.home, self.config, sources=['wecom'])
        self.assertEqual(result['results'][0]['status'], 'needs_input')

    def test_run_dry_run_no_lock_writes(self):
        before = self.hashes()
        result = run_sources(self.home, self.config, dry_run=True)
        self.assertTrue(result['success']); self.assertEqual(before, self.hashes())

    def test_cycles_explicit_bounded(self):
        for bad in (0, 61, True):
            with self.assertRaises(IMError): run_cycles(self.home, self.config, cycles=bad)
        with patch('im_hub.operations.run_sources', return_value={'success': True}), patch('im_hub.operations.time.sleep') as sleep:
            result = run_cycles(self.home, self.config, cycles=2, interval=1)
            self.assertEqual(result['cycles_completed'], 2); sleep.assert_called_once_with(1)

    def test_export_all_crosses_page_boundary(self):
        self.existing(205); result = export_all(self.home)
        self.assertEqual(result['records'], 205)
        self.assertEqual(len((Path(result['directory']) / 'messages.jsonl').read_text('utf-8').splitlines()), 205)
        self.assertTrue(result['query_complete']); self.assertFalse(result['source_history_complete'])

    def test_export_limit_failure_leaves_no_partial_success(self):
        self.existing(4)
        with self.assertRaisesRegex(IMError, 'EXPORT_TOO_LARGE'): export_all(self.home, max_records=2)
        self.assertEqual(list((self.home / 'exports').iterdir()), [])

    def test_local_report_cites_each_message(self):
        self.existing(2); result = report(self.home)
        text = Path(result['report']).read_text('utf-8')
        self.assertEqual(text.count('immsg:'), 2); self.assertFalse(result['business_state_committed'])

    def test_verify_is_read_only(self):
        self.existing(); before = self.hashes(); self.assertTrue(verify(self.home)['valid']); self.assertEqual(before, self.hashes())

    def test_health_is_read_only(self):
        self.existing(); before = self.hashes(); self.assertIn(health(self.home)['status'], ['ok', 'degraded']); self.assertEqual(before, self.hashes())

    def test_backup_restore_roundtrip(self):
        self.collect(); self.existing(3)
        path = self.root / 'backup.zip'; a = backup(self.home, path)
        restored = self.root / 'restored'; b = restore(path, restored)
        self.assertEqual((a['records_verified'], b['records_verified']), (4, 4))
        self.assertEqual(desktop_status(restored)['items'][0]['runs_by_status']['committed'], 1)

    def test_backup_excludes_private_machine_config_and_reports(self):
        self.existing(); (self.home / 'sources.local.json').write_text('private fixture config')
        (self.home / 'runtime' / 'probe.log').write_text('private fixture log')
        path = self.root / 'backup.zip'; backup(self.home, path)
        with zipfile.ZipFile(path) as z:
            self.assertNotIn('sources.local.json', z.namelist()); self.assertNotIn('runtime/probe.log', z.namelist())

    def test_backup_rejects_inside_home_and_existing_file(self):
        with self.assertRaises(IMError): backup(self.home, self.home / 'x.zip')
        path = self.root / 'x.zip'; path.write_bytes(b'existing')
        with self.assertRaises(IMError): backup(self.home, path)
        self.assertEqual(path.read_bytes(), b'existing')

    def test_backup_lock_rejects_active_writer(self):
        with writer(self.home):
            with self.assertRaisesRegex(IMError, 'WRITER_BUSY'): backup(self.home, self.root / 'locked.zip')
        self.assertFalse((self.root / 'locked.zip').exists())

    def test_restore_no_overwrite(self):
        self.existing(); path = self.root / 'b.zip'; backup(self.home, path)
        with self.assertRaises(IMError): restore(path, self.home)

    def test_restore_rejects_tampered_payload_and_removes_stage(self):
        self.existing(); path = self.root / 'b.zip'; backup(self.home, path)
        changed = self.root / 'bad.zip'
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(changed, 'x') as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == 'im-hub.json': data = b'X' * len(data)
                dst.writestr(item, data)
        with self.assertRaises(IMError): restore(changed, self.root / 'restored')
        self.assertFalse((self.root / 'restored').exists()); self.assertFalse(list(self.root.glob('.restore-*')))

    def test_restore_rejects_casefold_alias(self):
        path = self.root / 'alias.zip'
        with zipfile.ZipFile(path, 'x') as z:
            z.writestr('BACKUP.json', '{}'); z.writestr('backup.json', '{}')
        with self.assertRaisesRegex(IMError, 'BACKUP_INDEX'): restore(path, self.root / 'restored')

    def test_restore_path_whitelist(self):
        for value in ('../index.sqlite3', '/index.sqlite3', 'batches/../index.sqlite3',
                      'batches/CON.json', 'batches/a:stream.json', 'batches/a. /x.json', 'batches/aux/x.json',
                      'batches/a\\x.json', 'batches/a/<x>.json', 'runtime/x.json'):
            self.assertFalse(_allowed(value), value)
        self.assertTrue(_allowed('batches/abc/source.json'))

    def test_restore_bad_zip_safe_error(self):
        path = self.root / 'bad.zip'; path.write_text('not zip')
        with self.assertRaises(IMError): restore(path, self.root / 'restored')

    def test_cli_partial_run_returns_nonzero_and_structured_result(self):
        self.source.unlink(); output = io.StringIO()
        with redirect_stdout(output): code = main(['--home', str(self.home), 'run', '--config', str(self.config)])
        self.assertEqual(code, 4); self.assertFalse(json.loads(output.getvalue())['data']['success'])


if __name__ == '__main__': unittest.main(verbosity=2)
