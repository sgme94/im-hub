"""Opt-in Windows desktop acquisition through reviewed semantic selectors.
No shell recipes, arbitrary key strings, OCR, model calls, login or send actions.
This driver requires per-client profile calibration. Unit tests are not UI acceptance.
"""
from __future__ import annotations
import ctypes
import os
import time
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from .common import IMError, MAX_BYTES, digest, now, read_blob

# Executable identity is a source boundary; product version is telemetry only.
CLIENTS = {'qq': 'tim.exe', 'wecom': 'wxwork.exe'}
VERSION_POLICY = 'capability_probe_not_version_whitelist'


def accepts_client(platform, identity):
    return (platform in CLIENTS and isinstance(identity, tuple) and len(identity) == 2
            and isinstance(identity[0], str) and identity[0].lower() == CLIENTS[platform])
CONTROL_TYPES = {'ButtonControl', 'MenuItemControl', 'ListItemControl', 'TextControl', 'PaneControl',
                 'ListControl', 'EditControl', 'ComboBoxControl', 'WindowControl', 'TreeItemControl', 'CheckBoxControl'}
NAV_NAMES = {'消息记录', '消息管理器', '导出消息记录', '导出消息记录...', '导出消息记录…',
             '文本文件 (*.txt)', '文本文件(*.txt)', '多选', '消息', '聊天记录', '查看消息记录'}


def selector(value):
    if not isinstance(value, dict) or not value or not set(value) <= {'name', 'automation_id', 'class_name', 'control_type'}:
        raise IMError('INVALID_DESKTOP_SELECTOR')
    if value.get('control_type') not in CONTROL_TYPES:
        raise IMError('DESKTOP_SELECTOR_CONTROL_TYPE_REQUIRED')
    if not any(value.get(k) for k in ('name', 'automation_id', 'class_name')):
        raise IMError('DESKTOP_SELECTOR_IDENTITY_REQUIRED')
    if any(not isinstance(x, str) or not x or len(x) > 256 or '\x00' in x for x in value.values()):
        raise IMError('INVALID_DESKTOP_SELECTOR')
    return value


def validate_profile(ui: dict, platform: str, conversation: str):
    if not isinstance(ui, dict) or ui.get('profile_reviewed') is not True or platform not in CLIENTS:
        raise IMError('REVIEWED_DESKTOP_PROFILE_REQUIRED')
    selector(ui.get('window')); selector(ui.get('conversation_header'))
    if ui['conversation_header'].get('name') != conversation:
        raise IMError('CONVERSATION_HEADER_EXACT_NAME_REQUIRED')
    steps = ui.get('navigation', [])
    if not isinstance(steps, list) or len(steps) > 12: raise IMError('DESKTOP_NAVIGATION_LIMIT')
    for step in steps:
        if not isinstance(step, dict) or step.get('action') not in ('press', 'context-menu'):
            raise IMError('DESKTOP_ACTION_NOT_ALLOWED')
        s = selector(step.get('selector'))
        if s.get('name') not in NAV_NAMES | {conversation}:
            raise IMError('DESKTOP_NAVIGATION_NAME_NOT_ALLOWED')
        if step['action'] == 'context-menu' and s.get('name') != conversation:
            raise IMError('CONTEXT_MENU_ONLY_BOUND_CONVERSATION')
    if platform == 'qq':
        for key in ('save_dialog', 'filename_field', 'filetype_field', 'save_button'):
            selector(ui.get(key))
        if ui['filename_field']['control_type'] != 'EditControl' or ui['save_dialog']['control_type'] != 'WindowControl':
            raise IMError('INVALID_SAVE_DIALOG_PROFILE')
        if ui['save_button'].get('name') not in ('保存', '保存(S)', '保存(&S)', 'Save'):
            raise IMError('ONLY_NATIVE_SAVE_BUTTON_ALLOWED')
        if not isinstance(ui.get('expected_filename'), str) or conversation not in ui['expected_filename'] or len(ui['expected_filename']) > 260:
            raise IMError('REVIEWED_EXPORT_FILENAME_REQUIRED')
        if ui.get('expected_filetype') not in ('文本文件 (*.txt)', '文本文件(*.txt)'):
            raise IMError('TIM_TEXT_EXPORT_REQUIRED')
    else:
        for key in ('message_list', 'select_menu', 'selection_indicator'):
            selector(ui.get(key))
        if ui['select_menu'].get('name') != '多选': raise IMError('ONLY_WECOM_MULTISELECT_ALLOWED')
        if ui.get('message_control_type') not in ('ListItemControl', 'PaneControl'):
            raise IMError('MESSAGE_CONTAINER_PROFILE_REQUIRED')
        pages = ui.get('max_pages', 3)
        if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 50:
            raise IMError('DESKTOP_PAGE_LIMIT')
    return {'valid': True, 'platform': platform, 'real_ui_acceptance': 'required',
            'version_policy': VERSION_POLICY, 'source_client_version_required': False}


