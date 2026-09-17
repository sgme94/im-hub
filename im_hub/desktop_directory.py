"""Opt-in, calibrated UIA directory discovery, not message acquisition.

This candidate reads only four reviewed metadata fields. It never activates a
client, opens a conversation, copies a message, uses OCR, or sends a key. Only
native ExpandCollapse and Scroll patterns are used, inside the existing desktop
lease. Profiles without accessible native identity/count fields fail closed.
Synthetic tests are not acceptance of TIM/WeCom's real accessibility providers.
"""
from __future__ import annotations

import copy
import math
import os
from contextlib import contextmanager
from pathlib import Path

from .common import IMError, canonical, digest, iso_epoch, label, load_json, now, read_blob
from .database_readers import integer
from .desktop_runtime import check_policy
from .windows_desktop import NativeWindows, selector

FIELDS = ('native_id', 'name', 'type_label', 'last_activity')
PROFILE_KEYS = {'schema', 'profile_reviewed', 'identity_source', 'window', 'account_header',
                'row_control_type', 'fields', 'type_labels', 'time_format', 'max_pages',
                'max_rows', 'sections'}


def validate_directory_profile(value: dict) -> dict:
    """Static validation only: never instantiate a Windows API object."""
    if (not isinstance(value, dict) or set(value) - PROFILE_KEYS
            or value.get('schema') != 'im-hub-directory-profile/1'
            or value.get('profile_reviewed') is not True):
        raise IMError('REVIEWED_DIRECTORY_PROFILE_REQUIRED')
    p = copy.deepcopy(value)
    if p.get('identity_source') != 'native_conversation_id_text':
        raise IMError('DIRECTORY_NATIVE_ID_TEXT_REQUIRED')
    selector(p.get('window'))
    if p['window']['control_type'] != 'WindowControl':
        raise IMError('DIRECTORY_WINDOW_REQUIRED')
    selector(p.get('account_header'))
    if not p['account_header'].get('name') or p['account_header']['control_type'] != 'TextControl':
        raise IMError('DIRECTORY_EXACT_ACCOUNT_HEADER_REQUIRED')
    if p.get('row_control_type') not in ('ListItemControl', 'TreeItemControl'):
        raise IMError('DIRECTORY_ROW_CONTROL_REQUIRED')
    if not isinstance(p.get('fields'), dict) or set(p['fields']) != set(FIELDS):
        raise IMError('DIRECTORY_METADATA_FIELDS_REQUIRED')
    for field in FIELDS:
        selector(p['fields'][field])
        if p['fields'][field]['control_type'] != 'TextControl':
            raise IMError('DIRECTORY_METADATA_TEXT_CONTROL_REQUIRED')
    if len({canonical(s) for s in p['fields'].values()}) != len(FIELDS):
        raise IMError('DIRECTORY_METADATA_SELECTORS_MUST_DIFFER')
    types = p.get('type_labels')
    if (not isinstance(types, dict) or not 1 <= len(types) <= 16
            or any(not isinstance(k, str) or not k or len(k) > 80
                   or v not in ('group', 'direct', 'service') for k, v in types.items())):
        raise IMError('DIRECTORY_EXPLICIT_TYPE_MAPPING_REQUIRED')
    if p.get('time_format') not in ('iso8601', 'epoch_seconds'):
        raise IMError('DIRECTORY_ABSOLUTE_TIME_FORMAT_REQUIRED')
    p['max_pages'] = integer(p.get('max_pages', 50), 'DIRECTORY_PAGE_BOUND', 1, 200)
    p['max_rows'] = integer(p.get('max_rows', 5000), 'DIRECTORY_ROW_BOUND', 1, 20000)
    sections = p.get('sections')
    if not isinstance(sections, list) or not 2 <= len(sections) <= 8:
        raise IMError('DIRECTORY_REGULAR_AND_FOLDED_SECTION_REQUIRED')
    seen = set()
    for s in sections:
        if not isinstance(s, dict) or set(s) - {'id', 'kind', 'listing', 'total_count', 'expand'}:
            raise IMError('DIRECTORY_SECTION_INVALID')
        sid = label(s.get('id'))
        if len(sid) > 80 or sid in seen or s.get('kind') not in ('regular', 'folded'):
            raise IMError('DIRECTORY_SECTION_INVALID')
        seen.add(sid)
        selector(s.get('listing')); selector(s.get('total_count'))
        if s['listing']['control_type'] not in ('ListControl', 'PaneControl', 'TreeItemControl'):
            raise IMError('DIRECTORY_LISTING_CONTROL_REQUIRED')
        if s['total_count']['control_type'] != 'TextControl':
            raise IMError('DIRECTORY_TOTAL_TEXT_REQUIRED')
        if 'expand' in s:
            selector(s['expand'])
            if s['kind'] != 'folded' or s['expand']['control_type'] != 'TreeItemControl':
                raise IMError('DIRECTORY_EXPAND_ONLY_REVIEWED_FOLDED_TREE')
    if {s['kind'] for s in sections} != {'regular', 'folded'}:
        raise IMError('DIRECTORY_REGULAR_AND_FOLDED_SECTION_REQUIRED')
    if len({canonical(s['listing']) for s in sections}) != len(sections):
        raise IMError('DIRECTORY_SECTION_LISTINGS_MUST_DIFFER')
    return p


