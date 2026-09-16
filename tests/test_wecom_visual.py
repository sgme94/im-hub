"""Synthetic control images and fake native payloads; never drive a real client."""
from __future__ import annotations
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from im_hub.common import IMError
from im_hub.desktop_runtime import check_policy, activation_requested, activate_bound
from im_hub.visual_desktop import prepare_visual_profile
from im_hub.wecom_visual import WECOM_ANCHORS, WeComReader, locate_many, bubble_point, scrollbar_geometry
from test_product import native_message

VISUAL=all(importlib.util.find_spec(p) is not None for p in ('numpy','cv2','PIL'))


def profile():
    anchors={k:{'path':k+'.png','sha256':'1'*64,'region':[.24,.08,.29,.86],
                'mode':'gray' if k.startswith('checkbox') else 'dark'} for k in WECOM_ANCHORS}
    return {'strategy':'visual-anchors-v1','profile_reviewed':True,'foreground_only':True,
            'max_pages':3,'message_region':[.246,.08,.835,.70],
            'bubble_colors':[[228,231,235]],'scrollbar_region':[.827,.07,.839,.713],
            'scrollbar_bottom_offset':52,'anchors':anchors}


class DesktopPolicyTests(unittest.TestCase):
    def test_deadline_expiration_includes_equality(self):
        for now in (1704067200,1704067201):
            with self.assertRaisesRegex(IMError,'EXPIRED'):
                check_policy({'IM_HUB_DESKTOP_UNTIL':'2024-01-01T00:00:00Z'},clock=lambda:now)
    def test_timezone_required(self):
        with self.assertRaises(IMError):check_policy({'IM_HUB_DESKTOP_UNTIL':'2024-01-01'},clock=lambda:0)
    def test_activation_without_deadline_refused(self):
        with self.assertRaisesRegex(IMError,'EXPLICIT_DEADLINE'):check_policy({'IM_HUB_ACTIVATE_CLIENTS':'1'})
    def test_future_deadline_allowed(self):
        self.assertIsNotNone(check_policy({'IM_HUB_DESKTOP_UNTIL':'2024-01-01T00:00:00Z'},clock=lambda:0))
    def test_stop_file_stops_before_actions(self):
        with tempfile.TemporaryDirectory() as d:
            stop=Path(d)/'STOP';stop.write_text('stop')
            with self.assertRaisesRegex(IMError,'STOP_REQUESTED'):check_policy({'IM_HUB_STOP_FILE':str(stop)})
    def test_activation_default_off(self):
        self.assertFalse(activation_requested({}))
    def test_activation_is_specific_optin(self):
        self.assertTrue(activation_requested({'IM_HUB_ACTIVATE_CLIENTS':'1','IM_HUB_DESKTOP_UNTIL':'2099-01-01T00:00:00Z'}))
    def test_missing_activation_target_no_focus_effect(self):
        s=SimpleNamespace(p={'platform':'wecom','conversation_name':'fixture'},g=Mock())
        s.g.EnumWindows.side_effect=lambda visit,arg:None
        with patch.dict('os.environ',{'IM_HUB_ACTIVATE_CLIENTS':'1','IM_HUB_DESKTOP_UNTIL':'2099-01-01T00:00:00Z'},clear=True):
            with self.assertRaisesRegex(IMError,'TARGET_MISSING'):activate_bound(s)
        s.g.SetForegroundWindow.assert_not_called()


