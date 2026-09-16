"""Explicit bounded serial collection with owned children, stop and evidence logs.
No service/scheduler registration. An external operator may run further segments.
Windows child trees use a kill-on-close Job; the child waits for ownership first.
"""
from __future__ import annotations
import io
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from .common import IMError, canonical, check_home, digest, iso_epoch, now, read_blob, readonly, stamp, writer
from .operations import config_check

GATE = 'im-hub-owned-collection-child-v1\n'
MAX_LOG_BYTES = 2 * 1024 * 1024
TRANSIENT_ERRORS = {'WRITER_BUSY','DESKTOP_BUSY','SOURCE_CHANGED_DURING_SNAPSHOT_RETRY',
                    'ACTIVE_ROLLBACK_JOURNAL_RETRY','SOURCE_SQLITE_QUERY_FAILED_OR_TIMED_OUT'}


def parent_gate():
    """Invoked before CLI effects only in an explicitly marked supervisor child."""
    if os.environ.get('IM_HUB_PARENT_GATE') != '1':return
    if sys.stdin.readline(64) != GATE:
        raise IMError('SUPERVISOR_OWNERSHIP_NOT_CONFIRMED')
    os.environ.pop('IM_HUB_PARENT_GATE',None)


def _atomic(path: Path, value):
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with tmp.open('x',encoding='utf-8') as f:
        f.write(canonical(value));f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)


def _revision(home):
    con=readonly(home/'index.sqlite3')
    try:return con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]
    finally:con.close()


def new_event_refs(home, after_revision, baseline, platform):
    """Metadata only. Event-to-observation lag is NOT measured network latency."""
    con=readonly(home/'index.sqlite3')
    try:
        rows=con.execute('SELECT message_key,event_ms,metadata_json FROM records WHERE revision>? ORDER BY revision,event_ms,message_key LIMIT 20001',(after_revision,)).fetchall()
    finally:con.close()
    if len(rows)>20000:raise IMError('SOAK_NEW_RECORD_EVIDENCE_BOUND')
    found=[]
    for row in rows:
        metadata=json.loads(row['metadata_json'])
        if metadata.get('platform')!=platform or metadata.get('data_class')!='real':continue
        if row['event_ms']<=baseline*1000:continue
        found.append({'evidence_ref':'immsg:'+row['message_key'],'event_time':metadata['event_time'],
                      'observed_event_lag_seconds':round(max(0,time.time()-row['event_ms']/1000),3),
                      'lag_basis':'message_timestamp_to_query_after_collection_not_network_latency'})
    return {'count':len(found),'references':found[:20],'references_truncated':len(found)>20}


class ChildTree:
    def __init__(self):
        self.job=None
        if os.name=='nt':
            try:
                import win32job
                self.api=win32job;self.job=win32job.CreateJobObject(None,'Local\\im-hub-child-'+uuid.uuid4().hex)
                info=win32job.QueryInformationJobObject(self.job,win32job.JobObjectExtendedLimitInformation)
                info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(self.job,win32job.JobObjectExtendedLimitInformation,info)
            except Exception:
                if self.job is not None:self.job.Close()
                raise IMError('OWNED_CHILD_JOB_UNAVAILABLE') from None
    def bind(self, process):
        if self.job is not None:
            try:self.api.AssignProcessToJobObject(self.job,process._handle)
            except Exception:raise IMError('OWNED_CHILD_JOB_ASSIGNMENT_FAILED') from None
    def close(self, process):
        if self.job is not None:
            self.job.Close();self.job=None
        elif process is not None and os.name!='nt':
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        if process is not None:
            if process.poll() is None:process.kill()
            process.wait(timeout=10)


