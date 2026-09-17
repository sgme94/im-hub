"""Frozen-week conversation inventory, resumable backfill and honest coverage.
Discovery is metadata-only. Backfill explicitly persists authorized group/direct
messages; other categories and unscanned platforms are never silently counted as
complete. GUI discovery is opt-in and deadline-bound; no model or background work.
"""
from __future__ import annotations
import html
import json
import os
import re
import tempfile
import uuid
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from .common import IMError, canonical, check_home, digest, iso_epoch, load_json, now, read_blob, stamp, stream_key, write_new, writer
from .activity_sources import ACCOUNT_KINDS, CATALOG_LIMIT, account_snapshot, discover_account, prepare_accounts, read_messages, snapshot_identity

BINDING = ('platform','account_namespace','conversation_id','conversation_name','conversation_type','source_epoch','data_class')


def _atomic(path, data):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    write_new(temporary,canonical(data).encode('utf-8'))
    os.replace(temporary,path)


def _folder(home, inventory_id):
    check_home(home)
    if not isinstance(inventory_id,str) or not re.fullmatch(r'[0-9a-f]{32}',inventory_id):
        raise IMError('INVALID_WEEK_INVENTORY_ID')
    return home/'activity'/inventory_id


def _load(home, inventory_id):
    folder=_folder(home,inventory_id);blob=read_blob(folder/'inventory.json')
    expected=(folder/'inventory.sha256').read_text('ascii').strip()
    if digest(blob)!=expected:raise IMError('WEEK_INVENTORY_HASH_MISMATCH')
    data=load_json(blob)
    if not isinstance(data,dict) or data.get('schema')!='im-hub-week-inventory/1' or data.get('inventory_id')!=inventory_id:
        raise IMError('INVALID_WEEK_INVENTORY')
    return folder,data


def _inventory_fence(folder, data, expected_sha256=None):
    """Bind multi-stage readers to the same verified immutable inventory."""
    raw=read_blob(folder/'inventory.json');current=digest(raw)
    declared=(folder/'inventory.sha256').read_text('ascii').strip()
    if current!=declared:raise IMError('WEEK_INVENTORY_HASH_MISMATCH')
    if expected_sha256 is not None:
        if current!=expected_sha256:raise IMError('WEEK_INVENTORY_CHANGED')
    elif load_json(raw)!=data:
        raise IMError('WEEK_INVENTORY_CHANGED')
    return current


def _receipt(folder,key):
    if not re.fullmatch(r'[0-9a-f]{64}',key):raise IMError('INVALID_CONVERSATION_KEY')
    path=folder/'results'/(key+'.json')
    return load_json(read_blob(path)) if path.is_file() else None


def discover_week(home: Path, config: Path, since=None, until=None, days=7, snapshotter=account_snapshot, *, allow_ui=False) -> dict:
    check_home(home)
    if isinstance(days,bool) or not isinstance(days,int) or not 1<=days<=31:raise IMError('DISCOVERY_DAYS_MUST_BE_1_TO_31')
    end=int(iso_epoch(until)) if until is not None else int(iso_epoch(now()))
    start=iso_epoch(since) if since is not None else end-days*86400
    if not 946684800<=start<end<=iso_epoch(now())+1 or end-start>31*86400:
        raise IMError('DISCOVERY_WINDOW_INVALID_OR_TOO_LARGE')
    accounts,config_hash=prepare_accounts(config.resolve())
    identifier=uuid.uuid4().hex;entries=[];summaries={}
    with writer(home,lock_name='.activity.lock'):
        folder=home/'activity'/identifier
        folder.mkdir(parents=True,mode=0o700)
        for part in ('packets','results'):(folder/part).mkdir(mode=0o700)
        for name,p in accounts.items():
            base={'platform':p['platform'],'account_namespace':p['account_namespace']}
            if p['transport']=='not-scanned':
                summaries[name]={**base,'status':'not_scanned','error':p['reason'],'discovered_conversations':None};continue
            try:
                if p['transport']=='desktop-directory':
                    from .desktop_directory import discover_desktop_account
                    items,summary=discover_desktop_account(p,start,end,allow_ui=allow_ui)
                else:
                    with snapshotter(p,home) as sources:items,summary=discover_account(p,sources,start,end)
                    summary={**summary,'status':'scanned_local_scope'}
                for e in items:e['account_key']=name
                entries.extend(items)
                summaries[name]={**base,**summary,'discovered_conversations':len(items)}
            except (IMError,OSError,ValueError,TypeError,KeyError) as exc:
                summaries[name]={**base,'status':'failed','error':exc.code if isinstance(exc,IMError) else 'LOCAL_DISCOVERY_FAILED_'+type(exc).__name__,
                                 'discovered_conversations':None}
        if len(entries)>CATALOG_LIMIT:raise IMError('TOTAL_ACTIVE_CONVERSATION_BOUND')
        keys=[e['key'] for e in entries]
        if len(keys)!=len(set(keys)):raise IMError('CONVERSATION_IDENTITY_COLLISION')
        data={'schema':'im-hub-week-inventory/1','inventory_id':identifier,'created_at':now(),
              'window':{'since':stamp(start),'until_exclusive':stamp(end)},
              'config_sha256':config_hash,'accounts':accounts,'account_results':summaries,
              'conversations':sorted(entries,key=lambda e:(e['platform'],e['conversation_type'],e['key'])),
              'message_bodies_collected':False,'ui_used':any(s.get('ui_used',False) for s in summaries.values()),'source_versions_required':False,
              'classification_policy':'group_and_direct_only; service_and_unknown_are_separate',
              'all_platform_discovery_complete':False,'server_history_complete':False}
        blob=canonical(data).encode('utf-8');write_new(folder/'inventory.json',blob)
        write_new(folder/'inventory.sha256',(digest(blob)+'\n').encode('ascii'))
        result=week_status(home,identifier)
        # Discovery persists an inventory/report; it is not a read-only query.
        result['query_only']=False
        result['ui_used']=data['ui_used']
        _save_report(folder,data,result)
        return result


