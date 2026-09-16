"""Synthetic-only regression suite. Fake backend tests are not ChatLab acceptance."""
from __future__ import annotations
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from im_hub.adapters import normalize, spec_for
from im_hub.analysis import export_packet, validate_candidates
from im_hub.cli import main
from im_hub.common import IMError, canonical, db_path, digest, initialize, iso_epoch, readonly, stream_key, writer
from im_hub.query import query_messages, source_status
from im_hub.store import ingest

TIME = 1789459200
OBSERVED = '2026-09-15T12:00:00+00:00'

def spec(platform='wechat', adapter='normalized-v2', account='fixture', conv='g1', data='real'):
    return spec_for(platform, account, conv, '测试群', adapter, 'fixture-generation', data,
                    '66' if platform == 'wecom' else None, 'fixture-uin' if adapter == 'qce-json' else None)

def normalized(mid='1', ts=TIME, text='请跟进网关', platform='wechat', group='g1'):
    return {'platform': platform, 'group_id': group, 'group_name': '测试群', 'evidence_id': 'fixture:' + mid,
            'timestamp': ts, 'text': text, 'type': '文本' if platform == 'wechat' else '4',
            'sender': 'fixture', 'sender_raw_id': 1, 'sender_id': 1, 'sender_verified': False,
            'body_available': True, 'source_shard': 'fixture.db', 'message_id': int(mid), 'server_id': mid}