def invoke_source(home,config,source,ui,activate,deadline,stop_file,folder,timeout=150):
    env={k:v for k,v in os.environ.items() if k.upper() in
         {'PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','USERPROFILE','HOME','APPDATA','LOCALAPPDATA',
          'TEMP','TMP','USERDOMAIN','USERNAME','NUMBER_OF_PROCESSORS','PROCESSOR_ARCHITECTURE',
          'IM_HUB_CHATLAB_DIR','IM_HUB_NODE','PYTHONPATH'}}
    env.update({'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1','IM_HUB_PARENT_GATE':'1',
                'IM_HUB_DESKTOP_UNTIL':stamp(deadline),'IM_HUB_STOP_FILE':str(stop_file),
                'IM_HUB_ACTIVATE_CLIENTS':'1' if activate else '0'})
    env['IM_HUB_EXPECT_CONFIG_SHA256']=digest(read_blob(config))
    exe=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,'-X','utf8','-B','-m','im_hub']
    args=exe+['--home',str(home),'collect','--config',str(config),'--source',source]
    if ui:args.append('--allow-ui')
    token=uuid.uuid4().hex;out_path=folder/(token+'.stdout.json');err_path=folder/(token+'.stderr.log')
    process=None;tree=ChildTree();started=time.monotonic()
    try:
        with out_path.open('xb') as out,err_path.open('xb') as err:
            process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=out,stderr=err,env=env,
                                     start_new_session=os.name!='nt',creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            tree.bind(process)
            process.stdin.write(GATE.encode());process.stdin.flush();process.stdin.close()
            while process.poll() is None:
                if time.time()>=deadline:raise IMError('DESKTOP_AUTHORIZATION_EXPIRED')
                if stop_file.exists():raise IMError('DESKTOP_STOP_REQUESTED')
                if time.monotonic()-started>timeout:raise IMError('SOURCE_CHILD_TIMEOUT')
                if out_path.stat().st_size+err_path.stat().st_size>MAX_LOG_BYTES:raise IMError('SOURCE_CHILD_OUTPUT_BOUND')
                time.sleep(.1)
        if out_path.stat().st_size>MAX_LOG_BYTES:raise IMError('SOURCE_CHILD_OUTPUT_BOUND')
        try:result=json.loads(out_path.read_text('utf-8'))
        except (ValueError,UnicodeError):raise IMError('SOURCE_CHILD_INVALID_JSON') from None
        if not isinstance(result,dict) or not isinstance(result.get('error',{}),dict):
            raise IMError('SOURCE_CHILD_INVALID_JSON')
        if process.returncode or not result.get('ok'):
            code=result.get('error',{}).get('code','SOURCE_CHILD_FAILED')
            if not isinstance(code,str) or len(code)>150:code='SOURCE_CHILD_FAILED'
            raise IMError(code)
        if not isinstance(result.get('data'),dict):raise IMError('SOURCE_CHILD_INVALID_JSON')
        return result['data']
    finally:
        tree.close(process)
        # Child stdout can contain local path/account labels, so never copy it into
        # the public soak log. A failed local trace remains private for diagnosis.
        if process is not None and process.returncode==0:
            out_path.unlink(missing_ok=True);err_path.unlink(missing_ok=True)