def normalize_activity(blob,spec,since=None,until=None):
    from .adapters import _record
    p=load_json(blob)
    if not isinstance(p,dict) or p.get('schema')!='im-hub-activity/1' or p.get('binding')!={k:spec[k] for k in BINDING}:
        raise IMError('ACTIVITY_PACKET_BINDING_MISMATCH')
    if spec['conversation_type'] not in ('group','direct'):raise IMError('ACTIVITY_CONVERSATION_TYPE_REQUIRED')
    window=p.get('window',{})
    if iso_epoch(window.get('since'))!=since or iso_epoch(window.get('until_exclusive'))!=until:
        raise IMError('ACTIVITY_PACKET_WINDOW_MISMATCH')
    observed=iso_epoch(p.get('observed_at'))
    if p.get('local_window_read_complete') is not True or p.get('source_kind') not in ('native_local_database','authenticated_local_database'):
        raise IMError('ACTIVITY_LOCAL_SNAPSHOT_PROOF_REQUIRED')
    source=p.get('rows')
    if not isinstance(source,list) or len(source)>200000:raise IMError('ACTIVITY_RECORD_COUNT_BOUND')
    rows=[];index=[]
    for r in source:
        if not isinstance(r,dict) or not isinstance(r.get('index_tuple'),list) or len(r['index_tuple'])!=4:
            raise IMError('ACTIVITY_RECORD_INVALID')
        ts=r.get('timestamp')
        if isinstance(ts,bool) or not isinstance(ts,int) or not since<=ts<until or ts>observed+300:
            raise IMError('ACTIVITY_RECORD_OUTSIDE_FIXED_WINDOW')
        if not isinstance(r.get('flags',[]),list) or any(not isinstance(f,str) for f in r.get('flags',[])):
            raise IMError('ACTIVITY_FLAGS_INVALID')
        if not isinstance(r.get('extras',{}),dict) or not isinstance(r.get('locator'),dict):raise IMError('ACTIVITY_METADATA_INVALID')
        ix=r['index_tuple']
        if not isinstance(ix[0],str) or any(isinstance(v,bool) or not isinstance(v,int) for v in ix[1:]) or ix[2]!=ts:
            raise IMError('ACTIVITY_INDEX_TUPLE_INVALID')
        index.append(ix)
        row=_record(spec,r.get('identity'),'native_local_identity_scoped_to_type_and_account',ts,r.get('text'),r.get('type'),
                    r.get('sender_id') or 'unresolved',r.get('sender_name') or '未核实发送者',r.get('sender_verified') is True,
                    r['locator'],native_id=r.get('native_id'),flags=r.get('flags',[]),reply=r.get('reply_to'),extras=r.get('extras',{}))
        row.update(conversation_type=spec['conversation_type'],source_kind=p['source_kind'],source_time_basis='local_database_read_not_server_sync')
        rows.append(row)
    if len({r['message_key'] for r in rows})!=len(rows):raise IMError('ACTIVITY_DUPLICATE_NATIVE_ID')
    index.sort(key=lambda r:(r[0],r[1],r[2],r[3]))
    if p.get('index_signature')!=digest(index):raise IMError('ACTIVITY_INDEX_SIGNATURE_MISMATCH')
    rows.sort(key=lambda r:(r['event_ms'],r['message_key']))
    return rows,{'scope':'active_conversation_fixed_local_window','conversation_type':spec['conversation_type'],
                'source_kind':p['source_kind'],'selected_records':len(rows),'source_records_recognized':len(rows),
                'requested_since':stamp(since),'requested_until_exclusive':stamp(until),
                'observed_event_min':rows[0]['event_time'] if rows else None,'observed_event_max':rows[-1]['event_time'] if rows else None,
                'local_window_read_complete':True,'source_observed_at':p['observed_at'],
                'complete_through':None,'history_completeness':'partial','server_sync_verified':False,
                'gaps':['server_sync_not_verified','attachment_and_nontext_bodies_may_be_partial'],
                'cross_snapshot_dedup':'native_id_within_account_type_conversation_and_source_epoch'}


