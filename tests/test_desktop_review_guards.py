"""Review regressions: bounded planning and immutable snapshot fences; synthetic only."""
from __future__ import annotations
import json
import unittest
from unittest.mock import patch
from im_hub.common import IMError, canonical, digest
from im_hub.activity import week_status
from im_hub.desktop_directory import plan_desktop_backfill
from im_hub.desktop_backfill import backfill_desktop_week, load_capture
import test_desktop_backfill as fixtures
from test_desktop_directory import FakeReader, page, row


class DesktopReviewGuards(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DesktopBackfillTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def change_inventory(self):
        f=self.fixture
        path=f.folder/'inventory.json'
        data=json.loads(path.read_text(encoding='utf-8'))
        data['conversations'][0]['coverage_gaps'].append('synthetic_concurrent_replacement')
        raw=canonical(data).encode('utf-8')
        path.write_bytes(raw)
        (f.folder/'inventory.sha256').write_text(digest(raw)+'\n',encoding='ascii')

    def two_candidates(self):
        f=self.fixture
        f.make_inventory(driver=FakeReader({'regular':[page([row('r1',name='测试群'),row('r2',name='测试群')])],'folded':[page([])]}))
        b={**f.binding,'conversation_id':'r2','directory_binding':{'native_conversation_id':'r2','conversation_type':'group'}}
        f.save_binding({'bound-two':b})

    def test_plan_reads_only_requested_page_captures(self):
        self.two_candidates();f=self.fixture
        with patch('im_hub.desktop_backfill.load_capture',return_value={}) as load:
            p=plan_desktop_backfill(f.home,f.inventory,f.config,limit=1,offset=0)
        self.assertEqual(p['total_items'],2);self.assertEqual(p['next_offset'],1)
        self.assertEqual(load.call_count,1)
        self.assertEqual(load.call_args.args[1]['key'],p['items'][0]['key'])

    def test_empty_page_does_not_open_any_capture(self):
        self.two_candidates();f=self.fixture
        with patch('im_hub.desktop_backfill.load_capture',return_value={}) as load:
            p=plan_desktop_backfill(f.home,f.inventory,f.config,limit=1,offset=2)
        self.assertFalse(p['items']);load.assert_not_called()

    def test_plan_rejects_validly_rehashed_inventory_change(self):
        f=self.fixture
        def changed(*args,**kwargs):
            value=load_capture(*args,**kwargs);self.change_inventory();return value
        with patch('im_hub.desktop_backfill.load_capture',side_effect=changed):
            with self.assertRaisesRegex(IMError,'INVENTORY_CHANGED'):
                plan_desktop_backfill(f.home,f.inventory,f.config)

    def test_plan_rejects_binding_config_changed_during_capture_check(self):
        f=self.fixture
        def changed(*args,**kwargs):
            value=load_capture(*args,**kwargs)
            f.binding['enabled']=False;f.save_binding();return value
        with patch('im_hub.desktop_backfill.load_capture',side_effect=changed):
            with self.assertRaisesRegex(IMError,'CONFIG_CHANGED'):
                plan_desktop_backfill(f.home,f.inventory,f.config)

    def test_backfill_does_not_mix_preplan_inventory_with_new_plan(self):
        f=self.fixture
        def changed(*args,**kwargs):
            self.change_inventory();return plan_desktop_backfill(*args,**kwargs)
        with patch('im_hub.desktop_directory.plan_desktop_backfill',side_effect=changed), patch('im_hub.desktop_backfill.ingest') as ingest:
            with self.assertRaisesRegex(IMError,'INVENTORY_CHANGED'):
                backfill_desktop_week(f.home,f.inventory,f.config)
            ingest.assert_not_called()
        self.assertFalse(list((f.folder/'results').glob('*.json')))

    def test_backfill_snapshot_replacement_during_import_never_gets_receipt(self):
        f=self.fixture
        from test_core import fake_backend
        def changed(home,args):
            value=fake_backend(home,args)
            if args[0]=='import':self.change_inventory()
            return value
        with self.assertRaisesRegex(IMError,'INVENTORY_CHANGED'):
            backfill_desktop_week(f.home,f.inventory,f.config,backend=changed)
        self.assertFalse(list((f.folder/'results').glob('*.json')))

    def test_receipt_numeric_types_are_not_coerced(self):
        f=self.fixture;f.fill();path=next((f.folder/'results').glob('*.json'))
        original=json.loads(path.read_text(encoding='utf-8'))
        for field,value in (('records',2.0),('body_gap_records',False)):
            with self.subTest(field=field):
                bad={**original,field:value};path.write_text(canonical(bad),encoding='utf-8')
                s=week_status(f.home,f.inventory,verify=True)
                self.assertEqual(s['desktop_captured_conversations'],0)
                self.assertEqual(s['desktop_capture_failures'],1)
        path.write_text(canonical(original),encoding='utf-8')

    def test_receipt_cannot_promote_partial_capture_to_complete_history(self):
        f=self.fixture;f.fill();path=next((f.folder/'results').glob('*.json'))
        original=json.loads(path.read_text(encoding='utf-8'))
        for field,value in (('history_complete',True),('complete_through',original['window']['until_exclusive']),('ui_used',True)):
            with self.subTest(field=field):
                path.write_text(canonical({**original,field:value}),encoding='utf-8')
                s=week_status(f.home,f.inventory,verify=True)
                self.assertEqual(s['desktop_captured_conversations'],0)
                self.assertEqual(s['desktop_capture_failures'],1)
        path.write_text(canonical(original),encoding='utf-8')


if __name__=='__main__':unittest.main()