def run_soak(home:Path,config:Path,until:str,interval=60,duration=3300,allow_ui=False,activate=False,
             max_cycles=None,invoke=None,clock=time.time,sleep=time.sleep):
    check_home(home);config=config.resolve();deadline=iso_epoch(until);started=clock()
    if not started<deadline<=started+24*3600:raise IMError('SOAK_FUTURE_DEADLINE_WITHIN_24H_REQUIRED')
    if isinstance(duration,bool) or not isinstance(duration,int) or not 1<=duration<=86400:raise IMError('SOAK_DURATION_MUST_BE_1_TO_86400')
    if isinstance(interval,bool) or not isinstance(interval,int) or not 15<=interval<=3600:raise IMError('SOAK_INTERVAL_MUST_BE_15_TO_3600')
    if max_cycles is not None and (isinstance(max_cycles,bool) or not isinstance(max_cycles,int) or not 1<=max_cycles<=1000):raise IMError('SOAK_CYCLE_BOUND')
    if activate and not allow_ui:raise IMError('UI_CONSENT_REQUIRED')
    sources=[s for s in config_check(config)['sources'] if s['enabled']]
    if not sources:raise IMError('SOAK_NO_ENABLED_SOURCES')
    if any(s['manual_copy_required'] for s in sources):raise IMError('MANUAL_CLIPBOARD_NOT_AUTOMATIC_SOURCE')
    if any(s['ui_required'] for s in sources) and not allow_ui:raise IMError('UI_CONSENT_REQUIRED')
    finish=min(deadline,started+duration);fn=invoke or invoke_source;config_hash=digest(read_blob(config))
    directory=home/'runtime/soak';directory.mkdir(exist_ok=True)
    stop=directory/'STOP';sid=uuid.uuid4().hex;folder=directory/sid
    status={'schema':'im-hub-soak/1','segment_id':sid,'pid':os.getpid(),'started_at':stamp(started),
            'authorized_until':until,'segment_until':stamp(finish),'status':'running','cycles_completed':0,
            'source_config_sha256':config_hash,'paused_sources':{},'retry_attempts':{},'latest_results':[],
            'new_event_candidates':{s['platform']:0 for s in sources},
            'source_version_required':False,'llm_calls':0,'scheduler_installed':False,
            'full_final_acceptance':False,'new_events_are_not_guaranteed':True}
    with writer(home,lock_name='.soak.lock'):
        if stop.exists():raise IMError('DESKTOP_STOP_REQUESTED')
        baseline=started;carried={s['platform']:0 for s in sources}
        prior_path=directory/'status.json'
        if prior_path.exists():
            prior=json.loads(read_blob(prior_path))
            if isinstance(prior,dict) and prior.get('authorized_until')==until and prior.get('source_config_sha256')==config_hash:
                baseline=iso_epoch(prior.get('observation_baseline',prior['started_at']))
                carried={k:int(prior.get('cumulative_new_event_candidates',{}).get(k,0)) for k in carried}
        status.update(observation_baseline=stamp(baseline),cumulative_new_event_candidates=dict(carried))
        folder.mkdir()
        _atomic(directory/'status.json',status)
        with (folder/'cycles.jsonl').open('x',encoding='utf-8') as log:
            results=[]
            try:
                while clock()<finish and (max_cycles is None or status['cycles_completed']<max_cycles):
                    cycle_started=clock();results=[]
                    if digest(read_blob(config))!=config_hash:raise IMError('SOAK_CONFIG_CHANGED_RESTART_REQUIRED')
                    for src in sources:
                        if clock()>=finish:break
                        if digest(read_blob(config))!=config_hash:raise IMError('SOAK_CONFIG_CHANGED_RESTART_REQUIRED')
                        if stop.exists():raise IMError('DESKTOP_STOP_REQUESTED')
                        name=src['source'];item={'source':name,'platform':src['platform'],'started_at':stamp(clock())}
                        if name in status['paused_sources']:
                            results.append({**item,'status':'paused','error':status['paused_sources'][name]});continue
                        rev=_revision(home);begin=clock()
                        status.update(current_source=name,updated_at=stamp(clock()),latest_results=results)
                        _atomic(directory/'status.json',status)
                        try:
                            data=fn(home,config,name,allow_ui,activate,finish,stop,folder,timeout=min(150,max(1,finish-clock())))
                            if not isinstance(data,dict):raise IMError('SOURCE_CHILD_INVALID_JSON')
                            refs=new_event_refs(home,rev,baseline,src['platform'])
                            acquisition=data.get('acquisition') or {}
                            status['retry_attempts'].pop(name,None)
                            item.update(status='ok',records=data.get('records'),new_records=data.get('new_records'),duplicates=data.get('duplicates'),
                                        run_id=data.get('run_id'),duration_seconds=round(clock()-begin,3),
                                        captured_at=acquisition.get('captured_at',data.get('source_observed_at')),
                                        ui_performed=data.get('ui_performed',False),
                                        client_activated=acquisition.get('client_activated_under_explicit_deadline',False),
                                        new_event_candidates=refs)
                            status['new_event_candidates'][src['platform']]+=refs['count']
                            status['cumulative_new_event_candidates'][src['platform']]=carried[src['platform']]+status['new_event_candidates'][src['platform']]
                        except IMError as exc:
                            item.update(status='failed',error=exc.code,duration_seconds=round(clock()-begin,3))
                            if any(x in exc.code for x in ('USER_INPUT','HELD_INPUT','FOREGROUND_PAUSED','AUTHORIZATION_EXPIRED','STOP_REQUESTED','ACTIVATION_DENIED')):
                                results.append(item);raise
                            attempts=status['retry_attempts'].get(name,0)+1
                            if exc.code in TRANSIENT_ERRORS and attempts<=3:
                                status['retry_attempts'][name]=attempts
                                item.update(status='retryable',retry_attempt=attempts)
                            else:
                                status['paused_sources'][name]=exc.code
                        results.append(item)
                    status['cycles_completed']+=1;status['latest_results']=results;status['updated_at']=stamp(clock())
                    status['current_source']=None
                    status['last_cycle_complete']=len(results)==len(sources)
                    status['last_cycle_all_succeeded']=status['last_cycle_complete'] and all(r['status']=='ok' for r in results)
                    log.write(canonical({'cycle':status['cycles_completed'],'at':status['updated_at'],'results':results})+'\n');log.flush();os.fsync(log.fileno())
                    _atomic(directory/'status.json',status)
                    if len(status['paused_sources'])==len(sources):status['status']='all_sources_paused';break
                    if max_cycles is not None and status['cycles_completed']>=max_cycles:break
                    wait_until=min(finish,max(clock(),cycle_started+interval))
                    while clock()<wait_until:
                        if stop.exists():raise IMError('DESKTOP_STOP_REQUESTED')
                        sleep(min(.5,wait_until-clock()))
                if status['status']=='running':
                    status['status']='segment_complete' if status.get('last_cycle_complete') else 'deadline_reached'
            except (IMError,KeyboardInterrupt) as exc:
                status['status']='stopped';status['stop_reason']=exc.code if isinstance(exc,IMError) else 'INTERRUPTED'
                status['latest_results']=results
            except Exception as exc:
                status['status']='failed';status['stop_reason']='SUPERVISION_ERROR'
                status['error_type']=type(exc).__name__;status['latest_results']=results
            finally:
                status['current_source']=None
                status['completed_at']=stamp(clock());status['wall_seconds']=round(clock()-started,3)
                _atomic(directory/'status.json',status);_atomic(folder/'summary.json',status)
    return status


def soak_status(home):
    check_home(home);path=home/'runtime/soak/status.json'
    if not path.exists():return {'status':'not_started','query_only':True}
    result=json.loads(read_blob(path));result['query_only']=True
    result['process_liveness_verified']=False
    return result