def clipboard_status() -> dict:
    if os.name != 'nt': raise IMError('WINDOWS_REQUIRED')
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetClipboardSequenceNumber.restype = wintypes.DWORD
    return {'clipboard_sequence': user.GetClipboardSequenceNumber(), 'clipboard_content_read': False}


def wait_and_capture(home, config, source_name, wait_seconds=60):
    from .operations import config_check
    from .collection import collect
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or not 1 <= wait_seconds <= 120:
        raise IMError('CAPTURE_WAIT_MUST_BE_1_TO_120_SECONDS')
    cfg = config_check(config)
    selected = [x for x in cfg['sources'] if x['source'] == source_name and x['enabled']]
    if len(selected) != 1 or selected[0]['transport'] != 'wecom-clipboard':
        raise IMError('ENABLED_WECOM_CLIPBOARD_SOURCE_REQUIRED')
    baseline = clipboard_status()['clipboard_sequence']
    if not baseline:
        raise IMError('CLIPBOARD_ACCESS_UNAVAILABLE')
    end = time.monotonic() + wait_seconds
    while time.monotonic() < end:
        if clipboard_status()['clipboard_sequence'] != baseline:
            return collect(home, config, source_name, after_sequence=baseline)
        time.sleep(0.1)
    raise IMError('CAPTURE_TIMEOUT_NO_NEW_COPY')


