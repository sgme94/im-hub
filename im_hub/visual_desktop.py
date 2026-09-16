"""Deterministic, opt-in visual anchors for custom-rendered IM controls.

Images are operator-reviewed private calibration, never OCR/model output. This
strategy starts from the bound foreground client; it does not discover or launch
hidden clients. It rejects ambiguous anchors, focus/input changes and missing
native metadata. Successful bounded capture is NOT all-history completeness.
"""
from __future__ import annotations
import ctypes
import io
import math
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from .common import IMError, MAX_BYTES, digest, now, read_blob
from .database_readers import local_path

STRATEGY = 'visual-anchors-v1'
TIM_ANCHORS = {'main_header', 'group_item', 'history_toggle', 'manager_button', 'manager_header', 'manager_group', 'export_menu'}


def region(value):
    if (not isinstance(value, list) or len(value) != 4 or
        any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value) or
        not (0 <= value[0] < value[2] <= 1 and 0 <= value[1] < value[3] <= 1)):
        raise IMError('INVALID_VISUAL_SEARCH_REGION')
    return value


def prepare_visual_profile(base: Path, ui: dict, platform: str) -> dict:
    if not isinstance(ui, dict) or ui.get('strategy') != STRATEGY or ui.get('profile_reviewed') is not True:
        raise IMError('REVIEWED_VISUAL_PROFILE_REQUIRED')
    if ui.get('foreground_only') is not True:
        raise IMError('VISUAL_FOREGROUND_CLIENT_REQUIRED')
    if platform != 'qq':
        raise IMError('VISUAL_STRATEGY_CURRENTLY_TIM_ONLY')
    required = TIM_ANCHORS
    if not isinstance(ui.get('anchors'), dict) or set(ui['anchors']) != required:
        raise IMError('VISUAL_ANCHOR_SET_MISMATCH')
    result = {**ui, 'anchors': {}}
    for key, value in ui['anchors'].items():
        if not isinstance(value, dict) or not set(value) <= {'path', 'sha256', 'region', 'mode'}:
            raise IMError('INVALID_VISUAL_ANCHOR')
        path = local_path(base, value.get('path'))
        if path.suffix.lower() != '.png' or not re.fullmatch('[0-9a-f]{64}', str(value.get('sha256', ''))):
            raise IMError('VISUAL_ANCHOR_HASH_REQUIRED')
        region(value.get('region'))
        if value.get('mode', 'dark') not in ('dark', 'light', 'gray'):
            raise IMError('INVALID_VISUAL_ANCHOR_MODE')
        result['anchors'][key] = {**value, 'path': str(path)}
    pages = ui.get('max_pages', 3)
    if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 20:
        raise IMError('VISUAL_PAGE_LIMIT')
    result['max_pages'] = pages
    return result


def _libraries():
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageGrab
        return cv2, np, Image, ImageGrab
    except ImportError:
        raise IMError('INSTALL_VISUAL_WINDOWS_EXTRA_REQUIRED') from None


def _mask(array, mode):
    cv, np, _, _ = _libraries()
    if len(array.shape) == 3:
        array = cv.cvtColor(array, cv.COLOR_RGB2GRAY)
    if mode == 'dark': return (array < 110).astype(np.uint8) * 255
    if mode == 'light': return (array > 220).astype(np.uint8) * 255
    return array


