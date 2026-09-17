"""Synthetic native-directory fixtures. No client GUI, real messages, keys or LLM."""
from __future__ import annotations
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from im_hub.activity import backfill_week, discover_week, week_status, normalize_activity
from im_hub.activity_sources import account_snapshot, discover_account, prepare_accounts
from im_hub.adapters import normalize, spec_for
from im_hub.common import IMError, canonical, digest, initialize, readonly, stamp, stream_key
from im_hub.query import query_messages
from im_hub.store import payload_for
from im_hub.cli import main
from test_core import fake_backend

START=1704067200
END=START+7*86400
BODY='synthetic-message-body-not-metadata'


def kim_fixture(path):
    path.parent.mkdir(parents=True)
    c=sqlite3.connect(path)
    c.executescript('''
    CREATE TABLE session(id INTEGER PRIMARY KEY,type INTEGER,typeID INTEGER,typeName TEXT,lastMsgTime INTEGER,pMsgOffFlag INTEGER,mMsgOffFlag INTEGER);
    CREATE TABLE "group"(id INTEGER PRIMARY KEY,name TEXT,sessionID INTEGER);
    CREATE TABLE user(id INTEGER PRIMARY KEY,name TEXT);
    CREATE TABLE message(id INTEGER PRIMARY KEY,sender INTEGER,senderName TEXT,sendTime INTEGER,contentType INTEGER,content TEXT,sessionID INTEGER,msgIdx INTEGER,sessionType INTEGER);
    ''')
    c.executemany('INSERT INTO session VALUES(?,?,?,?,?,?,?)',[(11,1,15,'same name',START+10,1,1),(12,0,15,'same name',START+20,1,1),(13,5,99,'service',START+30,0,0),(14,0,16,'index-only',START+40,0,0)])
    c.execute('INSERT INTO "group" VALUES(15,?,11)',('same name',));c.executemany('INSERT INTO user VALUES(?,?)',[(15,'same name'),(16,'index-only')])
    body=canonical({'content':[{'type':0,'text':BODY}]})
    c.executemany('INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?)',[(1,15,'fixture',START,4,body,11,1,1),(2,15,'fixture',START,4,body,11,2,1),(3,15,'fixture',END,4,body,11,3,1),(4,15,'fixture',START+20,4,body,12,1,0),(5,99,'service',START+30,4,body,13,1,5)])
    c.execute('ALTER TABLE session ADD COLUMN creater INTEGER')
    c.execute('UPDATE session SET creater=1')
    c.execute('ALTER TABLE message ADD COLUMN receiver INTEGER')
    c.execute('UPDATE message SET receiver=1')
    c.execute("INSERT INTO user VALUES(1,'self-fixture')")
    c.commit();c.close()


def wechat_fixture(root):
    root.mkdir()
    contact=root/'contact.db';session=root/'session.db';messages=root/'message.db'
    c=sqlite3.connect(contact)
    c.executescript('CREATE TABLE contact(username TEXT,local_type INTEGER,remark TEXT,nick_name TEXT,verify_flag INTEGER,chat_room_notify INTEGER);CREATE TABLE chat_room(username TEXT);CREATE TABLE biz_info(username TEXT);')
    c.executemany('INSERT INTO contact VALUES(?,?,?,?,?,?)',[('room@chatroom',2,'same name','',0,1),('peer',1,'same name','',0,0),('gh_service',1,'service','',24,1),('empty_peer',1,'index-only','',0,0)])
    c.execute('INSERT INTO chat_room VALUES(?)',('room@chatroom',));c.execute('INSERT INTO biz_info VALUES(?)',('gh_service',));c.commit();c.close()
    c=sqlite3.connect(session)
    c.executescript('CREATE TABLE SessionTable(username TEXT,last_timestamp INTEGER,is_hidden INTEGER);CREATE TABLE SessionNoContactInfoTable(username TEXT,session_title TEXT);')
    c.executemany('INSERT INTO SessionTable VALUES(?,?,?)',[(n,START+50,1) for n in ('room@chatroom','peer','gh_service','empty_peer')]);c.commit();c.close()
    c=sqlite3.connect(messages)
    c.execute('CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY,is_session INTEGER)')
    c.executemany('INSERT INTO Name2Id VALUES(?,1)',[(n,) for n in ('room@chatroom','peer','gh_service')])
    for n in ('room@chatroom','peer','gh_service'):
        table='Msg_'+hashlib.md5(n.encode()).hexdigest()
        c.execute('CREATE TABLE '+table+'(local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,compress_content TEXT)')
        c.execute('INSERT INTO '+table+' VALUES(1,101,1,2,?,?,NULL)',(START+50,BODY))
    c.commit();c.close()
    return {'session':session,'contact':contact,'messages':messages}