class NativeWindows:
    def __init__(self):
        if os.name != 'nt': raise IMError('WINDOWS_REQUIRED')
        try:
            import win32api, win32clipboard, win32process
        except ImportError:
            raise IMError('INSTALL_WINDOWS_EXTRA_REQUIRED') from None
        self.api, self.clip, self.proc = win32api, win32clipboard, win32process
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user.GetClipboardSequenceNumber.restype = wintypes.DWORD
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.pid = None; self.last_input = None; self.deadline = None; self.a = None
        self.client_version = None

    def process_identity(self, pid):
        # PROCESS_QUERY_LIMITED_INFORMATION: executable identity only. Never VM_READ.
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle: raise IMError('CLIENT_IDENTITY_ACCESS_DENIED')
        try:
            buf = ctypes.create_unicode_buffer(32768); size = wintypes.DWORD(len(buf))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                raise IMError('CLIENT_IDENTITY_UNAVAILABLE')
            path = Path(buf.value)
        finally: self.kernel.CloseHandle(handle)
        version = None
        try:
            info = self.api.GetFileVersionInfo(str(path), '\\')
            ms, ls = info['FileVersionMS'], info['FileVersionLS']
            version = f'{ms >> 16}.{ms & 65535}.{ls >> 16}.{ls & 65535}'
        except Exception:
            # pywintypes.error is not an OSError. This isolated metadata probe is
            # best effort; executable identity failures above still fail closed.
            pass
        return path.name.lower(), version

    def capture(self, after_sequence):
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or not 0 <= after_sequence <= 0xffffffff:
            raise IMError('INVALID_BASELINE_SEQUENCE')
        seq = self.user.GetClipboardSequenceNumber()
        if not seq or seq == after_sequence: raise IMError('NO_NEW_COPY_SINCE_BASELINE')
        owner = self.clip.GetClipboardOwner()
        if not owner: raise IMError('CLIPBOARD_HAS_NO_OWNER')
        pid = self.proc.GetWindowThreadProcessId(owner)[1]
        identity = self.process_identity(pid)
        if not accepts_client('wecom', identity): raise IMError('CLIPBOARD_OWNER_NOT_WECOM')
        if self.pid is not None and pid != self.pid: raise IMError('CLIPBOARD_WRONG_CLIENT_INSTANCE')
        self.clip.OpenClipboard()
        try:
            if self.clip.GetClipboardOwner() != owner or self.user.GetClipboardSequenceNumber() != seq:
                raise IMError('CLIPBOARD_CHANGED_BEFORE_READ')
            fmt = self.clip.RegisterClipboardFormat('WeWork Message')
            if not self.clip.IsClipboardFormatAvailable(fmt): raise IMError('NATIVE_MESSAGE_FORMAT_ABSENT')
            blob = self.clip.GetClipboardData(fmt)
            if not isinstance(blob, bytes) or not 0 < len(blob) <= 4 * 1024**2: raise IMError('INVALID_NATIVE_CLIPBOARD_SIZE')
            if self.clip.GetClipboardOwner() != owner or self.user.GetClipboardSequenceNumber() != seq:
                raise IMError('CLIPBOARD_CHANGED_DURING_READ')
        finally: self.clip.CloseClipboard()
        return blob, {'captured_at': now(), 'sequence': seq, 'client_version': identity[1],
                      'version_policy': VERSION_POLICY,
                      'clipboard_owner_verified': True, 'process_memory_read': False, 'transport': 'wecom-clipboard'}

    def input_tick(self):
        class LastInput(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]
        data = LastInput(); data.cbSize = ctypes.sizeof(data)
        if not self.user.GetLastInputInfo(ctypes.byref(data)): raise IMError('INPUT_IDLE_STATE_UNAVAILABLE')
        return data.dwTime

    def guard(self):
        if time.monotonic() > self.deadline: raise IMError('DESKTOP_RUN_TIME_LIMIT')
        if self.input_tick() != self.last_input: raise IMError('USER_INPUT_DETECTED_PAUSED')
        hwnd = self.user.GetForegroundWindow()
        if not hwnd or self.proc.GetWindowThreadProcessId(hwnd)[1] != self.pid:
            raise IMError('CLIENT_LOST_FOREGROUND_PAUSED')

    @contextmanager
    def lease(self, platform, ui):
        try: import uiautomation as a
        except ImportError: raise IMError('INSTALL_WINDOWS_EXTRA_REQUIRED') from None
        self.a = a; a.SetGlobalSearchTimeout(2)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        mutex = self.kernel.CreateMutexW(None, False, 'Local\\im-hub-desktop-v1')
        if not mutex: raise IMError('DESKTOP_LEASE_UNAVAILABLE')
        locked = False
        try:
            wait = self.kernel.WaitForSingleObject(mutex, 0)
            if wait not in (0, 0x80): raise IMError('DESKTOP_BUSY')
            locked = True
            roots = [w for w in a.GetRootControl().GetChildren() if self.matches(w, ui['window'])]
            valid = []
            for w in roots:
                identity = self.process_identity(w.ProcessId)
                if accepts_client(platform, identity): valid.append((w, identity[1]))
            if len(valid) != 1: raise IMError('CLIENT_WINDOW_MISSING_OR_AMBIGUOUS')
            self.root, self.client_version = valid[0]
            self.pid = self.root.ProcessId; self.deadline = time.monotonic() + 120
            self.last_input = self.input_tick()
            if ((self.api.GetTickCount() - self.last_input) & 0xffffffff) < 1500:
                raise IMError('USER_ACTIVE_RETRY_WHEN_IDLE')
            if ui.get('allow_activation') is True:
                self.root.SetActive(waitTime=0.2)
            self.guard()
            yield self
        finally:
            if locked: self.kernel.ReleaseMutex(mutex)
            self.kernel.CloseHandle(mutex)
        # Do not force focus back after a user interruption or an uncertain dialog.

    def matches(self, c, s):
        attributes = {'name': 'Name', 'automation_id': 'AutomationId', 'class_name': 'ClassName', 'control_type': 'ControlTypeName'}
        return all(getattr(c, attributes[k]) == v for k, v in s.items())

    def find(self, s, root=None):
        self.guard(); selector(s)
        q = [(root or self.root, 0)]; found = []; visited = 0; end = time.monotonic() + 5
        while q:
            c, depth = q.pop(0); visited += 1
            if visited > 1500 or time.monotonic() > end: raise IMError('DESKTOP_SELECTOR_SEARCH_BOUND')
            if c.ProcessId == self.pid and self.matches(c, s) and not c.IsOffscreen: found.append(c)
            if depth < 12: q.extend((x, depth + 1) for x in c.GetChildren())
        if len(found) != 1: raise IMError('DESKTOP_SELECTOR_MISSING_OR_AMBIGUOUS')
        return found[0]

    def current_root(self):
        self.guard()
        return self.a.ControlFromHandle(self.user.GetForegroundWindow())

    def action(self, c, kind='press'):
        self.guard()
        if c.ProcessId != self.pid or c.IsOffscreen or not c.IsEnabled: raise IMError('DESKTOP_TARGET_UNSAFE')
        if kind == 'context-menu': c.RightClick(waitTime=0)
        else:
            try: pattern = c.GetInvokePattern()
            except Exception: pattern = None
            if pattern: pattern.Invoke()
            else: c.Click(waitTime=0)
        # Idle detection is cooperative, not a system-wide input isolation guarantee.
        self.last_input = self.input_tick(); time.sleep(0.2); self.guard()

    def navigate(self, ui):
        for step in ui.get('navigation', []):
            # Menus/dialogs may be top-level but must retain the same bound process.
            root = self.current_root()
            self.action(self.find(step['selector'], root), step['action'])
        self.find(ui['conversation_header'], self.root)

    def export_tim(self, ui, destination):
        self.navigate(ui)
        # The source conversation header and exported default filename are independent checks.
        dialog = self.find(ui['save_dialog'], self.current_root())
        filename = self.find(ui['filename_field'], dialog)
        value = filename.GetValuePattern().Value
        if value != ui['expected_filename']: raise IMError('TIM_EXPORT_FILENAME_CONTEXT_MISMATCH')
        combo = self.find(ui['filetype_field'], dialog)
        selected = combo.GetValuePattern().Value
        if selected != ui['expected_filetype']: raise IMError('TIM_EXPORT_NOT_TEXT_FORMAT')
        self.guard(); filename.GetValuePattern().SetValue(str(destination))
        self.action(self.find(ui['save_button'], dialog))
        deadline = time.monotonic() + 25; previous = None; stable = 0
        while time.monotonic() < deadline:
            self.guard()
            if destination.is_file():
                data = read_blob(destination); sig = digest(data)
                if data and sig == previous: stable += 1
                else: stable = 0
                previous = sig
                if stable >= 3:
                    # Open with read-only sharing: an outstanding writer causes rejection.
                    import win32file, win32con
                    handle = win32file.CreateFile(str(destination), win32con.GENERIC_READ, win32con.FILE_SHARE_READ,
                                                  None, win32con.OPEN_EXISTING, 0, None)
                    handle.Close()
                    return data
            time.sleep(0.4)
        raise IMError('TIM_EXPORT_NOT_FINISHED')

    def wecom_pages(self, ui):
        self.navigate(ui); blobs = []; previous_page = None
        for page in range(ui.get('max_pages', 3)):
            self.guard(); self.find(ui['conversation_header'], self.root)
            listing = self.find(ui['message_list'], self.root)
            messages = [c for c in listing.GetChildren() if c.ControlTypeName == ui['message_control_type'] and not c.IsOffscreen]
            if not messages: raise IMError('NO_ACCESSIBLE_MESSAGE_ELEMENTS')
            if len(messages) > 80: raise IMError('VISIBLE_MESSAGE_LIMIT')
            page_hashes = []
            for index in range(len(messages)):
                self.guard(); self.find(ui['conversation_header'], self.root)
                # Reacquire the visible list each time; never reuse coordinates after layout changes.
                current = self.find(ui['message_list'], self.root)
                fresh = [c for c in current.GetChildren() if c.ControlTypeName == ui['message_control_type'] and not c.IsOffscreen]
                if len(fresh) != len(messages): raise IMError('MESSAGE_LAYOUT_CHANGED_RETRY')
                self.action(fresh[index], 'context-menu')
                self.action(self.find(ui['select_menu'], self.current_root()))
                self.find(ui['selection_indicator'], self.root)
                if self.a.GetFocusedControl().ControlTypeName == 'EditControl': raise IMError('MESSAGE_COPY_FOCUS_IS_INPUT')
                baseline = self.user.GetClipboardSequenceNumber(); self.guard()
                self.a.SendKeys('{Ctrl}c', waitTime=0); self.last_input = self.input_tick()
                end = time.monotonic() + 2
                while self.user.GetClipboardSequenceNumber() == baseline and time.monotonic() < end:
                    time.sleep(0.05); self.guard()
                blob, _ = self.capture(baseline); blobs.append(blob); page_hashes.append(digest(blob))
                self.guard(); self.a.SendKeys('{Esc}', waitTime=0); self.last_input = self.input_tick()
                if sum(map(len, blobs)) > MAX_BYTES: raise IMError('DESKTOP_CAPTURE_SIZE_LIMIT')
            signature = digest(page_hashes)
            if signature == previous_page: break
            previous_page = signature
            if page + 1 < ui.get('max_pages', 3):
                listing = self.find(ui['message_list'], self.root); self.guard()
                listing.WheelUp(wheelTimes=3, waitTime=0); self.last_input = self.input_tick(); time.sleep(0.3)
        return blobs


def acquire_ui(profile: dict, folder: Path, driver=None):
    ui = profile['desktop']; platform = profile['platform']
    validate_profile(ui, platform, profile['conversation_name'])
    native = driver or NativeWindows()
    with native.lease(platform, ui):
        if platform == 'qq':
            blobs = [native.export_tim(ui, folder / 'official-export.txt')]
        else:
            blobs = native.wecom_pages(ui)
    return blobs, {'transport': profile['transport'], 'captured_at': now(), 'ui_performed': True,
                   'client_version': getattr(native, 'client_version', None),
                   'version_policy': VERSION_POLICY, 'coverage': 'bounded_ui_selection',
                   'history_complete': False, 'llm_calls': 0, 'source_epoch': profile['source_epoch']}
