"""Read only explicit encrypted files using an EXISTING operator-supplied raw-key cache.
Never discovers/extracts keys, initializes a client reader, reads process memory, or
changes a client file. A stable byte snapshot, page MACs and committed WAL prefix
are verified before a temporary plaintext database is queried. No version gate.
Formats: https://www.sqlite.org/fileformat.html and https://www.zetetic.net/sqlcipher/design/
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import struct
import tempfile
from pathlib import Path
from .common import IMError, canonical, digest, iso_epoch, now, read_blob

PAGE = 4096
RESERVE = 80
MAX_SOURCE = 256 * 1024**2


def checksum(data: bytes, endian: str, previous=(0, 0)):
    if len(data) % 8:
        raise IMError('WAL_CHECKSUM_INPUT_INVALID')
    a,b = previous
    for x,y in struct.iter_unpack(endian+'II', data):
        a=(a+x+b)&0xffffffff; b=(b+y+a)&0xffffffff
    return a,b


def wal_committed(wal: bytes) -> tuple[dict[int, bytes], dict]:
    if not wal:
        return {}, {'valid_frames':0,'committed_frames':0,'commit_pages':None,'tail_ignored':False}
    if len(wal)<32:
        raise IMError('WAL_HEADER_INCOMPLETE')
    magic,version,size=struct.unpack('>III',wal[:12])
    if magic not in (0x377f0682,0x377f0683) or version!=3007000 or size!=PAGE:
        raise IMError('WAL_FORMAT_NOT_SUPPORTED')
    endian='<' if magic==0x377f0682 else '>'
    ck=checksum(wal[:24],endian)
    if ck != struct.unpack('>II',wal[24:32]):
        raise IMError('WAL_HEADER_CHECKSUM_FAILED')
    staged=[]; committed=0; pages=None
    for offset in range(32,len(wal)-PAGE-23,PAGE+24):
        head=wal[offset:offset+24]; body=wal[offset+24:offset+24+PAGE]
        pg,n=struct.unpack('>II',head[:8])
        # SQLite stops at the first invalid/reused frame; later bytes are not a transaction.
        if head[8:16]!=wal[16:24] or pg<1 or pg>MAX_SOURCE//PAGE:
            break
        next_ck=checksum(body,endian,checksum(head[:8],endian,ck))
        if next_ck != struct.unpack('>II',head[16:24]):
            break
        ck=next_ck;staged.append((pg,body))
        if n:
            if n>MAX_SOURCE//PAGE:raise IMError('WAL_COMMIT_SIZE_LIMIT')
            pages=n;committed=len(staged)
    selected={pg:body for pg,body in staged[:committed] if pg<=pages} if pages else {}
    return selected,{'valid_frames':len(staged),'committed_frames':committed,'commit_pages':pages,
                     'tail_ignored':len(wal)!=32+committed*(PAGE+24)}


def stat_signature(path: Path):
    if not path.exists():return None
    if path.is_symlink() or not path.is_file():raise IMError('UNSAFE_ENCRYPTED_SOURCE')
    s=path.stat()
    if s.st_size>MAX_SOURCE:raise IMError('ENCRYPTED_SOURCE_SIZE_LIMIT')
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)


def stable_bytes(path: Path):
    wal=Path(str(path)+'-wal');journal=Path(str(path)+'-journal')
    before=(stat_signature(path),stat_signature(wal),stat_signature(journal))
    if before[0] is None:raise IMError('ENCRYPTED_SOURCE_MISSING')
    if before[2] and before[2][2]:raise IMError('ACTIVE_ROLLBACK_JOURNAL_RETRY')
    with path.open('rb') as stream:data=stream.read(MAX_SOURCE+1)
    if before[1]:
        with wal.open('rb') as stream:log=stream.read(MAX_SOURCE+1)
    else:log=b''
    if len(data)>MAX_SOURCE or len(log)>MAX_SOURCE:raise IMError('ENCRYPTED_SOURCE_SIZE_LIMIT')
    observed=now()
    if before!=(stat_signature(path),stat_signature(wal),stat_signature(journal)):
        raise IMError('SOURCE_CHANGED_DURING_SNAPSHOT_RETRY')
    if not data or len(data)%PAGE:raise IMError('ENCRYPTED_DATABASE_PAGE_LAYOUT_UNSUPPORTED')
    return data,log,observed,before[0][:2]


def mac_key(key: bytes, page1: bytes):
    if len(key) not in (32,48):raise IMError('EXISTING_KEY_FORMAT_UNSUPPORTED')
    salt=key[32:] if len(key)==48 else page1[:16]
    return hashlib.pbkdf2_hmac('sha512',key[:32],bytes(x^0x3a for x in salt),2,32)


def decrypt_page(page: bytes, number: int, key: bytes, authentication_key: bytes):
    if len(page)!=PAGE:raise IMError('ENCRYPTED_PAGE_SIZE_INVALID')
    begin=16 if number==1 else 0
    authenticated=page[begin:PAGE-64]+struct.pack('<I',number)
    expected=hmac.new(authentication_key,authenticated,hashlib.sha512).digest()
    if not hmac.compare_digest(expected,page[-64:]):
        raise IMError('SOURCE_PAGE_AUTHENTICATION_FAILED')
    try:from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:raise IMError('INSTALL_CRYPTO_EXTRA_REQUIRED') from None
    cipher=Cipher(algorithms.AES(key[:32]),modes.CBC(page[PAGE-RESERVE:PAGE-64])).decryptor()
    body=cipher.update(page[begin:PAGE-RESERVE])+cipher.finalize()
    prefix=(page[:16] if len(key)==48 else b'SQLite format 3\x00') if number==1 else b''
    return prefix+body+b'\x00'*RESERVE


def decrypt_snapshot(data: bytes, wal: bytes, key: bytes, destination: Path):
    changes,summary=wal_committed(wal)
    auth=mac_key(key,data[:PAGE]); count=summary['commit_pages'] or len(data)//PAGE
    # Authenticate the original header too, even if WAL replaces page 1.
    decrypt_page(data[:PAGE],1,key,auth)
    with destination.open('xb') as out:
        for number in range(1,count+1):
            raw=changes.get(number,data[(number-1)*PAGE:number*PAGE])
            plain=decrypt_page(raw,number,key,auth)
            if number==1:
                if plain[:16]!=b'SQLite format 3\x00' or plain[20]!=RESERVE:
                    raise IMError('DECRYPTED_SQLITE_HEADER_UNSUPPORTED')
                # This derived immutable DB contains the committed WAL state. No source sidecars.
                plain=plain[:18]+b'\x01\x01'+plain[20:28]+struct.pack('>I',count)+plain[32:]
            out.write(plain)
        out.flush();os.fsync(out.fileno())
    return summary


def read_live(p: dict, since: float, until: float, private_home: Path):
    from .database_readers import read_wechat
    # Only the explicitly configured existing cache is consumed, never a scan or extractor.
    cache=Path(p['existing_key_file'])
    if not cache.is_file() or cache.is_symlink() or cache.stat().st_size>1024**2:
        raise IMError('EXISTING_KEY_CACHE_UNAVAILABLE_OR_TOO_LARGE')
    with cache.open('rb') as source:key_blob=source.read(1024**2+1)
    if len(key_blob)>1024**2:raise IMError('EXISTING_KEY_CACHE_UNAVAILABLE_OR_TOO_LARGE')
    try:keys=json.loads(key_blob.decode('utf-8-sig'))
    except (ValueError,UnicodeError):raise IMError('EXISTING_KEY_CACHE_INVALID') from None
    if not isinstance(keys,dict) or len(keys)>4096:raise IMError('EXISTING_KEY_CACHE_INVALID')
    entries={str(k).replace('\\','/'):v for k,v in keys.items()}
    rows=[];descriptors=[];observations=[];wal_stats=[]
    scratch=private_home/'runtime';scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='wechat-read-',dir=scratch) as temporary:
        target=Path(temporary)
        for shard in p['shards']:
            try:key=bytes.fromhex(entries[shard['key_id'].replace('\\','/')])
            except (KeyError,ValueError,TypeError):raise IMError('CONFIGURED_EXISTING_KEY_NOT_AVAILABLE') from None
            source=Path(shard['path']);data,wal,observed,identity=stable_bytes(source)
            derived=target/(shard['id']+'.db')
            stats=decrypt_snapshot(data,wal,key,derived)
            local={**p,'shards':[{'id':shard['id'],'path':str(derived)}]}
            subset,desc,_=read_wechat(local,since,until)
            if len(rows)+len(subset)>p['max_records']:raise IMError('SOURCE_WINDOW_TOO_LARGE_NO_CURSOR_ADVANCE')
            rows.extend(subset);observations.append(observed);wal_stats.append(stats)
            descriptors.append({'shard':shard['id'],'identity':list(identity),'source':str(source),
                                'schema_sha256':desc[0]['schema_sha256']})
    return {'schema':'im-hub-database/1','binding':{k:p[k] for k in
             ('platform','account_namespace','conversation_id','conversation_name','source_epoch','data_class')},
            'records':rows,'acquisition':{'transport':'wechat-live','source_kind':'authenticated_local_database',
                'observed_at':min(observations,key=iso_epoch),'source_fingerprint':digest(descriptors),
                'database_count':len(descriptors),'window_since':since,'window_until_exclusive':until,
                'local_window_read_complete':True,'page_authentication_verified':True,'wal':wal_stats,
                'snapshot_consistency':'stable_bytes_per_database_committed_WAL','atomic_across_databases':False,
                'existing_key_cache_used':True,'new_key_extraction':False,'process_memory_read':False,
                'client_sync_verified':False,'client_refreshed':False,'upstream_observed_at':None,'llm_calls':0}}