def require_consent(allow_ui: bool) -> None:
    if allow_ui is not True:
        raise IMError('UI_CONSENT_REQUIRED')
    until = check_policy()
    if until is None:
        raise IMError('DIRECTORY_EXPLICIT_DEADLINE_REQUIRED')
    # check_policy validates the timezone, expiry and STOP even without activation.


def _normalize_row(p: dict, raw: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) != set(FIELDS):
        raise IMError('DIRECTORY_METADATA_FIELDS_INVALID')
    cid = label(raw['native_id'], 'DIRECTORY_NATIVE_ID_UNAVAILABLE')
    name = label(raw['name'], 'DIRECTORY_DISPLAY_NAME_UNAVAILABLE')
    kind_text = label(raw['type_label'], 'DIRECTORY_TYPE_UNAVAILABLE')
    time_text = raw['last_activity']
    if not isinstance(time_text, str) or len(time_text) > 80:
        raise IMError('DIRECTORY_ACTIVITY_FIELD_INVALID')
    kind = p['type_labels'].get(kind_text, 'unknown')
    timestamp = None
    try:
        if p['time_format'] == 'iso8601':
            timestamp = iso_epoch(time_text)
        elif time_text.isascii() and time_text.isdigit():
            timestamp = int(time_text)
        if timestamp is not None and not 946684800 <= timestamp < 4102444800:
            timestamp = None
    except IMError:
        # A relative or unavailable date is not zero activity and is not guessed.
        pass
    return {'native_id': cid, 'name': name, 'kind': kind, 'last_epoch': timestamp}