def locate(image, template, search_region, mode='dark', threshold=.95):
    """Find one uniquely supported pixel anchor. Coordinates belong to this image."""
    cv, np, _, _ = _libraries()
    region(search_region)
    source = np.asarray(image)
    h, w = source.shape[:2]
    left, top, right, bottom = [int(x * size) for x, size in zip(search_region, (w, h, w, h))]
    tpl = _mask(np.asarray(template), mode)
    th, tw = tpl.shape
    if not 6 <= tw <= 800 or not 6 <= th <= 300 or float(tpl.std()) < 5:
        raise IMError('INVALID_VISUAL_TEMPLATE_DIMENSIONS_OR_CONTRAST')
    target = _mask(source[top:bottom, left:right], mode)
    if target.shape[0] < th or target.shape[1] < tw:
        raise IMError('VISUAL_ANCHOR_REGION_TOO_SMALL')
    scores = cv.matchTemplate(target, tpl, cv.TM_CCOEFF_NORMED)
    _, score, _, point = cv.minMaxLoc(scores)
    if not math.isfinite(score) or score < threshold:
        raise IMError('VISUAL_ANCHOR_NOT_FOUND')
    x, y = point
    remaining = scores.copy()
    remaining[max(0,y-th//2):y+th//2+1, max(0,x-tw//2):x+tw//2+1] = -1
    if float(remaining.max()) >= threshold:
        raise IMError('VISUAL_ANCHOR_AMBIGUOUS')
    return {'x': left+x+tw//2, 'y': top+y+th//2, 'score': round(score, 6),
            'box': [left+x, top+y, left+x+tw, top+y+th]}


def wait_export(path: Path, timeout=35, guard=lambda: None, pause=time.sleep):
    """A created file can still be write-locked. Never alter permissions to read it."""
    deadline = time.monotonic() + timeout
    previous = None; stable = 0
    while time.monotonic() < deadline:
        guard()
        try:
            data = read_blob(path)
            signature = digest(data)
            stable = stable + 1 if data and signature == previous else 0
            previous = signature
            if stable >= 3:
                if os.name == 'nt':
                    import win32file, win32con
                    handle = win32file.CreateFile(str(path), win32con.GENERIC_READ, win32con.FILE_SHARE_READ,
                                                 None, win32con.OPEN_EXISTING, 0, None)
                    handle.Close()
                return data
        except (PermissionError, FileNotFoundError):
            stable = 0
        except IMError as exc:
            if exc.code != 'SOURCE_MISSING_OR_TOO_LARGE' or path.exists(): raise
            stable = 0
        except Exception as exc:
            # Win32 sharing-violation errors are not OSError subclasses.
            if getattr(exc, 'winerror', None) not in (2, 32, 33): raise
            stable = 0
        pause(.25)
    raise IMError('TIM_EXPORT_NOT_FINISHED')


class VisualSession:
    def __init__(self, profile):
        if os.name != 'nt': raise IMError('WINDOWS_REQUIRED')
        from .windows_desktop import NativeWindows, accepts_client
        import win32gui
        from pywinauto import mouse, Desktop
        self.n = NativeWindows(); self.g = win32gui; self.mouse = mouse
        self.Desktop = Desktop; self.accepts = accepts_client
        self.p = profile; self.ui = profile['desktop']; self.pid = None
        self.actions = 0; self.matches = []; self.templates = {}; self.pages = []
        self.cv, self.np, self.Image, self.Grab = _libraries()
        # Physical screen space; no hardcoded DPI or software-version requirement.
        self.n.user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.n.user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p

    def guard(self):
        if time.monotonic() > self.deadline: raise IMError('DESKTOP_RUN_TIME_LIMIT')
        if self.n.input_tick() != self.last_input: raise IMError('USER_INPUT_DETECTED_PAUSED')
        if any(self.n.api.GetAsyncKeyState(k) & 0x8000 for k in (1, 2, 16, 17, 18)):
            raise IMError('HELD_INPUT_DETECTED_PAUSED')
        h = self.g.GetForegroundWindow()
        if not h or self.n.proc.GetWindowThreadProcessId(h)[1] != self.pid:
            raise IMError('CLIENT_LOST_FOREGROUND_PAUSED')
        return h

    @contextmanager
    def lease(self):
        k = self.n.kernel
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k.CreateMutexW.restype = ctypes.c_void_p
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k.ReleaseMutex.argtypes = [ctypes.c_void_p]
        mutex = k.CreateMutexW(None, False, 'Local\\im-hub-desktop-v1')
        if not mutex: raise IMError('DESKTOP_LEASE_UNAVAILABLE')
        locked = False; old_dpi = None
        try:
            if k.WaitForSingleObject(mutex, 0) not in (0, 0x80): raise IMError('DESKTOP_BUSY')
            locked = True
            old_dpi = self.n.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            h = self.g.GetForegroundWindow()
            self.pid = self.n.proc.GetWindowThreadProcessId(h)[1]
            identity = self.n.process_identity(self.pid)
            if not self.accepts(self.p['platform'], identity): raise IMError('BOUND_CLIENT_NOT_FOREGROUND')
            self.version = identity[1]; self.n.pid = self.pid
            self.last_input = self.n.input_tick(); self.deadline = time.monotonic() + 120
            if (self.n.api.GetTickCount() - self.last_input) & 0xffffffff < 1500:
                raise IMError('USER_ACTIVE_RETRY_WHEN_IDLE')
            self.guard()
            for key, spec in self.ui['anchors'].items():
                data = read_blob(Path(spec['path']))
                if len(data) > 1024*1024 or digest(data) != spec['sha256']:
                    raise IMError('VISUAL_TEMPLATE_HASH_MISMATCH')
                with self.Image.open(io.BytesIO(data)) as im:
                    if im.format != 'PNG' or im.width > 800 or im.height > 300:
                        raise IMError('INVALID_VISUAL_TEMPLATE')
                    self.templates[key] = im.convert('RGB').copy()
            yield self
        finally:
            if old_dpi: self.n.user.SetThreadDpiAwarenessContext(old_dpi)
            if locked: k.ReleaseMutex(mutex)
            k.CloseHandle(mutex)

    def frame(self, hwnd=None):
        current = self.guard(); hwnd = hwnd or current
        if not self.g.IsWindowVisible(hwnd) or self.g.IsIconic(hwnd): raise IMError('CLIENT_WINDOW_NOT_VISIBLE')
        if self.n.proc.GetWindowThreadProcessId(hwnd)[1] != self.pid: raise IMError('WRONG_CLIENT_WINDOW')
        l,t,r,b = self.g.GetWindowRect(hwnd)
        sw,sh = self.n.api.GetSystemMetrics(0),self.n.api.GetSystemMetrics(1)
        # Small native borders may extend outside the primary display.
        l,t,r,b = max(0,l),max(0,t),min(sw,r),min(sh,b)
        if r-l < 100 or b-t < 100: raise IMError('CLIENT_WINDOW_TOO_SMALL')
        return self.Grab.grab(bbox=(l,t,r,b)), (l,t)

    def anchor(self, key, hwnd=None, optional=False):
        image, origin = self.frame(hwnd)
        spec = self.ui['anchors'][key]
        try:
            found = locate(image, self.templates[key], spec['region'], spec.get('mode','dark'))
        except IMError as exc:
            if optional and exc.code == 'VISUAL_ANCHOR_NOT_FOUND': return None
            raise IMError(exc.code + '_' + key.upper()) from None
        self.matches.append({'anchor': key, 'score': found['score']})
        return (found['x']+origin[0],found['y']+origin[1])

    def click(self, point, button='left'):
        self.guard(); x,y = point
        target = self.g.WindowFromPoint((int(x),int(y)))
        if not target or self.n.proc.GetWindowThreadProcessId(target)[1] != self.pid:
            raise IMError('VISUAL_POINT_OCCLUDED_OR_WRONG_PROCESS')
        self.mouse.click(button=button,coords=(int(x),int(y)))
        self.actions += 1; self.last_input=self.n.input_tick();time.sleep(.2);self.guard()

    def await_title(self, titles, timeout=5):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            hwnd=self.guard()
            if self.g.GetWindowText(hwnd) in titles: return hwnd
            time.sleep(.1)
        raise IMError('EXPECTED_NATIVE_DIALOG_NOT_OPEN')

    def await_anchor(self, key, hwnd, timeout=8):
        # Opening history can render its toolbar after the native window appears.
        # Wait for exact evidence; never lower the matching threshold or click blind.
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            point = self.anchor(key, hwnd, optional=True)
            if point is not None:
                return point
            time.sleep(.15)
        raise IMError('VISUAL_ANCHOR_WAIT_TIMEOUT_' + key.upper())

    def tim(self, folder):
        hwnd=self.guard()
        if self.g.GetWindowText(hwnd) != '消息管理器':
            self.main=hwnd
            if not self.anchor('main_header',hwnd,optional=True):
                self.click(self.anchor('group_item',hwnd))
            self.anchor('main_header',hwnd)
            target=self.anchor('manager_button',hwnd,optional=True)
            if target is None:
                self.click(self.anchor('history_toggle',hwnd))
                target=self.await_anchor('manager_button',hwnd)
            self.click(target);hwnd=self.await_title({'消息管理器'})
        else: self.main=None
        manager=hwnd;self.await_anchor('manager_header',manager)
        self.click(self.await_anchor('manager_group',manager),button='right')
        menus=[w for w in self.Desktop(backend='uia').windows() if w.window_text()=='TXMenuWindow' and w.process_id()==self.pid]
        if len(menus)!=1: raise IMError('TIM_EXPORT_MENU_NOT_UNIQUE')
        names=[c.element_info.name for c in menus[0].descendants(control_type='MenuItem')]
        if names.count('导出消息记录(&E)')!=1:raise IMError('TIM_EXPORT_MENU_CAPABILITY_MISSING')
        # Custom TIM menus advertise Invoke/mnemonics but neither worked on the
        # observed client. Recheck the rendered, unique export label instead.
        self.click(self.anchor('export_menu', menus[0].handle))
        dialog=self.await_title({'另存为','Save As'})
        d=self.Desktop(backend='win32').window(handle=dialog).wrapper_object()
        fields=[c for c in d.descendants(class_name='Edit') if c.control_id()==1148]
        combos=[c for c in d.descendants(class_name='ComboBox') if c.control_id()==1136]
        saves=[c for c in d.descendants(class_name='Button') if c.control_id()==1]
        if tuple(map(len,(fields,combos,saves)))!=(1,1,1):raise IMError('TIM_NATIVE_SAVE_CAPABILITY_MISSING')
        filename=fields[0].window_text()
        if Path(filename).stem!=self.p['conversation_name'] or Path(filename).suffix.lower() not in ('.bak','.txt','.mht'):
            raise IMError('TIM_EXPORT_FILENAME_CONTEXT_MISMATCH')
        choices=combos[0].item_texts()
        matches=[s for s in choices if s.startswith('文本文件(') and '*.txt' in s]
        if len(matches)!=1:raise IMError('TIM_TEXT_FORMAT_NOT_UNIQUE')
        self.guard();combos[0].select(matches[0]);self.last_input=self.n.input_tick()
        if combos[0].selected_text()!=matches[0]:raise IMError('TIM_TEXT_FORMAT_SELECTION_FAILED')
        path=(folder/'official-export.txt').resolve()
        if path.exists():raise IMError('TIM_EXPORT_DESTINATION_EXISTS')
        fields[0].set_edit_text(str(path))
        if fields[0].window_text()!=str(path):raise IMError('TIM_SAVE_PATH_NOT_SET')
        self.guard();saves[0].click();self.actions+=1;self.last_input=self.n.input_tick()
        data=wait_export(path,guard=self.guard)
        from .codecs.desktop import parse_tim
        rows=parse_tim(data,self.p['conversation_name'])
        self.pages.append({'records':len(rows),'group_header_verified':True,'filename_verified':True,'native_export':True})
        # Close only our completed manager, not the client or any uncertain dialog.
        if self.g.GetForegroundWindow()==manager:
            import win32con
            self.g.PostMessage(manager,win32con.WM_CLOSE,0,0);time.sleep(.2)
        return [data]


def acquire_visual(profile, folder):
    session=VisualSession(profile)
    with session.lease():
        if profile['platform']=='qq': blobs=session.tim(folder)
        else: raise IMError('WECOM_VISUAL_CAPTURE_NOT_YET_CALIBRATED')
    return blobs,{'transport':profile['transport'],'captured_at':now(),'ui_performed':True,
                  'client_version':session.version,'version_policy':'capability_probe_not_version_whitelist',
                  'driver':STRATEGY,'input_interruption_detected':False,'llm_calls':0,'ocr_calls':0,
                  'automated_actions':session.actions,'anchors':session.matches,'pages':session.pages,
                  'history_complete':False,'requires_initial_foreground':True}
