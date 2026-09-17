"""Account-level metadata discovery and fixed-window native reads, with no GUI.
Only explicit existing-account files and existing keys are read. No discovery of
credentials, process memory, other accounts, or arbitrary SQL. Client presentation
flags never filter conversations. Native unknown categories remain unknown.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager, ExitStack
from pathlib import Path
from .common import IMError, canonical, digest, iso_epoch, label, load_json, now, read_blob, readonly
from .database_readers import integer, local_path, snapshot, table_schema, source_body, kim_blocks
from .wechat_live import stable_bytes, decrypt_snapshot

CATALOG_LIMIT = 20000
MESSAGE_LIMIT = 200000
ACCOUNT_KINDS = {'wechat': 'wechat-account', 'kim': 'kim-account', 'qq': 'not-scanned', 'wecom': 'not-scanned'}
WX_REQUIRED = {'local_id','server_id','local_type','real_sender_id','create_time','message_content','compress_content'}
KIM_REQUIRED = {'id','sender','senderName','sendTime','contentType','content','sessionID','msgIdx','sessionType'}


def prepare_accounts(config: Path) -> tuple[dict, str]:
    config_blob = read_blob(config)
    data = load_json(config_blob)
    if not isinstance(data, dict) or data.get('schema') != 'im-hub-active-accounts/1':
        raise IMError('ACTIVE_ACCOUNT_CONFIG_SCHEMA_REQUIRED')
    values = data.get('accounts')
    if not isinstance(values, dict) or not 1 <= len(values) <= 16:
        raise IMError('ACTIVE_ACCOUNT_COUNT_BOUND')
    result = {}
    for key, original in values.items():
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', key) or not isinstance(original, dict):
            raise IMError('INVALID_ACTIVE_ACCOUNT')
        p = dict(original)
        if p.get('enabled') is not True or p.get('platform') not in ACCOUNT_KINDS:
            raise IMError('EXPLICIT_ENABLED_ACCOUNT_REQUIRED')
        platform = p['platform']
        label(p.get('account_namespace')); label(p.get('source_epoch'))
        if p.get('data_class') not in ('real', 'synthetic'):
            raise IMError('INVALID_DATA_CLASS')
        if p.get('transport') != ACCOUNT_KINDS[platform]:
            raise IMError('ACTIVE_ACCOUNT_TRANSPORT_UNSUPPORTED')
        if p.get('include_types') != ['group', 'direct']:
            raise IMError('ACTIVE_GROUP_AND_DIRECT_SCOPE_REQUIRED')
        p['timeout_seconds'] = integer(p.get('timeout_seconds',30), 'INVALID_QUERY_TIMEOUT', 1, 120)
        p['max_messages_per_conversation'] = integer(p.get('max_messages_per_conversation',20000), 'INVALID_RECORD_LIMIT', 1, MESSAGE_LIMIT)
        if platform in ('qq','wecom'):
            # An unscanned account is a visible coverage gap, never an empty success.
            p['reason'] = 'NATIVE_CONVERSATION_DIRECTORY_DRIVER_NOT_IMPLEMENTED_NO_GUI_RUN'
        else:
            account = local_path(config.parent, p.get('account_directory'))
            p['account_directory'] = str(account)
            if platform == 'kim':
                db = local_path(config.parent,p.get('database'))
                if db.parent != account or db.name != 'user.db':
                    raise IMError('ACCOUNT_DIRECTORY_BINDING_MISMATCH')
                p['database'] = str(db)
                p['self_user_id'] = integer(p.get('self_user_id'), 'KIM_ACCOUNT_SELF_ID_REQUIRED', 1)
            else:
                if p.get('key_policy') != 'existing-only':
                    raise IMError('EXISTING_KEY_ONLY_POLICY_REQUIRED')
                p['existing_key_file'] = str(local_path(config.parent,p.get('existing_key_file')))
                files = p.get('files')
                if not isinstance(files,list) or not 3 <= len(files) <= 18:
                    raise IMError('EXPLICIT_ACCOUNT_DATABASE_FILES_REQUIRED')
                clean=[]
                for f in files:
                    if not isinstance(f,dict) or f.get('role') not in ('session','contact','messages'):
                        raise IMError('INVALID_ACCOUNT_DATABASE_ROLE')
                    fid=label(f.get('id'))
                    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',fid):raise IMError('INVALID_SHARD_ID')
                    path=local_path(config.parent,f.get('path'))
                    if not path.is_relative_to(account) or path.suffix.lower()!='.db':
                        raise IMError('ACCOUNT_DIRECTORY_BINDING_MISMATCH')
                    kid=label(f.get('key_id')).replace('\\','/')
                    if kid.startswith('/') or '..' in kid.split('/') or ':' in kid:
                        raise IMError('INVALID_CACHED_KEY_ID')
                    clean.append({'id':fid,'path':str(path),'role':f['role'],'key_id':kid})
                if len({f['id'] for f in clean})!=len(clean) or len({f['path'] for f in clean})!=len(clean):
                    raise IMError('DUPLICATE_SHARD_BINDING')
                if any(sum(f['role']==role for f in clean)!=1 for role in ('session','contact')) or not any(f['role']=='messages' for f in clean):
                    raise IMError('ACCOUNT_DIRECTORY_DATABASES_REQUIRED')
                p['files']=clean
        result[key]=p
    if len({(p['platform'],p['account_namespace']) for p in result.values()})!=len(result):
        raise IMError('DUPLICATE_ACCOUNT_NAMESPACE')
    # Hash the bytes actually parsed, not a second potentially changed read.
    return result,digest(config_blob)


def bounded(con, sql, params=(), limit=CATALOG_LIMIT):
    rows=con.execute(sql,params).fetchmany(limit+1)
    if len(rows)>limit:raise IMError('ACCOUNT_METADATA_BOUND_EXCEEDED')
    return rows


def _rows(con, table, required, columns):
    table_schema(con,table,required)
    return bounded(con,'SELECT '+columns+' FROM "'+table+'"')


@contextmanager
def account_snapshot(p: dict, home: Path):
    """Open immutable derivatives / read transactions once per account, not per chat."""
    with ExitStack() as stack:
        sources=[]
        if p['platform']=='kim':
            c,identity,observed=stack.enter_context(snapshot(Path(p['database']),p['timeout_seconds']))
            sources.append({'id':'kim-main','role':'messages','con':c,'identity':identity,'observed_at':observed})
        elif p['platform']=='wechat':
            cache=Path(p['existing_key_file'])
            if cache.is_symlink() or not cache.is_file() or cache.stat().st_size>1024**2:
                raise IMError('EXISTING_KEY_CACHE_UNAVAILABLE_OR_TOO_LARGE')
            with cache.open('rb') as f:blob=f.read(1024**2+1)
            if len(blob)>1024**2:raise IMError('EXISTING_KEY_CACHE_UNAVAILABLE_OR_TOO_LARGE')
            keys=load_json(blob)
            if not isinstance(keys,dict) or len(keys)>4096:raise IMError('EXISTING_KEY_CACHE_INVALID')
            keys={str(k).replace('\\','/'):v for k,v in keys.items()}
            folder=Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='active-read-',dir=home/'runtime')))
            for f in p['files']:
                try:key=bytes.fromhex(keys[f['key_id']])
                except (KeyError,ValueError,TypeError):raise IMError('CONFIGURED_EXISTING_KEY_NOT_AVAILABLE') from None
                data,wal,observed,identity=stable_bytes(Path(f['path']))
                derived=folder/(f['id']+'.db')
                stats=decrypt_snapshot(data,wal,key,derived)
                c=readonly(derived);stack.callback(c.close)
                c.execute('BEGIN');c.execute('SELECT count(*) FROM sqlite_master').fetchone()
                sources.append({'id':f['id'],'role':f['role'],'con':c,'identity':list(identity),
                                'observed_at':observed,'wal':stats})
        else:
            raise IMError('NATIVE_CONVERSATION_DIRECTORY_DRIVER_NOT_IMPLEMENTED_NO_GUI_RUN')
        end=time.monotonic()+p['timeout_seconds']
        for s in sources:
            s['con'].set_progress_handler(lambda:int(time.monotonic()>end),1000)
        try:yield sources
        except sqlite3.Error:raise IMError('ACCOUNT_SQLITE_READ_FAILED_OR_TIMED_OUT') from None


def snapshot_identity(sources):
    return digest([{'id':s['id'],'identity':s['identity']} for s in sources])


def entry(p, native_id, name, kind, index, parts, latest=None, flags=None, basis='native_directory', extra=None):
    # Index entries are tuples [shard-id, local-id, event-seconds, native-type].
    index.sort(key=lambda r:(r[0],r[1],r[2],r[3]))
    return {'platform':p['platform'],'account_namespace':p['account_namespace'],
            'conversation_id':str(native_id),'conversation_name':name or str(native_id),
            'conversation_type':kind,'classification_basis':basis,
            'activity':'message_confirmed' if index else 'directory_candidate',
            'local_message_count':len(index),'index_signature':digest(index),
            'message_min_epoch':min((r[2] for r in index),default=None),
            'message_max_epoch':max((r[2] for r in index),default=None),
            'directory_last_epoch':latest,'source_parts':parts,
            'presentation_flags':flags or {},'folded_state':'not_independently_verified',
            'eligible':kind in ('group','direct') and bool(index),
            'coverage_gaps':['server_sync_not_verified']+([] if index else ['directory_active_but_no_local_messages']),
            'native_metadata':extra or {}}


def discover_kim(p, sources, since, until):
    c=sources[0]['con']
    sessions={r['id']:dict(r) for r in _rows(c,'session',{'id','type','typeID','typeName','lastMsgTime'},'id,type,typeID,typeName,lastMsgTime,pMsgOffFlag,mMsgOffFlag,creater')}
    users={r['id']:r['name'] for r in _rows(c,'user',{'id','name'},'id,name')}
    groups={}
    for r in _rows(c,'group',{'id','name','sessionID'},'id,name,sessionID'):
        if r['sessionID'] in groups:raise IMError('KIM_GROUP_SESSION_AMBIGUOUS')
        groups[r['sessionID']]=dict(r)
    table_schema(c,'message',KIM_REQUIRED)
    index={}
    for r in bounded(c,'SELECT id,sessionID,sessionType,sendTime,contentType,sender,receiver FROM message WHERE sendTime>=? AND sendTime<? ORDER BY sessionID,sendTime,id',(since,until),MESSAGE_LIMIT):
        sid=integer(r['sessionID'],minimum=1)
        index.setdefault(sid,[]).append(['kim-main',integer(r['id'],minimum=1),integer(r['sendTime'],minimum=946684800),integer(r['contentType'])])
        if sid in sessions and r['sessionType']!=sessions[sid]['type']:
            raise IMError('KIM_SESSION_TYPE_CONFLICT')
        if sid in sessions and sessions[sid]['type']==0:
            participants={sessions[sid]['creater'],sessions[sid]['typeID']}
            if {r['sender'],r['receiver']} != participants or p['self_user_id'] not in participants:
                raise IMError('KIM_DIRECT_PARTICIPANTS_NOT_BOUND_TO_ACCOUNT')
    targets=set(index)|{sid for sid,s in sessions.items() if isinstance(s['lastMsgTime'],int) and since<=s['lastMsgTime']<until}
    result=[]
    for sid in sorted(targets):
        s=sessions.get(sid,{});g=groups.get(sid)
        participants={s.get('creater'),s.get('typeID')}
        peers=participants-{p['self_user_id']}
        peer=next(iter(peers)) if p['self_user_id'] in participants and len(peers)==1 else None
        if g and s.get('type') in (1,2) and s.get('typeID')==g['id']:
            kind='group';native=str(g['id']);name=g['name'];basis='session_type_and_group_session_binding'
        elif s.get('type')==0 and peer in users and not g:
            kind='direct';native='dm-session:'+str(sid);name=users[peer];basis='session_participants_self_excluded_and_user_binding'
        else:
            kind='service' if s.get('type')==5 and not g else 'unknown'
            native='session:'+str(sid);name=s.get('typeName') or native;basis='native_type_separate_not_assumed_person'
        result.append(entry(p,native,name,kind,index.get(sid,[]),['kim-main'],s.get('lastMsgTime'),
                            {'pMsgOffFlag':s.get('pMsgOffFlag'),'mMsgOffFlag':s.get('mMsgOffFlag')},basis,
                            {'session_id':sid,'session_type':s.get('type'),'peer_id':peer if kind=='direct' else s.get('typeID'),
                             'creator_id':s.get('creater'),'self_user_id':p['self_user_id']}))
    return result,{'native_session_count':len(sessions),'message_table_enumeration_complete':True,
                  'metadata_activity_scan_complete':True,'folded_and_muted_not_filtered':True,
                  'scope':'configured_current_account_local_database','unscanned_databases':[]}


def discover_wechat(p,sources,since,until):
    cc=next(s['con'] for s in sources if s['role']=='contact');sc=next(s['con'] for s in sources if s['role']=='session')
    contacts={r['username']:dict(r) for r in _rows(cc,'contact',{'username','local_type','remark','nick_name'},'username,local_type,remark,nick_name,verify_flag,chat_room_notify')}
    rooms={r['username'] for r in _rows(cc,'chat_room',{'username'},'username')}
    business={r['username'] for r in _rows(cc,'biz_info',{'username'},'username')}
    sessions={r['username']:dict(r) for r in _rows(sc,'SessionTable',{'username','last_timestamp','is_hidden'},'username,last_timestamp,is_hidden')}
    fallback={r['username']:r['session_title'] for r in _rows(sc,'SessionNoContactInfoTable',{'username','session_title'},'username,session_title')}
    mapping={};invalid_names=0
    def add_name(n):
        nonlocal invalid_names
        if not isinstance(n,str) or not n.strip() or len(n)>512 or '\x00' in n:
            invalid_names+=1
            return  # Reserved/invalid directory rows do not hide unmapped active tables.
        key=hashlib.md5(n.encode('utf-8')).hexdigest()
        if key in mapping and mapping[key]!=n:raise IMError('NATIVE_TABLE_NAME_COLLISION')
        mapping[key]=n
    for n in set(contacts)|set(sessions)|rooms:add_name(n)
    message_sources=[s for s in sources if s['role']=='messages']
    for s in message_sources:
        for r in _rows(s['con'],'Name2Id',{'user_name'},'user_name'):add_name(r['user_name'])
    indexes={};parts={};table_count=0;unmapped=0
    for s in message_sources:
        tables=bounded(s['con'],"SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*' ORDER BY name")
        for row in tables:
            table=row['name']
            if not re.fullmatch(r'Msg_[0-9a-f]{32}',table):raise IMError('UNKNOWN_NATIVE_MESSAGE_TABLE_FORMAT')
            table_schema(s['con'],table,WX_REQUIRED);table_count+=1
            items=bounded(s['con'],'SELECT local_id,create_time,local_type FROM "'+table+'" WHERE create_time>=? AND create_time<? ORDER BY create_time,local_id',(since,until),MESSAGE_LIMIT)
            if not items:continue
            native=mapping.get(table[4:])
            if native is None:native='unresolved-table:'+table;unmapped+=1
            indexes.setdefault(native,[]).extend([[s['id'],integer(r['local_id'],minimum=1),integer(r['create_time'],minimum=946684800),integer(r['local_type'])] for r in items])
            parts.setdefault(native,[]).append(s['id'])
            if sum(map(len,indexes.values()))>MESSAGE_LIMIT:raise IMError('ACCOUNT_MESSAGE_INDEX_LIMIT')
    targets=set(indexes)|{n for n,s in sessions.items() if isinstance(s['last_timestamp'],int) and since<=s['last_timestamp']<until}
    result=[]
    for n in sorted(targets):
        c=contacts.get(n,{});s=sessions.get(n,{})
        if n in rooms or n.endswith('@chatroom'):kind='group';basis='native_chatroom_identity'
        elif n in business or n.startswith('gh_'):kind='service';basis='native_business_directory'
        elif c.get('local_type') in (1,5):kind='direct';basis='contact_native_type_after_business_exclusion'
        else:kind='unknown';basis='unresolved_native_conversation_class'
        name=c.get('remark') or c.get('nick_name') or fallback.get(n) or n
        result.append(entry(p,n,name,kind,indexes.get(n,[]),parts.get(n,[]),s.get('last_timestamp'),
                            {'is_hidden':s.get('is_hidden'),'chat_room_notify':c.get('chat_room_notify')},basis,
                            {'local_type':c.get('local_type'),'verify_flag':c.get('verify_flag')}))
    return result,{'native_session_count':len(sessions),'message_tables_scanned':table_count,
                  'unmapped_active_tables':unmapped,'invalid_native_name_entries':invalid_names,'metadata_activity_scan_complete':True,
                  'message_table_enumeration_complete':True,'folded_and_muted_not_filtered':True,
                  'scope':'explicit_main_message_shards_plus_native_session_contact_catalog',
                  'unscanned_databases':['business_chat_and_other_nonconfigured_message_stores']}


def discover_account(p,sources,since,until):
    items,summary=(discover_kim if p['platform']=='kim' else discover_wechat)(p,sources,since,until)
    if len(items)>CATALOG_LIMIT:raise IMError('ACTIVE_CONVERSATION_COUNT_BOUND')
    for e in items:
        e['key']=digest([e['platform'],e['account_namespace'],e['conversation_type'],e['conversation_id']])
        e['source_identity']=snapshot_identity(sources)
        e['source_observed_at']=min((s['observed_at'] for s in sources),key=iso_epoch)
        if e['local_message_count']>p['max_messages_per_conversation']:
            e['eligible']=False;e['coverage_gaps'].append('conversation_exceeds_configured_message_bound')
    return items,summary


def activity_body(value):
    """Decode only observed standalone Zstandard frames, with strict size bounds.
    The hash always refers to original native bytes. No XML execution or URL fetch.
    """
    if not isinstance(value,bytes) or not value.startswith(b'\x28\xb5\x2f\xfd'):
        return source_body(value)
    from .database_readers import MAX_BODY_BYTES
    if len(value)>MAX_BODY_BYTES:raise IMError('SOURCE_MESSAGE_TOO_LARGE_NO_CURSOR_ADVANCE')
    original=digest(value)
    try:import zstandard as zstd
    except ImportError:return None,original,['zstandard_dependency_missing']
    try:
        parameters=zstd.get_frame_parameters(value)
        if parameters.content_size not in (zstd.CONTENTSIZE_UNKNOWN,zstd.CONTENTSIZE_ERROR) and parameters.content_size>MAX_BODY_BYTES:
            raise IMError('DECOMPRESSED_MESSAGE_EXCEEDS_BOUND')
        decoded=zstd.ZstdDecompressor().decompress(value,max_output_size=MAX_BODY_BYTES,allow_extra_data=False)
        if len(decoded)>MAX_BODY_BYTES:raise IMError('DECOMPRESSED_MESSAGE_EXCEEDS_BOUND')
    except zstd.ZstdError:
        return None,original,['compressed_body_decode_failed']
    text,_,flags=source_body(decoded)
    return text,original,flags


def read_messages(p,sources,e,since,until):
    output=[];index=[];platform=p['platform']
    if platform=='kim':
        c=sources[0]['con'];sid=e['native_metadata']['session_id']
        rows=bounded(c,'SELECT id,sender,senderName,sendTime,contentType,content,sessionID,msgIdx FROM message WHERE sessionID=? AND sendTime>=? AND sendTime<? ORDER BY sendTime,id',(sid,since,until),p['max_messages_per_conversation'])
        for r in rows:
            text,body_hash,flags=source_body(r['content']);body=None;reply=None;quoted=None
            try:obj=json.loads(text) if text is not None else None
            except (ValueError,RecursionError):obj=None;flags.append('invalid_kim_body_json')
            native_type=integer(r['contentType'])
            if isinstance(obj,dict) and native_type in (4,13):
                body,extra_flags=kim_blocks(obj.get('replyContent') if native_type==13 else obj);flags+=extra_flags
                if native_type==13:
                    quoted,extra_flags=kim_blocks(obj.get('replyedContent'));flags+=['quote_'+f for f in extra_flags]
                    if isinstance(obj.get('replyedMsgId'),int):reply=str(obj['replyedMsgId'])
            else:flags.append('unsupported_kim_message_type_or_body')
            ix=['kim-main',r['id'],r['sendTime'],native_type];index.append(ix)
            output.append({'identity':'kim:'+str(sid)+':'+str(r['id']),'native_id':str(r['id']),
                           'timestamp':r['sendTime'],'type':{4:0,13:25,3:4}.get(native_type,99),
                           'text':body,'sender_id':str(r['sender']),'sender_name':r['senderName'] or '未核实发送者',
                           'sender_verified':False,'flags':sorted(set(flags)),'reply_to':reply,
                           'locator':{'session_id':sid,'message_id':r['id'],'msg_idx':r['msgIdx']},
                           'index_tuple':ix,'extras':{'source_type':native_type,'raw_body_sha256':body_hash,'quoted_text':quoted}})
    else:
        table='Msg_'+hashlib.md5(e['conversation_id'].encode()).hexdigest()
        for s in sources:
            if s['role']!='messages' or s['id'] not in e['source_parts']:continue
            c=s['con'];table_schema(c,table,WX_REQUIRED)
            senders={r['rowid']:r['user_name'] for r in bounded(c,'SELECT rowid,user_name FROM Name2Id')}
            rows=bounded(c,'SELECT local_id,server_id,local_type,real_sender_id,create_time,message_content,compress_content FROM "'+table+'" WHERE create_time>=? AND create_time<? ORDER BY create_time,local_id',(since,until),p['max_messages_per_conversation'])
            for r in rows:
                text,body_hash,flags=activity_body(r['message_content']);_,compressed_hash,_=source_body(r['compress_content'])
                native_type=integer(r['local_type']);kind={1:0,3:1,34:2,43:3,47:5,10000:80}.get(native_type & 0xffffffff,99)
                if kind!=0:flags.append('nontext_body_not_fully_decoded')
                sender=senders.get(r['real_sender_id']) or s['id']+':'+str(r['real_sender_id'])
                ix=[s['id'],r['local_id'],r['create_time'],native_type];index.append(ix)
                output.append({'identity':s['id']+':'+str(r['local_id']),'native_id':str(r['server_id']) if r['server_id'] else None,
                               'timestamp':r['create_time'],'type':kind,'text':text,'sender_id':sender,
                               'sender_name':sender,'sender_verified':False,'flags':sorted(set(flags)),
                               'locator':{'shard':s['id'],'local_id':r['local_id']},'index_tuple':ix,
                               'extras':{'source_type':native_type,'raw_body_sha256':body_hash,'compressed_body_sha256':compressed_hash,
                                         'body_encoding':'zstandard' if isinstance(r['message_content'],bytes) and r['message_content'].startswith(b'\x28\xb5\x2f\xfd') else 'native_utf8_or_unknown'}})
    if len(output)>p['max_messages_per_conversation']:raise IMError('CONVERSATION_MESSAGE_COUNT_BOUND')
    index.sort(key=lambda r:(r[0],r[1],r[2],r[3]))
    if len(output)!=e['local_message_count'] or digest(index)!=e['index_signature']:
        raise IMError('DISCOVERED_MESSAGE_INDEX_CHANGED_REDISCOVER')
    return output