def discover_desktop_account(account: dict, since: float, until: float,
                             allow_ui=False, driver=None) -> tuple[list, dict]:
    """Scan reviewed regular/folded sections into directory-only evidence.

    A complete traversal is scoped to these calibrated sections, not a proof of
    server history, account-wide hidden stores, or message/body completeness.
    """
    p = validate_directory_profile(account.get('directory'))
    if account.get('platform') not in ('qq', 'wecom'):
        raise IMError('DIRECTORY_PLATFORM_UNSUPPORTED')
    if not 946684800 <= since < until < 4102444800:
        raise IMError('DIRECTORY_WINDOW_INVALID')
    require_consent(allow_ui)
    reader = driver if driver is not None else UIADirectoryReader()
    found = {}; receipts = []; error = None; ui_attempted = False
    try:
        ui_attempted = True
        with reader.lease({**account, 'directory': p}):
            for section in p['sections']:
                seen = set(); previous_ids = None; previous_position = None
                expected = None; ended = False; count = 0
                receipt = {'id': section['id'], 'kind': section['kind'], 'pages': [],
                           'unique_rows': 0, 'complete': False}
                receipts.append(receipt)
                for page in reader.pages(section):
                    require_consent(allow_ui)
                    count += 1
                    if count > p['max_pages']:
                        raise IMError('DIRECTORY_PAGE_BOUND')
                    if not isinstance(page, dict) or set(page) != {'rows','position','at_end','total'}:
                        raise IMError('DIRECTORY_PAGE_INVALID')
                    position = page['position']
                    if (isinstance(position, bool) or not isinstance(position, (float,int))
                            or not math.isfinite(position) or not 0 <= position <= 100
                            or not isinstance(page['at_end'], bool)):
                        raise IMError('DIRECTORY_SCROLL_PROOF_INVALID')
                    total = page['total']
                    if total is None:
                        raise IMError('DIRECTORY_TOTAL_COUNT_UNAVAILABLE')
                    integer(total, 'DIRECTORY_TOTAL_COUNT_INVALID', 0, p['max_rows'])
                    if expected is None: expected = total
                    elif expected != total: raise IMError('DIRECTORY_TOTAL_COUNT_CHANGED')
                    if previous_position is None and position != 0:
                        raise IMError('DIRECTORY_SCAN_NOT_AT_START')
                    raw_rows = page['rows']
                    if not isinstance(raw_rows, list) or len(raw_rows) > p['max_rows']:
                        raise IMError('DIRECTORY_ROW_BOUND')
                    rows = [_normalize_row(p, r) for r in raw_rows]
                    ids = [r['native_id'] for r in rows]
                    if len(ids) != len(set(ids)):
                        raise IMError('DIRECTORY_NATIVE_ID_COLLISION')
                    if previous_position is not None:
                        if position <= previous_position or not (set(ids) - seen):
                            raise IMError('DIRECTORY_PAGINATION_STALLED')
                        if previous_ids and ids and not (set(ids) & previous_ids):
                            raise IMError('DIRECTORY_PAGINATION_GAP')
                    for row in rows:
                        old = found.get(row['native_id'])
                        if old is not None and old['metadata'] != row:
                            raise IMError('DIRECTORY_IDENTITY_OR_METADATA_CHANGED')
                    for row in rows:
                        old = found.setdefault(row['native_id'], {'metadata': row, 'sections': [], 'folded': False})
                        if section['id'] not in old['sections']:old['sections'].append(section['id'])
                        old['folded'] |= section['kind'] == 'folded'
                    seen.update(ids)
                    if len(found) > p['max_rows']:
                        raise IMError('DIRECTORY_ROW_BOUND')
                    receipt['unique_rows'] = len(seen)
                    receipt['pages'].append({'position': position, 'rows': len(rows), 'sha256': digest(rows),
                                             'at_end': page['at_end'], 'native_total': total})
                    previous_ids = set(ids); previous_position = position
                    if page['at_end']:
                        if len(seen) != total: raise IMError('DIRECTORY_TOTAL_COUNT_MISMATCH')
                        ended = True;receipt['complete'] = True
                        break
                if not ended:raise IMError('DIRECTORY_END_NOT_PROVEN')
    except IMError as exc:
        error = exc.code
    except Exception:
        # UIA/COM errors may include private labels; never expose exception strings.
        error = 'DIRECTORY_PROVIDER_UNAVAILABLE'
    observed = now()
    identity = digest([account['platform'],account['account_namespace'],account['source_epoch'],p])
    items = []
    for cid, value in sorted(found.items()):
        row = value['metadata']; ts = row['last_epoch']; gaps = ['directory_only_no_message_readback','server_sync_not_verified']
        if ts is None:gaps.append('activity_time_unresolved')
        elif ts >= until:gaps.append('last_activity_after_window_requires_history_check')
        if row['kind'] == 'unknown':gaps.append('unresolved_conversation_type')
        if error:gaps.append('directory_scan_incomplete')
        items.append({'key': digest([account['platform'],account['account_namespace'],account['source_epoch'],row['kind'],cid]),
                      'platform':account['platform'],'account_namespace':account['account_namespace'],
                      'conversation_id':cid,'conversation_name':row['name'],'conversation_type':row['kind'],
                      'classification_basis':'reviewed_native_directory_fields',
                      'activity':'outside_window_directory' if ts is not None and ts < since else 'directory_candidate',
                      'directory_last_epoch':ts,'local_message_count':None,'index_signature':None,
                      'message_min_epoch':None,'message_max_epoch':None,'source_parts':value['sections'],
                      'presentation_flags':{'observed_in_folded_section':value['folded']},
                      'folded_state':'observed_in_reviewed_folded_section' if value['folded'] else 'not_observed_in_folded_section',
                      'eligible':False,'coverage_gaps':gaps,'native_metadata':{'directory_profile_sha256':digest(p)},
                      'source_identity':identity,'source_observed_at':observed,'source_kind':'desktop_directory_metadata'})
    complete = error is None and len(receipts) == len(p['sections']) and all(s['complete'] for s in receipts)
    return items, {'status':'scanned_directory_scope' if complete else 'partial_directory' if items else 'failed',
                   'error':error,'directory_enumeration_complete':complete,
                   'scope':'reviewed_accessible_regular_and_folded_sections_only',
                   'sections':receipts,'discovered_conversations':len(items),
                   'activity_time_unknown':sum(i['directory_last_epoch'] is None for i in items),
                   'profile_sha256':digest(p),'source_observed_at':observed,
                   'message_bodies_collected':False,'full_channel_coverage_verified':False,
                   'ui_used':ui_attempted,'source_client_version_required':False,'llm_calls':0}


