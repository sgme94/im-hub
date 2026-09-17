"""Execute the real UIA reader against fake controls, never a real desktop."""
from __future__ import annotations
import copy
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from im_hub.common import IMError
from im_hub.desktop_directory import UIADirectoryReader, discover_desktop_account
from test_desktop_directory import profile, account, row, AUTH


class Control:
    def __init__(self, identity='', kind='TextControl', name='', children=None):
        self.AutomationId=identity;self.ControlTypeName=kind;self.Name=name
        self.ProcessId=7;self.IsOffscreen=False;self.IsEnabled=True;self.IsPassword=False
        self.children=children or [];self.scroll=None;self.expand=None
    def GetChildren(self):return self.children
    def GetScrollPattern(self):return self.scroll
    def GetExpandCollapsePattern(self):return self.expand
    def Click(self,*a,**k):raise AssertionError('coordinate fallback')
    def SetActive(self,*a,**k):raise AssertionError('activation')


class Scroll:
    def __init__(self,listing,pages):
        self.listing=listing;self.pages=pages;self.position=0;self.actions=[]
        self.listing.children=pages[0]
    @property
    def VerticallyScrollable(self):return len(self.pages)>1
    @property
    def VerticalScrollPercent(self):return 0 if self.position==0 else 100
    def SetScrollPercent(self,h,v,waitTime=0):
        self.actions.append(('reset',h,v));self.position=0;self.listing.children=self.pages[0];return True
    def Scroll(self,h,v,waitTime=0):
        self.actions.append(('scroll',h,v));self.position+=1;self.listing.children=self.pages[self.position];return True


class Expand:
    def __init__(self):self.ExpandCollapseState=0;self.calls=0
    def Expand(self,waitTime=0):self.calls+=1;self.ExpandCollapseState=1;return True


def controls(r):
    return Control(kind='ListItemControl',children=[Control(k,name=v) for k,v in r.items()])


class FakeNative:
    def __init__(self):
        a=controls(row('r1'));b=controls(row('r2'));c=controls(row('r3'))
        self.regular=Control('regular-list','ListControl');self.regular.scroll=Scroll(self.regular,[[a,b],[b,c]])
        self.folded=Control('folded-list','ListControl');self.folded.scroll=Scroll(self.folded,[[]])
        self.expander=Control('folded-folder','TreeItemControl');self.expander.expand=Expand()
        self.account=Control(name='Synthetic account')
        self.root=Control('fixture-window','WindowControl',children=[self.account,self.regular,self.folded,
                 self.expander,Control('regular-count',name='3'),Control('folded-count',name='0')])
        self.pid=7;self.lease_args=None;self.guard_calls=0
    @contextmanager
    def lease(self,platform,ui):
        self.lease_args=(platform,ui)
        yield self
    def guard(self):self.guard_calls+=1
    def find(self,s,root=None):
        lookup={'name':'Name','automation_id':'AutomationId','control_type':'ControlTypeName'}
        queue=[root or self.root];found=[]
        while queue:
            c=queue.pop(0)
            if all(getattr(c,lookup[k],None)==v for k,v in s.items()):found.append(c)
            queue.extend(c.GetChildren())
        if len(found)!=1:raise IMError('DESKTOP_SELECTOR_MISSING_OR_AMBIGUOUS')
        return found[0]


class NativeDirectoryReaderTests(unittest.TestCase):
    def scan(self,native):
        from test_desktop_directory import START,END
        with patch.dict(os.environ,AUTH,clear=True),patch('im_hub.desktop_directory.NativeWindows',return_value=native):
            return discover_desktop_account(account(),START,END,allow_ui=True)

    def test_native_scroll_expand_and_double_snapshot_path(self):
        n=FakeNative();items,result=self.scan(n)
        self.assertEqual(len(items),3);self.assertTrue(result['directory_enumeration_complete'])
        self.assertEqual(n.regular.scroll.actions,[('reset',-1,0),('scroll',2,4)])
        self.assertEqual(n.expander.expand.calls,1)
        self.assertFalse(n.lease_args[1]['allow_activation'])
        self.assertGreater(n.guard_calls,20)

    def test_missing_scroll_pattern_no_fallback(self):
        n=FakeNative();n.regular.scroll=None
        items,r=self.scan(n);self.assertFalse(items)
        self.assertEqual(r['error'],'DIRECTORY_SCROLL_PATTERN_UNAVAILABLE')

    def test_protected_metadata_rejected(self):
        n=FakeNative();n.regular.children[0].children[0].IsPassword=True
        _,r=self.scan(n);self.assertEqual(r['error'],'DIRECTORY_METADATA_CONTROL_UNSAFE')

    def test_foreign_process_child_rejected(self):
        n=FakeNative();n.regular.children[0].children[0].ProcessId=8
        _,r=self.scan(n);self.assertEqual(r['error'],'DIRECTORY_METADATA_CONTROL_UNSAFE')

    def test_account_identity_changed(self):
        n=FakeNative();n.account.Name='other account'
        _,r=self.scan(n);self.assertEqual(r['error'],'DIRECTORY_ACCOUNT_IDENTITY_CHANGED')

    def test_expander_without_native_pattern_partial(self):
        n=FakeNative();n.expander.expand=None
        items,r=self.scan(n);self.assertEqual(len(items),3)
        self.assertEqual(r['error'],'DIRECTORY_EXPAND_PATTERN_UNAVAILABLE')
        self.assertFalse(r['directory_enumeration_complete'])

    def test_native_provider_error_sanitized(self):
        n=FakeNative()
        def error(*a):raise RuntimeError('sensitive account preview')
        n.regular.GetScrollPattern=error
        items,r=self.scan(n)
        self.assertEqual(r['error'],'DIRECTORY_PROVIDER_UNAVAILABLE')
        self.assertNotIn('sensitive',str(r));self.assertFalse(items)

    def test_missing_metadata_not_guessed_from_row_name(self):
        n=FakeNative();n.regular.children[0].children=n.regular.children[0].children[1:]
        n.regular.children[0].Name='pretend native id'
        _,r=self.scan(n);self.assertFalse(r['directory_enumeration_complete'])


if __name__=='__main__':unittest.main()
