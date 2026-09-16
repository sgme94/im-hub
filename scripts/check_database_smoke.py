"""Actual ChatLab integration using disposable synthetic databases only.
Run with the installed Python environment and IM_HUB_CHATLAB_DIR configured.
No actual client/database credentials, source discovery, GUI or model calls.
"""
from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from im_hub.common import canonical, initialize, stamp

T = 1704153600


def main():
    with tempfile.TemporaryDirectory(prefix='im-hub-synthetic-backend-') as tmp:
        root = Path(tmp)
        home = root / 'state'; initialize(home)
        account = root / 'synthetic-account'; account.mkdir()
        kim = account / 'user.db'
        c = sqlite3.connect(kim)
        c.executescript('CREATE TABLE "group"(id INTEGER PRIMARY KEY,name TEXT,sessionID INTEGER);'
                        'CREATE TABLE message(id INTEGER PRIMARY KEY,sender INTEGER,senderName TEXT,sendTime INTEGER,'
                        'contentType INTEGER,content TEXT,sessionID INTEGER,msgIdx INTEGER);')
        c.execute('INSERT INTO "group" VALUES(123,?,456)', ('Synthetic group',))
        body = canonical({'content': [{'type': 0, 'text': '[合成] 请验证同秒增量'}]})
        c.execute('INSERT INTO message VALUES(1,10,?, ?,4,?,456,1)', ('Fixture', T, body))
        c.commit(); c.close()
        group = 'synthetic@chatroom'
        table = 'Msg_' + hashlib.md5(group.encode()).hexdigest()
        shards = []
        for i in range(2):
            path = account / ('message_'+str(i)+'.db'); con = sqlite3.connect(path)
            con.execute('CREATE TABLE "'+table+'"(local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,'
                        'sort_seq INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,compress_content TEXT)')
            con.execute('INSERT INTO "'+table+'" VALUES(1,101,1,1,2,?,?,NULL)', (T, '[合成] 微信分片'))
            con.commit(); con.close(); shards.append({'id': 'shard-'+str(i), 'path': str(path)})
        base = {'enabled': True, 'adapter': 'database-json', 'account_namespace': 'synthetic-account',
                'conversation_name': 'Synthetic group', 'source_epoch': 'synthetic-v1', 'data_class': 'synthetic',
                'account_directory': str(account), 'initial_since': stamp(T-60), 'overlap_seconds': 60}
        config = root / 'sources.json'
        config.write_text(canonical({'version': 1, 'sources': {
            'kim-demo': {**base, 'platform': 'kim', 'transport': 'kim-sqlite', 'conversation_id': '123', 'database': str(kim), 'expected_session_id': 456},
            'wechat-demo': {**base, 'platform': 'wechat', 'transport': 'wechat-sqlite', 'conversation_id': group, 'shards': shards}}}), 'utf-8')

        def cli(args):
            process = subprocess.run([sys.executable, '-B', '-m', 'im_hub', '--home', str(home), *args],
                                     capture_output=True, timeout=150, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            try:
                result = json.loads(process.stdout.decode('utf-8'))
            except ValueError:
                raise AssertionError('CLI_JSON_INVALID') from None
            if process.returncode or not result.get('ok'):
                raise AssertionError('CLI_FAILED_' + str(result.get('error', {}).get('code')))
            return result['data']

        def collect(source, end):
            return cli(['collect', '--config', str(config), '--source', source, '--until', stamp(end)])

        first = collect('kim-demo', T+20)
        repeat = collect('kim-demo', T+20)
        c = sqlite3.connect(kim)
        c.execute('INSERT INTO message VALUES(2,10,?,?,4,?,456,2)', ('Fixture', T, body))
        c.commit(); c.close()
        increment = collect('kim-demo', T+21)
        last = collect('kim-demo', T+21)
        assert (first['new_records'], repeat['new_records'], increment['new_records'], last['new_records']) == (1, 0, 1, 0)
        wx = collect('wechat-demo', T+21)
        wx_repeat = collect('wechat-demo', T+21)
        assert wx['new_records'] == 2 and wx_repeat['new_records'] == 0
        assert cli(['query'])['count'] == 0
        query = cli(['query', '--include-synthetic', '--full'])
        assert query['count'] == 4 and len({m['message_key'] for m in query['items']}) == 4
        coverage = cli(['coverage', '--include-synthetic', '--max-age-seconds', '3600'])
        assert next(s for s in coverage['items'] if s['platform']=='wechat')['freshness'] == 'unknown'
        health = cli(['collection-status'])
        assert all(s['pending_runs'] == 0 and not s['last_error'] for s in health['items'])
        print(json.dumps({'ok': True, 'backend': 'actual_chatlab_0.37.1', 'data': 'synthetic_only',
                          'kim_initial_replay_increment_replay': [1, 0, 1, 0],
                          'wechat_two_shards_initial_replay': [2, 0], 'query_count': 4,
                          'same_second_same_text_preserved': True, 'checkpoints_committed': True,
                          'cache_freshness_unknown': True, 'llm_calls': 0, 'actual_client_access': False}))


if __name__ == '__main__':
    main()