def _spec(account,e):
    from .adapters import spec_for
    return spec_for(account['platform'],account['account_namespace'],e['conversation_id'],e['conversation_name'],
                    'activity-json',account['source_epoch'],account['data_class'],conversation_type=e['conversation_type'])


def _failure(folder,e,code):
    old=_receipt(folder,e['key']) or {}
    _atomic(folder/'results'/(e['key']+'.json'),{**old,'status':'failed','error':code,'updated_at':now()})


def backfill_week(home:Path,inventory_id:str,max_conversations=200,replay=False,backend=None,snapshotter=account_snapshot):
    from .store import ingest
    if isinstance(max_conversations,bool) or not isinstance(max_conversations,int) or not 1<=max_conversations<=2000:
        raise IMError('BACKFILL_CONVERSATION_BOUND')
    folder,data=_load(home,inventory_id);start=iso_epoch(data['window']['since']);end=iso_epoch(data['window']['until_exclusive'])
    processed=[];chosen=[];new_total=0
    with writer(home,lock_name='.activity.lock'):
        for e in data['conversations']:
            if not e['eligible']:continue
            result=_receipt(folder,e['key'])
            if not replay and result and result.get('status')=='committed':continue
            chosen.append(e)
            if len(chosen)>=max_conversations:break
        for account_key,p in data['accounts'].items():
            need=[e for e in chosen if e['account_key']==account_key and not (folder/'packets'/(e['key']+'.json')).is_file()]
            if not need:continue
            staged=[]
            try:
                with tempfile.TemporaryDirectory(prefix='backfill-stage-',dir=home/'runtime') as td:
                    with snapshotter(p,home) as sources:
                        current,_=discover_account(p,sources,start,end);bykey={e['key']:e for e in current}
                        for e in need:
                            try:
                                current_e=bykey.get(e['key'])
                                if current_e is None or current_e['source_identity']!=e['source_identity']:
                                    raise IMError('ACCOUNT_SOURCE_REPLACED_REDISCOVER')
                                if (current_e['index_signature']!=e['index_signature'] or current_e['local_message_count']!=e['local_message_count']
                                    or current_e['native_metadata']!=e['native_metadata']):
                                    raise IMError('DISCOVERED_MESSAGE_INDEX_CHANGED_REDISCOVER')
                                rows=read_messages(p,sources,current_e,start,end);spec=_spec(p,e)
                                packet={'schema':'im-hub-activity/1','binding':{k:spec[k] for k in BINDING},
                                        'window':data['window'],'observed_at':min((s['observed_at'] for s in sources),key=iso_epoch),
                                        'source_kind':'authenticated_local_database' if p['platform']=='wechat' else 'native_local_database',
                                        'source_identity':snapshot_identity(sources),'local_window_read_complete':True,
                                        'index_signature':e['index_signature'],'rows':rows}
                                raw=canonical(packet).encode('utf-8');normalize_activity(raw,spec,start,end)
                                temp=Path(td)/(e['key']+'.json');write_new(temp,raw);staged.append((temp,e))
                            except (IMError,OSError,ValueError,KeyError,TypeError) as exc:
                                _failure(folder,e,exc.code if isinstance(exc,IMError) else 'CONVERSATION_BACKFILL_FAILED_'+type(exc).__name__)
                    # A failed account snapshot cannot leave partially accepted new packets.
                    for temp,e in staged:
                        dest=folder/'packets'/temp.name;temp.rename(dest)
                        write_new(dest.with_suffix('.sha256'),(digest(read_blob(dest))+'\n').encode('ascii'))
            except (IMError,OSError,ValueError,KeyError,TypeError) as exc:
                for e in need:_failure(folder,e,exc.code if isinstance(exc,IMError) else 'ACCOUNT_BACKFILL_FAILED_'+type(exc).__name__)
        for e in chosen:
            path=folder/'packets'/(e['key']+'.json')
            if not path.is_file():
                receipt=_receipt(folder,e['key']) or {'status':'failed','error':'PACKET_NOT_STAGED'}
                processed.append({'key':e['key'],'status':'failed','error':receipt.get('error')});continue
            try:
                raw=read_blob(path);expected=path.with_suffix('.sha256').read_text('ascii').strip()
                if digest(raw)!=expected:raise IMError('STAGED_ACTIVITY_PACKET_CHANGED')
                packet=load_json(raw);p=data['accounts'][e['account_key']];spec=_spec(p,e)
                extra={'backend':backend} if backend is not None else {}
                imported=ingest(home,path,spec,packet['observed_at'],since=start,until=end,expected_sha256=expected,**extra)
                if imported['records']!=e['local_message_count']:raise IMError('BACKFILL_COUNT_READBACK_MISMATCH')
                old=_receipt(folder,e['key']) or {}
                receipt={'status':'committed','error':None,'updated_at':now(),'first_imported_at':old.get('first_imported_at',now()),
                         'stream_id':imported['stream_id'],'packet_sha256':expected,'records':imported['records'],
                         'new_records_last_run':imported['new_records'],'duplicates_last_run':imported['duplicates'],
                         'body_gap_records':sum(bool(r.get('flags')) or r.get('text') is None for r in packet['rows']),
                         'readback':imported['readback'],'source_observed_at':packet['observed_at']}
                _atomic(folder/'results'/(e['key']+'.json'),receipt)
                processed.append({'key':e['key'],'status':'committed','records':imported['records'],'new_records':imported['new_records']})
                new_total+=imported['new_records']
            except (IMError,OSError,ValueError,KeyError,TypeError) as exc:
                code=exc.code if isinstance(exc,IMError) else 'CONVERSATION_IMPORT_FAILED_'+type(exc).__name__
                _failure(folder,e,code);processed.append({'key':e['key'],'status':'failed','error':code})
        result=week_status(home,inventory_id)
        result.update(query_only=False,processed_this_call=len(processed),new_records_this_call=new_total,
                      failed_this_call=sum(x['status']=='failed' for x in processed),
                      replay_requested=bool(replay))
        _atomic(folder/'last-backfill.json',{'at':now(),'result':result,'processed':processed})
        _save_report(folder,data,result)
        return result


