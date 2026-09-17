"""Explicit per-conversation acquisition followed by the existing window backfill.

Only identity-verified inventory entries with reviewed exact source bindings may
acquire. A visible-label probe cannot become an inventory. No scheduler, discovery
by guessing, credentials, or replacement store is added. UI still uses the existing
collector's reviewed profile plus explicit consent/deadline. Lost dispatches are
fenced by an intent written BEFORE acquisition; retries never blindly re-drive UI.
"""
from __future__ import annotations
import re
from pathlib import Path
from .common import IMError, canonical, digest, load_json, read_blob, write_new, now, writer
from .database_readers import integer
from .activity import _load, _inventory_fence
from .desktop_sources import collect_desktop, prepare_desktop_profile, profile_fingerprint, import_window_bounds
from .desktop_directory import require_consent
from .desktop_backfill import backfill_desktop_week, load_capture

BINDING = ('platform','account_namespace','source_epoch','data_class','conversation_id','conversation_name')
ROUTING = {'directory_binding','captured_run_id'}


def _match(entry, account, sources):
    found=[]
    for name,p in sources.items():
        if not isinstance(p,dict) or p.get('enabled') is not True:continue
        if any(p.get(k)!=account[k] for k in ('platform','account_namespace','source_epoch','data_class')):continue
        if any(p.get(k)!=entry[k] for k in ('conversation_id','conversation_name')):continue
        if p.get('directory_binding')!={'native_conversation_id':entry['conversation_id'],
                                        'conversation_type':entry['conversation_type']}:continue
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',name):
            raise IMError('INVALID_SOURCE_NAME')
        found.append((name,p))
    return found


def _private_child(root: Path, name: str):
    child=root/name
    if child.exists() and (child.is_symlink() or child.resolve().parent!=root.resolve()):
        raise IMError('DESKTOP_REQUEST_PATH_UNSAFE')
    child.mkdir(exist_ok=True,mode=0o700)
    return child


def _read_record(path):
    if path.is_symlink():raise IMError('DESKTOP_REQUEST_PATH_UNSAFE')
    return load_json(read_blob(path)) if path.is_file() else None


