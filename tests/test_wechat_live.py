"""Synthetic SQLCipher-style page/WAL fixtures, never touches a real client."""
import hashlib,hmac,json,os,sqlite3,struct,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
try:
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
from im_hub.common import IMError,canonical,initialize,stamp
from im_hub.collection import collect
from im_hub.database_readers import prepare_profile
from im_hub.wechat_live import PAGE,RESERVE,checksum,wal_committed,mac_key,decrypt_page,decrypt_snapshot
from im_hub.query import query_messages
from test_core import fake_backend

T=1704153600
ROOM='synthetic@chatroom'
TABLE='Msg_'+hashlib.md5(ROOM.encode()).hexdigest()

def reserved_fixture(path):
    c=sqlite3.connect(path)
    c.execute('CREATE TABLE "'+TABLE+'"(local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,sort_seq INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,compress_content TEXT)')
    c.execute('INSERT INTO "'+TABLE+'" VALUES(1,10,1,1,2,?, ?,NULL)',(T,'[合成] 原生增量'))
    c.commit();c.close()
    data=bytearray(path.read_bytes());data[20]=80
    # This tiny fixture has leaf B-tree pages, no freelist, overflow or fragmentation.
    for offset in range(0,len(data),PAGE):
        h=offset+(100 if offset==0 else 0)
        assert data[h]==13 and data[h+1:h+3]==b'\x00\x00'
        count=struct.unpack('>H',data[h+3:h+5])[0];start=struct.unpack('>H',data[h+5:h+7])[0]
        data[offset+start-80:offset+PAGE-80]=data[offset+start:offset+PAGE]
        data[offset+PAGE-80:offset+PAGE]=b'\x00'*80
        data[h+5:h+7]=struct.pack('>H',start-80)
        for i in range(count):
            pos=h+8+2*i;cell=struct.unpack('>H',data[pos:pos+2])[0]
            data[pos:pos+2]=struct.pack('>H',cell-80)
    return bytes(data)

def encrypt_page(plain,number,key,salt):
    iv=os.urandom(16);begin=16 if number==1 else 0
    enc=Cipher(algorithms.AES(key),modes.CBC(iv)).encryptor()
    encrypted=enc.update(plain[begin:PAGE-80])+enc.finalize()
    part=(salt if number==1 else b'')+encrypted+iv
    auth=hashlib.pbkdf2_hmac('sha512',key,bytes(v^0x3a for v in salt),2,32)
    return part+hmac.new(auth,part[begin:]+struct.pack('<I',number),hashlib.sha512).digest()

