"""Bounded supervisor contracts. No real UI, client, or message transmission."""
from __future__ import annotations
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from im_hub.common import IMError,canonical,digest,initialize,stamp,writer
from im_hub.collection import collect
from im_hub.soak import GATE,parent_gate,run_soak,soak_status,new_event_refs,ChildTree
from test_core import normalized,spec,fake_backend,OBSERVED
from im_hub.store import ingest

class Clock:
    def __init__(self):self.t=1789459200.0
    def __call__(self):return self.t
    def sleep(self,seconds):self.t+=seconds

class SoakTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='im-hub-soak-synthetic-')
        self.root=Path(self.tmp.name);self.home=self.root/'home';initialize(self.home)
        self.config=self.root/'config.json';self.clock=Clock();self.calls=[]
        self.profiles={'fixture':{'enabled':True,'platform':'wechat','transport':'file','adapter':'normalized-v2',
            'account_namespace':'fixture','conversation_id':'g1','conversation_name':'测试群','source_epoch':'fixture',
            'data_class':'synthetic','input':'fixture.jsonl','manifest':'fixture-manifest.json'}}
        self.save()
    def tearDown(self):self.tmp.cleanup()
    def save(self):self.config.write_text(canonical({'version':1,'sources':self.profiles}),'utf-8')
    def fn(self,*args,**kwargs):
        self.calls.append(args[2]);self.clock.t+=1
        return {'records':0,'new_records':0,'duplicates':0}
    def run_it(self,**kw):
        opts={'until':stamp(self.clock()+1000),'interval':15,'duration':40,'max_cycles':2,
              'invoke':self.fn,'clock':self.clock,'sleep':self.clock.sleep};opts.update(kw)
        return run_soak(self.home,self.config,**opts)
    def test_two_cycles_and_readonly_status(self):
        r=self.run_it();self.assertEqual(r['status'],'segment_complete');self.assertEqual(r['cycles_completed'],2)
        self.assertEqual(self.calls,['fixture','fixture']);self.assertFalse(r['full_final_acceptance'])
        before={p:digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}
        self.assertTrue(soak_status(self.home)['query_only'])
        self.assertEqual(before,{p:digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()})
    def test_duration_and_interval_bounds(self):
        for k,vals in [('duration',(0,86401,True)),('interval',(0,14,3601,True)),('max_cycles',(0,1001,True))]:
            for v in vals:
                with self.assertRaises(IMError):self.run_it(**{k:v})
        self.assertFalse(self.calls)
    def test_eight_hour_window_supported_without_extending_deadline(self):
        end=stamp(self.clock()+28800)
        r=self.run_it(until=end,duration=86400,max_cycles=1)
        self.assertEqual(r['authorized_until'],end);self.assertEqual(r['segment_until'],end)
    def test_expired_or_unbounded_authorization(self):
        for until in (stamp(self.clock()),stamp(self.clock()+86401),'2026-09-16T12:00:00'):
            with self.assertRaises(IMError):self.run_it(until=until)
    def test_activation_needs_ui_consent(self):
        with self.assertRaisesRegex(IMError,'UI_CONSENT'):self.run_it(activate=True)
    def test_no_enabled_source_rejected(self):
        self.profiles['fixture']['enabled']=False;self.save()
        with self.assertRaisesRegex(IMError,'NO_ENABLED'):self.run_it()
    def test_manual_copy_cannot_masquerade_as_automatic(self):
        self.profiles['fixture'].update(platform='wecom',transport='wecom-clipboard',adapter='wecom-native',binding='66')
        self.save()
        with self.assertRaisesRegex(IMError,'MANUAL_CLIPBOARD'):self.run_it(allow_ui=True)
    def test_no_second_supervisor(self):
        with writer(self.home,lock_name='.soak.lock'):
            with self.assertRaisesRegex(IMError,'WRITER_BUSY'):self.run_it()
        self.assertFalse(self.calls)
    def test_stop_before_any_collection(self):
        p=self.home/'runtime/soak';p.mkdir();(p/'STOP').write_text('stop')
        with self.assertRaisesRegex(IMError,'STOP_REQUESTED'):self.run_it()
        self.assertFalse(self.calls)
    def test_stop_between_sources_not_new_effect(self):
        self.profiles['second']=dict(self.profiles['fixture']);self.save()
        def fn(*a,**k):
            result=self.fn(*a,**k);(self.home/'runtime/soak/STOP').write_text('stop');return result
        r=self.run_it(invoke=fn);self.assertEqual(r['status'],'stopped');self.assertEqual(len(self.calls),1)
    def test_changed_config_stops_before_next_source(self):
        self.profiles['second']=dict(self.profiles['fixture']);self.save()
        def fn(*a,**k):
            r=self.fn(*a,**k);self.config.write_text('{}','utf-8');return r
        r=self.run_it(invoke=fn);self.assertEqual(r['stop_reason'],'SOAK_CONFIG_CHANGED_RESTART_REQUIRED')
        self.assertEqual(len(self.calls),1)
    def test_user_input_stops_not_retried(self):
        def fn(*a,**k):self.calls.append(a[2]);raise IMError('USER_INPUT_DETECTED_PAUSED')
        r=self.run_it(invoke=fn);self.assertEqual(r['status'],'stopped');self.assertEqual(len(self.calls),1)
    def test_capability_failure_pauses_only_that_source(self):
        self.profiles['second']=dict(self.profiles['fixture']);self.save()
        def fn(*a,**k):
            if a[2]=='fixture':self.calls.append('fixture');raise IMError('VISUAL_ANCHOR_NOT_FOUND')
            return self.fn(*a,**k)
        r=self.run_it(invoke=fn)
        self.assertEqual(self.calls,['fixture','second','second'])
        self.assertEqual(r['paused_sources']['fixture'],'VISUAL_ANCHOR_NOT_FOUND')
    def test_transient_lock_retries_next_cycle_and_recovers(self):
        def fn(*a,**k):
            if not self.calls:
                self.calls.append(a[2]);raise IMError('WRITER_BUSY')
            return self.fn(*a,**k)
        r=self.run_it(invoke=fn)
        self.assertEqual(len(self.calls),2);self.assertFalse(r['paused_sources']);self.assertTrue(r['last_cycle_all_succeeded'])
    def test_repeated_transient_error_has_a_retry_bound(self):
        def fn(*a,**k):self.calls.append(a[2]);raise IMError('SOURCE_CHANGED_DURING_SNAPSHOT_RETRY')
        r=self.run_it(invoke=fn,max_cycles=8,duration=140)
        self.assertEqual(len(self.calls),4);self.assertEqual(r['status'],'all_sources_paused')
    def test_retryable_last_pass_is_not_success(self):
        def fn(*a,**k):raise IMError('WRITER_BUSY')
        r=self.run_it(invoke=fn,max_cycles=1)
        self.assertFalse(r['last_cycle_all_succeeded']);self.assertEqual(r['latest_results'][0]['status'],'retryable')
    def test_unexpected_error_never_success(self):
        def fn(*a,**k):raise RuntimeError('sensitive raw text must not leak')
        r=self.run_it(invoke=fn);self.assertEqual(r['status'],'failed');self.assertNotIn('sensitive',canonical(r))
    def test_invalid_child_shape_never_success(self):
        r=self.run_it(invoke=lambda *a,**k:[])
        self.assertEqual(r['paused_sources']['fixture'],'SOURCE_CHILD_INVALID_JSON')
    def test_segment_deadline_prevents_next_source(self):
        self.profiles['second']=dict(self.profiles['fixture']);self.save()
        def fn(*a,**k):self.clock.t+=50;return self.fn(*a,**k)
        r=self.run_it(invoke=fn);self.assertEqual(len(self.calls),1);self.assertEqual(r['status'],'deadline_reached')
    def test_resume_preserves_observation_baseline(self):
        until=stamp(self.clock()+1000);a=self.run_it(until=until,max_cycles=1)
        self.clock.t+=30;b=self.run_it(until=until,max_cycles=1)
        self.assertEqual(a['observation_baseline'],b['observation_baseline'])
        self.assertNotEqual(a['started_at'],b['started_at'])
    def test_collection_configuration_guard_is_before_source_access(self):
        with patch.dict(os.environ,{'IM_HUB_EXPECT_CONFIG_SHA256':'0'*64}):
            with self.assertRaisesRegex(IMError,'CONFIG_CHANGED'):collect(self.home,self.config,'fixture')
    def test_old_real_message_not_counted_as_new_event(self):
        source=self.root/'in.jsonl';source.write_text(canonical(normalized())+'\n','utf-8')
        ingest(self.home,source,spec(),OBSERVED,backend=fake_backend)
        self.assertEqual(new_event_refs(self.home,0,self.clock()+1000,'wechat')['count'],0)
    def test_real_new_message_reference_is_metadata_only(self):
        source=self.root/'in.jsonl';row=normalized(text='Do not emit this body')
        source.write_text(canonical(row)+'\n','utf-8');ingest(self.home,source,spec(),OBSERVED,backend=fake_backend)
        r=new_event_refs(self.home,0,row['timestamp']-1,'wechat')
        self.assertEqual(r['count'],1);self.assertNotIn('emit',canonical(r));self.assertTrue(r['references'][0]['evidence_ref'].startswith('immsg:'))

class OwnershipTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows Job Objects')
    def test_real_owned_job_can_be_created_without_a_child(self):
        tree=ChildTree();self.assertIsNotNone(tree.job);tree.close(None);self.assertIsNone(tree.job)
    def test_unmarked_normal_cli_never_reads_stdin(self):
        with patch.dict(os.environ,{},clear=True),patch('sys.stdin',new=io.StringIO('anything')) as inp:
            parent_gate();self.assertEqual(inp.tell(),0)
    def test_missing_owner_gate_refused(self):
        for value in ('','wrong\n'):
            with patch.dict(os.environ,{'IM_HUB_PARENT_GATE':'1'},clear=True),patch('sys.stdin',new=io.StringIO(value)):
                with self.assertRaisesRegex(IMError,'OWNERSHIP'):parent_gate()
    def test_owner_gate_consumed_once(self):
        with patch.dict(os.environ,{'IM_HUB_PARENT_GATE':'1'},clear=True),patch('sys.stdin',new=io.StringIO(GATE)):
            parent_gate();self.assertNotIn('IM_HUB_PARENT_GATE',os.environ);parent_gate()

if __name__=='__main__':unittest.main()
