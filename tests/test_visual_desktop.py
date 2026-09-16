"""Synthetic anchors and mocked file readiness. No desktop actions in these tests."""
from __future__ import annotations
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from im_hub.common import IMError, digest
from im_hub.visual_desktop import TIM_ANCHORS, locate, prepare_visual_profile, region, wait_export

AVAILABLE = all(importlib.util.find_spec(x) is not None for x in ('numpy', 'cv2', 'PIL'))


def profile():
    return {'strategy':'visual-anchors-v1','profile_reviewed':True,'foreground_only':True,
            'anchors':{key:{'path':key+'.png','sha256':'1'*64,'region':[0,0,1,1],'mode':'dark'} for key in TIM_ANCHORS}}


class VisualProfileTests(unittest.TestCase):
    def test_static_validation_does_not_open_client_or_source_images(self):
        with patch('im_hub.visual_desktop.read_blob', side_effect=AssertionError('read')):
            result=prepare_visual_profile(Path.cwd(),profile(),'qq')
        self.assertEqual(len(result['anchors']),7)
    def test_visual_consent_is_explicit(self):
        p=profile();p['profile_reviewed']=False
        with self.assertRaises(IMError):prepare_visual_profile(Path.cwd(),p,'qq')
    def test_foreground_precondition_not_silent_activation(self):
        p=profile();p['foreground_only']=False
        with self.assertRaisesRegex(IMError,'FOREGROUND'):prepare_visual_profile(Path.cwd(),p,'qq')
    def test_tim_profile_cannot_be_used_as_wecom(self):
        with self.assertRaisesRegex(IMError,'ANCHOR_SET_MISMATCH'):prepare_visual_profile(Path.cwd(),profile(),'wecom')
    def test_source_version_is_not_required(self):
        for version in ('999',None):
            p=profile();p['client_version']=version
            self.assertEqual(prepare_visual_profile(Path.cwd(),p,'qq')['strategy'],'visual-anchors-v1')
    def test_unknown_send_anchor_rejected(self):
        p=profile();p['anchors']['send_button']=p['anchors']['group_item']
        with self.assertRaises(IMError):prepare_visual_profile(Path.cwd(),p,'qq')
    def test_anchor_hash_required(self):
        p=profile();p['anchors']['group_item']['sha256']='bad'
        with self.assertRaisesRegex(IMError,'HASH'):prepare_visual_profile(Path.cwd(),p,'qq')
    def test_nonimage_anchor_rejected(self):
        p=profile();p['anchors']['group_item']['path']='commands.ps1'
        with self.assertRaises(IMError):prepare_visual_profile(Path.cwd(),p,'qq')
    def test_network_anchor_rejected(self):
        p=profile();p['anchors']['group_item']['path']='https://example.invalid/x.png'
        with self.assertRaises(IMError):prepare_visual_profile(Path.cwd(),p,'qq')
    def test_bad_region_rejected(self):
        for box in ([0,0,2,1],[0,0,0,1],[0,1,1,0],[0,0,float('nan'),1],[False,0,1,1],[0,0,1]):
            with self.assertRaises(IMError):region(box)
    def test_page_work_bound(self):
        for bad in (0,21,True):
            p=profile();p['max_pages']=bad
            with self.assertRaises(IMError):prepare_visual_profile(Path.cwd(),p,'qq')
    def test_wait_for_writer_release_without_chmod(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'export.txt';p.write_bytes(b'fixture')
            with patch('im_hub.visual_desktop.read_blob',side_effect=[PermissionError(),b'fixture',b'fixture',b'fixture',b'fixture']):
                self.assertEqual(wait_export(p,pause=lambda _:None),b'fixture')
            self.assertEqual(p.read_bytes(),b'fixture')
    def test_input_interruption_aborts_file_wait(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'export.txt';p.write_bytes(b'fixture')
            def stop():raise IMError('USER_INPUT_DETECTED_PAUSED')
            with self.assertRaisesRegex(IMError,'USER_INPUT'):wait_export(p,guard=stop,pause=lambda _:None)
            self.assertEqual(p.read_bytes(),b'fixture')
    def test_render_wait_retries_only_absent_anchor(self):
        from im_hub.visual_desktop import VisualSession
        from unittest.mock import Mock
        session=object.__new__(VisualSession)
        session.anchor=Mock(side_effect=[None,None,(10,20)])
        with patch('im_hub.visual_desktop.time.sleep'):
            self.assertEqual(session.await_anchor('manager_button',1),(10,20))
        self.assertEqual(session.anchor.call_count,3)
    def test_render_wait_does_not_ignore_ambiguity(self):
        from im_hub.visual_desktop import VisualSession
        from unittest.mock import Mock
        session=object.__new__(VisualSession)
        session.anchor=Mock(side_effect=IMError('VISUAL_ANCHOR_AMBIGUOUS'))
        with self.assertRaisesRegex(IMError,'AMBIGUOUS'):session.await_anchor('manager_button',1)
        self.assertEqual(session.anchor.call_count,1)
    def test_render_wait_is_bounded(self):
        from im_hub.visual_desktop import VisualSession
        session=object.__new__(VisualSession)
        with self.assertRaisesRegex(IMError,'WAIT_TIMEOUT'):session.await_anchor('manager_button',1,timeout=0)
    def test_export_timeout_is_not_empty_success(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaisesRegex(IMError,'NOT_FINISHED'):wait_export(Path(t)/'missing.txt',timeout=0)


@unittest.skipUnless(AVAILABLE,'optional visual image dependencies not installed')
class VisualMatchingTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np=np
        self.template=(np.random.default_rng(123).integers(0,2,(25,70))*255).astype(np.uint8)
        self.image=np.full((240,400),245,dtype=np.uint8)
        self.image[70:95,80:150]=self.template
    def test_exact_unique_anchor(self):
        found=locate(self.image,self.template,[0,0,1,1])
        self.assertEqual(found['box'],[80,70,150,95]);self.assertGreater(found['score'],.99)
    def test_ambiguous_anchor_rejected(self):
        self.image[140:165,230:300]=self.template
        with self.assertRaisesRegex(IMError,'AMBIGUOUS'):locate(self.image,self.template,[0,0,1,1])
    def test_anchor_outside_allowed_region_not_clicked(self):
        with self.assertRaisesRegex(IMError,'NOT_FOUND'):locate(self.image,self.template,[.5,0,1,1])
    def test_stale_visual_pattern_not_guessed(self):
        other=(self.np.random.default_rng(456).integers(0,2,(25,70))*255).astype(self.np.uint8)
        with self.assertRaisesRegex(IMError,'NOT_FOUND'):locate(self.image,other,[0,0,1,1])
    def test_blank_template_rejected(self):
        with self.assertRaisesRegex(IMError,'CONTRAST'):locate(self.image,self.np.full((25,70),255,self.np.uint8),[0,0,1,1])
    def test_small_region_rejected(self):
        with self.assertRaisesRegex(IMError,'TOO_SMALL'):locate(self.image,self.template,[0,0,.02,.02])
    def test_stale_uia_coordinates_are_irrelevant(self):
        found=locate(self.image,self.template,[0,0,1,1])
        stale_uia_point=(10,10)
        self.assertNotEqual((found['x'],found['y']),stale_uia_point)

if __name__=='__main__':unittest.main()