def collect_desktop_week(home: Path, inventory_id: str, config: Path, *,
                         max_conversations=1, offset=0, allow_ui=False,
                         expected_config_sha256=None, backend=None, collector=None):
    """One bounded foreground call. It installs no job, watcher or recurring task.

    A missing capture can use tim-export/tim-ui/wecom-ui only. Completed captures
    are re-used on retry, even without UI permission. An intent without a durable
    result stays reconciliation_required until an explicit captured_run_id is
    supplied; changing a config never silently clears that fence.
    """
    integer(max_conversations,'DESKTOP_WEEK_COLLECTION_BOUND',1,20)
    integer(offset,'INVENTORY_PAGE_BOUND',0)
    folder,inventory=_load(home,inventory_id);inv_hash=_inventory_fence(folder,inventory)
    import_window_bounds(inventory['window'])
    raw=read_blob(config);config_hash=digest(raw);cfg=load_json(raw)
    if expected_config_sha256 is not None and expected_config_sha256!=config_hash:
        raise IMError('DESKTOP_BINDING_CONFIG_CHANGED')
    if (not isinstance(cfg,dict) or cfg.get('version')!=1 or not isinstance(cfg.get('sources'),dict)
            or len(cfg['sources'])>64):raise IMError('SOURCE_CONFIG_VERSION_UNSUPPORTED')
    sources=cfg['sources'];run=collector if collector is not None else collect_desktop
    candidates=[e for e in inventory['conversations']
                if e.get('source_kind')=='desktop_directory_metadata' and e['activity']!='outside_window_directory']
    results=[];new_records=0;capture_calls=0;ui_attempted=False;stopped=False

    def fence():
        _inventory_fence(folder,inventory,inv_hash)
        if digest(read_blob(config))!=config_hash:raise IMError('DESKTOP_BINDING_CONFIG_CHANGED')

    with writer(home,lock_name='.run.lock'):
        fence()
        for index,e in enumerate(candidates[offset:offset+max_conversations],start=offset):
            fence();base={'key':e['key'],'platform':e['platform'],'conversation_type':e['conversation_type'],
                         'folded_state':e['folded_state'],'window':inventory['window']}
            if e['conversation_type']!='group':
                results.append({**base,'status':'requires_typed_direct_desktop_adapter' if e['conversation_type']=='direct' else 'unresolved_conversation_type'});continue
            matches=_match(e,inventory['accounts'][e['account_key']],sources)
            if len(matches)!=1:
                results.append({**base,'status':'ambiguous_source_binding' if matches else 'requires_source_binding'});continue
            source_name,p=matches[0];pinned=p.get('captured_run_id');capture_result=None;dispatched=False
            try:
                if pinned is None:
                    # Strip only orchestration fields; existing source fingerprint stays identical.
                    source_profile={k:v for k,v in p.items() if k not in ROUTING}
                    if source_profile.get('transport') not in ('tim-export','tim-ui','wecom-ui'):
                        raise IMError('REVIEWED_AUTOMATIC_DESKTOP_SOURCE_REQUIRED')
                    prepared=prepare_desktop_profile(config.resolve().parent,source_profile)
                    if not re.fullmatch(r'[0-9a-f]{64}',str(e['key'])):
                        raise IMError('INVALID_CONVERSATION_KEY')
                    acquire_root=folder/'acquire';request=acquire_root/e['key']
                    for child,parent in ((acquire_root,folder),(request,acquire_root)):
                        if child.exists() and (child.is_symlink() or child.resolve().parent!=parent.resolve()):
                            raise IMError('DESKTOP_REQUEST_PATH_UNSAFE')
                    intent_path=request/'intent.json';result_path=request/'capture.json'
                    intended={'schema':'im-hub-week-capture-intent/1','inventory_sha256':inv_hash,
                              'entry_key':e['key'],'source_name':source_name,'window':inventory['window'],
                              'profile_sha256':profile_fingerprint(prepared)}
                    existing=_read_record(intent_path) if request.exists() else None
                    captured=_read_record(result_path) if request.exists() else None
                    if existing is not None:
                        if not isinstance(existing,dict) or existing.get('intent')!=intended:
                            raise IMError('DESKTOP_CAPTURE_INTENT_CHANGED_RECONCILE')
                        if not isinstance(captured,dict) or captured.get('intent_sha256')!=digest(existing):
                            raise IMError('DESKTOP_CAPTURE_OUTCOME_REQUIRES_RECONCILIATION')
                        pinned=captured.get('captured_run_id')
                        if not isinstance(pinned,str) or not re.fullmatch(r'[0-9a-f]{32}',pinned):
                            raise IMError('DESKTOP_CAPTURE_OUTCOME_REQUIRES_RECONCILIATION')
                    else:
                        if captured is not None:raise IMError('DESKTOP_CAPTURE_INTENT_MISSING')
                        if prepared['transport'].endswith('-ui'):require_consent(allow_ui)
                        acquire_root=_private_child(folder,'acquire');request=_private_child(acquire_root,e['key'])
                        fence()
                        envelope={'intent':intended,'created_at':now()}
                        write_new(intent_path,canonical(envelope).encode('utf-8'))
                        dispatched=True;capture_calls+=1;ui_attempted|=prepared['transport'].endswith('-ui')
                        capture_result=run(home,config.resolve().parent,source_name,source_profile,
                                           backend=backend,allow_ui=allow_ui,import_window=inventory['window'])
                        pinned=capture_result.get('run_id')
                        if not isinstance(pinned,str) or not re.fullmatch(r'[0-9a-f]{32}',pinned):
                            raise IMError('DESKTOP_CAPTURE_RESULT_INVALID')
                        # Persist the correlation even when a later config fence detects change.
                        record={'intent_sha256':digest(envelope),'captured_run_id':pinned,'observed_at':now()}
                        write_new(result_path,canonical(record).encode('utf-8'))
                binding={k:p[k] for k in BINDING}|{'enabled':True,'directory_binding':p['directory_binding'],'captured_run_id':pinned}
                captured=load_capture(home,e,source_name,binding,inventory['window'])
                fence()
                pins=_private_child(folder,'capture-bindings')
                payload=canonical({'version':1,'sources':{source_name:binding}}).encode('utf-8')
                pin_path=pins/(digest(payload)+'.json')
                if pin_path.exists():
                    if pin_path.is_symlink() or read_blob(pin_path)!=payload:raise IMError('DESKTOP_PINNED_BINDING_CHANGED')
                else:write_new(pin_path,payload)
                finished=backfill_desktop_week(home,inventory_id,pin_path,max_conversations=1,offset=index,
                                               expected_config_sha256=digest(payload),backend=backend,upstream_fence=fence)
                fence()
                if finished['committed_conversations']!=1 or finished['failed_conversations'] or finished['held_conversations']:
                    raise IMError('DESKTOP_WINDOW_BACKFILL_NOT_COMMITTED')
                capture_added=(capture_result or {}).get('new_records',0)
                if type(capture_added) is not int or capture_added<0:raise IMError('DESKTOP_CAPTURE_RESULT_INVALID')
                added=capture_added+finished['new_records'];new_records+=added
                results.append({**base,'status':'committed','source_name':source_name,'captured_run_id':pinned,
                                'records_checked':len(captured['rows']),'new_records':added,
                                'new_acquisition_performed':capture_result is not None and not capture_result.get('resumed_pending',False),
                                'history_complete':False,'complete_through':None})
            except Exception as exc:
                code=exc.code if isinstance(exc,IMError) else 'DESKTOP_WEEK_COLLECTION_FAILED'
                unknown=dispatched
                results.append({**base,'status':'reconciliation_required' if unknown or 'RECONCIL' in code else 'held',
                                'error':code,'new_acquisition_may_have_occurred':unknown})
                if dispatched or 'RECONCIL' in code or code in ('WEEK_INVENTORY_CHANGED','WEEK_INVENTORY_HASH_MISMATCH','DESKTOP_BINDING_CONFIG_CHANGED'):
                    stopped=True;break
        fence()
    retry_offsets=[offset+i for i,r in enumerate(results) if r['status']!='committed']
    next_offset=offset+len(results)
    # An unknown dispatch is not a consumed conversation. Resume its exact entry.
    if stopped and retry_offsets:next_offset=retry_offsets[-1]
    return {'inventory_id':inventory_id,'inventory_sha256':inv_hash,'source_config_sha256':config_hash,
            'window':inventory['window'],'items':results,'total_candidates':len(candidates),
            'next_offset':next_offset if next_offset<len(candidates) else None,
            'retry_offsets':retry_offsets,
            'committed_conversations':sum(r['status']=='committed' for r in results),
            'held_conversations':sum(r['status']!='committed' for r in results),
            'new_capture_calls':capture_calls,'new_records':new_records,'stopped_early':stopped,
            'ui_attempted':ui_attempted,'history_complete':False,'four_platform_complete':False,
            'query_only':False,'llm_calls':0,'scheduler_installed':False,
            'scope':'explicit_identity_bound_group_sources_in_frozen_inventory'}
