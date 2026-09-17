"""Recovery and upstream-config fences; synthetic captures, no real GUI."""
import json
import unittest
from unittest.mock import patch

from im_hub.common import IMError, canonical
from im_hub.desktop_week_collection import collect_desktop_week
import test_desktop_week_collection as base
from test_core import fake_backend


class CollectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case=base.DesktopWeekCollectionTests('test_retry_uses_exact_captured_run_not_another_acquisition')
        self.case.setUp()

    def tearDown(self):
        self.case.tearDown()

    def test_unknown_outcome_resumes_same_offset_not_end_of_list(self):
        c=self.case
        def unknown(*a,**kw):raise IMError('SYNTHETIC_LOST_RESPONSE')
        result=c.run_collection(collector=unknown)
        self.assertTrue(result['stopped_early'])
        self.assertEqual(result['next_offset'],0)
        self.assertEqual(result['retry_offsets'],[0])
        retry=c.run_collection(offset=result['next_offset'],collector=lambda *a,**kw:1/0)
        self.assertEqual(retry['new_capture_calls'],0)
        self.assertEqual(retry['retry_offsets'],[0])

    def test_held_binding_retains_retry_offset_separate_from_scan_cursor(self):
        c=self.case;c.source['conversation_id']='wrong';c.save()
        result=c.run_collection(collector=lambda *a,**kw:1/0)
        self.assertEqual(result['retry_offsets'],[0])
        self.assertIsNone(result['next_offset'])
        self.assertEqual(result['committed_conversations'],0)

    def test_success_clears_retry_offsets_and_finishes_scan(self):
        result=self.case.run_collection()
        self.assertEqual(result['retry_offsets'],[])
        self.assertIsNone(result['next_offset'])
        self.assertEqual(result['committed_conversations'],1)

    def test_original_config_change_inside_pinned_import_no_completion_receipt(self):
        c=self.case;c.source={**c.f.binding};c.save()
        original=c.config.read_bytes()
        def backend(home,args):
            result=fake_backend(home,args)
            if args[0]=='import':
                c.config.write_text(canonical({'version':1,'sources':{}}),'utf-8')
            return result
        with self.assertRaisesRegex(IMError,'CONFIG_CHANGED'):
            collect_desktop_week(c.f.home,c.f.inventory,c.config,backend=backend)
        self.assertFalse(list(c.f.folder.glob('results/*.json')))
        c.config.write_bytes(original)
        resumed=c.run_collection(collector=lambda *a,**kw:1/0)
        self.assertEqual(resumed['new_capture_calls'],0)
        self.assertEqual(resumed['committed_conversations'],1)
        self.assertEqual(resumed['new_records'],0)

    def test_guard_change_after_readback_preserves_existing_receipt(self):
        c=self.case;c.source={**c.f.binding};c.save();c.run_collection()
        receipt=next(c.f.folder.glob('results/*.json'));before=receipt.read_bytes()
        import im_hub.desktop_backfill as module
        original=module.verify_desktop_receipt
        def verify(*a,**kw):
            checked=original(*a,**kw)
            c.config.write_text(canonical({'version':1,'sources':{}}),'utf-8')
            return checked
        with patch.object(module,'verify_desktop_receipt',side_effect=verify):
            with self.assertRaisesRegex(IMError,'CONFIG_CHANGED'):
                c.run_collection(collector=lambda *a,**kw:1/0)
        self.assertEqual(before,receipt.read_bytes())


if __name__=='__main__':unittest.main()
