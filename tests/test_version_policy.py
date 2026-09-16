"""Source-version compatibility is exercised without actual desktop actions."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from im_hub.common import IMError, canonical, digest, initialize
from im_hub.desktop_sources import collect_desktop, prepare_desktop_profile, profile_fingerprint
from im_hub.windows_desktop import NativeWindows, accepts_client, validate_profile
from test_core import fake_backend
from test_product import ui_profile, tim_export, native_message

class VersionPolicyTests(unittest.TestCase):
    def test_process_name_not_version_controls_identity(self):
        for v in ('5.0.10.6025','5.0.11.6018','999',None):
            self.assertTrue(accepts_client('wecom', ('WXWork.exe',v)))
            self.assertTrue(accepts_client('qq', ('TIM.exe',v)))
            self.assertFalse(accepts_client('wecom', ('other.exe',v)))
    def test_version_resource_library_error_is_only_missing_metadata(self):
        class VersionResourceError(Exception):pass
        class Kernel:
            def OpenProcess(self,*args):return 7
            def QueryFullProcessImageNameW(self,handle,flags,buffer,length):
                buffer.value='WXWork.exe';return True
            def CloseHandle(self,handle):pass
        class Api:
            def GetFileVersionInfo(self,*args):raise VersionResourceError('no version resource')
        n=object.__new__(NativeWindows);n.kernel=Kernel();n.api=Api()
        self.assertEqual(n.process_identity(3),('wxwork.exe',None))
        n.kernel.OpenProcess=lambda *args:0
        with self.assertRaisesRegex(IMError,'IDENTITY_ACCESS_DENIED'):n.process_identity(3)
    def test_selectors_still_required_for_any_version(self):
        ui=ui_profile();ui['client_version']='999';ui['conversation_header']['name']='wrong'
        with self.assertRaises(IMError):validate_profile(ui,'qq','测试群')
    def test_version_does_not_change_profile_identity(self):
        a={'desktop':ui_profile(),'account_namespace':'fixture'}
        b={**a,'client_version':'new','desktop':{**a['desktop'],'client_version':'new'}}
        self.assertEqual(profile_fingerprint(a),profile_fingerprint(b))
        self.assertNotEqual(profile_fingerprint(a),profile_fingerprint({**b,'account_namespace':'different'}))
    def test_actual_capture_path_new_version_and_unknown(self):
        class User:
            def GetClipboardSequenceNumber(self):return 2
        class Proc:
            def GetWindowThreadProcessId(self,hwnd):return (1,3)
        class Clip:
            def GetClipboardOwner(self):return 4
            def OpenClipboard(self):pass
            def CloseClipboard(self):pass
            def RegisterClipboardFormat(self,name):return 5
            def IsClipboardFormatAvailable(self,fmt):return True
            def GetClipboardData(self,fmt):return native_message()
        n=object.__new__(NativeWindows);n.user=User();n.proc=Proc();n.clip=Clip();n.pid=None
        for version in ('5.0.11.6018',None):
            n.process_identity=lambda pid:('wxwork.exe',version)
            blob,meta=n.capture(1)
            self.assertEqual(blob,native_message());self.assertEqual(meta['client_version'],version)
        n.process_identity=lambda pid:('not-wecom.exe','999')
        with self.assertRaises(IMError):n.capture(1)
    def test_old_profile_migrates_then_version_change_replays(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);home=root/'state';initialize(home)
            raw=tim_export();(root/'in.txt').write_bytes(raw)
            (root/'manifest.json').write_text(canonical({'source_id':'tim','observed_at':'2024-01-02T12:00:00+08:00','sha256':digest(raw)}),'utf-8')
            p={'enabled':True,'platform':'qq','transport':'tim-export','adapter':'tim-sequence-json',
               'account_namespace':'fixture','conversation_id':'fixture','conversation_name':'测试群',
               'source_epoch':'one','data_class':'synthetic','export_mode':'full-history-append-only',
               'input':'in.txt','manifest':'manifest.json','client_version':'old'}
            collect_desktop(home,root,'tim',p,backend=fake_backend)
            con=sqlite3.connect(home/'desktop.sqlite3');con.execute('UPDATE sources SET profile_sha256=?',(digest(prepare_desktop_profile(root,p)),));con.commit();con.close()
            self.assertEqual(collect_desktop(home,root,'tim',p,backend=fake_backend)['new_records'],0)
            self.assertEqual(collect_desktop(home,root,'tim',{**p,'client_version':'999'},backend=fake_backend)['new_records'],0)