def week_status(home,inventory_id,details=False,limit=100,offset=0,verify=False):
    from .store import validate_readback
    folder,data=_load(home,inventory_id)
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=500 or isinstance(offset,bool) or not isinstance(offset,int) or offset<0:
        raise IMError('INVENTORY_PAGE_BOUND')
    entries=[];platforms={k:{'platform':k,'account_statuses':[],'confirmed_groups':0,'confirmed_direct':0,
                            'service_conversations':0,'unknown_conversations':0,'directory_only_candidates':0,
                            'eligible_conversations':0,'expected_local_messages':0,'committed_conversations':0,
                            'committed_messages':0,'pending_conversations':0,'failed_conversations':0,'body_gap_records':0,
                            'directory_group_candidates':0,'directory_direct_candidates':0,'directory_time_unknown':0,
                            'desktop_captured_conversations':0,'desktop_captured_messages':0,'desktop_capture_failures':0}
                        for k in ('wechat','kim','qq','wecom')}
    for account_key,summary in data['account_results'].items():
        platforms[summary['platform']]['account_statuses'].append({'account_key':account_key,**summary})
    for e in data['conversations']:
        ps=platforms[e['platform']];typ=e['conversation_type'];receipt=_receipt(folder,e['key']);state='pending' if e['eligible'] else 'excluded_service' if typ=='service' else 'needs_review_or_source_data'
        if typ in ('group','direct') and e['activity']=='message_confirmed':ps['confirmed_'+('groups' if typ=='group' else 'direct')]+=1
        if typ=='service':ps['service_conversations']+=1
        if typ=='unknown':ps['unknown_conversations']+=1
        if e['activity']=='directory_candidate':ps['directory_only_candidates']+=1
        if e.get('source_kind')=='desktop_directory_metadata' and e['activity']=='directory_candidate':
            if typ in ('group','direct'):ps['directory_'+typ+'_candidates']+=1
            if e.get('directory_last_epoch') is None:ps['directory_time_unknown']+=1
        error=None
        if e['eligible']:
            ps['eligible_conversations']+=1;ps['expected_local_messages']+=e['local_message_count']
            state=(receipt or {}).get('status','pending');error=(receipt or {}).get('error')
            if state=='committed' and verify:
                try:
                    path=folder/'packets'/(e['key']+'.json');raw=read_blob(path)
                    if digest(raw)!=receipt['packet_sha256']:raise IMError('STAGED_ACTIVITY_PACKET_CHANGED')
                    spec=_spec(data['accounts'][e['account_key']],e)
                    packet=load_json(raw)
                    rows,_=normalize_activity(raw,spec,iso_epoch(data['window']['since']),iso_epoch(data['window']['until_exclusive']))
                    if receipt['stream_id']!=stream_key(spec):
                        raise IMError('ACTIVITY_RECEIPT_STREAM_MISMATCH')
                    if (isinstance(receipt.get('records'),bool) or not isinstance(receipt.get('records'),int)
                        or receipt['records']!=len(rows) or len(rows)!=e['local_message_count']):
                        raise IMError('ACTIVITY_RECEIPT_COUNT_MISMATCH')
                    if packet['index_signature']!=e['index_signature'] or packet.get('source_identity')!=e['source_identity']:
                        raise IMError('ACTIVITY_PACKET_INVENTORY_MISMATCH')
                    actual_gaps=sum(bool(r['content_flags']) or r['text'] is None for r in rows)
                    if receipt.get('body_gap_records')!=actual_gaps:
                        raise IMError('ACTIVITY_RECEIPT_BODY_GAP_MISMATCH')
                    validate_readback(home,receipt['stream_id'],rows)
                except (IMError,OSError,ValueError,KeyError,TypeError) as exc:
                    state='failed';error=exc.code if isinstance(exc,IMError) else 'BACKFILL_READBACK_FAILED'
            if state=='committed':
                ps['committed_conversations']+=1;ps['committed_messages']+=receipt['records'];ps['body_gap_records']+=receipt.get('body_gap_records',0)
            elif state=='failed':ps['failed_conversations']+=1
            else:ps['pending_conversations']+=1
        if e.get('source_kind')=='desktop_directory_metadata' and receipt:
            state=receipt.get('status','failed');error=receipt.get('error')
            if state=='committed':
                try:
                    from .desktop_backfill import verify_desktop_receipt
                    checked=verify_desktop_receipt(home,folder,data,e,receipt)
                    ps['desktop_captured_conversations']+=1;ps['desktop_captured_messages']+=checked['records']
                except (IMError,OSError,ValueError,KeyError,TypeError) as exc:
                    state='failed';error=exc.code if isinstance(exc,IMError) else 'DESKTOP_CAPTURE_READBACK_FAILED'
            if state=='failed':ps['desktop_capture_failures']+=1
        entries.append({k:e[k] for k in ('key','platform','conversation_type','conversation_id','conversation_name','activity','local_message_count','message_min_epoch','message_max_epoch','presentation_flags','folded_state','coverage_gaps')}|{'backfill_status':state,'error':error})
    for ps in platforms.values():
        ps['eligible_local_backfill_complete']=(bool(ps['account_statuses']) and all(s['status']=='scanned_local_scope' for s in ps['account_statuses']) and ps['pending_conversations']==0 and ps['failed_conversations']==0)
        ps['full_channel_coverage_verified']=False
        directory_scopes=[s for s in ps['account_statuses'] if 'directory_enumeration_complete' in s]
        ps['directory_enumeration_complete']=(bool(directory_scopes) and len(directory_scopes)==len(ps['account_statuses'])
                                              and all(s['directory_enumeration_complete'] for s in directory_scopes))
    result={'inventory_id':inventory_id,'window':data['window'],'platforms':list(platforms.values()),
            'eligible_conversations':sum(x['eligible_conversations'] for x in platforms.values()),
            'expected_local_messages':sum(x['expected_local_messages'] for x in platforms.values()),
            'committed_conversations':sum(x['committed_conversations'] for x in platforms.values()),
            'committed_messages':sum(x['committed_messages'] for x in platforms.values()),
            'pending_conversations':sum(x['pending_conversations'] for x in platforms.values()),
            'failed_conversations':sum(x['failed_conversations'] for x in platforms.values()),
            'desktop_captured_conversations':sum(x['desktop_captured_conversations'] for x in platforms.values()),
            'desktop_captured_messages':sum(x['desktop_captured_messages'] for x in platforms.values()),
            'desktop_capture_failures':sum(x['desktop_capture_failures'] for x in platforms.values()),
            'four_platform_complete':False,'server_history_complete':False,'body_completeness_not_implied':True,
            'query_only':True,'committed_records_reverified':bool(verify),'ui_used':False,'llm_calls':0,
            'inventory_path':str(folder/'inventory.json'),'coverage_report':str(folder/'coverage.md')}
    if details:result.update(items=entries[offset:offset+limit],total_items=len(entries),next_offset=offset+limit if offset+limit<len(entries) else None)
    return result


