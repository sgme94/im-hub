"""Synthetic acceptance for product operations and native-source envelopes.
Fake clipboard/UI backends prove orchestration, never real-client compatibility.
"""
import copy
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from im_hub.common import IMError, canonical, digest, initialize, read_blob, writer
from im_hub.collection import collect, capabilities
from im_hub.desktop_sources import collect_desktop, desktop_status, prepare_desktop_profile, reconcile_tim
from im_hub.operations import config_check, run_sources, export_all, verify, backup, restore
from im_hub.windows_desktop import validate_profile, acquire_ui
from im_hub.query import query_messages
from im_hub.cli import main
from test_core import fake_backend, normalized, spec, OBSERVED
from im_hub.store import ingest


def varint(n):
    b = bytearray()
    while n > 127: b.append((n & 127) | 128); n >>= 7
    return bytes(b + bytes([n]))


def number(k, n): return varint(k * 8) + varint(n)
def rawfield(k, b): return varint(k * 8 + 2) + varint(len(b)) + b

def native_blob(mid=1, text='[合成测试] 请验证消息', binding=66):
    element = number(1, 0) + rawfield(2, rawfield(1, text.encode('utf-8')))
    payload = b''.join(number(k, v) for k, v in ((1,mid),(2,2),(3,1),(4,4),(6,binding),(7,2),(12,1704153600)))
    payload += rawfield(8, rawfield(1, element))
    return b'22 serialization::archive 19 0 0 1 0 ' + str(len(payload)).encode() + b' ' + payload


def ui_selector(name, control='ButtonControl'):
    return {'name': name, 'control_type': control}


def tim_profile():
    return {'enabled': True, 'transport': 'tim-export', 'platform': 'qq', 'account_namespace': 'fixture',
            'conversation_id': 'fixture-group', 'conversation_name': '测试群', 'source_epoch': 'fixture-only',
            'data_class': 'synthetic', 'export_mode': 'full-history-append-only', 'input': 'tim.txt', 'manifest': 'manifest.json'}


def reviewed_ui(platform='qq'):
    base = {'profile_reviewed': True, 'client_version': '3.5.1.22171' if platform == 'qq' else '5.0.10.6025',
            'window': ui_selector('fixture-client','WindowControl'), 'conversation_header': ui_selector('测试群','TextControl'),
            'navigation': [{'action':'press','selector':ui_selector('消息记录')}], 'allow_activation': False}
    if platform == 'qq':
        base.update({'save_dialog':ui_selector('fixture-save','WindowControl'), 'filename_field':ui_selector('fixture-name','EditControl'),
                     'filetype_field':ui_selector('fixture-type','ComboBoxControl'), 'save_button':ui_selector('保存'),
                     'expected_filename':'测试群.txt','expected_filetype':'文本文件 (*.txt)'})
    else:
        base.update({'message_list':ui_selector('fixture-list','ListControl'), 'select_menu':ui_selector('多选','MenuItemControl'),
                     'selection_indicator':ui_selector('fixture-selection','PaneControl'), 'message_control_type':'ListItemControl', 'max_pages':2})
    return base


class ProductDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='im-hub-delivery-fixture-')
        self.root = Path(self.tmp.name); self.home = self.root / 'state'; initialize(self.home)
        self.profile = tim_profile(); self.config = self.root/'sources.json'
        self.save_tim(['[合成测试] 收到','[合成测试] 收到'])
    def tearDown(self): self.tmp.cleanup()
    def save_tim(self, messages):
        text = '消息对象:测试群\n' + ''.join('2024-01-02 上午 08:00:00 Fixture(1)\n' + x + '\n' for x in messages)
        (self.root/'tim.txt').write_text(text,'utf-8-sig')
        (self.root/'manifest.json').write_text(canonical({'source_id':'tim','observed_at':'2024-01-03T12:00:00+08:00','sha256':digest(read_blob(self.root/'tim.txt'))}),'utf-8')
        self.config.write_text(canonical({'version':1,'sources':{'tim':self.profile}}),'utf-8')
    def run_tim(self, **kwargs):
        return collect(self.home,self.config,'tim',backend=fake_backend,**kwargs)
    def add_rows(self, count=1):
        path=self.root/'normalized.jsonl';path.write_text(''.join(canonical(normalized(str(i)))+'\n' for i in range(1,count+1)), 'utf-8')
        return ingest(self.home,path,spec(),OBSERVED,backend=fake_backend)
    def all_hashes(self):
        return {p.relative_to(self.home).as_posix():digest(p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}
    def test_tim_full_snapshot_repeat(self):
        a=self.run_tim();b=self.run_tim()
        self.assertEqual((a['new_records'],b['new_records'],b['duplicates']),(2,0,2))
    def test_tim_append_same_second_same_body_is_distinct(self):
        self.run_tim();self.save_tim(['[合成测试] 收到']*3);out=self.run_tim()
        self.assertEqual((out['new_records'],out['duplicates']),(1,2))
        self.assertEqual(query_messages(self.home,include_synthetic=True)['count'],3)
    def test_tim_modified_history_rejected(self):
        self.run_tim();self.save_tim(['changed','[合成测试] 收到'])
        with self.assertRaisesRegex(IMError,'NOT_EXACT_APPEND'):self.run_tim()
        self.assertEqual(query_messages(self.home,include_synthetic=True)['count'],2)
    def test_tim_truncated_history_rejected(self):
        self.run_tim();self.save_tim(['[合成测试] 收到'])
        with self.assertRaisesRegex(IMError,'NOT_EXACT_APPEND'):self.run_tim()
    def test_tim_prepend_late_history_rejected(self):
        self.run_tim();self.save_tim(['older','[合成测试] 收到','[合成测试] 收到'])
        with self.assertRaisesRegex(IMError,'NOT_EXACT_APPEND'):self.run_tim()
    def test_tim_bad_manifest_hash_rejected(self):
        (self.root/'tim.txt').write_text('changed','utf-8')
        with self.assertRaisesRegex(IMError,'SOURCE_HASH_MISMATCH'):self.run_tim()
    def test_tim_group_mismatch(self):
        p=prepare_desktop_profile(self.root,self.profile)
        with self.assertRaisesRegex(IMError,'GROUP_MISMATCH'):reconcile_tim('消息对象:Other\n2024-01-02 上午 08:00:00 Fixture(1)\na\n'.encode(),p,None)
    def test_tim_profile_change_stops(self):
        self.run_tim();self.profile['source_epoch']='different';self.save_tim(['[合成测试] 收到']*2)
        with self.assertRaisesRegex(IMError,'PROFILE_CHANGED'):self.run_tim()
    def test_pending_import_resumes_without_reading_source(self):
        def fail(home,args):
            result=fake_backend(home,args)
            if args[0]=='import':raise IMError('SYNTHETIC_CRASH_AFTER_IMPORT')
            return result
        with self.assertRaises(IMError):collect(self.home,self.config,'tim',backend=fail)
        self.assertEqual(query_messages(self.home,include_synthetic=True)['count'],0)
        (self.root/'tim.txt').unlink()
        result=self.run_tim();self.assertTrue(result['resumed_pending']);self.assertEqual(result['new_records'],2)
    def test_pending_payload_tamper_stops(self):
        with self.assertRaises(IMError):
            collect(self.home,self.config,'tim',backend=lambda *_:(_ for _ in ()).throw(IMError('SYNTHETIC_FAILURE')))
        next((self.home/'desktop').glob('*/source.json')).write_text('{}','utf-8')
        with self.assertRaisesRegex(IMError,'SOURCE_HASH_MISMATCH'):self.run_tim()
    def test_desktop_status_readonly(self):
        before=self.all_hashes();self.assertEqual(desktop_status(self.home)['items'],[]);self.assertEqual(before,self.all_hashes())
    def test_profile_dry_run_no_writes(self):
        before=self.all_hashes();out=self.run_tim(dry_run=True)
        self.assertTrue(out['profile_valid']);self.assertEqual(before,self.all_hashes())
    def test_native_wecom_cli_integration_fake_capture(self):
        class FakeClipboard:
            def capture(self, baseline):
                self_baseline=baseline
                if self_baseline!=100:raise IMError('INVALID_BASELINE')
                return native_blob(),{'captured_at':'2024-01-03T12:00:00+08:00','clipboard_owner_verified':True}
        p={**self.profile,'transport':'wecom-clipboard','platform':'wecom','binding':'66'}
        a=collect_desktop(self.home,self.root,'wecom',p,backend=fake_backend,after_sequence=100,driver=FakeClipboard())
        b=collect_desktop(self.home,self.root,'wecom',p,backend=fake_backend,after_sequence=100,driver=FakeClipboard())
        self.assertEqual((a['new_records'],b['new_records'],b['duplicates']),(1,0,1))
        row=query_messages(self.home,include_synthetic=True)['items'][0]
        self.assertFalse(row['native_global_id_verified']);self.assertFalse(row['sender_verified'])
    def test_native_clipboard_requires_baseline(self):
        p={**self.profile,'transport':'wecom-clipboard','platform':'wecom','binding':'66'}
        with self.assertRaisesRegex(IMError,'EXPLICIT_COPY_BASELINE'):
            collect_desktop(self.home,self.root,'wecom',p,backend=fake_backend)
    def test_native_group_binding_mismatch(self):
        class Fake:
            def capture(self,baseline):return native_blob(binding=67),{'captured_at':'2024-01-03T12:00:00+08:00'}
        p={**self.profile,'transport':'wecom-clipboard','platform':'wecom','binding':'66'}
        with self.assertRaises(ValueError):collect_desktop(self.home,self.root,'wecom',p,backend=fake_backend,after_sequence=1,driver=Fake())
    def test_ui_driver_requires_optin(self):
        p={**self.profile,'transport':'tim-ui','desktop':reviewed_ui()}
        with self.assertRaisesRegex(IMError,'UI_CONSENT_REQUIRED'):collect_desktop(self.home,self.root,'ui',p,backend=fake_backend)
    def test_ui_profile_forbids_send_action(self):
        p=reviewed_ui();p['navigation']=[{'action':'press','selector':ui_selector('发送')}]
        with self.assertRaisesRegex(IMError,'NOT_ALLOWED'):validate_profile(p,'qq','测试群')
    def test_ui_profile_forbids_arbitrary_keys(self):
        p=reviewed_ui();p['navigation']=[{'action':'keys','selector':ui_selector('消息记录')}]
        with self.assertRaisesRegex(IMError,'NOT_ALLOWED'):validate_profile(p,'qq','测试群')
    def test_ui_profile_wrong_version(self):
        p=reviewed_ui();p['client_version']='unknown'
        with self.assertRaisesRegex(IMError,'VERSION_NOT_REVIEWED'):validate_profile(p,'qq','测试群')
    def test_ui_orchestration_fake_never_implies_live_acceptance(self):
        data=read_blob(self.root/'tim.txt')
        class FakeUI:
            @contextmanager
            def lease(self,platform,ui):yield self
            def export_tim(self,ui,path):return data
        p={**self.profile,'transport':'tim-ui','desktop':reviewed_ui()}
        result=collect_desktop(self.home,self.root,'ui',p,backend=fake_backend,allow_ui=True,driver=FakeUI())
        self.assertEqual(result['new_records'],2)
        self.assertFalse(capabilities()['desktop_driver']['real_ui_acceptance'])
    def test_installed_runtime_backend_configuration(self):
        from im_hub.store import resolve_backend_location
        (self.root/'im-hub-runtime.json').write_text(canonical({'schema':'im-hub-runtime/1','chatlab_directory':str(self.root/'backend')}),'utf-8-sig')
        with patch('im_hub.store.sys.prefix',str(self.root)), patch.dict('os.environ',{},clear=True):
            path,error=resolve_backend_location()
        self.assertFalse(error);self.assertEqual(path,self.root/'backend')
    def test_invalid_runtime_config_fails_import_not_query_importability(self):
        from im_hub.store import resolve_backend_location
        (self.root/'im-hub-runtime.json').write_text('bad-json','utf-8')
        with patch('im_hub.store.sys.prefix',str(self.root)), patch.dict('os.environ',{},clear=True):
            path,error=resolve_backend_location()
        self.assertTrue(error)
    def test_config_check_does_not_expose_input_paths(self):
        out=config_check(self.config)
        self.assertTrue(out['valid']);self.assertNotIn(str(self.root),canonical(out))
    def test_run_partial_does_not_stop_next_source(self):
        data={'version':1,'sources':{'bad':self.profile,'good':self.profile}};self.config.write_text(canonical(data),'utf-8')
        called=[]
        def fn(home,config,name,**kwargs):
            called.append(name)
            if name=='bad':raise IMError('SYNTHETIC_SOURCE_OFFLINE')
            return {'new_records':2}
        out=run_sources(self.home,self.config,collect_fn=fn)
        self.assertFalse(out['success']);self.assertEqual(called,['bad','good']);self.assertEqual(out['results'][1]['status'],'ok')
    def test_run_dry_no_new_files(self):
        before=self.all_hashes();out=run_sources(self.home,self.config,dry_run=True)
        self.assertTrue(out['dry_run']);self.assertEqual(before,self.all_hashes())
    def test_cli_partial_run_exit_four(self):
        out=io.StringIO()
        with patch('im_hub.cli.run_sources',return_value={'success':False,'results':[]}):
            with redirect_stdout(out):code=main(['--home',str(self.home),'run','--config',str(self.config)])
        self.assertEqual(code,4);self.assertFalse(json.loads(out.getvalue())['ok'])
    def test_full_export_over_page_boundary(self):
        self.add_rows(205);out=export_all(self.home)
        rows=[json.loads(x) for x in (Path(out['directory'])/'messages.jsonl').read_text('utf-8').splitlines()]
        self.assertEqual(len(rows),205);self.assertEqual(len({r['message_key'] for r in rows}),205)
        self.assertTrue(out['query_complete']);self.assertFalse(out['source_history_complete'])
    def test_full_export_limit_failure_leaves_no_false_complete(self):
        self.add_rows(3)
        with self.assertRaisesRegex(IMError,'EXPORT_TOO_LARGE'):export_all(self.home,max_records=2)
        self.assertEqual(list((self.home/'exports').iterdir()),[])
    def test_verify_detects_deleted_physical_record(self):
        self.add_rows();db=next((self.home/'chatlab/data/databases').glob('*.db'))
        con=sqlite3.connect(db);con.execute('DELETE FROM message');con.commit();con.close()
        with self.assertRaises(IMError):verify(self.home)
    def test_verify_readonly(self):
        self.add_rows();before=self.all_hashes();self.assertEqual(verify(self.home)['records_verified'],1);self.assertEqual(before,self.all_hashes())
    def test_backup_restore_messages_and_desktop_cursor(self):
        self.add_rows(3);self.run_tim();(self.home/'credentials.local.json').write_text('PRIVATE_NOT_BACKED_UP','utf-8')
        path=self.root/'backup.zip';out=backup(self.home,path)
        self.assertEqual(out['records_verified'],5)
        with zipfile.ZipFile(path) as z:self.assertNotIn('credentials.local.json',z.namelist())
        dest=self.root/'restored';r=restore(path,dest)
        self.assertEqual(r['records_verified'],5)
        replay=collect(dest,self.config,'tim',backend=fake_backend)
        self.assertEqual(replay['new_records'],0)
        self.assertFalse(r['source_configuration_restored'])
    def test_backup_refuses_existing_output(self):
        path=self.root/'backup.zip';path.write_bytes(b'KEEP')
        with self.assertRaises(IMError):backup(self.home,path)
        self.assertEqual(path.read_bytes(),b'KEEP')
    def test_backup_refuses_output_in_state(self):
        with self.assertRaises(IMError):backup(self.home,self.home/'bad.zip')
    def test_backup_creation_permission_before_sensitive_write(self):
        self.add_rows();called=[]
        def secure(path):
            if not called:self.assertEqual(path.stat().st_size,0)
            called.append(True)
        with patch('im_hub.operations._secure_file',side_effect=secure):backup(self.home,self.root/'backup.zip')
        self.assertGreaterEqual(len(called),1)
    def test_restore_refuses_existing_directory(self):
        path=self.root/'backup.zip';backup(self.home,path)
        with self.assertRaises(IMError):restore(path,self.home)
    def test_restore_hash_tamper_rejected(self):
        self.add_rows();path=self.root/'backup.zip';backup(self.home,path)
        changed=self.root/'tampered.zip'
        with zipfile.ZipFile(path) as z,zipfile.ZipFile(changed,'w') as out:
            for name in z.namelist():
                data=z.read(name)
                if name=='im-hub.json':data=data.replace(b'im-hub-private',b'xx-hub-private')
                out.writestr(name,data)
        dest=self.root/'badrestore'
        with self.assertRaisesRegex(IMError,'HASH_MISMATCH'):restore(changed,dest)
        self.assertFalse(dest.exists());self.assertFalse(list(self.root.glob('.r-*')))
    def test_restore_under_long_windows_parent(self):
        self.add_rows();path=self.root/'backup.zip';backup(self.home,path)
        length=max(1,145-len(str(self.root))-1)
        parent=self.root/('x'*length);parent.mkdir()
        restored=restore(path,parent/'restored')
        self.assertEqual(restored['records_verified'],1)
    def test_restore_traversal_rejected(self):
        path=self.root/'bad.zip'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('../outside.json','x');z.writestr('BACKUP.json',canonical({'schema':'im-hub-backup/1','entries':[]}))
        with self.assertRaises(IMError):restore(path,self.root/'out')
        self.assertFalse((self.root.parent/'outside.json').exists())
    def test_backup_lock_blocks_simultaneous_collection(self):
        with writer(self.home,lock_name='.collection.lock'):
            with self.assertRaisesRegex(IMError,'WRITER_BUSY'):backup(self.home,self.root/'bad.zip')
    def test_uninitialized_backup_no_state_created(self):
        missing=self.root/'missing'
        with self.assertRaises(IMError):backup(missing,self.root/'bad.zip')
        self.assertFalse(missing.exists())


if __name__=='__main__':unittest.main()
