"""Actual harmless child processes verify ownership and kill-on-close, not IM UI."""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from im_hub.soak import ChildTree,GATE

@unittest.skipUnless(os.name=='nt','Windows Job Objects')
class OwnedChildrenTests(unittest.TestCase):
    def test_gate_prevents_effect_before_ownership(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'synthetic-effect.txt';tree=ChildTree();child=None
            try:
                code="import sys,time;from pathlib import Path;line=sys.stdin.readline();assert line=="+repr(GATE)+";Path(sys.argv[1]).write_text('synthetic');time.sleep(30)"
                child=subprocess.Popen([sys.executable,'-c',code,str(p)],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                time.sleep(.2);self.assertFalse(p.exists())
                tree.bind(child);child.stdin.write(GATE.encode());child.stdin.flush();child.stdin.close()
                end=time.monotonic()+5
                while not p.exists() and time.monotonic()<end:time.sleep(.02)
                self.assertEqual(p.read_text(),'synthetic')
            finally:tree.close(child)
            self.assertIsNotNone(child.poll())
    def test_close_terminates_grandchild_tree(self):
        with tempfile.TemporaryDirectory() as td:
            marker=Path(td)/'grandchild.pid';tree=ChildTree();child=None;handle=None
            try:
                code="import sys,time,subprocess;from pathlib import Path;assert sys.stdin.readline()=="+repr(GATE)+";p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)"
                child=subprocess.Popen([sys.executable,'-c',code,str(marker)],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                tree.bind(child);child.stdin.write(GATE.encode());child.stdin.flush();child.stdin.close()
                end=time.monotonic()+5
                while not marker.exists() and time.monotonic()<end:time.sleep(.02)
                self.assertTrue(marker.exists());pid=int(marker.read_text())
                import win32api,win32event
                handle=win32api.OpenProcess(0x00100000,False,pid)
                tree.close(child);child=None
                self.assertEqual(win32event.WaitForSingleObject(handle,5000),0)
            finally:
                tree.close(child)
                if handle is not None:handle.Close()

if __name__=='__main__':unittest.main()
