"""Synthetic per-conversation collection intent/window contracts, never real GUI."""
import copy
import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from im_hub.common import IMError, canonical, digest, readonly, stamp
from im_hub.desktop_sources import collect_desktop, import_window_bounds
from im_hub.desktop_week_collection import collect_desktop_week
from im_hub.query import query_messages
from im_hub.cli import main
import test_desktop_backfill as previous
import test_product as product
from test_core import fake_backend
from test_desktop_directory import START, END, AUTH

# Remove desktop consent in tests, not Windows' native runtime/crypto environment.
# Clearing SYSTEMROOT/WINDIR made the real Node backend abort before any import.
OS_RUNTIME_ENV={k:v for k,v in os.environ.items() if k.upper() in
                {'SYSTEMROOT','WINDIR','PATH','COMSPEC','PATHEXT','TEMP','TMP','PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS'}}


class DesktopWeekCollectionTests(unittest.TestCase):
    def setUp(self):
        self.f=previous.DesktopBackfillTests('test_replay_zero_new_and_no_freshness_reset');self.f.setUp()
        self.config=self.f.root/'collection.json';self.source={**self.f.p,'directory_binding':{'native_conversation_id':'r1','conversation_type':'group'}}
        self.save()
    def tearDown(self):self.f.tearDown()
    def save(self,extra=None):
        self.config.write_text(canonical({'version':1,'sources':{'bound':self.source,**(extra or {})}}),'utf-8')
    def run_collection(self,**kw):
        return collect_desktop_week(self.f.home,self.f.inventory,self.config,backend=fake_backend,**kw)
    def intents(self):return list(self.f.folder.glob('acquire/*/intent.json'))

    def test_wecom_ui_payload_runs_fixed_window_pipeline_without_real_ui(self):
        from contextlib import contextmanager
        from test_desktop_directory import account
        self.f.a=account('wecom');self.f.make_inventory()
        self.source={k:self.f.a[k] for k in ('platform','account_namespace','source_epoch','data_class')}
        S=product.S
        self.source.update(enabled=True,transport='wecom-ui',binding='66',conversation_id='r1',conversation_name='测试群',
                           directory_binding={'native_conversation_id':'r1','conversation_type':'group'},
                           desktop={'profile_reviewed':True,'window':S('企业微信','WindowControl'),
                             'conversation_header':S('测试群','TextControl'),'navigation':[],
                             'message_list':S('消息','ListControl'),'select_menu':S('多选','MenuItemControl'),
                             'selection_indicator':S('选择','TextControl'),'message_control_type':'ListItemControl','max_pages':1})
        self.config.write_text(canonical({'version':1,'sources':{'wecom-bound':self.source}}),'utf-8')
        class Fake:
            @contextmanager
            def lease(self,platform,ui):yield self
            def wecom_pages(self,ui):return [product.native_message(timestamp=START+10)]
        def acquire(*a,**kw):return collect_desktop(*a,**kw,driver=Fake())
        with patch.dict(os.environ,{**OS_RUNTIME_ENV,**AUTH},clear=True):
            a=self.run_collection(allow_ui=True,collector=acquire)
        b=self.run_collection(collector=lambda *a,**kw:1/0)
        self.assertEqual((a['new_records'],a['committed_conversations'],b['new_records']),(1,1,0))
        self.assertFalse(b['ui_attempted'])

    def test_config_change_after_acquire_keeps_correlation_no_false_receipt(self):
        original=self.config.read_bytes()
        def acquire(*a,**kw):
            result=collect_desktop(*a,**kw)
            self.config.write_text(canonical({'version':1,'sources':{}}),'utf-8')
            return result
        with self.assertRaisesRegex(IMError,'CONFIG_CHANGED'):self.run_collection(collector=acquire)
        self.assertEqual(len(list(self.f.folder.glob('acquire/*/capture.json'))),1)
        self.assertFalse(list(self.f.folder.glob('results/*.json')))
        self.config.write_bytes(original)
        resumed=self.run_collection(collector=lambda *a,**kw:1/0)
        self.assertEqual((resumed['new_capture_calls'],resumed['new_records'],resumed['committed_conversations']),(0,0,1))

    def test_payload_observed_before_window_end_is_rejected_before_import(self):
        meta=json.loads(self.f.meta.read_text());meta['observed_at']=stamp(START+1)
        self.f.meta.write_text(canonical(meta),'utf-8')
        r=self.run_collection();self.assertEqual(r['committed_conversations'],0)
        self.assertEqual(r['items'][0]['error'],'DESKTOP_CAPTURE_PREDATES_WINDOW_END')

    def test_generic_failure_text_does_not_leak(self):
        def fail(*a,**kw):raise RuntimeError('private preview secret')
        r=self.run_collection(collector=fail)
        self.assertNotIn('private preview',canonical(r));self.assertEqual(r['items'][0]['status'],'reconciliation_required')

    def test_cli_unknown_outcome_is_nonzero_and_does_not_acquire_again(self):
        self.run_collection(collector=lambda *a,**k: (_ for _ in ()).throw(IMError('LOST')))
        out=io.StringIO()
        with redirect_stdout(out):
            code=main(['--home',str(self.f.home),'collect-desktop-week','--inventory',self.f.inventory,'--config',str(self.config)])
        self.assertEqual(code,4);self.assertEqual(json.loads(out.getvalue())['data']['new_capture_calls'],0)

    def test_file_capture_window_then_backfill_uses_same_batch(self):
        r=self.run_collection()
        self.assertEqual((r['new_capture_calls'],r['committed_conversations'],r['new_records']),(1,1,2))
        self.assertFalse(r['ui_attempted']);self.assertFalse(r['four_platform_complete'])
        self.assertEqual(r['items'][0]['records_checked'],2)
        q=query_messages(self.f.home,include_synthetic=True,since=START,until=END,full=True)
        self.assertEqual({x['text'] for x in q['items']},{'start','inside'})
        pin=json.loads(next(self.f.folder.glob('acquire/*/capture.json')).read_text())
        c=readonly(self.f.home/'desktop.sqlite3');meta=json.loads(c.execute('SELECT manifest_json FROM runs WHERE run_id=?',(pin['captured_run_id'],)).fetchone()[0]);c.close()
        self.assertEqual(meta['requested_window'],{'since':float(START),'until':float(END)})

    def test_retry_uses_exact_captured_run_not_another_acquisition(self):
        a=self.run_collection()
        b=self.run_collection(collector=lambda *a,**k: (_ for _ in ()).throw(AssertionError('must not acquire')))
        self.assertEqual((b['new_capture_calls'],b['new_records'],b['committed_conversations']),(0,0,1))
        self.assertEqual(a['items'][0]['captured_run_id'],b['items'][0]['captured_run_id'])

    def test_explicit_pin_reuses_without_profile_or_ui(self):
        self.source={**self.f.binding};self.save()
        with patch.dict(os.environ,OS_RUNTIME_ENV,clear=True):r=self.run_collection(collector=lambda *a,**k:1/0)
        self.assertEqual(r['committed_conversations'],1);self.assertEqual(r['new_capture_calls'],0)

    def test_invalid_explicit_pin_is_not_acquisition_fallback(self):
        self.source['captured_run_id']='0'*32;self.save()
        r=self.run_collection(collector=lambda *a,**k:1/0)
        self.assertEqual(r['held_conversations'],1);self.assertEqual(r['new_capture_calls'],0)
        self.assertFalse(self.intents())

    def test_unknown_capture_outcome_is_not_retried(self):
        def unknown(*a,**kw):raise IMError('SIMULATED_OUTCOME_UNKNOWN')
        a=self.run_collection(collector=unknown)
        self.assertEqual(a['items'][0]['status'],'reconciliation_required');self.assertEqual(len(self.intents()),1)
        b=self.run_collection(collector=lambda *a,**kw:1/0)
        self.assertEqual(b['new_capture_calls'],0)
        self.assertEqual(b['items'][0]['error'],'DESKTOP_CAPTURE_OUTCOME_REQUIRES_RECONCILIATION')

    def test_explicit_pin_can_reconcile_unknown_effect(self):
        self.run_collection(collector=lambda *a,**k: (_ for _ in ()).throw(IMError('LOST')))
        self.source['captured_run_id']=self.f.capture['run_id'];self.save()
        b=self.run_collection(collector=lambda *a,**k:1/0)
        self.assertEqual(b['committed_conversations'],1);self.assertEqual(b['new_capture_calls'],0)

    def test_configuration_change_does_not_clear_unknown_intent(self):
        self.run_collection(collector=lambda *a,**k: (_ for _ in ()).throw(IMError('LOST')))
        self.source['max_records']=100000;self.save()
        b=self.run_collection(collector=lambda *a,**k:1/0)
        self.assertEqual(b['items'][0]['error'],'DESKTOP_CAPTURE_INTENT_CHANGED_RECONCILE')
        self.assertEqual(b['new_capture_calls'],0)

    def test_intent_is_durable_before_collector(self):
        def checked(*args,**kwargs):
            self.assertEqual(len(self.intents()),1)
            envelope=json.loads(self.intents()[0].read_text())
            self.assertEqual(envelope['intent']['source_name'],'bound')
            return collect_desktop(*args,**kwargs)
        self.assertEqual(self.run_collection(collector=checked)['committed_conversations'],1)

    def test_direct_not_silently_treated_as_group(self):
        self.f.make_inventory(kind='DIRECT');self.source['directory_binding']['conversation_type']='direct';self.save()
        r=self.run_collection(collector=lambda *a,**k:1/0)
        self.assertEqual(r['items'][0]['status'],'requires_typed_direct_desktop_adapter');self.assertFalse(self.intents())

    def test_wrong_identity_is_held_before_capture(self):
        self.source['conversation_id']='other';self.save()
        r=self.run_collection(collector=lambda *a,**k:1/0)
        self.assertEqual(r['items'][0]['status'],'requires_source_binding');self.assertFalse(self.intents())

    def test_duplicate_source_is_not_arbitrarily_chosen(self):
        self.save({'other':dict(self.source)})
        r=self.run_collection();self.assertEqual(r['items'][0]['status'],'ambiguous_source_binding')
        self.assertFalse(self.intents())

    def test_ui_consent_and_deadline_before_intent(self):
        self.source.update(transport='tim-ui',desktop=product.ui_profile());self.save()
        with patch.dict(os.environ,OS_RUNTIME_ENV,clear=True):
            a=self.run_collection(collector=lambda *a,**k:1/0)
            b=self.run_collection(allow_ui=True,collector=lambda *a,**k:1/0)
        self.assertEqual(a['items'][0]['error'],'UI_CONSENT_REQUIRED')
        self.assertEqual(b['items'][0]['error'],'DIRECTORY_EXPLICIT_DEADLINE_REQUIRED')
        self.assertFalse(self.intents())

    def test_expired_and_stop_never_dispatch(self):
        self.source.update(transport='tim-ui',desktop=product.ui_profile());self.save()
        with patch.dict(os.environ,{**OS_RUNTIME_ENV,'IM_HUB_DESKTOP_UNTIL':'2020-01-01T00:00:00Z'},clear=True):
            a=self.run_collection(allow_ui=True,collector=lambda *a,**k:1/0)
        self.assertEqual(a['items'][0]['error'],'DESKTOP_AUTHORIZATION_EXPIRED')
        self.assertFalse(self.intents())

    def test_config_hash_before_write(self):
        before=self.f.hashes()
        with self.assertRaisesRegex(IMError,'CONFIG_CHANGED'):self.run_collection(expected_config_sha256='0'*64)
        self.assertEqual(before,self.f.hashes())

    def test_window_mismatch_pending_stops_before_new_source_read(self):
        def fail(*a):raise IMError('SIMULATED_IMPORT_FAILED')
        window={'since':stamp(START),'until_exclusive':stamp(END)}
        with self.assertRaises(IMError):collect_desktop(self.f.home,self.f.root,'bound',self.f.p,backend=fail,import_window=window)
        changed={'since':stamp(START+1),'until_exclusive':stamp(END)}
        with self.assertRaisesRegex(IMError,'WINDOW_MISMATCH'):
            collect_desktop(self.f.home,self.f.root,'bound',self.f.p,backend=fake_backend,import_window=changed)
        r=collect_desktop(self.f.home,self.f.root,'bound',self.f.p,backend=fake_backend,import_window=window)
        self.assertTrue(r['resumed_pending']);self.assertEqual(r['records'],2)

    def test_selection_window_does_not_rebind_existing_source(self):
        c=readonly(self.f.home/'desktop.sqlite3');before=c.execute('SELECT profile_sha256 FROM sources WHERE source_id=?',('bound',)).fetchone()[0];c.close()
        self.run_collection()
        c=readonly(self.f.home/'desktop.sqlite3');after=c.execute('SELECT profile_sha256 FROM sources WHERE source_id=?',('bound',)).fetchone()[0];c.close()
        self.assertEqual(before,after)

    def test_empty_page_performs_no_capture(self):
        r=self.run_collection(offset=999,collector=lambda *a,**k:1/0)
        self.assertEqual(r['new_capture_calls'],0);self.assertEqual(r['items'],[]);self.assertFalse(self.intents())

    def test_invalid_bounds_rejected(self):
        for n in (0,True,21):
            with self.assertRaises(IMError):self.run_collection(max_conversations=n)
        for w in ({},{'since':stamp(END),'until_exclusive':stamp(START)},
                  {'since':'2024-01-01','until_exclusive':stamp(END)}):
            with self.assertRaises(IMError):import_window_bounds(w)


if __name__=='__main__':unittest.main()
