"""Bounded normal OpenClipboard acquisition; never changes permissions or data.
OpenClipboard can fail while another window holds it (Microsoft Win32 contract).
We retain strict source owner/sequence checks, a time limit, and cancellation.
"""
from __future__ import annotations
import time
from .common import IMError


def open_snapshot(clip, user, owner, sequence, guard=None, *, timeout=1.5,
                  clock=time.monotonic, pause=time.sleep):
    from .desktop_runtime import check_policy
    check=guard or check_policy
    if not isinstance(timeout,(int,float)) or isinstance(timeout,bool) or not 0<timeout<=3:
        raise IMError('INVALID_CLIPBOARD_WAIT_BOUND')
    deadline=clock()+timeout;attempts=0
    while True:
        check()
        if clip.GetClipboardOwner()!=owner:
            raise IMError('CLIPBOARD_CHANGED_BEFORE_READ')
        if user.GetClipboardSequenceNumber()!=sequence:
            raise IMError('CLIPBOARD_SOURCE_SEQUENCE_CHANGED_RETRY')
        attempts+=1
        try:
            clip.OpenClipboard()
            return attempts
        except Exception as exc:
            # pywintypes.error is not an OSError. Do not leak its message/body.
            code=getattr(exc,'winerror',None)
            if code is None and exc.args and isinstance(exc.args[0],int):code=exc.args[0]
            if code!=5:raise IMError('CLIPBOARD_OPEN_FAILED') from None
            # One bounded wait through the very same API, not permission changes,
            # an alternate reader, new key strokes, or an elevated retry.
            if clock()>=deadline:raise IMError('CLIPBOARD_OPEN_UNAVAILABLE') from None
            pause(min(.05,max(0,deadline-clock())))
