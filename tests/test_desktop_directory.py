"""Synthetic directory controls only. No real client, messages or desktop lease."""
from __future__ import annotations
import copy
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from im_hub.common import IMError, canonical, digest, initialize, stamp
from im_hub.desktop_directory import validate_directory_profile, discover_desktop_account, plan_desktop_backfill

START = 1704067200
END = START + 7 * 86400
AUTH = {'IM_HUB_DESKTOP_UNTIL': '2099-01-01T00:00:00Z'}


def sel(identity, kind='TextControl'):
    return {'automation_id': identity, 'control_type': kind}


def profile():
    return {'schema': 'im-hub-directory-profile/1', 'profile_reviewed': True,
            'identity_source': 'native_conversation_id_text',
            'window': sel('fixture-window', 'WindowControl'),
            'account_header': {'name': 'Synthetic account', 'control_type': 'TextControl'},
            'row_control_type': 'ListItemControl',
            'fields': {name: sel(name) for name in ('native_id','name','type_label','last_activity')},
            'type_labels': {'GROUP': 'group', 'DIRECT': 'direct', 'SERVICE': 'service'},
            'time_format': 'iso8601', 'max_pages': 10, 'max_rows': 100,
            'sections': [
                {'id': 'regular', 'kind': 'regular', 'listing': sel('regular-list', 'ListControl'), 'total_count': sel('regular-count')},
                {'id': 'folded', 'kind': 'folded', 'listing': sel('folded-list', 'ListControl'), 'total_count': sel('folded-count'),
                 'expand': sel('folded-folder', 'TreeItemControl')} ]}


def account(platform='qq'):
    return {'platform': platform, 'enabled': True, 'transport': 'desktop-directory',
            'account_namespace': 'synthetic-account', 'source_epoch': 'synthetic-generation',
            'data_class': 'synthetic', 'include_types': ['group','direct'], 'directory': profile()}


def row(cid, kind='GROUP', timestamp=None, name='same display name'):
    return {'native_id': cid, 'name': name, 'type_label': kind,
            'last_activity': stamp(START + 10) if timestamp is None else timestamp}


def page(rows, position=0.0, end=True, total=None):
    return {'rows': rows, 'position': position, 'at_end': end,
            'total': len(rows) if total is None else total}


class FakeReader:
    def __init__(self, sections=None, failure=None):
        self.sections = sections or {'regular': [page([row('r1')])], 'folded': [page([row('r2', 'DIRECT')])]}
        self.failure = failure
        self.opened = False
        self.visited = []

    @contextmanager
    def lease(self, p):
        self.opened = True
        if self.failure:
            raise IMError(self.failure)
        yield self

    def pages(self, section):
        self.visited.append(section['id'])
        for value in self.sections[section['id']]:
            if isinstance(value, Exception):
                raise value
            yield copy.deepcopy(value)


