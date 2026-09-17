"""Read-only real-provider capability probe. Never activate, expand or scroll.

Visible labels are provisional locators, NOT native conversation identities or
an active-week denominator. No message preview, credential, runtime ID or image
is read. The probe does not create a data home, inventory, cursor or receipt.
"""
from __future__ import annotations
import time
from collections import Counter
from .common import IMError, digest, now


def probe_desktop_directory(platform: str, reader=None) -> dict:
    if platform not in ('qq','wecom'):
        raise IMError('DIRECTORY_PLATFORM_UNSUPPORTED')
    native=reader if reader is not None else VisibleSidebarReader()
    first=native.snapshot(platform);second=native.snapshot(platform)
    if not isinstance(first,dict) or not isinstance(second,dict):raise IMError('DIRECTORY_PROBE_INVALID')
    if first!=second:raise IMError('DIRECTORY_VIEW_CHANGED_RETRY_OBSERVATION')
    if type(first.get('listing_observed')) is not bool or type(first.get('truncated')) is not bool:
        raise IMError('DIRECTORY_PROBE_INVALID')
    raw=first.get('rows')
    if not isinstance(raw,list) or len(raw)>200:
        raise IMError('DIRECTORY_VISIBLE_ROWS_INVALID')
    labels=[]
    for r in raw:
        if (not isinstance(r,dict) or set(r)!={'label','bounds','fully_visible'}
            or not isinstance(r['label'],str) or not r['label'].strip() or len(r['label'])>512
            or not isinstance(r['fully_visible'],bool) or not isinstance(r['bounds'],list)
            or len(r['bounds'])!=4 or any(type(v) is not int for v in r['bounds'])):
            raise IMError('DIRECTORY_VISIBLE_ROWS_INVALID')
        labels.append(r['label'])
    duplicate={k for k,n in Counter(labels).items() if n>1}
    rows=[]
    for i,r in enumerate(raw):
        special=platform=='qq' and r['label']=='群助手'
        rows.append({**r,'row_index':i,'locator_sha256':digest([platform,i,r]),
                     'row_kind':'group_helper_entry' if special else 'unclassified_conversation_candidate',
                     'duplicate_label':r['label'] in duplicate,'native_conversation_id':None,
                     'conversation_type':'unknown','last_activity_epoch':None,
                     'eligible_for_automatic_binding':False})
    accessible=bool(first.get('listing_observed'))
    return {'schema':'im-hub-visible-directory-probe/1','observed_at':now(),
            'platform':platform,'status':'visible_candidates_only' if accessible else 'directory_metadata_not_exposed',
            'rows':rows,'visible_rows':len(rows) if accessible else None,
            'fully_visible_rows':sum(r['fully_visible'] for r in rows) if accessible else None,
            'group_helper_entries':sum(r['row_kind']=='group_helper_entry' for r in rows),
            'observation_sha256':digest(first),'provider':first.get('provider'),
            'client_version':first.get('client_version'),'nodes_visited':first.get('nodes_visited'),
            'truncated':first.get('truncated',True),'scope':'current_visible_sidebar_only',
            'identity_quality':'display_labels_are_not_native_ids',
            'independent_total':None,'folded_children_scanned':False,'account_directory_complete':False,
            'inventory_ready':False,'ui_actions':0,'query_only':True,'client_observed':True,
            'messages_collected':0,'llm_calls':0,'source_version_required':False}


class VisibleSidebarReader:
    """Observe a uniquely matched visible TIM/WeCom top-level native window.

    A bounded raw UIA walk is used because some TIM providers return runtime IDs
    unsupported by the remote reusable-handle interface. No runtime ID is used
    for identity or effect targeting, and no input methods are called here.
    """
    def snapshot(self,platform):
        import os
        if os.name!='nt':raise IMError('WINDOWS_REQUIRED')
        try:
            import win32gui,win32process
            import uiautomation as auto
        except ImportError:raise IMError('INSTALL_WINDOWS_EXTRA_REQUIRED') from None
        from .windows_desktop import NativeWindows,CLIENTS
        native=NativeWindows();windows=[]
        def visit(hwnd,_):
            if not win32gui.IsWindowVisible(hwnd):return
            expected='TXGuiFoundation' if platform=='qq' else 'WeWorkWindow'
            if win32gui.GetClassName(hwnd)!=expected:return
            pid=win32process.GetWindowThreadProcessId(hwnd)[1]
            identity=native.process_identity(pid)
            if identity[0]==CLIENTS[platform]:windows.append((hwnd,pid,identity[1]))
        win32gui.EnumWindows(visit,None)
        if len(windows)!=1:raise IMError('DIRECTORY_CLIENT_WINDOW_MISSING_OR_AMBIGUOUS')
        hwnd,pid,version=windows[0];root=auto.ControlFromHandle(hwnd);box=root.BoundingRectangle
        left,top,right,bottom=(box.left,box.top,box.right,box.bottom)
        end=time.monotonic()+12;visited=0;cut=False;rows=[];listings=[]
        queue=[(root,0,False)]
        while queue:
            if visited>=1200 or time.monotonic()>=end:cut=True;break
            control,depth,in_listing=queue.pop(0);visited+=1
            rect=control.BoundingRectangle
            if depth and (control.ProcessId!=pid or control.IsPassword):continue
            # Only the left-side navigation subtree, not the body or member list.
            sidebar_right=min((x[2] for x in listings),default=left+(right-left)*.35)
            if depth and (rect.left>=sidebar_right or rect.right<=left
                          or rect.bottom<=top or rect.top>=bottom):continue
            kind=control.ControlTypeName
            is_listing=kind in ('PaneControl','ListControl') and control.Name=='会话列表'
            if is_listing:listings.append((rect.left,rect.top,rect.right,rect.bottom))
            in_listing=in_listing or is_listing
            if kind=='ListItemControl' and in_listing:
                name=control.Name
                if name and len(name)<=512:
                    bounds=[int(rect.left-left),int(rect.top-top),int(rect.right-left),int(rect.bottom-top)]
                    rows.append({'label':name,'bounds':bounds,
                                 'fully_visible':not control.IsOffscreen and rect.bottom<=bottom and rect.top>=top})
                continue  # Never read row Value/preview or descendant text.
            if depth<22:queue.extend((c,depth+1,in_listing) for c in control.GetChildren())
            else:cut=True
        if not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1]!=pid:
            raise IMError('DIRECTORY_CLIENT_WINDOW_CHANGED')
        rows.sort(key=lambda r:(r['bounds'][1],r['bounds'][0],r['label']))
        return {'provider':'Windows_UIA_read_only_sidebar','client_version':version,
                'listing_observed':bool(listings),'rows':rows,'nodes_visited':visited,'truncated':cut}