class WeComProfileTests(unittest.TestCase):
    def test_complete_profile_without_version(self):
        p=prepare_visual_profile(Path.cwd(),profile(),'wecom')
        self.assertEqual(set(p['anchors']),WECOM_ANCHORS)
    def test_new_version_metadata_not_rejected(self):
        p=profile();p['client_version']='unseen-version'
        prepare_visual_profile(Path.cwd(),p,'wecom')
    def test_no_send_or_delete_anchor(self):
        for name in ('send','delete'):
            p=profile();p['anchors'][name]=p['anchors']['main_header']
            with self.assertRaisesRegex(IMError,'SET_MISMATCH'):prepare_visual_profile(Path.cwd(),p,'wecom')
    def test_composer_region_not_allowed(self):
        p=profile();p['message_region']=[.25,.6,.8,.95]
        with self.assertRaisesRegex(IMError,'UNSAFE'):prepare_visual_profile(Path.cwd(),p,'wecom')
    def test_checkbox_scan_not_full_window(self):
        p=profile();p['anchors']['checkbox_off']['region']=[0,0,1,1]
        with self.assertRaisesRegex(IMError,'UNSAFE'):prepare_visual_profile(Path.cwd(),p,'wecom')
    def test_missing_scrollbar_calibration_not_latest_claim(self):
        p=profile();p.pop('scrollbar_region')
        with self.assertRaises(IMError):prepare_visual_profile(Path.cwd(),p,'wecom')
    def test_invalid_colors_not_generic_fallback(self):
        for colors in ([],[[True,0,0]],[[1,2]],[[300,1,1]]):
            p=profile();p['bubble_colors']=colors
            with self.assertRaises(IMError):prepare_visual_profile(Path.cwd(),p,'wecom')
    def test_multi_copy_refused_before_input(self):
        r=object.__new__(WeComReader);r.s=Mock();r.selection=Mock();r.boxes=Mock(return_value=({'on':[{},{}],'off':[]},(0,0)))
        with self.assertRaisesRegex(IMError,'SELECTION_CHANGED'):r.copy(2)
        r.s.n.capture.assert_not_called()
    def test_selection_layout_change_refused(self):
        r=object.__new__(WeComReader);r.s=Mock();r.boxes=Mock(return_value=({'on':[{'x':100,'y':100}],'off':[]},(0,0)))
        with self.assertRaisesRegex(IMError,'LAYOUT_CHANGED'):r.select_one(0,[{'x':100,'y':130}])
        r.s.click.assert_not_called()
    def test_padding_difference_does_not_select_other_message(self):
        r=object.__new__(WeComReader);r.s=Mock();r.boxes=Mock(return_value=({'on':[{'x':100,'y':100}],'off':[]},(0,0)))
        r.select_one(0,[{'x':100,'y':102}]);r.s.click.assert_not_called()
    def test_copy_auto_exit_never_clicks_stale_close(self):
        if importlib.util.find_spec('uiautomation') is None:self.skipTest('Windows UI module absent')
        r=object.__new__(WeComReader);s=Mock();r.s=s;r.root=1;r.selection=Mock();r.normal=Mock()
        r.boxes=Mock(return_value=({'on':[{'x':100,'y':100}],'off':[{}]},(0,0)))
        s.n.user.GetClipboardSequenceNumber.side_effect=[1,2]
        s.n.capture.return_value=(native_message(),{'captured_at':'2024-01-02T00:00:00Z'})
        s.p={'account_namespace':'fixture','conversation_name':'测试群','binding':'66'};s.actions=0;s.anchor.return_value=(100,100)
        with patch('uiautomation.GetFocusedControl',return_value=SimpleNamespace(ControlTypeName='PaneControl')),patch('uiautomation.SendKeys') as keys:
            blob,rows,meta=r.copy(1)
        self.assertEqual(len(rows),1);s.click.assert_not_called();keys.assert_called_once_with('{Ctrl}c',waitTime=0)
    def test_hidden_scrollbar_is_exposed_not_assumed_latest(self):
        r=object.__new__(WeComReader);r.s=Mock();r.root=1;r.ui=profile()
        r.s.frame.return_value=(object(),(0,0));r.normal=Mock(return_value=(500,500));r.scroll=Mock()
        with patch('im_hub.wecom_visual.scrollbar_geometry',side_effect=[IMError('MESSAGE_SCROLLBAR_NOT_VISIBLE'),{'y':348,'height':100}]):
            result=r.reset_latest()
        self.assertTrue(result['latest_view_verified']);r.scroll.assert_called_once_with(down=True)
    def test_ambiguous_scrollbar_is_not_automatically_retried(self):
        r=object.__new__(WeComReader);r.s=Mock();r.root=1;r.ui=profile()
        r.s.frame.return_value=(object(),(0,0));r.normal=Mock(return_value=(500,500));r.scroll=Mock()
        with patch('im_hub.wecom_visual.scrollbar_geometry',side_effect=IMError('MESSAGE_SCROLLBAR_NOT_UNIQUE')):
            with self.assertRaisesRegex(IMError,'NOT_UNIQUE'):r.reset_latest()
        r.scroll.assert_not_called()
    def test_permanently_missing_scrollbar_is_bounded_failure(self):
        r=object.__new__(WeComReader);r.s=Mock();r.root=1;r.ui=profile()
        r.s.frame.return_value=(object(),(0,0));r.normal=Mock(return_value=(500,500));r.scroll=Mock()
        with patch('im_hub.wecom_visual.scrollbar_geometry',side_effect=IMError('MESSAGE_SCROLLBAR_NOT_VISIBLE')):
            with self.assertRaisesRegex(IMError,'WITHIN_BOUND'):r.reset_latest()
        self.assertEqual(r.scroll.call_count,12)
    def test_copy_rejects_input_focus(self):
        if importlib.util.find_spec('uiautomation') is None:self.skipTest('Windows UI module absent')
        r=object.__new__(WeComReader);r.s=Mock();r.selection=Mock();r.boxes=Mock(return_value=({'on':[{}],'off':[]},(0,0)))
        with patch('uiautomation.GetFocusedControl',return_value=SimpleNamespace(ControlTypeName='EditControl')),patch('uiautomation.SendKeys') as keys:
            with self.assertRaisesRegex(IMError,'FOCUS_IS_INPUT'):r.copy(1)
        keys.assert_not_called()


