"""Fixed-window reuse of pinned desktop captures. Synthetic stores, no live UI."""
from __future__ import annotations
import copy
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from im_hub.activity import discover_week, week_status
from im_hub.common import IMError, canonical, digest, initialize, readonly, stamp, db_path, writer
from im_hub.desktop_sources import collect_desktop
from im_hub.desktop_directory import plan_desktop_backfill
from im_hub.desktop_backfill import backfill_desktop_week
from im_hub.query import query_messages
from im_hub.cli import main
from test_core import fake_backend
from test_product import native_message
from test_desktop_directory import account, row, page, FakeReader, AUTH, START, END


class DesktopBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='im-hub-pinned-desktop-fixture-')
        self.root=Path(self.tmp.name);self.home=self.root/'private';initialize(self.home)
        self.a=account();self.a['directory']['sections'][1].pop('expand',None)
        self.accounts=self.root/'accounts.json';self.config=self.root/'bindings.json'
        self.source=self.root/'tim.txt';self.meta=self.root/'manifest.json'
        self.p={k:self.a[k] for k in ('platform','account_namespace','source_epoch','data_class')}
        self.p.update(enabled=True,transport='tim-export',adapter='tim-sequence-json',conversation_id='r1',
                      conversation_name='测试群',export_mode='full-history-append-only',
                      input=str(self.source),manifest=str(self.meta),since=stamp(END))
        raw=('消息对象:测试群\n2024-01-01 上午 07:59:59 Fixture(1)\nbefore\n'
             '2024-01-01 上午 08:00:00 Fixture(1)\nstart\n'
             '2024-01-02 上午 08:00:00 Fixture(1)\ninside\n'
             '2024-01-08 上午 08:00:00 Fixture(1)\nend\n'
             '2024-01-08 上午 09:00:00 Fixture(1)\nafter\n').encode('utf-8-sig')
        self.source.write_bytes(raw)
        self.meta.write_text(canonical({'source_id':'bound','observed_at':stamp(END+86400),'sha256':digest(raw)}),'utf-8')
        self.capture=collect_desktop(self.home,self.root,'bound',self.p,backend=fake_backend)
        self.make_inventory()
        self.binding={k:self.p[k] for k in ('enabled','platform','account_namespace','source_epoch','data_class','conversation_id','conversation_name')}
        self.binding.update(captured_run_id=self.capture['run_id'],
                            directory_binding={'native_conversation_id':'r1','conversation_type':'group'})
        self.save_binding()

    def tearDown(self):self.tmp.cleanup()

    def make_inventory(self, kind='GROUP', driver=None):
        self.accounts.write_text(canonical({'schema':'im-hub-active-accounts/1','accounts':{'account':self.a}}),'utf-8')
        fake=driver or FakeReader({'regular':[page([row('r1',kind,name='测试群')])],'folded':[page([])]})
        with patch.dict(os.environ,AUTH,clear=True),patch('im_hub.desktop_directory.UIADirectoryReader',return_value=fake):
            result=discover_week(self.home,self.accounts,stamp(START),stamp(END),allow_ui=True)
        self.inventory=result['inventory_id'];self.folder=Path(result['inventory_path']).parent

    def save_binding(self, extra=None):
        self.config.write_text(canonical({'version':1,'sources':{'bound':self.binding,**(extra or {})}}),'utf-8')

    def fill(self, **kw):
        return backfill_desktop_week(self.home,self.inventory,self.config,backend=fake_backend,**kw)

    def hashes(self):return {p.relative_to(self.home).as_posix():digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}

    def test_wecom_pinned_native_group_capture_reuses_fixed_window(self):
        self.a=account('wecom');self.make_inventory()
        p={k:self.a[k] for k in ('platform','account_namespace','source_epoch','data_class')}
        p.update(enabled=True,transport='wecom-clipboard',conversation_id='r1',conversation_name='测试群',binding='66',since=stamp(END))
        class Fake:
            def capture(self,seq):return native_message(timestamp=START+10),{'captured_at':stamp(END+100)}
        captured=collect_desktop(self.home,self.root,'wecom-bound',p,backend=fake_backend,after_sequence=1,driver=Fake())
        binding={k:p[k] for k in ('enabled','platform','account_namespace','source_epoch','data_class','conversation_id','conversation_name')}
        binding.update(captured_run_id=captured['run_id'],directory_binding={'native_conversation_id':'r1','conversation_type':'group'})
        self.config.write_text(canonical({'version':1,'sources':{'wecom-bound':binding}}),'utf-8')
        a=self.fill();b=self.fill(replay=True)
        self.assertEqual((a['records_checked'],a['new_records'],b['new_records']),(1,1,0))
        self.assertFalse(a['history_complete'])

    def test_config_change_during_import_retains_batch_not_false_receipt(self):
        def backend(home,args):
            result=fake_backend(home,args)
            if args[0]=='import':
                data=json.loads(self.config.read_text());data['changed']=True
                self.config.write_text(canonical(data),'utf-8')
            return result
        a=backfill_desktop_week(self.home,self.inventory,self.config,backend=backend)
        self.assertEqual(a['committed_conversations'],0);self.assertEqual(a['failed_conversations'],1)
        self.assertEqual(self.fill()['new_records'],0)

    def test_paging_has_bounded_empty_tail(self):
        out=self.fill(offset=1)
        self.assertFalse(out['items']);self.assertEqual(out['committed_conversations'],0)

    def test_cli_hold_is_nonzero_not_empty_success(self):
        self.binding['captured_run_id']='0'*32;self.save_binding();output=io.StringIO()
        with redirect_stdout(output):code=main(['--home',str(self.home),'backfill-desktop-week','--inventory',self.inventory,'--config',str(self.config)])
        self.assertEqual(code,4)

    def test_fixed_window_import_reuses_stream_and_excludes_both_outsides(self):
        out=self.fill();self.assertEqual(out['records_checked'],2);self.assertEqual(out['new_records'],2)
        q=query_messages(self.home,since=START,until=END,include_synthetic=True,full=True)
        self.assertEqual({r['text'] for r in q['items']},{'start','inside'})
        self.assertFalse(out['history_complete']);self.assertFalse(out['four_platform_complete'])
        self.assertEqual(out['committed_conversations'],1)

    def test_replay_zero_new_and_no_freshness_reset(self):
        a=self.fill();receipt=next((self.folder/'results').glob('*.json'))
        observed=json.loads(receipt.read_text())['source_observed_at']
        b=self.fill(replay=True)
        self.assertEqual((a['new_records'],b['new_records']),(2,0))
        self.assertEqual(json.loads(receipt.read_text())['source_observed_at'],observed)

    def test_no_gui_even_without_desktop_authorization(self):
        with (patch.dict(os.environ,{},clear=True),
              patch('im_hub.desktop_directory.UIADirectoryReader',side_effect=AssertionError('UI')),
              patch('im_hub.windows_desktop.NativeWindows',side_effect=AssertionError('UI'))):
            self.assertFalse(self.fill()['ui_used'])

    def test_unbound_same_name_is_not_eligible(self):
        self.binding['conversation_id']='other';self.save_binding()
        out=self.fill();self.assertEqual(out['committed_conversations'],0);self.assertEqual(out['held_conversations'],1)

    def test_native_directory_binding_required(self):
        self.binding.pop('directory_binding');self.save_binding()
        out=self.fill();self.assertEqual(out['held_conversations'],1)

    def test_wrong_pinned_run_never_selects_latest(self):
        self.binding['captured_run_id']='0'*32;self.save_binding()
        plan=plan_desktop_backfill(self.home,self.inventory,self.config)
        self.assertFalse(plan['items'][0]['execution_supported'])
        self.assertEqual(self.fill()['held_conversations'],1)

    def test_direct_does_not_reuse_group_adapter(self):
        self.make_inventory(kind='DIRECT');self.binding['directory_binding']['conversation_type']='direct';self.save_binding()
        out=self.fill();self.assertEqual(out['held_conversations'],1)
        self.assertEqual(out['items'][0]['status'],'requires_typed_direct_desktop_adapter')

    def test_multiple_bindings_refused(self):
        self.save_binding({'duplicate':dict(self.binding)})
        plan=plan_desktop_backfill(self.home,self.inventory,self.config)
        self.assertEqual(plan['items'][0]['status'],'ambiguous_source_binding')

    def test_partial_directory_can_reuse_observed_group_not_claim_all(self):
        self.make_inventory(driver=FakeReader({'regular':[page([row('r1',name='测试群')],0,False,2),IMError('USER_INPUT_DETECTED_PAUSED')], 'folded':[page([])]}))
        out=self.fill();self.assertEqual(out['committed_conversations'],1)
        self.assertFalse(out['directory_enumeration_complete']);self.assertFalse(out['four_platform_complete'])

    def test_failed_import_retains_retry_without_capture(self):
        def failed(home,args):
            if args[0]=='import':raise IMError('SYNTHETIC_IMPORT_FAILURE')
            return fake_backend(home,args)
        a=backfill_desktop_week(self.home,self.inventory,self.config,backend=failed)
        self.assertEqual(a['failed_conversations'],1);self.assertEqual(a['committed_conversations'],0)
        self.assertEqual(self.fill()['committed_conversations'],1)

    def test_failed_import_does_not_advance_capture_sequence(self):
        c=readonly(self.home/'desktop.sqlite3');before=dict(c.execute('SELECT * FROM sources').fetchone());c.close()
        def failed(*a):raise IMError('SYNTHETIC_IMPORT_FAILURE')
        backfill_desktop_week(self.home,self.inventory,self.config,backend=failed)
        c=readonly(self.home/'desktop.sqlite3');after=dict(c.execute('SELECT * FROM sources').fetchone());c.close()
        self.assertEqual(before,after)

    def test_captured_payload_tamper_rejected(self):
        (self.home/'desktop'/self.capture['run_id']/'source.json').write_text('{}','utf-8')
        out=self.fill();self.assertEqual(out['committed_conversations'],0)
        self.assertFalse(plan_desktop_backfill(self.home,self.inventory,self.config)['items'][0]['execution_supported'])

    def test_receipt_tamper_not_trusted_by_status(self):
        self.fill();p=next((self.folder/'results').glob('*.json'));j=json.loads(p.read_text());j['records']=999;p.write_text(canonical(j),'utf-8')
        status=week_status(self.home,self.inventory,verify=True)
        self.assertEqual(status['desktop_captured_conversations'],0)
        self.assertEqual(status['desktop_capture_failures'],1)

    def test_readback_loss_does_not_report_committed(self):
        self.fill();c=sqlite3.connect(db_path(self.home,self.capture['stream_id']));c.execute('DELETE FROM message WHERE content=?',('inside',));c.commit();c.close()
        status=week_status(self.home,self.inventory,verify=True)
        self.assertEqual(status['desktop_capture_failures'],1)

    def test_status_readonly_after_backfill(self):
        self.fill();before=self.hashes();out=week_status(self.home,self.inventory,verify=True)
        self.assertEqual(out['desktop_captured_messages'],2);self.assertEqual(self.hashes(),before)
        self.assertFalse(out['four_platform_complete'])

    def test_config_hash_guard_before_any_write(self):
        before=self.hashes()
        with self.assertRaisesRegex(IMError,'CONFIG_CHANGED'):
            self.fill(expected_config_sha256='0'*64)
        self.assertEqual(before,self.hashes())

    def test_inventory_hash_tamper_rejected(self):
        (self.folder/'inventory.json').write_text('{}','utf-8')
        with self.assertRaisesRegex(IMError,'HASH_MISMATCH'):self.fill()

    def test_pinned_run_source_name_match_required(self):
        self.config.write_text(canonical({'version':1,'sources':{'wrong':self.binding}}),'utf-8')
        out=self.fill();self.assertEqual(out['committed_conversations'],0)

    def test_no_original_export_read_needed_for_reuse(self):
        self.source.unlink();self.meta.unlink()
        self.assertEqual(self.fill()['committed_conversations'],1)

    def test_path_traversal_pin_rejected(self):
        self.binding['captured_run_id']='../../source';self.save_binding()
        self.assertEqual(self.fill()['held_conversations'],1)

    def test_history_watermark_remains_unknown(self):
        self.fill();receipt=json.loads(next((self.folder/'results').glob('*.json')).read_text())
        self.assertIsNone(receipt['complete_through']);self.assertFalse(receipt['history_complete'])

    def test_writer_contention_fails_without_overriding_lock(self):
        with writer(self.home,lock_name='.activity.lock'):
            with self.assertRaisesRegex(IMError,'WRITER_BUSY'):self.fill()

    def test_cli_returns_json_and_no_collection_dispatch(self):
        output=io.StringIO()
        with patch('im_hub.desktop_backfill.ingest',wraps=__import__('im_hub.store',fromlist=['ingest']).ingest) as imp:
            self.fill()
        with redirect_stdout(output):code=main(['--home',str(self.home),'backfill-desktop-week','--inventory',self.inventory,'--config',str(self.config)])
        self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue())['data']['committed_conversations'],1)


if __name__=='__main__':unittest.main()