class UIADirectoryReader:
    """Real native-pattern reader. No coordinate, key, clipboard or force-focus fallback."""
    def __init__(self):
        self.n = NativeWindows()

    @contextmanager
    def lease(self, account):
        self.p = account['directory']
        require_consent(True)
        with self.n.lease(account['platform'], {'window':self.p['window'], 'allow_activation':False}):
            self.guard()
            yield self

    def guard(self):
        require_consent(True)
        self.n.guard()
        try:self.n.find(self.p['account_header'],self.n.root)
        except IMError as exc:
            if exc.code == 'DESKTOP_SELECTOR_MISSING_OR_AMBIGUOUS':
                raise IMError('DIRECTORY_ACCOUNT_IDENTITY_CHANGED') from None
            raise

    def _safe_control(self, control):
        self.guard()
        if control.ProcessId != self.n.pid or control.IsOffscreen or not control.IsEnabled or control.IsPassword:
            raise IMError('DIRECTORY_METADATA_CONTROL_UNSAFE')

    def _snapshot(self, section):
        self.guard()
        listing = self.n.find(section['listing'], self.n.root)
        self._safe_control(listing)
        pattern = listing.GetScrollPattern()
        if pattern is None:raise IMError('DIRECTORY_SCROLL_PATTERN_UNAVAILABLE')
        vertical = bool(pattern.VerticallyScrollable)
        position = float(pattern.VerticalScrollPercent) if vertical else 0.0
        count_control = self.n.find(section['total_count'],self.n.root)
        self._safe_control(count_control)
        count_text = count_control.Name
        if not isinstance(count_text,str) or not count_text.isascii() or not count_text.isdigit() or len(count_text)>6:
            raise IMError('DIRECTORY_TOTAL_COUNT_UNAVAILABLE')
        rows = []
        for item in listing.GetChildren():
            self.guard()
            if item.ControlTypeName != self.p['row_control_type'] or item.IsOffscreen:continue
            self._safe_control(item)
            raw = {}
            for field, match in self.p['fields'].items():
                control = self.n.find(match,item);self._safe_control(control)
                raw[field] = control.Name
            rows.append(raw)
            if len(rows)>self.p['max_rows']:raise IMError('DIRECTORY_ROW_BOUND')
        self.guard()
        return {'rows':rows,'position':position,'at_end':not vertical or position>=100.0,'total':int(count_text)}

    def pages(self, section):
        self.guard()
        if 'expand' in section:
            folder = self.n.find(section['expand'],self.n.root);self._safe_control(folder)
            pattern = folder.GetExpandCollapsePattern()
            if pattern is None:raise IMError('DIRECTORY_EXPAND_PATTERN_UNAVAILABLE')
            state = pattern.ExpandCollapseState
            if state == 0:
                self.guard()
                if not pattern.Expand(waitTime=0.1):raise IMError('DIRECTORY_EXPAND_FAILED')
                self.guard()
                if pattern.ExpandCollapseState != 1:raise IMError('DIRECTORY_EXPAND_NOT_CONFIRMED')
            elif state != 1:raise IMError('DIRECTORY_EXPAND_STATE_UNSUPPORTED')
        listing = self.n.find(section['listing'],self.n.root);self._safe_control(listing)
        scroll = listing.GetScrollPattern()
        if scroll is None:raise IMError('DIRECTORY_SCROLL_PATTERN_UNAVAILABLE')
        if scroll.VerticallyScrollable:
            self.guard()
            if not scroll.SetScrollPercent(-1,0,waitTime=0.1):raise IMError('DIRECTORY_RESET_FAILED')
        for _ in range(self.p['max_pages']):
            first=self._snapshot(section);second=self._snapshot(section)
            if first!=second:raise IMError('DIRECTORY_LAYOUT_CHANGED')
            yield second
            if second['at_end']:return
            self.guard()
            listing=self.n.find(section['listing'],self.n.root);self._safe_control(listing)
            scroll=listing.GetScrollPattern()
            if scroll is None:raise IMError('DIRECTORY_SCROLL_PATTERN_UNAVAILABLE')
            # UIA ScrollAmount.NoAmount=2, SmallIncrement=4; require overlap in the caller.
            if not scroll.Scroll(2,4,waitTime=0.1):raise IMError('DIRECTORY_SCROLL_FAILED')
        raise IMError('DIRECTORY_PAGE_BOUND')


