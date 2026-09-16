"""Bounded WeCom normal multi-select/copy, using private reviewed visual anchors.
Pixels only locate controls. Message bodies and IDs come from the native payload.
No OCR, model, sending, deletion, process-memory access or plaintext fallback.
"""
from __future__ import annotations
import math
import time
from .common import IMError, MAX_BYTES, digest

WECOM_ANCHORS = {'main_header', 'group_item', 'composer_tools', 'multiselect_menu',
                 'selection_close', 'checkbox_off', 'checkbox_on'}


def locate_many(image, template, search_region, mode='gray', threshold=.95, limit=80):
    from .visual_desktop import _libraries, _mask, region
    cv, np, _, _ = _libraries()
    region(search_region)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 80:
        raise IMError('VISIBLE_SELECTION_LIMIT')
    a = np.asarray(image); h, w = a.shape[:2]
    left, top, right, bottom = [int(v*s) for v,s in zip(search_region,(w,h,w,h))]
    tpl = _mask(np.asarray(template), mode); th, tw = tpl.shape
    if not 6 <= tw <= 100 or not 6 <= th <= 100 or float(tpl.std()) < 5:
        raise IMError('INVALID_CHECKBOX_TEMPLATE')
    a = _mask(a[top:bottom,left:right], mode)
    if a.shape[0] < th or a.shape[1] < tw: raise IMError('CHECKBOX_REGION_TOO_SMALL')
    scores = cv.matchTemplate(a,tpl,cv.TM_CCOEFF_NORMED); result=[]
    for _ in range(limit+1):
        _,score,_,(x,y)=cv.minMaxLoc(scores)
        if not math.isfinite(score) or score < threshold: break
        result.append({'x':left+x+tw//2,'y':top+y+th//2,'score':round(score,6),
                       'box':[left+x,top+y,left+x+tw,top+y+th]})
        scores[max(0,y-th//2):y+th//2+1,max(0,x-tw//2):x+tw//2+1]=-1
    if len(result)>limit: raise IMError('VISIBLE_SELECTION_LIMIT')
    return sorted(result,key=lambda p:(p['y'],p['x']))


def bubble_point(image, search_region, colors):
    """Return a point in one actually observed solid message bubble, never composer."""
    from .visual_desktop import _libraries, region
    cv,np,_,_=_libraries();region(search_region)
    a=np.asarray(image);h,w=a.shape[:2]
    l,t,r,b=[int(v*s) for v,s in zip(search_region,(w,h,w,h))]
    crop=a[t:b,l:r,:3].astype(np.int16);mask=np.zeros(crop.shape[:2],np.uint8)
    for color in colors:
        mask |= (np.max(np.abs(crop-np.array(color)),axis=2)<=3).astype(np.uint8)*255
    joined=cv.morphologyEx(mask,cv.MORPH_CLOSE,np.ones((7,7),np.uint8))
    count,labels,stats,_=cv.connectedComponentsWithStats(joined)
    candidates=[]
    for label in range(1,count):
        x,y,bw,bh,area=[int(v) for v in stats[label]]
        # Watermarks and glyphs are sparse; solid bubble backgrounds are dense.
        if bw<60 or bh<30 or area<1800 or area/(bw*bh)<.6: continue
        yy,xx=np.where((labels[y:y+bh,x:x+bw]==label)&(mask[y:y+bh,x:x+bw]>0))
        if len(xx)<1800:continue
        middle=np.argmin((xx-bw/2)**2+(yy-bh/2)**2)
        candidates.append((y+bh,{'x':l+x+int(xx[middle]),'y':t+y+int(yy[middle]),
                                'box':[l+x,t+y,l+x+bw,t+y+bh]}))
    if not candidates:raise IMError('NO_REVIEWED_MESSAGE_BUBBLE_VISIBLE')
    return max(candidates,key=lambda x:x[0])[1]


def scrollbar_geometry(image, search_region):
    from .visual_desktop import _libraries, region
    cv,np,_,_=_libraries();region(search_region)
    a=np.asarray(image);h,w=a.shape[:2]
    l,t,r,b=[int(v*s) for v,s in zip(search_region,(w,h,w,h))]
    part=a[t:b,l:r,:3].astype(np.int16)
    mask=((part.min(axis=2)>120)&(part.max(axis=2)<220)&
          (part.max(axis=2)-part.min(axis=2)<15)).astype(np.uint8)
    _,_,stats,_=cv.connectedComponentsWithStats(mask)
    candidates=[{'x':int(x+l),'y':int(y+t),'width':int(bw),'height':int(bh)}
                for x,y,bw,bh,area in stats[1:] if 4<=bw<=18 and bh>=35 and area/(bw*bh)>.7]
    if not candidates:raise IMError('MESSAGE_SCROLLBAR_NOT_VISIBLE')
    if len(candidates)!=1:raise IMError('MESSAGE_SCROLLBAR_NOT_UNIQUE')
    return candidates[0]


class WeComReader:
    def __init__(self, session):
        self.s=session;self.ui=session.ui;self.root=session.guard()
        self.stats=[]

    def normal(self):
        s=self.s
        s.anchor('main_header',self.root)
        return s.anchor('composer_tools',self.root)

    def selection(self):
        self.s.anchor('main_header',self.root)
        return self.s.anchor('selection_close',self.root,optional=True)

    def boxes(self):
        s=self.s;self.selection()
        image,origin=s.frame(self.root);result={}
        for state in ('off','on'):
            key='checkbox_'+state;spec=self.ui['anchors'][key]
            result[state]=locate_many(image,s.templates[key],spec['region'],spec.get('mode','gray'))
        all_boxes=result['off']+result['on']
        if len(all_boxes)>80:raise IMError('VISIBLE_SELECTION_LIMIT')
        if any(abs(a['x']-b['x'])<10 and abs(a['y']-b['y'])<10
               for a in result['off'] for b in result['on']):
            raise IMError('CHECKBOX_STATE_AMBIGUOUS')
        return result,origin

    def enter_selection(self):
        s=self.s;self.normal();image,origin=s.frame(self.root)
        found=bubble_point(image,self.ui['message_region'],self.ui['bubble_colors'])
        s.click((origin[0]+found['x'],origin[1]+found['y']),button='right')
        deadline=time.monotonic()+3;menus=[]
        while time.monotonic()<deadline:
            s.guard();menus=[]
            def visit(h,_):
                if (s.g.IsWindowVisible(h) and s.g.GetClassName(h)=='DuiMenuWnd'
                    and s.n.proc.GetWindowThreadProcessId(h)[1]==s.pid):menus.append(h)
            s.g.EnumWindows(visit,None)
            if menus:break
            time.sleep(.1)
        if len(menus)!=1:raise IMError('WECOM_MESSAGE_MENU_MISSING_OR_AMBIGUOUS')
        s.click(s.anchor('multiselect_menu',menus[0]))
        end=time.monotonic()+3
        while time.monotonic()<end:
            if self.selection() is not None:return
            time.sleep(.1)
        raise IMError('WECOM_SELECTION_MODE_NOT_ENTERED')

    def select_one(self, index, expected_layout=None):
        s=self.s;boxes,origin=self.boxes()
        layout=sorted(boxes['off']+boxes['on'],key=lambda b:(b['y'],b['x']))
        if not 0 <= index < len(layout):raise IMError('MESSAGE_INDEX_NO_LONGER_VISIBLE')
        if expected_layout is not None:
            if len(layout)!=len(expected_layout) or any(abs(a['x']-b['x'])>8 or abs(a['y']-b['y'])>8 for a,b in zip(layout,expected_layout)):
                raise IMError('MESSAGE_LAYOUT_CHANGED_RETRY')
        target=layout[index]
        # Multi-message copy produces a different aggregate format without our
        # validated source binding. Copy exactly one native message at a time.
        for checked in boxes['on']:
            if abs(checked['x']-target['x'])<=8 and abs(checked['y']-target['y'])<=8:continue
            s.click((origin[0]+checked['x'],origin[1]+checked['y']))
        current,origin=self.boxes()
        if not current['on']:
            s.click((origin[0]+target['x'],origin[1]+target['y']))
        end=time.monotonic()+2
        while time.monotonic()<end:
            current,_=self.boxes()
            if len(current['on'])==1 and abs(current['on'][0]['y']-target['y'])<=8:
                return layout
            time.sleep(.1)
        raise IMError('SINGLE_MESSAGE_SELECTION_NOT_CONFIRMED')

    def copy(self, expected):
        s=self.s;self.selection();boxes,_=self.boxes()
        if len(boxes['on'])!=expected or expected!=1:raise IMError('SELECTION_CHANGED_BEFORE_COPY')
        import uiautomation as a
        if a.GetFocusedControl().ControlTypeName=='EditControl':raise IMError('MESSAGE_COPY_FOCUS_IS_INPUT')
        baseline=s.n.user.GetClipboardSequenceNumber();s.guard()
        a.SendKeys('{Ctrl}c',waitTime=0);s.actions+=1;s.last_input=s.n.input_tick()
        end=time.monotonic()+3
        while s.n.user.GetClipboardSequenceNumber()==baseline and time.monotonic()<end:
            time.sleep(.05);s.guard()
        blob,meta=s.n.capture(baseline)
        from .codecs.desktop import enriched_wecom
        rows=enriched_wecom(blob,s.p['account_namespace'],s.p['conversation_name'],s.p['binding'])
        if len(rows)!=expected or any(not r.get('metadata_fields_available') for r in rows):
            raise IMError('WECOM_SELECTED_COUNT_OR_NATIVE_METADATA_MISMATCH')
        # The actual client exits multi-select on Ctrl+C. Do not blindly click a
        # stale close position, which becomes part of the composer afterwards.
        end=time.monotonic()+3
        while time.monotonic()<end:
            if s.anchor('composer_tools',self.root,optional=True) is not None:break
            close=self.selection()
            if close is not None:s.click(close)
            time.sleep(.1)
        self.normal()
        return blob,rows,meta

    def scroll(self, down=False):
        s=self.s;self.normal();image,origin=s.frame(self.root)
        l,t,r,b=self.ui['message_region'];point=(origin[0]+int((l+r)*image.width/2),
                                               origin[1]+int((t+b)*image.height/2))
        s.guard()
        if s.n.proc.GetWindowThreadProcessId(s.g.WindowFromPoint(point))[1]!=s.pid:
            raise IMError('SCROLL_POINT_OCCLUDED')
        # This custom client consumes one wheel event as one step even when
        # angleDelta contains several notches. Send bounded distinct events.
        for _ in range(8):
            s.guard();s.mouse.scroll(coords=point,wheel_dist=1 if not down else -1)
            s.actions+=1;s.last_input=s.n.input_tick();time.sleep(.06)
        time.sleep(.3);s.guard();self.normal()

    def reset_latest(self):
        s=self.s
        for step in range(13):
            tools=self.normal();image,origin=s.frame(self.root)
            try:
                thumb=scrollbar_geometry(image,self.ui['scrollbar_region'])
            except IMError as exc:
                if exc.code!='MESSAGE_SCROLLBAR_NOT_VISIBLE':raise
                # The native scrollbar auto-hides while inactive. A normal bounded
                # downward scroll exposes it; absence is never evidence of latest.
                if step==12:break
                self.scroll(down=True)
                continue
            expected=tools[1]-origin[1]-self.ui['scrollbar_bottom_offset']
            if abs(thumb['y']+thumb['height']-expected)<=8:
                return {'latest_view_verified':True,'scroll_passes':step,'basis':'reviewed_scrollbar_bottom_geometry'}
            if step==12:break
            self.scroll(down=True)
        raise IMError('LATEST_MESSAGE_VIEW_NOT_REACHED_WITHIN_BOUND')

    def capture_pages(self):
        s=self.s
        s.anchor('main_header',self.root)
        if s.anchor('composer_tools',self.root,optional=True) is None:
            # Recover only a positively identified selection UI left by our prior
            # interrupted pass; never close an arbitrary window or send Escape.
            close=self.selection()
            if close is None:raise IMError('UNKNOWN_WECOM_INITIAL_UI_STATE')
            s.click(close)
        self.normal();blobs=[];seen=set()
        latest=self.reset_latest()
        for page in range(self.ui['max_pages']):
            self.enter_selection();boxes,_=self.boxes()
            layout=sorted(boxes['off']+boxes['on'],key=lambda b:(b['y'],b['x']))
            if not layout:raise IMError('NO_VISIBLE_MESSAGE_CHECKBOXES')
            page_rows=[]
            for index in range(len(layout)):
                if index:self.enter_selection()
                self.select_one(index,layout)
                blob,rows,meta=self.copy(1)
                blobs.append(blob);page_rows.extend(rows)
                if sum(map(len,blobs))>MAX_BYTES:raise IMError('DESKTOP_CAPTURE_SIZE_LIMIT')
            keys={digest(r['source_id_fields']) for r in page_rows}
            if len(keys)!=len(layout):raise IMError('PAGE_MESSAGE_ID_DUPLICATION')
            s.pages.append({'page':page,'latest_reset':latest if page==0 else None,
                            'selected_messages':len(layout),'native_messages':len(page_rows),
                            'new_native_ids':len(keys-seen),'captured_at':meta['captured_at'],
                            'group_binding_verified':True,'native_format_verified':True,
                            'copy_mode':'one_native_message_per_copy',
                            'message_min_timestamp':min(r['timestamp_field12_raw'] for r in page_rows),
                            'message_max_timestamp':max(r['timestamp_field12_raw'] for r in page_rows),
                            'scope':'visible_checkbox_page_not_complete_history'})
            if not keys-seen:break
            seen.update(keys)
            if page+1<self.ui['max_pages']:self.scroll()
        return blobs
