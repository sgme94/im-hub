"""Explicit desktop consent/deadline and normal taskbar navigation.
No foreground-lock changes, thread attachment, force-focus keys, service or login.
"""
from __future__ import annotations
import os
import time
from pathlib import Path
from .common import IMError, iso_epoch


def check_policy(environ=None, clock=time.time):
    env=os.environ if environ is None else environ
    until=env.get('IM_HUB_DESKTOP_UNTIL')
    if until is not None and clock()>=iso_epoch(until):
        raise IMError('DESKTOP_AUTHORIZATION_EXPIRED')
    stop=env.get('IM_HUB_STOP_FILE')
    if stop and Path(stop).exists():raise IMError('DESKTOP_STOP_REQUESTED')
    if env.get('IM_HUB_ACTIVATE_CLIENTS')=='1' and until is None:
        raise IMError('ACTIVATION_REQUIRES_EXPLICIT_DEADLINE')
    return until


def activation_requested(environ=None):
    env=os.environ if environ is None else environ
    check_policy(env)
    return env.get('IM_HUB_ACTIVATE_CLIENTS')=='1'


def input_guard(session):
    check_policy()
    if session.n.input_tick()!=session.last_input:raise IMError('USER_INPUT_DETECTED_PAUSED')
    if any(session.n.api.GetAsyncKeyState(k)&0x8000 for k in (1,2,16,17,18)):
        raise IMError('HELD_INPUT_DETECTED_PAUSED')


def taskbar_target(session, application):
    """Find an exact standard running-window button, not a tray/account icon.
    Only one running window is admitted; a grouped preview needs separate support.
    """
    import uiautomation as auto
    bars=[c for c in auto.GetRootControl().GetChildren() if c.ClassName=='Shell_TrayWnd']
    if len(bars)!=1:raise IMError('PRIMARY_TASKBAR_NOT_UNIQUE')
    bar=bars[0]
    if session.n.process_identity(bar.ProcessId)[0]!='explorer.exe':
        raise IMError('TASKBAR_PROCESS_NOT_EXPLORER')
    expected={application+' - 1 个运行窗口',application+' - 1 running window'}
    queue=[(bar,0)];found=[];visited=0;end=time.monotonic()+5
    while queue:
        input_guard(session)
        control,depth=queue.pop(0);visited+=1
        if visited>300 or time.monotonic()>end:raise IMError('TASKBAR_SEARCH_BOUND')
        if (control.ControlTypeName=='ButtonControl' and control.Name in expected
            and control.ProcessId==bar.ProcessId and not control.IsOffscreen and control.IsEnabled):
            found.append(control)
        if depth<8:queue.extend((c,depth+1) for c in control.GetChildren())
    if len(found)!=1:raise IMError('RUNNING_CLIENT_TASKBAR_BUTTON_NOT_UNIQUE')
    rect=found[0].BoundingRectangle;br=bar.BoundingRectangle
    x,y=int((rect.left+rect.right)/2),int((rect.top+rect.bottom)/2)
    if (rect.right-rect.left<8 or rect.bottom-rect.top<8 or
        not br.left<=x<br.right or not br.top<=y<br.bottom):
        raise IMError('TASKBAR_BUTTON_RECTANGLE_INVALID')
    return (x,y),bar.ProcessId


def activate_bound(session):
    """Use a reviewed ordinary taskbar click under explicit desktop consent.
    This is the selected activation strategy, not a retry after force-focus failed.
    No alternate operation is attempted when this strategy fails.
    """
    check_policy()
    if not activation_requested():raise IMError('BOUND_CLIENT_NOT_FOREGROUND')
    s=session
    expected={'qq':('TXGuiFoundation',s.p['conversation_name']),
              'wecom':('WeWorkWindow','企业微信')}[s.p['platform']]
    candidates=[]
    def visit(h,_):
        if not s.g.IsWindowVisible(h) or (s.g.GetClassName(h),s.g.GetWindowText(h))!=expected:return
        pid=s.n.proc.GetWindowThreadProcessId(h)[1]
        if s.accepts(s.p['platform'],s.n.process_identity(pid)):candidates.append(h)
    s.g.EnumWindows(visit,None)
    if len(candidates)!=1:raise IMError('ACTIVATION_TARGET_MISSING_OR_AMBIGUOUS')
    input_guard(s);target=candidates[0]
    point,explorer_pid=taskbar_target(s,'TIM' if s.p['platform']=='qq' else '企业微信')
    input_guard(s)
    hit=s.g.WindowFromPoint(point)
    if not hit or s.n.proc.GetWindowThreadProcessId(hit)[1]!=explorer_pid:
        raise IMError('TASKBAR_BUTTON_OCCLUDED')
    s.mouse.click(button='left',coords=point)
    s.actions+=1;s.last_input=s.n.input_tick()
    end=time.monotonic()+3
    while time.monotonic()<end:
        input_guard(s)
        if s.g.GetForegroundWindow()==target:
            s.activated=True;s.activation_method='verified_running_taskbar_button'
            return target
        time.sleep(.05)
    raise IMError('CLIENT_ACTIVATION_NOT_CONFIRMED')
