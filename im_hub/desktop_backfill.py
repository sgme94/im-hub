"""Reuse exact committed desktop captures in a frozen directory inventory window.

No GUI, source refresh, clipboard, login, network or schedule. The existing
collectors retain ownership of acquisition/prefix checkpoints; this module only
passes a pinned captured payload to the existing transactional ingest/readback.
Directory identity must be explicitly mapped to the captured group's scope.
A filtered capture is NOT evidence of complete client or server history.
"""
from __future__ import annotations
import re
from pathlib import Path
from .adapters import normalize
from .common import IMError, digest, iso_epoch, load_json, now, read_blob, readonly, stream_key, writer
from .database_readers import integer
from .store import ingest, validate_readback

BINDING = ('platform','account_namespace','source_epoch','data_class','conversation_id','conversation_name')


def load_capture(home: Path, entry: dict, source_name: str, binding: dict,
                 window: dict) -> dict:
    """Read and authenticate local provenance, never select the newest run implicitly."""
    if entry['conversation_type'] != 'group':
        raise IMError('DESKTOP_TYPED_DIRECT_ADAPTER_REQUIRED')
    run_id=binding.get('captured_run_id')
    if not isinstance(run_id,str) or not re.fullmatch(r'[0-9a-f]{32}',run_id):
        raise IMError('PINNED_DESKTOP_RUN_ID_REQUIRED')
    if not isinstance(source_name,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',source_name):
        raise IMError('INVALID_SOURCE_NAME')
    expected={'native_conversation_id':entry['conversation_id'],'conversation_type':'group'}
    if binding.get('directory_binding')!=expected:
        raise IMError('DESKTOP_DIRECTORY_BINDING_REQUIRED')
    start=iso_epoch(window['since']);end=iso_epoch(window['until_exclusive'])
    if not 946684800 <= start < end or end-start>31*86400:
        raise IMError('DESKTOP_FIXED_WINDOW_INVALID')
    con=readonly(home/'desktop.sqlite3')
    try:run=con.execute('SELECT * FROM runs WHERE run_id=?',(run_id,)).fetchone()
    finally:con.close()
    if not run or run['source_id']!=source_name or run['status']!='committed':
        raise IMError('PINNED_DESKTOP_RUN_NOT_COMMITTED_OR_WRONG_SOURCE')
    folder=home/'desktop'/run_id
    if (folder.is_symlink() or folder.parent.is_symlink()
            or folder.resolve().parent!=(home/'desktop').resolve()):
        raise IMError('DESKTOP_CAPTURE_PATH_UNSAFE')
    meta_path=folder/'manifest.json';payload=folder/'source.json'
    if meta_path.is_symlink() or payload.is_symlink():raise IMError('DESKTOP_CAPTURE_PATH_UNSAFE')
    metadata_bytes=read_blob(meta_path);meta=load_json(metadata_bytes)
    if meta!=load_json(run['manifest_json'].encode('utf-8')):
        raise IMError('DESKTOP_CAPTURE_MANIFEST_MISMATCH')
    spec=meta.get('spec',{})
    if any(spec.get(k)!=binding.get(k) for k in BINDING):
        raise IMError('DESKTOP_CAPTURE_SCOPE_MISMATCH')
    if any(spec.get(k)!=entry[k] for k in ('platform','account_namespace','conversation_id','conversation_name')):
        raise IMError('DESKTOP_CAPTURE_DIRECTORY_IDENTITY_MISMATCH')
    expected_adapter={'qq':'tim-sequence-json','wecom':'wecom-json'}.get(spec.get('platform'))
    if expected_adapter is None or spec.get('adapter')!=expected_adapter or spec.get('conversation_type') is not None:
        raise IMError('DESKTOP_CAPTURE_ADAPTER_UNSUPPORTED')
    if iso_epoch(meta['observed_at']) < end:
        raise IMError('DESKTOP_CAPTURE_PREDATES_WINDOW_END')
    raw=read_blob(payload);sha=digest(raw)
    if sha!=meta.get('sha256'):raise IMError('DESKTOP_CAPTURE_HASH_MISMATCH')
    rows,coverage=normalize(raw,spec,start,end)
    if digest(read_blob(payload))!=sha or read_blob(meta_path)!=metadata_bytes:
        raise IMError('DESKTOP_CAPTURE_CHANGED_DURING_READ')
    sid=stream_key(spec)
    return {'path':payload,'spec':spec,'rows':rows,'coverage':coverage,
            'sha256':sha,'run_id':run_id,'stream_id':sid,'observed_at':meta['observed_at'],
            'since':start,'until':end,
            'batch_id':digest([sid,sha,start,end,spec['adapter_version']]),
            'records_sha256':digest([(r['message_key'],r['semantic_sha256']) for r in rows]),
            'body_gap_records':sum(bool(r['content_flags']) or r['text'] is None for r in rows)}


def verify_desktop_receipt(home: Path, folder: Path, inventory: dict,
                           entry: dict, receipt: dict) -> dict:
    """Verify a status receipt against pinned capture, committed batch and real rows."""
    from .activity import _inventory_fence
    inventory_hash=_inventory_fence(folder,inventory)
    if (type(receipt.get('records')) is not int or type(receipt.get('body_gap_records')) is not int
            or receipt.get('history_complete') is not False or receipt.get('complete_through') is not None
            or receipt.get('ui_used') is not False):
        raise IMError('DESKTOP_RECEIPT_TYPES_OR_COVERAGE_INVALID')
    if (receipt.get('receipt_kind')!='desktop-fixed-window/1' or receipt.get('status')!='committed'
            or receipt.get('inventory_sha256')!=inventory_hash
            or receipt.get('entry_key')!=entry['key'] or receipt.get('window')!=inventory['window']):
        raise IMError('DESKTOP_RECEIPT_INVENTORY_MISMATCH')
    binding=receipt.get('binding',{})
    account=inventory['accounts'][entry['account_key']]
    if any(binding.get(k)!=account[k] for k in ('platform','account_namespace','source_epoch','data_class')):
        raise IMError('DESKTOP_RECEIPT_ACCOUNT_MISMATCH')
    captured=load_capture(home,entry,receipt.get('source_name'),binding,inventory['window'])
    for key in ('stream_id','batch_id','records_sha256','body_gap_records'):
        if receipt.get(key)!=captured[key]:raise IMError('DESKTOP_RECEIPT_CAPTURE_MISMATCH')
    if (isinstance(receipt.get('records'),bool) or receipt.get('records')!=len(captured['rows'])
            or receipt.get('source_sha256')!=captured['sha256']):
        raise IMError('DESKTOP_RECEIPT_COUNT_OR_HASH_MISMATCH')
    con=readonly(home/'index.sqlite3')
    try:batch=con.execute('SELECT * FROM batches WHERE batch_id=?',(captured['batch_id'],)).fetchone()
    finally:con.close()
    if (not batch or batch['status']!='committed' or batch['stream_id']!=captured['stream_id']
            or batch['source_sha256']!=captured['sha256'] or batch['observed_at']!=receipt.get('source_observed_at')):
        raise IMError('DESKTOP_RECEIPT_COMMITTED_BATCH_REQUIRED')
    readback=validate_readback(home,captured['stream_id'],captured['rows'])
    return {'records':len(captured['rows']),'body_gap_records':captured['body_gap_records'],'readback':readback}


def backfill_desktop_week(home: Path, inventory_id: str, config: Path,
                          max_conversations=100, replay=False, backend=None,
                          expected_config_sha256=None, offset=0, upstream_fence=None) -> dict:
    """Import selected rows of explicitly pinned completed captures, with no UI.

    Group-only until native direct-chat identities/types are separately verified.
    Unbound or unsupported entries stay HOLD and are never counted as zero messages.
    """
    from .activity import _load, _atomic, _receipt, _save_report, week_status, _inventory_fence
    from .desktop_directory import plan_desktop_backfill
    if upstream_fence is not None and not callable(upstream_fence):
        raise IMError('INVALID_UPSTREAM_FENCE')

    def check_upstream():
        # Preserve the caller's original config fence across an immutable pin file.
        if upstream_fence is not None:upstream_fence()

    check_upstream()
    integer(max_conversations,'DESKTOP_BACKFILL_BOUND',1,500)
    integer(offset,'INVENTORY_PAGE_BOUND',0)
    folder,inventory=_load(home,inventory_id)
    inventory_hash=_inventory_fence(folder,inventory)
    raw=read_blob(config);config_hash=digest(raw)
    if expected_config_sha256 is not None and config_hash!=expected_config_sha256:
        raise IMError('DESKTOP_BINDING_CONFIG_CHANGED')
    config_data=load_json(raw)
    plan=plan_desktop_backfill(home,inventory_id,config,limit=max_conversations,offset=offset)
    if plan['source_config_sha256']!=config_hash:raise IMError('DESKTOP_BINDING_CONFIG_CHANGED')
    if plan['inventory_sha256']!=inventory_hash:raise IMError('WEEK_INVENTORY_CHANGED')
    _inventory_fence(folder,inventory,inventory_hash)
    output=[];new_total=0;checked=0;committed=0;held=0;failed=0
    with writer(home,lock_name='.activity.lock'):
        check_upstream()
        _inventory_fence(folder,inventory,inventory_hash)
        if digest(read_blob(config))!=config_hash:raise IMError('DESKTOP_BINDING_CONFIG_CHANGED')
        entries={e['key']:e for e in inventory['conversations']}
        for item in plan['items']:
            check_upstream()
            _inventory_fence(folder,inventory,inventory_hash)
            if digest(read_blob(config))!=config_hash:raise IMError('DESKTOP_BINDING_CONFIG_CHANGED')
            if not item['execution_supported']:
                held+=1;output.append(item);continue
            entry=entries[item['key']];source_name=item['bound_sources'][0]
            binding=config_data['sources'][source_name]
            try:
                old=_receipt(folder,entry['key'])
                if (old and old.get('status')=='committed' and not replay
                        and old.get('source_config_sha256')==config_hash):
                    verified=verify_desktop_receipt(home,folder,inventory,entry,old)
                    check_upstream()
                    checked+=verified['records'];committed+=1
                    output.append({'key':entry['key'],'status':'committed','records':verified['records'],'new_records':0,'reused_receipt':True})
                    continue
                captured=load_capture(home,entry,source_name,binding,inventory['window'])
                extra={'backend':backend} if backend is not None else {}
                imported=ingest(home,captured['path'],captured['spec'],captured['observed_at'],
                                since=captured['since'],until=captured['until'],
                                expected_sha256=captured['sha256'],**extra)
                if imported['records']!=len(captured['rows']) or imported['batch_id']!=captured['batch_id']:
                    raise IMError('DESKTOP_FIXED_WINDOW_IMPORT_MISMATCH')
                receipt={'receipt_kind':'desktop-fixed-window/1','status':'committed',
                         'entry_key':entry['key'],'inventory_sha256':plan['inventory_sha256'],
                         'source_name':source_name,'binding':{k:binding[k] for k in BINDING}|{
                             'directory_binding':binding['directory_binding'],'captured_run_id':binding['captured_run_id']},
                         'source_config_sha256':config_hash,'window':inventory['window'],
                         'source_sha256':captured['sha256'],'stream_id':imported['stream_id'],
                         'batch_id':imported['batch_id'],'records':imported['records'],
                         'records_sha256':captured['records_sha256'],'body_gap_records':captured['body_gap_records'],
                         'source_observed_at':imported['source_observed_at'],'updated_at':now(),
                         'new_records_last_run':imported['new_records'],'readback':imported['readback'],
                         'history_complete':False,'complete_through':None,'ui_used':False,'llm_calls':0}
                verify_desktop_receipt(home,folder,inventory,entry,receipt)
                if digest(read_blob(config))!=config_hash:raise IMError('DESKTOP_BINDING_CONFIG_CHANGED')
                check_upstream()
                _atomic(folder/'results'/(entry['key']+'.json'),receipt)
                committed+=1;checked+=imported['records'];new_total+=imported['new_records']
                output.append({'key':entry['key'],'status':'committed','records':imported['records'],'new_records':imported['new_records']})
            except (IMError,OSError,ValueError,KeyError,TypeError) as exc:
                # Never overwrite a prior receipt after the upstream request changed.
                check_upstream()
                code=exc.code if isinstance(exc,IMError) else 'DESKTOP_FIXED_WINDOW_IMPORT_FAILED'
                if code in ('WEEK_INVENTORY_CHANGED','WEEK_INVENTORY_HASH_MISMATCH'):
                    # Preserve any committed ingest batch, but never stamp a changed inventory.
                    raise
                _atomic(folder/'results'/(entry['key']+'.json'),{'receipt_kind':'desktop-fixed-window/1',
                    'status':'failed','entry_key':entry['key'],'error':code,'updated_at':now()})
                failed+=1;output.append({'key':entry['key'],'status':'failed','error':code})
        _inventory_fence(folder,inventory,inventory_hash)
        check_upstream()
        status=week_status(home,inventory_id,verify=True)
        _save_report(folder,inventory,status)
    desktop_platforms=[p for p in status['platforms'] if any('directory_enumeration_complete' in s for s in p['account_statuses'])]
    return {'inventory_id':inventory_id,'window':inventory['window'],'items':output,
            'source_config_sha256':config_hash,'records_checked':checked,'new_records':new_total,
            'committed_conversations':committed,'failed_conversations':failed,'held_conversations':held,
            'total_candidates':plan['total_items'],'next_offset':plan['next_offset'],
            'directory_enumeration_complete':bool(desktop_platforms) and all(p['directory_enumeration_complete'] for p in desktop_platforms),
            'history_complete':False,'complete_through':None,'four_platform_complete':False,
            'query_only':False,'ui_used':False,'llm_calls':0,'acquisition_performed':False,
            'scope':'fixed_window_rows_in_explicitly_pinned_completed_captures'}