def encrypt_db(plain,key,salt):
    return b''.join(encrypt_page(plain[i:i+PAGE],i//PAGE+1,key,salt) for i in range(0,len(plain),PAGE))

def wal(frames,endian='<'):
    magic=0x377f0682 if endian=='<' else 0x377f0683
    h=struct.pack('>IIIIII',magic,3007000,PAGE,0,123,456);ck=checksum(h,endian)
    out=h+struct.pack('>II',*ck)
    for pg,commit,body in frames:
        head=struct.pack('>IIII',pg,commit,123,456)
        ck=checksum(body,endian,checksum(head[:8],endian,ck));out+=head+struct.pack('>II',*ck)+body
    return out

@unittest.skipUnless(HAS_CRYPTO, 'Optional crypto extra not installed')
class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.home=self.root/'state';initialize(self.home)
        self.account=self.root/'account';self.account.mkdir()
        self.plain=reserved_fixture(self.root/'fixture.db')
        self.key=os.urandom(32);self.salt=os.urandom(16)
        self.raw=encrypt_db(self.plain,self.key,self.salt)
        self.db=self.account/'message_0.db';self.db.write_bytes(self.raw)
        self.keys=self.root/'existing.json';self.keys.write_text(canonical({'message/message_0.db':self.key.hex()}),'utf-8')
        self.p={'enabled':True,'transport':'wechat-live','platform':'wechat','adapter':'database-json',
          'account_namespace':'synthetic','conversation_id':ROOM,'conversation_name':'测试群','source_epoch':'fixture','data_class':'synthetic',
          'account_directory':str(self.account),'existing_key_file':str(self.keys),'key_policy':'existing-only',
          'initial_since':stamp(T-100),'shards':[{'id':'s0','path':str(self.db),'key_id':'message/message_0.db'}]}
        self.config=self.root/'sources.json';self.config.write_text(canonical({'version':1,'sources':{'test':self.p}}),'utf-8')
    def tearDown(self):self.tmp.cleanup()
    def collect(self):return collect(self.home,self.config,'test',until=stamp(T+100),backend=fake_backend)
    def test_full_pipeline_authenticates_and_replays(self):
        before=self.db.read_bytes();a=self.collect();b=self.collect()
        self.assertEqual((a['new_records'],b['new_records']),(1,0));self.assertEqual(before,self.db.read_bytes())
        result=query_messages(self.home,include_synthetic=True,max_age=60)
        self.assertEqual(result['count'],1);self.assertEqual(result['coverage'][0]['freshness'],'fresh')
        self.assertEqual(result['items'][0]['source_kind'],'authenticated_local_database')
        self.assertEqual(list((self.home/'runtime').glob('wechat-read-*')),[])
    def test_wrong_key_fails_not_empty(self):
        self.keys.write_text(canonical({'message/message_0.db':os.urandom(32).hex()}),'utf-8')
        with self.assertRaisesRegex(IMError,'AUTHENTICATION'):self.collect()
        self.assertEqual(query_messages(self.home,include_synthetic=True)['count'],0)
    def test_missing_key_never_extracts(self):
        self.keys.write_text('{}','utf-8')
        with self.assertRaisesRegex(IMError,'EXISTING_KEY_NOT_AVAILABLE'):self.collect()
    def test_changed_page_mac_denied(self):
        damaged=bytearray(self.raw);damaged[4096+30]^=1;self.db.write_bytes(damaged)
        with self.assertRaisesRegex(IMError,'AUTHENTICATION'):self.collect()
    def test_key_policy_required(self):
        del self.p['key_policy']
        with self.assertRaisesRegex(IMError,'EXISTING_KEY_ONLY'):prepare_profile(self.root,self.p)
    def test_committed_wal_only(self):
        page=self.raw[PAGE:PAGE*2]
        for endian in ('<','>'):
            chosen,meta=wal_committed(wal([(2,2,page),(2,0,b'X'*PAGE)],endian))
            self.assertEqual(chosen[2],page);self.assertEqual(meta['committed_frames'],1);self.assertTrue(meta['tail_ignored'])
    def test_no_commit_wal_ignored(self):
        chosen,meta=wal_committed(wal([(2,0,self.raw[PAGE:2*PAGE])]))
        self.assertEqual(chosen,{});self.assertEqual(meta['committed_frames'],0)
    def test_bad_wal_header_rejected(self):
        broken=bytearray(wal([]));broken[24]^=1
        with self.assertRaisesRegex(IMError,'HEADER_CHECKSUM'):wal_committed(bytes(broken))
    def test_bad_frame_checksum_stops_at_previous_commit(self):
        page=self.raw[PAGE:2*PAGE];data=bytearray(wal([(2,2,page),(2,2,page)]));data[-1]^=1
        chosen,meta=wal_committed(bytes(data));self.assertEqual(meta['committed_frames'],1)
    def test_truncated_tail_ignored(self):
        page=self.raw[PAGE:2*PAGE];_,meta=wal_committed(wal([(2,2,page)])+b'tail')
        self.assertTrue(meta['tail_ignored']);self.assertEqual(meta['committed_frames'],1)
    def test_committed_wal_page_is_authenticated(self):
        page=bytearray(self.raw[PAGE:2*PAGE]);page[1]^=1
        Path(str(self.db)+'-wal').write_bytes(wal([(2,2,bytes(page))]))
        with self.assertRaisesRegex(IMError,'AUTHENTICATION'):self.collect()
    def test_source_modified_during_capture_is_retry(self):
        from im_hub.wechat_live import stable_bytes
        with patch('im_hub.wechat_live.stat_signature',side_effect=[(1,2,3,4),None,None,(1,2,3,5),None,None]):
            with self.assertRaisesRegex(IMError,'SOURCE_CHANGED'):stable_bytes(self.db)
    def test_new_native_id_same_second_kept(self):
        self.collect()
        file=self.root/'mutable.db';file.write_bytes(self.plain)
        c=sqlite3.connect(file);c.execute('INSERT INTO "'+TABLE+'" VALUES(2,11,1,2,2,?, ?,NULL)',(T,'[合成] 原生增量'));c.commit();c.close()
        self.db.write_bytes(encrypt_db(file.read_bytes(),self.key,self.salt))
        self.assertEqual(self.collect()['new_records'],1)
        self.assertEqual(query_messages(self.home,include_synthetic=True)['count'],2)
