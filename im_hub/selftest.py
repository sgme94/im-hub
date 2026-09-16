"""Executable acceptance using seven synthetic messages and the real local backend.
No real accounts, client discovery, UI, credentials, network calls or schedules.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from .common import IMError, canonical, digest, initialize, stamp
from .collection import collect
from .operations import backup, export_all, restore, verify
from .query import query_messages
from .store import backend_info

T = 1704153600


def _varint(number):
    result = bytearray()
    while number > 127:
        result.append((number & 127) | 128); number >>= 7
    result.append(number); return bytes(result)


def _field(number, value):
    return _varint(number << 3) + _varint(value) if isinstance(value, int) else _varint(number << 3 | 2) + _varint(len(value)) + value


def native_fixture():
    element = _field(1, 0) + _field(2, _field(1, '[合成] 原生消息校验'.encode('utf-8')))
    data = b''.join(_field(n, v) for n, v in ((1, 1), (2, 2), (3, 1), (4, 4), (6, 66), (7, 2), (8, _field(1, element)), (12, T)))
    return b'22 serialization::archive 19 0 0 1 0 ' + str(len(data)).encode() + b' ' + data


def selftest():
    if not backend_info()['ready']:
        raise IMError('PINNED_CHATLAB_BACKEND_NOT_READY')
    with tempfile.TemporaryDirectory(prefix='im-hub-selftest-') as temp:
        root = Path(temp); home = root / 'state'; initialize(home)
        inputs = home / 'selftest-inputs'; inputs.mkdir()
        account = inputs / 'account'; account.mkdir()
        kim = account / 'user.db'
        with sqlite3.connect(kim) as c:
            c.executescript('CREATE TABLE "group"(id INTEGER PRIMARY KEY,name TEXT,sessionID INTEGER);'
                            'CREATE TABLE message(id INTEGER PRIMARY KEY,sender INTEGER,senderName TEXT,sendTime INTEGER,'
                            'contentType INTEGER,content TEXT,sessionID INTEGER,msgIdx INTEGER);')
            c.execute('INSERT INTO "group" VALUES(123,?,456)', ('Synthetic group',))
            body = canonical({'content': [{'type': 0, 'text': '[合成] 请验证同秒增量'}]})
            c.execute('INSERT INTO message VALUES(1,10,?,?,4,?,456,1)', ('Fixture', T, body))
        c.close()
        group = 'synthetic@chatroom'; table = 'Msg_' + hashlib.md5(group.encode()).hexdigest()
        shards = []
        for i in range(2):
            path = account / ('message_' + str(i) + '.db'); c = sqlite3.connect(path)
            try:
                c.execute('CREATE TABLE "' + table + '"(local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,'
                          'sort_seq INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,compress_content TEXT)')
                c.execute('INSERT INTO "' + table + '" VALUES(1,101,1,1,2,?,?,NULL)', (T, '[合成] 跨分片'))
                c.commit()
            finally: c.close()
            shards.append({'id': 'shard-' + str(i), 'path': str(path)})
        def write_input(name, blob):
            path = inputs / (name + '.data'); path.write_bytes(blob)
            meta = inputs / (name + '.json')
            meta.write_text(canonical({'source_id': name, 'observed_at': stamp(T + 86400), 'sha256': digest(blob)}), 'utf-8')
            return str(path), str(meta)
        def tim(count):
            return ('消息对象:测试群\n' + '2024-01-02 上午 08:00:00 Fixture(1)\n[合成] 收到\n' * count).encode('utf-8-sig')
        tim_path, tim_meta = write_input('tim', tim(1))
        wecom_path, wecom_meta = write_input('wecom', native_fixture())
        base = {'enabled': True, 'account_namespace': 'selftest', 'source_epoch': 'selftest-v1', 'data_class': 'synthetic'}
        db = {**base, 'adapter': 'database-json', 'account_directory': str(account), 'conversation_name': 'Synthetic group', 'initial_since': stamp(T - 60), 'overlap_seconds': 60}
        config = inputs / 'sources.json'
        config.write_text(canonical({'version': 1, 'sources': {
            'kim': {**db, 'platform': 'kim', 'transport': 'kim-sqlite', 'conversation_id': '123', 'database': str(kim), 'expected_session_id': 456},
            'wechat': {**db, 'platform': 'wechat', 'transport': 'wechat-sqlite', 'conversation_id': group, 'shards': shards},
            'tim': {**base, 'platform': 'qq', 'transport': 'tim-export', 'adapter': 'tim-sequence-json', 'conversation_id': 'tim-selftest', 'conversation_name': '测试群', 'input': tim_path, 'manifest': tim_meta, 'export_mode': 'full-history-append-only'},
            'wecom': {**base, 'platform': 'wecom', 'transport': 'file', 'adapter': 'wecom-native', 'binding': '66', 'conversation_id': 'wecom-selftest', 'conversation_name': 'Synthetic group', 'input': wecom_path, 'manifest': wecom_meta}
        }}), 'utf-8')
        stages = []
        a = collect(home, config, 'kim', until=stamp(T + 20)); b = collect(home, config, 'kim', until=stamp(T + 20))
        with sqlite3.connect(kim) as c:
            c.execute('INSERT INTO message VALUES(2,10,?,?,4,?,456,2)', ('Fixture', T, body))
        c.close()
        d = collect(home, config, 'kim', until=stamp(T + 21))
        if [a['new_records'], b['new_records'], d['new_records']] != [1, 0, 1]: raise IMError('SELFTEST_KIM_INCREMENT')
        stages.append({'platform': 'kim', 'initial_replay_increment': [1, 0, 1]})
        a = collect(home, config, 'wechat', until=stamp(T + 21)); b = collect(home, config, 'wechat', until=stamp(T + 21))
        if [a['new_records'], b['new_records']] != [2, 0]: raise IMError('SELFTEST_WECHAT_SHARDS')
        stages.append({'platform': 'wechat', 'initial_replay': [2, 0]})
        a = collect(home, config, 'tim'); b = collect(home, config, 'tim'); write_input('tim', tim(2)); d = collect(home, config, 'tim')
        if [a['new_records'], b['new_records'], d['new_records']] != [1, 0, 1]: raise IMError('SELFTEST_TIM_PREFIX')
        stages.append({'platform': 'qq', 'initial_replay_increment': [1, 0, 1]})
        a = collect(home, config, 'wecom'); b = collect(home, config, 'wecom')
        if [a['new_records'], b['new_records']] != [1, 0]: raise IMError('SELFTEST_WECOM_NATIVE')
        stages.append({'platform': 'wecom', 'initial_replay': [1, 0]})
        if query_messages(home)['count'] != 0: raise IMError('SELFTEST_SYNTHETIC_ISOLATION')
        if verify(home)['records_verified'] != 7: raise IMError('SELFTEST_COUNT')
        exported = export_all(home, {'include_synthetic': True}, max_records=10)
        if exported['records'] != 7: raise IMError('SELFTEST_EXPORT')
        archive = root / 'state.zip'; saved = backup(home, archive)
        target = root / 'restored'; recovered = restore(archive, target)
        if saved['records_verified'] != 7 or recovered['records_verified'] != 7: raise IMError('SELFTEST_RESTORE')
        result = {'passed': True, 'backend': 'actual_chatlab_0.37.1', 'records': 7, 'platforms': stages,
                  'binary_mode': bool(getattr(sys, 'frozen', False)), 'backup_restore_verified': True,
                  'complete_export_verified': True, 'real_clients_accessed': False, 'synthetic_only': True,
                  'llm_calls': 0, 'ui_used': False, 'network_used': False}
    return result