@contextmanager
def fake_snapshot(p,home):
    if p['platform']=='kim':
        with account_snapshot(p,home) as sources:yield sources
        return
    sources=[]
    try:
        for f in p['files']:
            c=readonly(Path(f['path']));c.execute('BEGIN')
            sources.append({'id':f['id'],'role':f['role'],'con':c,'identity':[1,17],'observed_at':stamp(END+60)})
        yield sources
    finally:
        for s in sources:s['con'].close()


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='im-hub-activity-fixture-');self.root=Path(self.tmp.name)
        self.home=self.root/'home';initialize(self.home);self.kim=self.root/'kim/user.db';kim_fixture(self.kim)
        self.wx=wechat_fixture(self.root/'wx')
        base={'enabled':True,'account_namespace':'fixture-account','source_epoch':'fixture-epoch','data_class':'real','include_types':['group','direct']}
        self.accounts={'kim':{**base,'platform':'kim','transport':'kim-account','database':str(self.kim),'account_directory':str(self.kim.parent),'self_user_id':1},
                       'wx':{**base,'platform':'wechat','transport':'wechat-account','account_directory':str(self.root/'wx'),'existing_key_file':str(self.root/'unused-fixture-keys.json'),'key_policy':'existing-only',
                             'files':[{'id':role,'role':role,'path':str(path),'key_id':role+'/'+path.name} for role,path in self.wx.items()]},
                       'qq':{**base,'platform':'qq','transport':'not-scanned'},'wecom':{**base,'platform':'wecom','transport':'not-scanned'}}
        self.cfg=self.root/'accounts.json';self.save()
    def tearDown(self):self.tmp.cleanup()
    def save(self):self.cfg.write_text(canonical({'schema':'im-hub-active-accounts/1','accounts':self.accounts}),'utf-8')
    def discovery(self):return discover_week(self.home,self.cfg,stamp(START),stamp(END),snapshotter=fake_snapshot)
    def fill(self,id,**kw):return backfill_week(self.home,id,backend=fake_backend,snapshotter=fake_snapshot,**kw)
    def all_files(self):return {str(p.relative_to(self.home)):digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}
    def test_discovery_reports_local_mutation(self):
        before=self.all_files();d=self.discovery()
        self.assertFalse(d['query_only']);self.assertNotEqual(before,self.all_files())
    def test_backfill_and_replay_report_local_mutation(self):
        d=self.discovery();a=self.fill(d['inventory_id']);b=self.fill(d['inventory_id'],replay=True)
        self.assertFalse(a['query_only']);self.assertFalse(b['query_only'])
        saved=json.loads((self.home/'activity'/d['inventory_id']/'last-backfill.json').read_text('utf-8'))
        self.assertFalse(saved['result']['query_only'])
        self.assertTrue(week_status(self.home,d['inventory_id'],verify=True)['query_only'])
    def test_config_hash_matches_single_parsed_read(self):
        original=self.cfg.read_bytes()
        with patch('im_hub.activity_sources.read_blob',side_effect=[original,b'changed']) as read:
            _,hashed=prepare_accounts(self.cfg)
        self.assertEqual(hashed,digest(original));self.assertEqual(read.call_count,1)
    def test_receipt_count_mismatch_is_not_false_coverage(self):
        d=self.discovery();self.fill(d['inventory_id'])
        file=next((self.home/'activity'/d['inventory_id']/'results').glob('*.json'))
        value=json.loads(file.read_text('utf-8'));value['records']+=100;file.write_text(canonical(value),'utf-8')
        before=self.all_files();checked=week_status(self.home,d['inventory_id'],verify=True,details=True)
        self.assertEqual(checked['failed_conversations'],1)
        self.assertIn('ACTIVITY_RECEIPT_COUNT_MISMATCH',[r['error'] for r in checked['items']])
        self.assertEqual(before,self.all_files())
    def test_receipt_stream_mismatch_is_explicit(self):
        d=self.discovery();self.fill(d['inventory_id'])
        file=next((self.home/'activity'/d['inventory_id']/'results').glob('*.json'))
        value=json.loads(file.read_text('utf-8'));value['stream_id']='im-'+'0'*32;file.write_text(canonical(value),'utf-8')
        checked=week_status(self.home,d['inventory_id'],verify=True,details=True)
        self.assertEqual(checked['failed_conversations'],1)
        self.assertIn('ACTIVITY_RECEIPT_STREAM_MISMATCH',[r['error'] for r in checked['items']])
    def test_receipt_content_gaps_not_hidden(self):
        d=self.discovery();self.fill(d['inventory_id'])
        file=next((self.home/'activity'/d['inventory_id']/'results').glob('*.json'))
        value=json.loads(file.read_text('utf-8'));value['body_gap_records']=99;file.write_text(canonical(value),'utf-8')
        checked=week_status(self.home,d['inventory_id'],verify=True,details=True)
        self.assertEqual(checked['failed_conversations'],1)
        self.assertIn('ACTIVITY_RECEIPT_BODY_GAP_MISMATCH',[r['error'] for r in checked['items']])
    def test_groups_direct_service_unknown_scoped(self):
        d=self.discovery();self.assertEqual(d['eligible_conversations'],4);self.assertEqual(d['expected_local_messages'],5)
        self.assertFalse(d['four_platform_complete']);self.assertEqual(d['committed_messages'],0)
        p={x['platform']:x for x in d['platforms']}
        self.assertEqual(p['kim']['service_conversations'],1);self.assertEqual(p['wechat']['service_conversations'],1)
        self.assertEqual(p['wechat']['directory_only_candidates'],1)
        self.assertEqual(p['qq']['account_statuses'][0]['status'],'not_scanned')
    def test_discovery_never_persists_message_bodies(self):
        d=self.discovery();self.assertNotIn(BODY,Path(d['inventory_path']).read_text('utf-8'))
        self.assertFalse(list((self.home/'activity'/d['inventory_id']/'packets').iterdir()))
    def test_folded_and_muted_are_not_excluded(self):
        d=self.discovery();s=week_status(self.home,d['inventory_id'],details=True)
        wx=[e for e in s['items'] if e['platform']=='wechat' and e['activity']=='message_confirmed']
        self.assertEqual(len(wx),3);self.assertTrue(all(e['presentation_flags']['is_hidden']==1 for e in wx))
    def test_halfopen_exact_window(self):
        d=self.discovery();self.fill(d['inventory_id'])
        rows=query_messages(self.home,limit=20)['items']
        self.assertTrue(all(START<=r['timestamp']<END for r in rows));self.assertEqual(sum(r['timestamp']==START for r in rows),2)
    def test_backfill_and_replay_no_duplicates(self):
        d=self.discovery();a=self.fill(d['inventory_id']);b=self.fill(d['inventory_id'],replay=True)
        self.assertEqual((a['new_records_this_call'],b['new_records_this_call']),(5,0))
        self.assertEqual(b['failed_conversations'],0);self.assertEqual(query_messages(self.home,limit=20)['count'],5)
    def test_bounded_resume_skips_committed(self):
        d=self.discovery();a=self.fill(d['inventory_id'],max_conversations=1);self.assertEqual(a['committed_conversations'],1)
        b=self.fill(d['inventory_id'],max_conversations=1);self.assertEqual(b['committed_conversations'],2)
        self.fill(d['inventory_id']);self.assertEqual(week_status(self.home,d['inventory_id'])['pending_conversations'],0)
    def test_group_and_direct_same_id_not_merged(self):
        d=self.discovery();self.fill(d['inventory_id'])
        a=query_messages(self.home,platform='kim',conversation='15',conversation_type='group')['items']
        b=query_messages(self.home,platform='kim',conversation='dm-session:12',conversation_type='direct')['items']
        self.assertEqual((len(a),len(b)),(2,1));self.assertNotEqual(a[0]['stream_id'],b[0]['stream_id'])
    def test_chatlab_direct_type_not_group(self):
        d=self.discovery();self.fill(d['inventory_id'])
        payloads=[json.loads(p.read_text('utf-8')) for p in (self.home/'batches').glob('*/chatlab.json')]
        private=[p for p in payloads if p['meta']['type']=='private'];self.assertEqual(len(private),2)
        self.assertTrue(all('groupId' not in p['meta'] for p in private))
    def test_status_verify_is_read_only(self):
        d=self.discovery();self.fill(d['inventory_id']);before=self.all_files()
        r=week_status(self.home,d['inventory_id'],verify=True,details=True)
        self.assertEqual(r['committed_messages'],5);self.assertEqual(before,self.all_files())
    def test_queries_do_not_access_source(self):
        d=self.discovery();self.fill(d['inventory_id'])
        with patch('im_hub.activity_sources.account_snapshot',side_effect=AssertionError('source read')):
            self.assertEqual(query_messages(self.home,conversation_type='direct')['count'],2)
    def test_readback_drift_is_error(self):
        d=self.discovery();self.fill(d['inventory_id']);row=query_messages(self.home,limit=1)['items'][0]
        from im_hub.common import db_path
        c=sqlite3.connect(db_path(self.home,row['stream_id']));c.execute('DELETE FROM message');c.commit();c.close()
        r=week_status(self.home,d['inventory_id'],verify=True);self.assertGreater(r['failed_conversations'],0)
    def test_inventory_tamper_rejected(self):
        d=self.discovery();Path(d['inventory_path']).write_text('{}','utf-8')
        with self.assertRaisesRegex(IMError,'HASH_MISMATCH'):self.fill(d['inventory_id'])
    def test_packet_tamper_rejected(self):
        d=self.discovery();self.fill(d['inventory_id'])
        p=next((self.home/'activity'/d['inventory_id']/'packets').glob('*.json'));p.write_text('{}','utf-8')
        r=self.fill(d['inventory_id'],replay=True);self.assertEqual(r['failed_this_call'],1)
    def test_changed_index_requires_rediscovery(self):
        d=self.discovery();c=sqlite3.connect(self.kim)
        c.execute('INSERT INTO message VALUES(20,15,?,?,?,?,?,?,?,1)',('fixture',START+100,4,canonical({'content':[]}),11,20,1));c.commit();c.close()
        r=self.fill(d['inventory_id']);self.assertEqual(r['failed_this_call'],1)
        self.assertEqual(r['committed_conversations'],3)
    def test_pending_import_recovers_without_live_source(self):
        d=self.discovery()
        def fail(home,args):
            out=fake_backend(home,args)
            if args[0]=='import':raise IMError('INJECTED_AFTER_IMPORT')
            return out
        a=backfill_week(self.home,d['inventory_id'],backend=fail,snapshotter=fake_snapshot)
        self.assertEqual(a['failed_this_call'],4)
        with patch('im_hub.activity.account_snapshot',side_effect=AssertionError('read')):
            b=self.fill(d['inventory_id'])
        self.assertEqual(b['committed_messages'],5)
    def test_service_bodies_not_backfilled(self):
        d=self.discovery();self.fill(d['inventory_id'])
        rows=query_messages(self.home,limit=20)['items'];self.assertTrue(all(r['conversation_type'] in ('group','direct') for r in rows))
    def test_unknown_directory_category_not_dm(self):
        c=sqlite3.connect(self.kim);c.execute('INSERT INTO session VALUES(20,99,100,?, ?,0,0,1)',('unknown',START+50));c.commit();c.close()
        d=self.discovery();p=next(x for x in d['platforms'] if x['platform']=='kim');self.assertEqual(p['unknown_conversations'],1)
    def test_missing_account_source_kept_as_failure(self):
        self.accounts['kim']['database']=str(self.root/'missing/user.db');self.accounts['kim']['account_directory']=str(self.root/'missing');self.save()
        d=self.discovery();p=next(x for x in d['platforms'] if x['platform']=='kim');self.assertEqual(p['account_statuses'][0]['status'],'failed')
        self.assertFalse(p['eligible_local_backfill_complete'])
    def test_namespace_collision_rejected(self):
        self.accounts['duplicate']={**self.accounts['kim']};self.save()
        with self.assertRaisesRegex(IMError,'DUPLICATE_ACCOUNT_NAMESPACE'):self.discovery()
    def test_key_discovery_not_enabled(self):
        self.accounts['wx']['key_policy']='extract';self.save()
        with self.assertRaisesRegex(IMError,'EXISTING_KEY'):self.discovery()
    def test_new_versions_not_required(self):
        for p in self.accounts.values():p['source_client_version']='unseen'
        self.save();self.assertEqual(self.discovery()['eligible_conversations'],4)
    def test_source_files_unchanged(self):
        before={p:digest(p.read_bytes()) for p in [self.kim,*self.wx.values()]}
        d=self.discovery();self.fill(d['inventory_id'])
        self.assertEqual(before,{p:digest(p.read_bytes()) for p in before})
    def test_inventory_path_traversal_rejected(self):
        with self.assertRaisesRegex(IMError,'INVENTORY_ID'):week_status(self.home,'../../x')
    def test_bad_bounds(self):
        for days in (0,32,True):
            with self.assertRaises(IMError):discover_week(self.home,self.cfg,days=days)
        with self.assertRaises(IMError):discover_week(self.home,self.cfg,stamp(END),stamp(START))
    def test_typed_adapter_required(self):
        with self.assertRaisesRegex(IMError,'TYPE_REQUIRED'):spec_for('kim','a','b','c','activity-json','e','real')
    def test_cli_status_dispatch(self):
        d=self.discovery();out=io.StringIO()
        with redirect_stdout(out):code=main(['--home',str(self.home),'week-status','--inventory',d['inventory_id'],'--details','--limit','2'])
        r=json.loads(out.getvalue());self.assertEqual(code,0);self.assertEqual(len(r['data']['items']),2);self.assertEqual(r['data']['next_offset'],2)
    def test_inbound_kim_session_typeid_is_self_not_peer(self):
        c=sqlite3.connect(self.kim)
        c.execute('UPDATE session SET typeID=1,creater=15 WHERE id=12');c.commit();c.close()
        d=self.discovery();self.fill(d['inventory_id'])
        row=query_messages(self.home,platform='kim',conversation_type='direct')['items'][0]
        self.assertEqual(row['conversation_name'],'same name')
        self.assertNotEqual(row['conversation_name'],'self-fixture')
    def test_kim_wrong_self_is_not_silently_accepted(self):
        self.accounts['kim']['self_user_id']=17;self.save();d=self.discovery()
        p=next(x for x in d['platforms'] if x['platform']=='kim')
        self.assertEqual(p['account_statuses'][0]['error'],'KIM_DIRECT_PARTICIPANTS_NOT_BOUND_TO_ACCOUNT')
    def test_blank_native_directory_row_does_not_hide_active_chats(self):
        c=sqlite3.connect(self.wx['messages']);c.execute("INSERT INTO Name2Id VALUES('',0)");c.commit();c.close()
        d=self.discovery();p=next(x for x in d['platforms'] if x['platform']=='wechat')
        self.assertEqual(p['eligible_conversations'],2);self.assertEqual(p['account_statuses'][0]['invalid_native_name_entries'],1)
    def test_cursor_bound_to_conversation_type(self):
        d=self.discovery();self.fill(d['inventory_id']);page=query_messages(self.home,conversation_type='group',limit=1)
        with self.assertRaisesRegex(IMError,'CURSOR_SCOPE'):query_messages(self.home,conversation_type='direct',cursor=page['next_cursor'])

class CompressedBodyTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        if importlib.util.find_spec('zstandard') is None:self.skipTest('optional activity dependency')
        import zstandard
        self.z=zstandard
    def test_known_and_unknown_frame_size_decode(self):
        from im_hub.activity_sources import activity_body
        for known in (True,False):
            payload=self.z.ZstdCompressor(write_content_size=known).compress('合成文本'.encode('utf-8'))
            text,hashed,flags=activity_body(payload)
            self.assertEqual(text,'合成文本');self.assertEqual(hashed,digest(payload));self.assertFalse(flags)
    def test_decompression_bomb_rejected_before_allocation(self):
        from im_hub.activity_sources import activity_body
        payload=self.z.ZstdCompressor().compress(b'A'*1000001)
        with self.assertRaisesRegex(IMError,'EXCEEDS_BOUND'):activity_body(payload)
    def test_trailing_or_truncated_frame_remains_gap(self):
        from im_hub.activity_sources import activity_body
        payload=self.z.ZstdCompressor().compress(b'fixture text')
        for bad in (payload[:-2],payload+b'extra'):
            text,_,flags=activity_body(bad);self.assertIsNone(text);self.assertIn('compressed_body_decode_failed',flags)
    def test_plain_text_not_reinterpreted(self):
        from im_hub.activity_sources import activity_body
        self.assertEqual(activity_body('fixture')[0],'fixture')
    def test_actual_packet_uses_decompressed_text(self):
        case=ActivityTests();case.setUp()
        try:
            table='Msg_'+hashlib.md5(b'peer').hexdigest();c=sqlite3.connect(case.wx['messages'])
            c.execute('UPDATE '+table+' SET message_content=?',(self.z.ZstdCompressor().compress(BODY.encode()),));c.commit();c.close()
            d=case.discovery();case.fill(d['inventory_id'])
            row=query_messages(case.home,platform='wechat',conversation_type='direct')['items'][0]
            self.assertEqual(row['text'],BODY);self.assertEqual(row['extras']['body_encoding'],'zstandard')
            self.assertFalse(row['content_flags'])
        finally:case.tearDown()

if __name__=='__main__':unittest.main()
