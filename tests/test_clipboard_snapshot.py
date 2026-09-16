"""Clipboard contention and structured failure contracts; no chat content read."""
from __future__ import annotations
import io
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from contextlib import redirect_stdout
from im_hub.clipboard_snapshot import open_snapshot
from im_hub.common import IMError
from im_hub.cli import main
from im_hub.soak import ChildTree,GATE

class NativeError(Exception):pass

class Clock:
    def __init__(self):self.value=0.
    def __call__(self):return self.value
    def sleep(self,seconds):self.value+=seconds

class ClipboardSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.clip=Mock();self.clip.GetClipboardOwner.return_value=4
        self.user=Mock();self.user.GetClipboardSequenceNumber.return_value=2
        self.clock=Clock()
    def run_open(self,**kw):
        return open_snapshot(self.clip,self.user,4,2,guard=lambda:None,clock=self.clock,pause=self.clock.sleep,**kw)
    def test_immediate_open_is_one_attempt(self):
        self.assertEqual(self.run_open(),1);self.clip.GetClipboardData.assert_not_called()
        self.clip.EmptyClipboard.assert_not_called()
    def test_busy_then_available_same_api(self):
        self.clip.OpenClipboard.side_effect=[NativeError(5,'OpenClipboard','private'),NativeError(5,'OpenClipboard','private'),None]
        self.assertEqual(self.run_open(),3);self.assertEqual(self.clock.value,.1)
        self.clip.CloseClipboard.assert_not_called();self.clip.GetClipboardData.assert_not_called()
    def test_permanent_unavailability_bounded(self):
        self.clip.OpenClipboard.side_effect=NativeError(5,'OpenClipboard','private')
        with self.assertRaisesRegex(IMError,'CLIPBOARD_OPEN_UNAVAILABLE'):self.run_open(timeout=.1)
        self.assertEqual(self.clip.OpenClipboard.call_count,3)
        self.clip.CloseClipboard.assert_not_called()
    def test_owner_change_not_retried_as_busy(self):
        self.clip.OpenClipboard.side_effect=NativeError(5,'OpenClipboard','private')
        self.clip.GetClipboardOwner.side_effect=[4,99]
        with self.assertRaisesRegex(IMError,'CHANGED_BEFORE_READ'):self.run_open()
        self.assertEqual(self.clip.OpenClipboard.call_count,1)
    def test_sequence_change_stops_old_snapshot(self):
        self.clip.OpenClipboard.side_effect=NativeError(5,'OpenClipboard','private')
        self.user.GetClipboardSequenceNumber.side_effect=[2,3]
        with self.assertRaisesRegex(IMError,'SOURCE_SEQUENCE_CHANGED'):self.run_open()
        self.assertEqual(self.clip.OpenClipboard.call_count,1)
    def test_other_native_failure_not_retried(self):
        self.clip.OpenClipboard.side_effect=NativeError(6,'OpenClipboard','private')
        with self.assertRaisesRegex(IMError,'CLIPBOARD_OPEN_FAILED'):self.run_open()
        self.assertEqual(self.clip.OpenClipboard.call_count,1)
    def test_guard_stops_between_attempts(self):
        self.clip.OpenClipboard.side_effect=NativeError(5,'OpenClipboard','private')
        guard=Mock(side_effect=[None,IMError('USER_INPUT_DETECTED_PAUSED')])
        with self.assertRaisesRegex(IMError,'USER_INPUT'):
            open_snapshot(self.clip,self.user,4,2,guard=guard,clock=self.clock,pause=self.clock.sleep)
        self.assertEqual(self.clip.OpenClipboard.call_count,1)
    def test_wait_bounds(self):
        for bad in (0,True,4,float('nan')):
            with self.assertRaises(IMError):self.run_open(timeout=bad)
        self.clip.OpenClipboard.assert_not_called()
    def test_deadline_guard_before_clipboard_access(self):
        with patch.dict(os.environ,{'IM_HUB_DESKTOP_UNTIL':'2020-01-01T00:00:00Z'},clear=True):
            with self.assertRaisesRegex(IMError,'EXPIRED'):open_snapshot(self.clip,self.user,4,2)
        self.clip.OpenClipboard.assert_not_called()
    def test_native_exception_at_cli_boundary_is_json(self):
        out=io.StringIO()
        with patch('im_hub.cli.source_status',side_effect=NativeError(5,'private details')),redirect_stdout(out):
            code=main(['sources'])
        import json
        data=json.loads(out.getvalue());self.assertEqual(code,2)
        self.assertEqual(data['error']['code'],'LOCAL_OPERATION_FAILED')
        self.assertEqual(data['error']['type'],'NativeError');self.assertNotIn('private',out.getvalue())
    def test_keyboard_interrupt_keeps_separate_exit(self):
        out=io.StringIO()
        with patch('im_hub.cli.source_status',side_effect=KeyboardInterrupt()),redirect_stdout(out):
            self.assertEqual(main(['sources']),130)

@unittest.skipUnless(os.name=='nt','Windows normal clipboard contention')
class NativeClipboardContentionTest(unittest.TestCase):
    def test_normal_open_waits_for_other_holder_without_reading_content(self):
        import win32clipboard
        child=None;tree=ChildTree();opened=False
        try:
            code="import sys,win32clipboard,win32gui;assert sys.stdin.readline()=="+repr(GATE)+";h=win32gui.CreateWindowEx(0,'STATIC','im-hub-synthetic-clipboard-holder',0,0,0,0,0,-3,0,0,None);win32clipboard.OpenClipboard(h);print('held',flush=True);assert sys.stdin.readline()=='release\\n';win32clipboard.CloseClipboard();win32gui.DestroyWindow(h)"
            child=subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
            tree.bind(child);child.stdin.write(GATE.encode());child.stdin.flush()
            self.assertEqual(child.stdout.readline().decode().strip(),'held')
            # Only Open/Close are real. No clipboard content or client data is read.
            released=False
            def actual_open():
                nonlocal released
                try:win32clipboard.OpenClipboard()
                except Exception:
                    if not released:
                        child.stdin.write(b'release\n');child.stdin.flush();child.stdin.close();released=True
                    raise
            clip=SimpleNamespace(GetClipboardOwner=lambda:4,OpenClipboard=actual_open)
            user=SimpleNamespace(GetClipboardSequenceNumber=lambda:2)
            attempts=open_snapshot(clip,user,4,2,guard=lambda:None,timeout=2)
            opened=True;self.assertGreater(attempts,1)
        finally:
            if opened:win32clipboard.CloseClipboard()
            tree.close(child)
            if child is not None:
                if child.stdin and not child.stdin.closed:child.stdin.close()
                if child.stdout:child.stdout.close()
                if child.stderr:child.stderr.close()

if __name__=='__main__':unittest.main()