def _safe(value):
    return html.escape(str(value)).replace('|','\\|').replace('\n',' ').replace('\r',' ').replace('`','\\`').replace('[','\\[').replace(']','\\]')


def _save_report(folder,data,result):
    lines=['# 最近七天活跃会话与回补覆盖','',
           '固定窗口：'+data['window']['since']+' 至 '+data['window']['until_exclusive']+'（右侧不含）。',
           '仅核实指定账号的本地数据。未扫描的渠道、未知会话及目录有活动但缺少消息者不算完成；折叠和免打扰标记不作为排除条件。',
           '群聊和单聊回补与公众号/系统通知分开；未知类别不自动当作联系人。正文缺口、附件内容和服务端同步完整性另行核验。','',
           '| 平台 | 活跃群聊 | 活跃单聊 | 可回补会话 | 已完成 | 本地应读消息 | 已入库 | 目录有活动但缺正文 | 未知类别 |',
           '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for p in result['platforms']:
        unscanned=not p['account_statuses'] or any(s['status']!='scanned_local_scope' for s in p['account_statuses'])
        directory_scope=any('directory_enumeration_complete' in s for s in p['account_statuses'])
        if directory_scope:
            lines.append('| '+p['platform']+' | 目录候选 '+str(p['directory_group_candidates'])+' | 目录候选 '+str(p['directory_direct_candidates'])+' | 已固定窗口回读 '+str(p['desktop_captured_conversations'])+' | — | 全量未核实 | '+str(p['desktop_captured_messages'])+' | '+str(p['directory_only_candidates'])+' | '+str(p['unknown_conversations'])+' |')
        elif unscanned:lines.append('| '+p['platform']+' | 未扫描 | 未扫描 | — | — | — | — | — | — |')
        else:lines.append('| '+' | '.join(str(p[k]) for k in ('platform','confirmed_groups','confirmed_direct','eligible_conversations','committed_conversations','expected_local_messages','committed_messages','directory_only_candidates','unknown_conversations'))+' |')
    lines+=['','## 逐会话清单','', '| 平台 | 类型 | 会话 | 本地七天消息 | 处理状态 | 缺口或错误 |','|---|---|---|---:|---|---|']
    for e in data['conversations']:
        r=_receipt(folder,e['key']) or {}
        state=r.get('status') or ('pending' if e['eligible'] else 'excluded_service' if e['conversation_type']=='service' else 'needs_review_or_source_data')
        gap=r.get('error') or '; '.join(e['coverage_gaps'])
        if e.get('source_kind')=='desktop_directory_metadata' and state=='committed':
            try:
                from .desktop_backfill import verify_desktop_receipt
                verify_desktop_receipt(folder.parent.parent,folder,data,e,r)
            except (IMError,OSError,ValueError,KeyError,TypeError) as exc:
                state='failed';gap=exc.code if isinstance(exc,IMError) else 'DESKTOP_CAPTURE_READBACK_FAILED'
        lines.append('| '+' | '.join(_safe(v) for v in (e['platform'],e['conversation_type'],e['conversation_name'],e['local_message_count'],state,gap))+' |')
    temp=folder/('coverage-'+uuid.uuid4().hex+'.tmp');write_new(temp,('\n'.join(lines)+'\n').encode('utf-8'));os.replace(temp,folder/'coverage.md')