class DesktopDirectoryTests(unittest.TestCase):
    def run_scan(self, p=None, driver=None, **kw):
        with patch.dict(os.environ, AUTH, clear=True):
            return discover_desktop_account(p or account(), START, END, allow_ui=True,
                                            driver=driver or FakeReader(), **kw)

    def test_no_consent_never_constructs_native_driver(self):
        with patch('im_hub.desktop_directory.UIADirectoryReader', side_effect=AssertionError('client')):
            with self.assertRaisesRegex(IMError, 'UI_CONSENT_REQUIRED'):
                discover_desktop_account(account(), START, END)

    def test_explicit_deadline_required_even_without_activation(self):
        d=FakeReader()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(IMError, 'DEADLINE_REQUIRED'):
                discover_desktop_account(account(), START, END, allow_ui=True, driver=d)
        self.assertFalse(d.opened)

    def test_expired_authorization_before_client(self):
        d=FakeReader()
        with patch.dict(os.environ, {'IM_HUB_DESKTOP_UNTIL': '2020-01-01T00:00:00Z'}, clear=True):
            with self.assertRaisesRegex(IMError, 'AUTHORIZATION_EXPIRED'):
                discover_desktop_account(account(), START, END, allow_ui=True, driver=d)
        self.assertFalse(d.opened)

    def test_stop_before_client(self):
        with tempfile.TemporaryDirectory() as td:
            stop=Path(td)/'STOP';stop.write_text('synthetic')
            with patch.dict(os.environ, {**AUTH,'IM_HUB_STOP_FILE': str(stop)}, clear=True):
                with self.assertRaisesRegex(IMError, 'STOP_REQUESTED'):
                    discover_desktop_account(account(), START, END, allow_ui=True, driver=FakeReader())

    def test_deadline_checked_during_enumeration(self):
        with patch.dict(os.environ, AUTH, clear=True):
            with patch('im_hub.desktop_directory.require_consent', side_effect=[None,None,IMError('DESKTOP_AUTHORIZATION_EXPIRED')]):
                items, report=discover_desktop_account(account(), START, END, allow_ui=True, driver=FakeReader())
        self.assertFalse(report['directory_enumeration_complete'])
        self.assertEqual(report['error'], 'DESKTOP_AUTHORIZATION_EXPIRED')

    def test_same_display_names_do_not_merge(self):
        items,s=self.run_scan()
        self.assertEqual(len(items),2)
        self.assertEqual(len({e['key'] for e in items}),2)
        self.assertTrue(s['directory_enumeration_complete'])
        self.assertFalse(s['full_channel_coverage_verified'])
        self.assertTrue(all(not e['eligible'] for e in items))
        self.assertTrue(all(e['local_message_count'] is None for e in items))

    def test_folded_section_is_required(self):
        p=profile();p['sections']=p['sections'][:1]
        with self.assertRaisesRegex(IMError,'FOLDED_SECTION_REQUIRED'):
            validate_directory_profile(p)

    def test_native_identity_not_runtime_or_automation_id(self):
        for source in ('runtime_id','automation_id','display_name'):
            p=profile();p['identity_source']=source
            with self.assertRaisesRegex(IMError,'NATIVE_ID_TEXT_REQUIRED'):
                validate_directory_profile(p)

    def test_activation_or_arbitrary_profile_action_rejected(self):
        for key in ('allow_activation','navigation','script'):
            p=profile();p[key]=True
            with self.assertRaises(IMError):validate_directory_profile(p)

    def test_unknown_type_preserved_not_direct(self):
        d=FakeReader({'regular':[page([row('r1','UNRECOGNIZED')])], 'folded':[page([])]})
        items,s=self.run_scan(driver=d)
        self.assertEqual(items[0]['conversation_type'],'unknown')
        self.assertIn('unresolved_conversation_type',items[0]['coverage_gaps'])

    def test_relative_date_preserved_unknown(self):
        d=FakeReader({'regular':[page([row('r1',timestamp='昨天')])], 'folded':[page([])]})
        items,s=self.run_scan(driver=d)
        self.assertEqual(items[0]['directory_last_epoch'],None)
        self.assertIn('activity_time_unresolved',items[0]['coverage_gaps'])
        self.assertEqual(s['activity_time_unknown'],1)

    def test_post_window_activity_not_silently_excluded(self):
        d=FakeReader({'regular':[page([row('r1',timestamp=stamp(END+100))])], 'folded':[page([])]})
        items,_=self.run_scan(driver=d)
        self.assertEqual(items[0]['activity'],'directory_candidate')
        self.assertIn('last_activity_after_window_requires_history_check',items[0]['coverage_gaps'])

    def test_old_directory_entry_separate_not_proof_of_empty_history(self):
        d=FakeReader({'regular':[page([row('r1',timestamp=stamp(START-1))])], 'folded':[page([])]})
        items,_=self.run_scan(driver=d)
        self.assertEqual(items[0]['activity'],'outside_window_directory')
        self.assertFalse(items[0]['eligible'])

    def test_overlapping_pages_dedup_native_ids(self):
        d=FakeReader({'regular':[page([row('a'),row('b')],0,False,3),page([row('b'),row('c')],100,True,3)], 'folded':[page([])]})
        items,s=self.run_scan(driver=d)
        self.assertEqual(len(items),3);self.assertTrue(s['directory_enumeration_complete'])
        self.assertEqual(d.visited,['regular','folded'])

    def test_empty_folded_section_requires_count_proof(self):
        d=FakeReader({'regular':[page([row('a')])], 'folded':[page([], total=0)]})
        _,s=self.run_scan(driver=d);self.assertTrue(s['directory_enumeration_complete'])
        self.assertEqual(s['sections'][1]['unique_rows'],0)

    def test_missing_total_never_complete(self):
        p=page([]);p['total']=None
        d=FakeReader({'regular':[page([row('a')])], 'folded':[p]})
        _,s=self.run_scan(driver=d);self.assertFalse(s['directory_enumeration_complete'])
        self.assertEqual(s['error'],'DIRECTORY_TOTAL_COUNT_UNAVAILABLE')

    def test_repeated_page_is_stall_not_end(self):
        d=FakeReader({'regular':[page([row('a')],0,False,2),page([row('a')],0,False,2)], 'folded':[page([])]})
        items,s=self.run_scan(driver=d)
        self.assertFalse(s['directory_enumeration_complete']);self.assertEqual(s['error'],'DIRECTORY_PAGINATION_STALLED')
        self.assertEqual(len(items),1)

    def test_nonoverlap_pages_fail_closed(self):
        d=FakeReader({'regular':[page([row('a')],0,False,2),page([row('b')],100,True,2)], 'folded':[page([])]})
        _,s=self.run_scan(driver=d);self.assertEqual(s['error'],'DIRECTORY_PAGINATION_GAP')

    def test_inconsistent_count_or_identity_is_partial(self):
        for second, expected in ((page([row('a'),row('b')],100,True,3),'DIRECTORY_TOTAL_COUNT_CHANGED'),
                                 (page([row('a','DIRECT'),row('b')],100,True,2),'DIRECTORY_IDENTITY_OR_METADATA_CHANGED')):
            d=FakeReader({'regular':[page([row('a')],0,False,2),second], 'folded':[page([])]})
            _,s=self.run_scan(driver=d);self.assertEqual(s['error'],expected)
            self.assertFalse(s['directory_enumeration_complete'])

    def test_end_count_mismatch_not_success(self):
        d=FakeReader({'regular':[page([row('a')],0,True,3)],'folded':[page([])]})
        _,s=self.run_scan(driver=d);self.assertEqual(s['error'],'DIRECTORY_TOTAL_COUNT_MISMATCH')

    def test_interruption_preserves_observed_rows(self):
        d=FakeReader({'regular':[page([row('a')],0,False,2),IMError('USER_INPUT_DETECTED_PAUSED')], 'folded':[page([])]})
        items,s=self.run_scan(driver=d);self.assertEqual(len(items),1)
        self.assertFalse(s['directory_enumeration_complete']);self.assertEqual(s['status'],'partial_directory')

    def test_wrong_account_no_completed_scope(self):
        items,s=self.run_scan(driver=FakeReader(failure='DIRECTORY_ACCOUNT_IDENTITY_CHANGED'))
        self.assertFalse(items);self.assertEqual(s['status'],'failed')

    def test_extra_message_preview_is_not_persisted(self):
        r=row('a');r['preview']='SHOULD_NOT_PERSIST'
        items,s=self.run_scan(driver=FakeReader({'regular':[page([r])],'folded':[page([])]}))
        self.assertNotIn('SHOULD_NOT_PERSIST',canonical([items,s]))
        self.assertEqual(s['error'],'DIRECTORY_METADATA_FIELDS_INVALID')

    def test_same_id_across_sections_records_fold_membership(self):
        d=FakeReader({'regular':[page([row('a')])],'folded':[page([row('a')])]})
        items,s=self.run_scan(driver=d);self.assertEqual(len(items),1)
        self.assertEqual(items[0]['source_parts'],['regular','folded'])
        self.assertEqual(items[0]['folded_state'],'observed_in_reviewed_folded_section')

    def test_source_epoch_changes_key(self):
        a,_=self.run_scan();p=account();p['source_epoch']='other-generation';b,_=self.run_scan(p)
        self.assertNotEqual(a[0]['key'],b[0]['key'])

    def test_bounds_reject_bool_and_oversize(self):
        for key,value in (('max_pages',True),('max_rows',20001),('max_pages',0)):
            p=profile();p[key]=value
            with self.assertRaises(IMError):validate_directory_profile(p)

    def test_plan_is_read_only_and_never_automatically_runs_ui(self):
        from im_hub.activity import discover_week
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);home=root/'home';initialize(home)
            cfg=root/'accounts.json';cfg.write_text(canonical({'schema':'im-hub-active-accounts/1','accounts':{'qq':account()}}),'utf-8')
            with patch.dict(os.environ, AUTH, clear=True), patch('im_hub.desktop_directory.UIADirectoryReader',return_value=FakeReader()):
                result=discover_week(home,cfg,stamp(START),stamp(END),allow_ui=True)
            before={str(f):digest(f.read_bytes()) for f in home.rglob('*') if f.is_file()}
            with patch('im_hub.desktop_directory.UIADirectoryReader',side_effect=AssertionError('client')):
                plan=plan_desktop_backfill(home,result['inventory_id'])
            self.assertEqual(before,{str(f):digest(f.read_bytes()) for f in home.rglob('*') if f.is_file()})
            self.assertTrue(plan['query_only']);self.assertFalse(plan['execution_performed'])
            self.assertEqual(len(plan['items']),2)
            self.assertTrue(all(i['status']=='requires_source_binding' for i in plan['items']))

    def test_discover_week_without_ui_records_not_scanned(self):
        from im_hub.activity import discover_week
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);home=root/'home';initialize(home)
            cfg=root/'accounts.json';cfg.write_text(canonical({'schema':'im-hub-active-accounts/1','accounts':{'qq':account()}}),'utf-8')
            with patch('im_hub.desktop_directory.UIADirectoryReader',side_effect=AssertionError('client')):
                result=discover_week(home,cfg,stamp(START),stamp(END))
            qq=next(p for p in result['platforms'] if p['platform']=='qq')
            self.assertEqual(qq['account_statuses'][0]['error'],'UI_CONSENT_REQUIRED')
            self.assertFalse(qq['eligible_local_backfill_complete'])
            self.assertFalse(result['four_platform_complete'])


if __name__ == '__main__':
    unittest.main()