def fake_backend(home, args):
    if args[0] == 'validate':
        return {'ok': True, 'data': {'valid': True}}
    payload = json.loads(Path(args[1]).read_text('utf-8'))
    sid = args[args.index('--session-id') + 1]
    path = db_path(home, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    try:
        c.execute('CREATE TABLE IF NOT EXISTS message(id INTEGER PRIMARY KEY,platform_message_id TEXT UNIQUE,ts INTEGER,type INTEGER,content TEXT)')
        for row in payload['messages']:
            c.execute('INSERT OR IGNORE INTO message(platform_message_id,ts,type,content) VALUES(?,?,?,?)',
                      (row['platformMessageId'], row['timestamp'], row['type'], row['content']))
        c.commit()
    finally:
        c.close()
    return {'ok': True}

class UnifiedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='im-hub-test-')
        self.root = Path(self.tmp.name)
        self.home = self.root / 'private'
        initialize(self.home)
        self.i = 0
    def tearDown(self):
        self.tmp.cleanup()
    def put(self, values, raw=False):
        self.i += 1
        p = self.root / ('input-' + str(self.i))
        p.write_bytes(values if raw else ''.join(canonical(x) + '\n' for x in values).encode('utf-8'))
        return p
    def add(self, values=None, s=None, **kwargs):
        p = self.put(values or [normalized()])
        return ingest(self.home, p, s or spec(), OBSERVED, backend=fake_backend, **kwargs)
    def test_initialization_idempotent(self):
        self.assertTrue(initialize(self.home)['already_exists'])
    def test_nonempty_home_rejected(self):
        with self.assertRaises(IMError): initialize(self.root)
    def test_repeat_batch_and_unchanged_source(self):
        p = self.put([normalized()]); before = digest(p.read_bytes())
        a = ingest(self.home, p, spec(), OBSERVED, backend=fake_backend)
        b = ingest(self.home, p, spec(), OBSERVED, backend=fake_backend)
        self.assertEqual((a['new_records'], b['new_records'], b['duplicates']), (1, 0, 1))
        self.assertEqual(before, digest(p.read_bytes()))
    def test_overlap_only_adds_new_message(self):
        self.add([normalized('1')]); result = self.add([normalized('1'), normalized('2')])
        self.assertEqual((result['new_records'], result['duplicates']), (1, 1))
    def test_same_second_same_text_different_ids(self):
        self.add([normalized('1'), normalized('2')]); self.assertEqual(query_messages(self.home)['count'], 2)
    def test_identity_conflict_rejected_before_backend(self):
        self.add([normalized('1')])
        with self.assertRaisesRegex(IMError, 'IDENTITY_CONTENT_CONFLICT'):
            self.add([normalized('1', text='changed')])
        self.assertEqual(query_messages(self.home)['items'][0]['text'], '请跟进网关')
    def test_account_namespace_isolation(self):
        self.add(s=spec(account='a')); self.add(s=spec(account='b'))
        self.assertEqual(query_messages(self.home)['count'], 2)
        self.assertEqual(query_messages(self.home, account='a')['count'], 1)
    def test_platform_isolation(self):
        self.add(); self.add([normalized(platform='kim')], spec(platform='kim'))
        self.assertEqual(query_messages(self.home, platform='kim')['count'], 1)
    def test_synthetic_excluded_by_default(self):
        self.add(s=spec(data='synthetic'))
        self.assertEqual(query_messages(self.home)['count'], 0)
        self.assertEqual(query_messages(self.home, include_synthetic=True)['count'], 1)
    def test_pagination_same_second(self):
        self.add([normalized(str(i)) for i in range(8)])
        found=[]; cursor=None
        while True:
            page=query_messages(self.home,limit=3,cursor=cursor); found += [x['message_key'] for x in page['items']]
            if not page['has_more']: break
            cursor=page['next_cursor']
        self.assertEqual(len(found),8); self.assertEqual(len(set(found)),8)
    def test_cursor_scope_binding(self):
        self.add([normalized('1'),normalized('2')]); page=query_messages(self.home,limit=1)
        with self.assertRaisesRegex(IMError,'CURSOR_SCOPE_MISMATCH'):
            query_messages(self.home,keyword='网关',cursor=page['next_cursor'])
    def test_cursor_detects_new_dataset(self):
        self.add([normalized('1'),normalized('2')]); page=query_messages(self.home,limit=1)
        self.add([normalized('3')])
        with self.assertRaisesRegex(IMError,'CURSOR_STALE'):
            query_messages(self.home,cursor=page['next_cursor'])
    def test_bad_cursor_safe_error(self):
        with self.assertRaises(IMError):query_messages(self.home,cursor='not-json')
    def test_literal_keyword_not_sql_or_like_pattern(self):
        self.add([normalized(text="contains % and _")])
        self.assertEqual(query_messages(self.home,keyword='%')['count'],1)
        self.assertEqual(query_messages(self.home,keyword="' OR 1=1--")['count'],0)
    def test_timezone_required(self):
        with self.assertRaises(IMError):iso_epoch('2026-09-15T12:00:00')
    def test_half_open_window(self):
        self.add([normalized('1',TIME),normalized('2',TIME+1)])
        self.assertEqual(query_messages(self.home,since=TIME,until=TIME+1)['count'],1)
    def test_empty_window_valid_but_not_complete_history(self):
        self.add(since=TIME+1,until=TIME+2)
        status=source_status(self.home)['items'][0]
        self.assertEqual(status['stored_records'],0); self.assertIsNone(status['complete_through'])
    def test_unknown_type_kept(self):
        r=normalized();r['type']='unobserved';self.add([r])
        row=query_messages(self.home)['items'][0]
        self.assertEqual(row['type'],99);self.assertEqual(row['body_state'],'partial')
    def test_source_mismatch_not_zero_messages(self):
        p=self.put([normalized(group='other')])
        with self.assertRaisesRegex(IMError,'CONVERSATION_NOT_PRESENT'):
            ingest(self.home,p,spec(),OBSERVED,backend=fake_backend)
    def test_hash_guard(self):
        with self.assertRaisesRegex(IMError,'SOURCE_HASH_MISMATCH'):
            self.add(expected_sha256='0'*64)
    def test_dry_run_writes_nothing(self):
        before={str(p):digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}
        self.add(dry_run=True)
        after={str(p):digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
    def test_query_does_not_invoke_backend_or_write(self):
        self.add();before={str(p):digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}
        with patch('im_hub.store.run_chatlab',side_effect=AssertionError('query invoked backend')):
            query_messages(self.home,keyword='网关')
        after={str(p):digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
    def test_query_readonly_connection(self):
        c=readonly(self.home/'index.sqlite3')
        try:
            with self.assertRaises(sqlite3.OperationalError):c.execute('DELETE FROM streams')
        finally:c.close()
    def test_partial_backend_failure_can_recover(self):
        p=self.put([normalized()])
        def fail(home,args):
            result=fake_backend(home,args)
            if args[0]=='import':raise IMError('INJECTED_AFTER_BACKEND_COMMIT')
            return result
        with self.assertRaises(IMError):ingest(self.home,p,spec(),OBSERVED,backend=fail)
        self.assertEqual(query_messages(self.home)['count'],0)
        self.assertEqual(source_status(self.home)['items'][0]['failed_or_pending_batches'],1)
        result=ingest(self.home,p,spec(),OBSERVED,backend=fake_backend)
        self.assertEqual(result['new_records'],1)
        self.assertEqual(query_messages(self.home)['count'],1)
    def test_body_tamper_detected(self):
        self.add();c=sqlite3.connect(db_path(self.home,stream_key(spec())))
        c.execute("UPDATE message SET content='tampered'");c.commit();c.close()
        with self.assertRaisesRegex(IMError,'PROVENANCE_DRIFT'):query_messages(self.home)
    def test_writer_lock_busy(self):
        with writer(self.home):
            with self.assertRaisesRegex(IMError,'WRITER_BUSY'):
                with writer(self.home):pass
    def test_old_replay_cannot_refresh_observed_at(self):
        p=self.put([normalized()]);ingest(self.home,p,spec(),OBSERVED,backend=fake_backend)
        result=ingest(self.home,p,spec(),'2026-09-15T12:01:00+00:00',backend=fake_backend)
        self.assertEqual(result['source_observed_at'],OBSERVED)
    def test_freshness_separate_from_completeness(self):
        self.add();row=source_status(self.home,max_age=10**9)['items'][0]
        self.assertEqual(row['freshness'],'fresh');self.assertEqual(row['history_completeness'],'partial')
        self.assertEqual(source_status(self.home,max_age=0)['items'][0]['freshness'],'stale')
    def test_export_and_triage_never_commit_business_state(self):
        self.add();result=export_packet(self.home,{},analyze=True)
        self.assertEqual(result['candidate_count'],1);self.assertFalse(result['business_state_committed'])
        self.assertTrue(Path(result['path']).is_file())
    def test_own_analysis_packet_validates_without_business_commit(self):
        self.add(); result=export_packet(self.home,{},analyze=True)
        validated=validate_candidates(self.home,Path(result['path']))
        self.assertEqual(validated['candidates_checked'],1)
        self.assertFalse(validated['semantic_claims_verified'])
        self.assertFalse(validated['business_state_committed'])
    def test_deleted_physical_message_is_error_not_empty(self):
        self.add();c=sqlite3.connect(db_path(self.home,stream_key(spec())))
        c.execute('DELETE FROM message');c.commit();c.close()
        with self.assertRaisesRegex(IMError,'CHATLAB_RECORD_PROVENANCE_DRIFT'):
            query_messages(self.home)
    def test_wecom_representations_share_bound_stream(self):
        a=spec('wecom','wecom-json');b=spec('wecom','wecom-native')
        self.assertEqual(stream_key(a),stream_key(b))
        r={'group_label':'测试群','source_id_fields':{'field_1':'1','field_2':'2','field_4':'4','field_6':'66'},
           'metadata_fields_available':True,'text':'收到','timestamp_field12_raw':TIME,'content_complete':True}
        rows,coverage=normalize(canonical([r]).encode(),a)
        p=self.put(canonical([r]).encode(),raw=True)
        ingest(self.home,p,a,OBSERVED,backend=fake_backend)
        binary=self.put(b'fixture-native-representation',raw=True)
        with patch('im_hub.store.normalize',return_value=(rows,coverage)):
            result=ingest(self.home,binary,b,OBSERVED,backend=fake_backend)
        self.assertEqual((result['new_records'],result['duplicates']),(0,1))
    def test_invalid_candidate_reference_rejected(self):
        p=self.put(canonical({'candidates':[{'title':'fixture','supporting_message_ids':['0'*64]}]}).encode(),raw=True)
        with self.assertRaises(IMError):validate_candidates(self.home,p)
    def test_tim_snapshot_identity_and_new_export_guard(self):
        blob='消息对象:测试群\n2026-09-15 上午 08:00:00 Alice(1)\n收到\n2026-09-15 上午 08:00:00 Alice(1)\n收到\n'.encode()
        s=spec('qq','tim-txt');p=self.put(blob,raw=True)
        first=ingest(self.home,p,s,OBSERVED,backend=fake_backend)
        self.assertEqual(first['new_records'],2)
        p2=self.put(blob+b'\n',raw=True)
        with self.assertRaisesRegex(IMError,'TIM_CROSS_EXPORT'):
            ingest(self.home,p2,s,OBSERVED,backend=fake_backend)
    def test_wecom_binding_and_provisional_sender(self):
        r={'group_label':'测试群','source_id_fields':{'field_1':'1','field_2':'2','field_4':'4','field_6':'66'},
           'metadata_fields_available':True,'text':'收到','timestamp_field12_raw':TIME,'content_complete':True}
        rows,cover=normalize(canonical([r]).encode(),spec('wecom','wecom-json'))
        self.assertEqual(rows[0]['identity_quality'],'provisional_native_fields')
        self.assertFalse(rows[0]['sender_verified']);self.assertEqual(cover['scope'],'sampled_history')
        r['source_id_fields']['field_6']='bad'
        with self.assertRaises(IMError):normalize(canonical([r]).encode(),spec('wecom','wecom-json'))
    def test_qce_large_string_id_and_numeric_rejection(self):
        p={'metadata':{'name':'QQChatExporter'},'chatInfo':{'peerUid':'g1','selfUin':'fixture-uin','type':'group'},
           'messages':[{'id':'9007199254740993123','type':'text','timestamp':TIME*1000,
                        'sender':{'uin':'fixture-sender'},'content':{'text':'fixture','elements':[]}}]}
        s=spec('qq','qce-json');rows,_=normalize(canonical(p).encode(),s)
        self.assertEqual(rows[0]['native_message_id'],'9007199254740993123')
        p['messages'][0]['id']=9007199254740993123
        with self.assertRaises(IMError):normalize(canonical(p).encode(),s)
    def test_cli_errors_are_json_without_raw_exception(self):
        out=io.StringIO()
        with redirect_stdout(out):code=main(['--home',str(self.home),'query','--limit','500'])
        self.assertEqual(code,2);self.assertEqual(json.loads(out.getvalue())['error']['code'],'LIMIT_MUST_BE_1_TO_200')

if __name__=='__main__':unittest.main(verbosity=2)
