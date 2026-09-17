"""Read-only probe contracts. Labels must never become account/message identity."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from im_hub.common import IMError
from im_hub.desktop_probe import probe_desktop_directory
from im_hub.cli import main


def snapshot(rows=None,listing=True):
    return {'rows':rows if rows is not None else [row('Synthetic group'),row('群助手')],
            'provider':'synthetic','client_version':None,'nodes_visited':3,
            'listing_observed':listing,'truncated':False}


def row(label,full=True):
    return {'label':label,'bounds':[100,100,300,200],'fully_visible':full}


class Reader:
    def __init__(self,first,second=None):self.first=first;self.second=first if second is None else second;self.calls=0
    def snapshot(self,platform):
        self.calls+=1
        return copy.deepcopy(self.first if self.calls==1 else self.second)


class DesktopProbeTests(unittest.TestCase):
    def test_no_home_created_by_cli(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)/'must-not-exist';out=io.StringIO()
            with patch('im_hub.desktop_probe.VisibleSidebarReader',return_value=Reader(snapshot())),redirect_stdout(out):
                code=main(['--home',str(home),'probe-desktop-directory','--platform','qq'])
            self.assertEqual(code,0);self.assertFalse(home.exists())
            self.assertTrue(json.loads(out.getvalue())['data']['query_only'])

    def test_visible_rows_are_not_ids_or_verified_active_conversations(self):
        r=probe_desktop_directory('qq',Reader(snapshot()))
        self.assertEqual(r['visible_rows'],2);self.assertFalse(r['inventory_ready'])
        self.assertTrue(all(x['native_conversation_id'] is None and not x['eligible_for_automatic_binding'] for x in r['rows']))
        self.assertIsNone(r['independent_total']);self.assertFalse(r['account_directory_complete'])

    def test_group_helper_is_not_ordinary_conversation(self):
        r=probe_desktop_directory('qq',Reader(snapshot()))
        self.assertEqual(r['group_helper_entries'],1)
        self.assertEqual(r['rows'][1]['row_kind'],'group_helper_entry')
        self.assertFalse(r['folded_children_scanned'])

    def test_wecom_missing_provider_is_unknown_not_empty_account(self):
        r=probe_desktop_directory('wecom',Reader(snapshot([],False)))
        self.assertIsNone(r['visible_rows']);self.assertEqual(r['status'],'directory_metadata_not_exposed')
        self.assertEqual(r['messages_collected'],0)

    def test_clipped_row_is_separate(self):
        r=probe_desktop_directory('qq',Reader(snapshot([row('a'),row('b',False)])))
        self.assertEqual((r['visible_rows'],r['fully_visible_rows']),(2,1))

    def test_duplicates_do_not_merge(self):
        r=probe_desktop_directory('qq',Reader(snapshot([row('same'),row('same')])))
        self.assertEqual(len(r['rows']),2);self.assertTrue(all(x['duplicate_label'] for x in r['rows']))
        self.assertNotEqual(r['rows'][0]['locator_sha256'],r['rows'][1]['locator_sha256'])

    def test_live_layout_change_is_not_stable_snapshot(self):
        with self.assertRaisesRegex(IMError,'VIEW_CHANGED'):
            probe_desktop_directory('qq',Reader(snapshot(),snapshot([row('other')])))

    def test_preview_fields_not_accepted(self):
        r=row('fixture');r['Value']='private preview'
        with self.assertRaisesRegex(IMError,'ROWS_INVALID'):
            probe_desktop_directory('qq',Reader(snapshot([r])))

    def test_boolean_coordinate_rejected(self):
        r=row('fixture');r['bounds'][0]=True
        with self.assertRaises(IMError):probe_desktop_directory('qq',Reader(snapshot([r])))

    def test_invalid_platform_no_provider(self):
        with patch('im_hub.desktop_probe.VisibleSidebarReader',side_effect=AssertionError('client')):
            with self.assertRaisesRegex(IMError,'PLATFORM_UNSUPPORTED'):probe_desktop_directory('wechat')

    def test_truncated_probe_is_not_account_complete(self):
        s=snapshot();s['truncated']=True;r=probe_desktop_directory('qq',Reader(s))
        self.assertTrue(r['truncated']);self.assertFalse(r['account_directory_complete'])

    def test_invalid_provider_shape_rejected(self):
        with self.assertRaises(IMError):probe_desktop_directory('qq',Reader([]))
        s=snapshot();s['listing_observed']='true'
        with self.assertRaises(IMError):probe_desktop_directory('qq',Reader(s))


if __name__=='__main__':unittest.main()
