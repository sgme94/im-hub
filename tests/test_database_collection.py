"""Synthetic databases only. Never open an actual IM client or its data."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from im_hub.acquisition import collection_status
from im_hub.adapters import normalize, spec_for
from im_hub.collection import collect
from im_hub.common import IMError, canonical, digest, initialize, stamp, writer
from im_hub.database_readers import prepare_profile, read_database, snapshot
from im_hub.query import query_messages, source_status
from im_hub.store import ingest
from test_core import fake_backend

T = 1704153600
GROUP = '123'
WG = 'fixture@chatroom'


def create_kim(path: Path):
    c = sqlite3.connect(path)
    c.executescript('CREATE TABLE "group"(id INTEGER PRIMARY KEY,name TEXT,sessionID INTEGER);'
                    'CREATE TABLE message(id INTEGER PRIMARY KEY,sender INTEGER,senderName TEXT,sendTime INTEGER,'
                    'contentType INTEGER,content TEXT,sessionID INTEGER,msgIdx INTEGER);')
    c.execute('INSERT INTO "group" VALUES(?,?,?)', (123, '合成工作群', 456))
    c.commit(); c.close()


def add_kim(path: Path, mid=1, ts=T, text='请验证采集', kind=4, body=None, session=456):
    c = sqlite3.connect(path)
    c.execute('INSERT INTO message VALUES(?,?,?,?,?,?,?,?)',
              (mid, 10, 'Fixture', ts, kind, canonical(body or {'content': [{'type': 0, 'text': text}]}), session, mid))
    c.commit(); c.close()


def create_wx(path: Path, mid=1, ts=T, text='合成微信', kind=1):
    table = 'Msg_' + hashlib.md5(WG.encode()).hexdigest()
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE "' + table + '"(local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,sort_seq INTEGER,'
              'real_sender_id INTEGER,create_time INTEGER,message_content TEXT,compress_content TEXT)')
    c.execute('INSERT INTO "' + table + '" VALUES(?,?,?,?,?,?,?,?)', (mid, mid + 100, kind, mid, 2, ts, text, None))
    c.commit(); c.close()


class DatabaseCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='im-hub-db-synthetic-')
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'; initialize(self.home)
        self.account = self.root / 'account'; self.account.mkdir()
        self.db = self.account / 'user.db'; create_kim(self.db); add_kim(self.db)
        self.p = {'enabled': True, 'transport': 'kim-sqlite', 'platform': 'kim', 'adapter': 'database-json',
                  'account_namespace': 'fixture-account', 'conversation_id': GROUP, 'conversation_name': '合成工作群',
                  'source_epoch': 'fixture-db-v1', 'data_class': 'synthetic', 'account_directory': str(self.account),
                  'database': str(self.db), 'expected_session_id': 456, 'initial_since': stamp(T-100),
                  'overlap_seconds': 10, 'max_records': 100, 'timeout_seconds': 2}
        self.config = self.root / 'sources.json'; self.save()
    def tearDown(self):
        self.tmp.cleanup()
    def save(self):
        self.config.write_text(canonical({'version': 1, 'sources': {'test': self.p}}), 'utf-8')
    def run_collect(self, until=T+20, **kwargs):
        return collect(self.home, self.config, 'test', backend=kwargs.pop('backend', fake_backend), until=stamp(until), **kwargs)
    def status(self):
        return collection_status(self.home)['items'][0]
    def query(self, **kw):
        return query_messages(self.home, include_synthetic=True, **kw)
    def wx(self, shards=1, text='合成微信', kind=1):
        self.p.update({'platform': 'wechat', 'transport': 'wechat-sqlite', 'conversation_id': WG,
                       'shards': [{'id': 'shard-'+str(i), 'path': str(self.account/('message_'+str(i)+'.db'))} for i in range(shards)]})
        self.p.pop('database'); self.p.pop('expected_session_id')
        for shard in self.p['shards']:
            create_wx(Path(shard['path']), text=text, kind=kind)
        self.save()
    def test_live_kim_first_and_replay(self):
        before=digest(self.db.read_bytes())
        a=self.run_collect(); b=self.run_collect(reconcile=True)
        self.assertEqual((a['new_records'], b['new_records'], b['duplicates']), (1,0,1))
        self.assertEqual(self.query()['count'],1)
        self.assertEqual(digest(self.db.read_bytes()),before)
        self.assertIsNone(a['client_complete_through'])
    def test_new_same_second_native_ids_are_preserved(self):
        self.run_collect(until=T+2)
        add_kim(self.db,mid=2,ts=T)
        result=self.run_collect(until=T+3)
        self.assertEqual(result['new_records'],1);self.assertEqual(self.query()['count'],2)
    def test_incremental_overlap_and_local_checkpoint(self):
        self.run_collect(until=T+20)
        add_kim(self.db,mid=2,ts=T+15)
        result=self.run_collect(until=T+30)
        self.assertEqual(result['coverage']['requested_since'],stamp(T+10))
        self.assertEqual(result['new_records'],1)
        self.assertEqual(self.status()['local_scan_until'],stamp(T+30))
    def test_reconcile_catches_older_late_sync(self):
        self.run_collect(until=T+20);add_kim(self.db,mid=2,ts=T-20)
        self.assertEqual(self.run_collect(until=T+30)['new_records'],0)
        self.assertEqual(self.run_collect(until=T+30,reconcile=True)['new_records'],1)
    def test_sqlite_wal_committed_not_checkpointed(self):
        c=sqlite3.connect(self.db)
        try:
            self.assertEqual(c.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
            c.execute('PRAGMA wal_autocheckpoint=0')
            c.execute('INSERT INTO message VALUES(?,?,?,?,?,?,?,?)',(2,10,'Fixture',T,4,canonical({'content':[{'type':0,'text':'WAL'}]}),456,2));c.commit()
            self.assertTrue(Path(str(self.db)+'-wal').is_file())
            self.assertEqual(self.run_collect()['new_records'],2)
        finally:c.close()
    def test_sqlite_uncommitted_not_visible(self):
        c=sqlite3.connect(self.db)
        try:
            c.execute('INSERT INTO message VALUES(?,?,?,?,?,?,?,?)',(2,10,'Fixture',T,4,'{}',456,2))
            self.assertEqual(self.run_collect()['new_records'],1)
        finally:c.rollback();c.close()
    def test_empty_known_group_is_success_not_history_complete(self):
        c=sqlite3.connect(self.db);c.execute('DELETE FROM message');c.commit();c.close()
        result=self.run_collect();self.assertEqual(result['records'],0)
        self.assertTrue(result['coverage']['local_window_read_complete'])
        self.assertIsNone(result['coverage']['complete_through'])
        self.assertEqual(self.query()['count'],0)
    def test_wrong_group_name(self):
        self.p['conversation_name']='Wrong';self.save()
        with self.assertRaisesRegex(IMError,'CONVERSATION_BINDING'):self.run_collect()
        self.assertIsNone(self.status()['local_scan_until'])
    def test_wrong_native_session(self):
        self.p['expected_session_id']=999;self.save()
        with self.assertRaisesRegex(IMError,'CONVERSATION_BINDING'):self.run_collect()
    def test_account_directory_binding(self):
        self.p['account_directory']=str(self.root);self.save()
        with self.assertRaisesRegex(IMError,'ACCOUNT_DIRECTORY'):self.run_collect()
    def test_other_session_excluded(self):
        add_kim(self.db,mid=2,session=999)
        self.assertEqual(self.run_collect()['records'],1)
    def test_missing_database_not_zero_or_advance(self):
        self.run_collect();self.db.rename(self.account/'saved.db')
        with self.assertRaisesRegex(IMError,'DATABASE_UNAVAILABLE'):self.run_collect(until=T+40)
        self.assertEqual(self.status()['local_scan_until'],stamp(T+20))
        self.assertEqual(self.status()['runs_by_status']['read_failed'],1)
    def test_changed_schema_is_rebind_not_auto_adoption(self):
        self.run_collect();c=sqlite3.connect(self.db);c.execute('ALTER TABLE message ADD COLUMN added TEXT');c.close()
        with self.assertRaisesRegex(IMError,'IDENTITY_OR_SCHEMA_CHANGED'):self.run_collect()
    def test_replaced_source_identity(self):
        self.run_collect();self.db.rename(self.account/'old.db');create_kim(self.db);add_kim(self.db)
        with self.assertRaisesRegex(IMError,'IDENTITY_OR_SCHEMA_CHANGED'):self.run_collect()
    def test_mutated_profile_rejected(self):
        self.run_collect();self.p['source_epoch']='different';self.save()
        with self.assertRaisesRegex(IMError,'SOURCE_PROFILE_CHANGED'):self.run_collect()
    def test_row_limit_fails_no_checkpoint(self):
        add_kim(self.db,mid=2);self.p['max_records']=1;self.save()
        with self.assertRaisesRegex(IMError,'WINDOW_TOO_LARGE'):self.run_collect()
        self.assertIsNone(self.status()['local_scan_until'])
    def test_unknown_type_preserved(self):
        add_kim(self.db,mid=2,kind=987)
        self.run_collect();found=[r for r in self.query()['items'] if r['native_message_id']=='2'][0]
        self.assertEqual(found['type'],99);self.assertEqual(found['body_state'],'partial')
    def test_reply_new_content_and_quote_separated(self):
        body={'replyedMsgId':1,'replyedContent':{'content':[{'type':0,'text':'old-address'}]},
              'replyContent':{'content':[{'type':2,'replyMemberName':'Fixture'},{'type':0,'text':'new-address'}]}}
        add_kim(self.db,mid=2,kind=13,body=body);self.run_collect()
        self.assertEqual(self.query(keyword='old-address')['count'],0)
        msg=self.query(keyword='new-address')['items'][0]
        self.assertEqual(msg['reply_to_source_id'],'1')
        self.assertEqual(msg['extras']['database']['quoted_text'],'old-address')
    def test_partial_backend_commit_resume_before_new_read(self):
        def fail(home,args):
            fake_backend(home,args)
            if args[0]=='import':raise IMError('INJECTED_AFTER_IMPORT')
        with self.assertRaisesRegex(IMError,'INJECTED'):self.run_collect(backend=fail)
        self.assertIsNone(self.status()['local_scan_until']);self.assertEqual(self.query()['count'],0)
        add_kim(self.db,mid=2,ts=T+5)
        with patch('im_hub.acquisition.read_database',side_effect=AssertionError('new read during resume')):
            recovered=self.run_collect(until=T+30)
        self.assertTrue(recovered['resumed_pending']);self.assertFalse(recovered['database_read_performed'])
        self.assertEqual(self.query()['count'],1)
        self.assertEqual(self.run_collect(until=T+30,reconcile=True)['new_records'],1)
    def test_crash_after_store_commit_before_checkpoint(self):
        def committed_then_fail(*args,**kwargs):
            ingest(*args,**kwargs)
            raise IMError('INJECTED_AFTER_STORE_COMMIT')
        with patch('im_hub.acquisition.ingest',side_effect=committed_then_fail):
            with self.assertRaises(IMError):self.run_collect()
        self.assertIsNone(self.status()['local_scan_until'])
        result=self.run_collect()
        self.assertEqual(result['new_records'],0);self.assertTrue(result['resumed_pending'])
    def test_pending_payload_tamper_rejected(self):
        def fail(*args):raise IMError('INJECTED_FAILURE')
        with self.assertRaises(IMError):self.run_collect(backend=fail)
        next((self.home/'acquisitions').glob('*/source.json')).write_text('{}','utf-8')
        with self.assertRaisesRegex(IMError,'PAYLOAD_CHANGED'):self.run_collect()
        self.assertIsNone(self.status()['local_scan_until'])
    def test_pending_resume_respects_narrower_explicit_end(self):
        def fail(*args):raise IMError('INJECTED_FAILURE')
        with self.assertRaises(IMError):self.run_collect(backend=fail)
        with self.assertRaisesRegex(IMError,'EXCEEDS_REQUESTED_END'):self.run_collect(until=T+10)
        self.assertIsNone(self.status()['local_scan_until'])
    def test_query_never_acquires_database(self):
        self.run_collect()
        with patch('im_hub.acquisition.read_database',side_effect=AssertionError('query read source')):
            self.assertEqual(self.query()['count'],1)
    def test_dry_run_no_owned_files_created_or_changed(self):
        before={str(p):digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}
        result=self.run_collect(dry_run=True)
        after={str(p):digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(before,after);self.assertEqual(result['records'],1)
    def test_collection_status_is_readonly_even_before_first_run(self):
        before=set(self.home.iterdir());self.assertEqual(collection_status(self.home)['items'],[])
        self.assertEqual(before,set(self.home.iterdir()))
    def test_collection_lock_conflict(self):
        with writer(self.home,lock_name='.collection.lock'):
            with self.assertRaisesRegex(IMError,'WRITER_BUSY'):self.run_collect()
    def test_until_clock_regression_rejected(self):
        self.run_collect()
        with self.assertRaisesRegex(IMError,'CLOCK_REGRESSION'):self.run_collect(until=T+10)
    def test_source_ro_enforced(self):
        with snapshot(self.db,2) as (c,_,__):
            with self.assertRaises(sqlite3.OperationalError):c.execute('DELETE FROM message')
    def test_wechat_cross_shard_local_ids_not_merged(self):
        self.wx(shards=2);result=self.run_collect()
        self.assertEqual(result['new_records'],2);self.assertEqual(self.query()['count'],2)
    def test_wechat_cache_unknown_freshness_and_no_false_observation(self):
        self.wx();self.run_collect()
        status=source_status(self.home,include_synthetic=True,max_age=10**9)['items'][0]
        self.assertEqual(status['freshness'],'unknown');self.assertIsNone(status['source_observed_at'])
        self.assertIsNotNone(status['cache_observed_at'])
        msg=self.query()['items'][0];self.assertIsNone(msg['source_observed_at']);self.assertIsNotNone(msg['cache_observed_at'])
    def test_wechat_rescans_old_cache_range(self):
        self.wx();self.run_collect()
        path=Path(self.p['shards'][0]['path']);table='Msg_'+hashlib.md5(WG.encode()).hexdigest()
        c=sqlite3.connect(path);c.execute('INSERT INTO "'+table+'" VALUES(?,?,?,?,?,?,?,?)',(2,102,1,2,2,T-30,'late-cache',None));c.commit();c.close()
        result=self.run_collect(until=T+40)
        self.assertTrue(result['cache_full_window_rescan']);self.assertEqual(result['new_records'],1)
    def test_wechat_encrypted_file_is_not_key_fallback(self):
        self.wx();Path(self.p['shards'][0]['path']).write_bytes(b'not-a-plaintext-database')
        with self.assertRaisesRegex(IMError,'NOT_PLAINTEXT_SQLITE'):self.run_collect()
    def test_wechat_binary_body_kept_as_gap(self):
        self.wx(text=b'\x28\xb5\x2f\xfd\x00binary')
        self.run_collect();msg=self.query()['items'][0]
        self.assertIsNone(msg['text']);self.assertEqual(msg['body_state'],'partial')
        self.assertIn('binary_or_compressed_body_not_decoded',msg['content_flags'])
    def test_envelope_wrong_account_and_window_rejected(self):
        p=prepare_profile(self.root,self.p);packet=read_database(p,T-100,T+20)
        s=spec_for('kim','fixture-account',GROUP,'合成工作群','database-json','fixture-db-v1','synthetic')
        with self.assertRaisesRegex(IMError,'WINDOW_MISMATCH'):normalize(canonical(packet).encode(),s,T-99,T+20)
        packet['binding']['account_namespace']='other'
        with self.assertRaisesRegex(IMError,'BINDING_MISMATCH'):normalize(canonical(packet).encode(),s,T-100,T+20)


if __name__=='__main__':
    unittest.main(verbosity=2)