def plan_desktop_backfill(home: Path, inventory_id: str, config: Path | None = None,
                          limit=100, offset=0) -> dict:
    """Read-only binding plan. Never silently reuses a same-name group or launches UI.

    Completed group captures may be reused through an explicitly pinned run id.
    New GUI acquisition and direct-chat adapters are not implied by this plan.
    """
    from .activity import _load, _inventory_fence
    folder, inventory = _load(home,inventory_id)
    inventory_hash = _inventory_fence(folder,inventory)
    integer(limit,'INVENTORY_PAGE_BOUND',1,500);integer(offset,'INVENTORY_PAGE_BOUND',0)
    sources={};config_hash=None
    if config is not None:
        raw=read_blob(config);data=load_json(raw);config_hash=digest(raw)
        if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('sources'),dict) or len(data['sources'])>64:
            raise IMError('SOURCE_CONFIG_VERSION_UNSUPPORTED')
        sources=data['sources']
    # Page metadata before reading/parsing captured message bodies.
    candidates=[e for e in inventory['conversations']
                if e.get('source_kind')=='desktop_directory_metadata' and e['activity']!='outside_window_directory']
    plan=[]
    for e in candidates[offset:offset+limit]:
        a=inventory['accounts'][e['account_key']]
        status='requires_source_binding';bound=[]
        for name,p in sources.items():
            if not isinstance(p,dict) or p.get('enabled') is not True:continue
            if any(p.get(k)!=a[k] for k in ('platform','account_namespace','source_epoch','data_class')):continue
            if p.get('conversation_id')!=e['conversation_id']:continue
            if p.get('conversation_name')!=e['conversation_name']:continue
            binding=p.get('directory_binding')
            if binding!={'native_conversation_id':e['conversation_id'],'conversation_type':e['conversation_type']} :continue
            bound.append(name)
        if e['conversation_type'] not in ('group','direct'):status='unresolved_conversation_type'
        elif len(bound)>1:status='ambiguous_source_binding'
        elif len(bound)==1:
            if e['conversation_type']=='direct':
                status='requires_typed_direct_desktop_adapter'
            else:
                from .desktop_backfill import load_capture
                try:
                    load_capture(home,e,bound[0],sources[bound[0]],inventory['window'])
                    status='ready_pinned_capture'
                except (IMError,OSError,ValueError,KeyError,TypeError):
                    status='requires_valid_pinned_capture'
        plan.append({'key':e['key'],'platform':e['platform'],'conversation_type':e['conversation_type'],
                     'conversation_id':e['conversation_id'],'conversation_name':e['conversation_name'],
                     'status':status,'bound_sources':bound,'window':inventory['window'],
                     'folded_state':e['folded_state'],'coverage_gaps':e['coverage_gaps'],
                     'execution_supported':status=='ready_pinned_capture'})
    _inventory_fence(folder,inventory,inventory_hash)
    if config is not None and digest(read_blob(config))!=config_hash:
        raise IMError('DESKTOP_BINDING_CONFIG_CHANGED')
    return {'inventory_id':inventory_id,'inventory_sha256':inventory_hash,
            'source_config_sha256':config_hash,'window':inventory['window'],
            'items':plan,'total_items':len(candidates),
            'next_offset':offset+limit if offset+limit<len(candidates) else None,
            'query_only':True,'execution_performed':False,'ui_used':False,'llm_calls':0,
            'four_platform_complete':False}
