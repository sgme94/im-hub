import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from im_hub.cli import main
from im_hub.analysis import export_packet
from im_hub.collection import capabilities, collect
from im_hub.common import IMError, canonical, digest, initialize, writer
from im_hub.query import query_messages
from test_core import fake_backend


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='im-hub-config-test-')
        self.root = Path(self.tmp.name)
        self.home = self.root / 'state'
        initialize(self.home)
        raw = '消息对象:测试群\n2024-01-02 上午 08:00:00 Fixture(1)\n[合成测试] 请验证\n'.encode('utf-8-sig')
        (self.root / 'tim.txt').write_bytes(raw)
        self.manifest = {'source_id': 'demo', 'sha256': digest(raw), 'observed_at': '2024-01-02T12:00:00+08:00'}
        self.profile = {'version': 1, 'sources': {'demo': {
            'enabled': True, 'transport': 'file', 'platform': 'qq', 'adapter': 'tim-txt',
            'account_namespace': 'fixture', 'conversation_id': 'fixture', 'conversation_name': '测试群',
            'source_epoch': 'fixture', 'data_class': 'synthetic', 'input': 'tim.txt', 'manifest': 'meta.json'}}}
        self.save()

    def save(self):
        (self.root / 'sources.json').write_text(canonical(self.profile), 'utf-8')
        (self.root / 'meta.json').write_text(canonical(self.manifest), 'utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def run_collect(self, **kwargs):
        return collect(self.home, self.root / 'sources.json', 'demo', backend=fake_backend, **kwargs)

    def test_configured_file_replay(self):
        a = self.run_collect(); b = self.run_collect()
        self.assertEqual((a['new_records'], b['new_records'], b['duplicates']), (1, 0, 1))
        self.assertFalse(a['client_access_performed']); self.assertEqual(a['llm_calls'], 0)

    def test_disabled_source(self):
        self.profile['sources']['demo']['enabled'] = False; self.save()
        with self.assertRaisesRegex(IMError, 'SOURCE_NOT_ENABLED'): self.run_collect()

    def test_changed_source_hash(self):
        (self.root / 'tim.txt').write_text('different', 'utf-8')
        with self.assertRaisesRegex(IMError, 'SOURCE_HASH_MISMATCH'): self.run_collect()

    def test_wrong_manifest_source(self):
        self.manifest['source_id'] = 'other'; self.save()
        with self.assertRaisesRegex(IMError, 'SOURCE_MANIFEST_BINDING_MISMATCH'): self.run_collect()

    def test_observation_is_required_not_import_clock(self):
        del self.manifest['observed_at']; self.save()
        with self.assertRaisesRegex(IMError, 'TIMEZONE_AWARE'): self.run_collect()

    def test_no_implicit_gui_fallback(self):
        self.profile['sources']['demo']['transport'] = 'gui'; self.save()
        with self.assertRaisesRegex(IMError, 'CLIENT_DRIVER_NOT_IMPLEMENTED'): self.run_collect()

    def test_dry_run_creates_no_batch(self):
        result = self.run_collect(dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual(list((self.home / 'batches').iterdir()), [])

    def test_synthetic_not_in_default_query(self):
        self.run_collect()
        self.assertEqual(query_messages(self.home)['count'], 0)
        self.assertEqual(query_messages(self.home, include_synthetic=True)['count'], 1)

    def test_legacy_home_readonly(self):
        self.run_collect()
        (self.home / 'im-hub.json').unlink()
        (self.home / 'im-unified.json').write_text(canonical({'kind': 'im-unified-private', 'schema_version': 1}), 'utf-8')
        self.assertEqual(query_messages(self.home, include_synthetic=True)['count'], 1)
        with self.assertRaisesRegex(IMError, 'LEGACY_HOME_READ_ONLY'):
            with writer(self.home): pass
        for analyze in (False, True):
            with self.assertRaisesRegex(IMError, 'LEGACY_HOME_READ_ONLY'):
                export_packet(self.home, {'include_synthetic': True}, analyze=analyze)
        self.assertEqual(list((self.home / 'exports').iterdir()), [])
        self.assertEqual(list((self.home / 'candidates').iterdir()), [])

    def test_capabilities_no_llm_or_frontend(self):
        out = io.StringIO()
        with redirect_stdout(out): status = main(['capabilities'])
        result = json.loads(out.getvalue())
        self.assertEqual(status, 0)
        self.assertFalse(result['data']['llm_required'])
        self.assertFalse(result['data']['frontend'])
        self.assertFalse(capabilities()['desktop_driver']['enabled_by_default'])
        self.assertFalse(capabilities()['desktop_driver']['unattended_acceptance'])

    def test_cli_pipeline_utf8_even_under_ascii_environment(self):
        self.run_collect()
        env = {**os.environ, 'PYTHONIOENCODING': 'ascii', 'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.run([sys.executable, '-m', 'im_hub', '--home', str(self.home),
                                  'query', '--include-synthetic', '--full'],
                                 env=env, capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout.decode('utf-8'))
        self.assertIn('请验证', result['data']['items'][0]['text'])


if __name__ == '__main__':
    unittest.main()