@unittest.skipUnless(VISUAL,'optional visual libraries absent')
class WeComImageTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np=np
        self.tpl=(np.random.default_rng(44).integers(0,2,(20,20))*255).astype(np.uint8)
        self.image=np.full((400,300),245,np.uint8)
        for y in (50,150,250):self.image[y:y+20,60:80]=self.tpl
    def test_all_checkboxes_ordered(self):
        result=locate_many(self.image,self.tpl,[.15,.05,.30,.90])
        self.assertEqual([r['y'] for r in result],[60,160,260])
    def test_checkbox_limit_raises(self):
        with self.assertRaisesRegex(IMError,'LIMIT'):locate_many(self.image,self.tpl,[0,0,1,1],limit=2)
    def test_no_checkbox_not_fabricated(self):
        self.assertEqual(locate_many(self.image,self.tpl,[.5,0,1,1]),[])
    def test_blank_checkbox_template_rejected(self):
        with self.assertRaises(IMError):locate_many(self.image,self.np.full((20,20),255,self.np.uint8),[0,0,1,1])
    def test_bubble_point_inside_solid_body(self):
        a=self.np.full((600,1000,3),[245,247,250],self.np.uint8);a[230:330,300:700]=[228,231,235]
        p=bubble_point(a,[.2,.1,.9,.7],[[228,231,235]])
        self.assertTrue(300<=p['x']<700 and 230<=p['y']<330)
    def test_composer_and_sidebar_excluded(self):
        a=self.np.full((600,1000,3),[245,247,250],self.np.uint8);a[490:570,300:700]=[228,231,235];a[100:330,920:990]=[228,231,235]
        with self.assertRaisesRegex(IMError,'NO_REVIEWED'):bubble_point(a,[.2,.1,.9,.7],[[228,231,235]])
    def test_scrollbar_requires_unique_geometry(self):
        a=self.np.full((600,1000,3),245,self.np.uint8);a[250:450,833:843]=[188,192,196]
        p=scrollbar_geometry(a,[.82,.1,.85,.8]);self.assertEqual(p['y']+p['height'],450)
        a[100:200,833:843]=[188,192,196]
        with self.assertRaisesRegex(IMError,'NOT_UNIQUE'):scrollbar_geometry(a,[.82,.1,.85,.8])
    def test_missing_scrollbar_not_latest(self):
        a=self.np.full((600,1000,3),245,self.np.uint8)
        with self.assertRaises(IMError):scrollbar_geometry(a,[.82,.1,.85,.8])


if __name__=='__main__':unittest.main()
