"""Taskbar activation unit contracts; all UI objects and input are fakes."""
from __future__ import annotations
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from im_hub.common import IMError
from im_hub.desktop_runtime import activate_bound,taskbar_target

ENV={'IM_HUB_ACTIVATE_CLIENTS':'1','IM_HUB_DESKTOP_UNTIL':'2099-01-01T00:00:00Z'}

class Control:
    def __init__(self,name='',kind='PaneControl',cls='',pid=9,rect=(0,900,1600,1000),children=()):
        self.Name=name;self.ControlTypeName=kind;self.ClassName=cls;self.ProcessId=pid
        self.IsOffscreen=False;self.IsEnabled=True;self.children=list(children)
        self.BoundingRectangle=SimpleNamespace(left=rect[0],top=rect[1],right=rect[2],bottom=rect[3])
    def GetChildren(self):return self.children

def session():
    s=SimpleNamespace(p={'platform':'wecom','conversation_name':'fixture'},last_input=1,actions=0,
                      g=Mock(),n=Mock(),mouse=Mock(),accepts=lambda p,i:i[0]=='wxwork.exe')
    s.n.input_tick.return_value=1;s.n.api.GetAsyncKeyState.return_value=0
    s.n.process_identity.side_effect=lambda pid:('wxwork.exe',None) if pid==2 else ('explorer.exe',None)
    s.n.proc.GetWindowThreadProcessId.side_effect=lambda h:(1,2 if h==10 else 9)
    s.g.EnumWindows.side_effect=lambda visit,arg:visit(10,arg)
    s.g.IsWindowVisible.return_value=True;s.g.GetClassName.return_value='WeWorkWindow';s.g.GetWindowText.return_value='企业微信'
    s.g.WindowFromPoint.return_value=20;s.g.GetForegroundWindow.return_value=10
    return s

class ActivationTests(unittest.TestCase):
    def test_normal_taskbar_click_confirms_exact_window(self):
        s=session()
        with patch.dict('os.environ',ENV,clear=True),patch('im_hub.desktop_runtime.taskbar_target',return_value=((400,950),9)):
            self.assertEqual(activate_bound(s),10)
        s.mouse.click.assert_called_once_with(button='left',coords=(400,950))
        self.assertTrue(s.activated);self.assertEqual(s.actions,1);s.g.SetForegroundWindow.assert_not_called()
    def test_occluded_taskbar_never_clicked(self):
        s=session();s.g.WindowFromPoint.return_value=10
        with patch.dict('os.environ',ENV,clear=True),patch('im_hub.desktop_runtime.taskbar_target',return_value=((400,950),9)):
            with self.assertRaisesRegex(IMError,'OCCLUDED'):activate_bound(s)
        s.mouse.click.assert_not_called()
    def test_user_input_aborts_before_taskbar_search(self):
        s=session();s.n.input_tick.return_value=2
        with patch.dict('os.environ',ENV,clear=True),patch('im_hub.desktop_runtime.taskbar_target') as locate:
            with self.assertRaisesRegex(IMError,'USER_INPUT'):activate_bound(s)
        locate.assert_not_called();s.mouse.click.assert_not_called()
    def test_expired_deadline_no_enumeration(self):
        s=session()
        with patch.dict('os.environ',{'IM_HUB_ACTIVATE_CLIENTS':'1','IM_HUB_DESKTOP_UNTIL':'2020-01-01T00:00:00Z'},clear=True):
            with self.assertRaisesRegex(IMError,'EXPIRED'):activate_bound(s)
        s.g.EnumWindows.assert_not_called();s.mouse.click.assert_not_called()
    def test_taskbar_failure_not_force_foreground_fallback(self):
        s=session()
        with patch.dict('os.environ',ENV,clear=True),patch('im_hub.desktop_runtime.taskbar_target',side_effect=IMError('RUNNING_CLIENT_TASKBAR_BUTTON_NOT_UNIQUE')):
            with self.assertRaises(IMError):activate_bound(s)
        s.g.SetForegroundWindow.assert_not_called();s.mouse.click.assert_not_called()

@unittest.skipUnless(importlib.util.find_spec('uiautomation') is not None,'optional Windows UI dependency')
class TaskbarTests(unittest.TestCase):
    def target(self,children,pid=9):
        s=session();bar=Control(cls='Shell_TrayWnd',pid=pid,children=children)
        root=Control(children=[bar])
        with patch.dict('os.environ',ENV,clear=True),patch('uiautomation.GetRootControl',return_value=root):
            return taskbar_target(s,'企业微信')
    def button(self,name='企业微信 - 1 个运行窗口',rect=(350,910,450,990)):
        return Control(name,kind='ButtonControl',rect=rect)
    def test_exact_running_button_not_tray_account_icon(self):
        result=self.target([self.button('企业微信: fixture-account'),self.button()])
        self.assertEqual(result,((400,950),9))
    def test_multiple_running_buttons_rejected(self):
        with self.assertRaisesRegex(IMError,'NOT_UNIQUE'):self.target([self.button(),self.button()])
    def test_wrong_process_not_a_taskbar(self):
        with self.assertRaisesRegex(IMError,'NOT_EXPLORER'):self.target([self.button()],pid=2)
    def test_offscreen_button_rejected(self):
        b=self.button();b.IsOffscreen=True
        with self.assertRaises(IMError):self.target([b])
    def test_rect_outside_taskbar_rejected(self):
        with self.assertRaisesRegex(IMError,'RECTANGLE_INVALID'):self.target([self.button(rect=(50,50,100,100))])
    def test_multiple_window_preview_is_not_blindly_selected(self):
        with self.assertRaises(IMError):self.target([self.button('企业微信 - 2 个运行窗口')])

if __name__=='__main__':unittest.main()
