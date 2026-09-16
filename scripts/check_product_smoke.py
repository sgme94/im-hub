"""Repeatable complete product smoke using actual ChatLab and synthetic data only.
No source client, clipboard, UI, model API, network or schedule is used.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def canonical(obj): return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--python',type=Path,default=Path(sys.executable))
    a=p.parse_args();root=a.output.resolve()
    if root.exists():p.error('Fresh output directory required; do not overwrite previous acceptance')
    root.mkdir(parents=True)
    home=root/'state';env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'}
    def cli(*args):
        proc=subprocess.run([str(a.python),'-X','utf8','-B','-m','im_hub','--home',str(home),*args],
                            capture_output=True,env=env,timeout=150)
        result=json.loads(proc.stdout.decode('utf-8'))
        if proc.returncode or result.get('ok') is not True:
            raise AssertionError({'command':args[0],'exit':proc.returncode,'error':result.get('error')})
        return result['data']
    cli('init')
    profile={'enabled':True,'transport':'tim-export','platform':'qq','account_namespace':'fixture-account',
             'conversation_id':'fixture-group','conversation_name':'合成测试群','source_epoch':'fixture-v1',
             'data_class':'synthetic','export_mode':'full-history-append-only','input':'source.txt','manifest':'source.meta.json'}
    import hashlib
    def source(n):
        raw=('消息对象:合成测试群\n'+''.join('2024-01-02 上午 08:00:00 Fixture(1)\n[合成测试] 同秒重复消息\n' for _ in range(n))).encode('utf-8-sig')
        (root/'source.txt').write_bytes(raw)
        (root/'source.meta.json').write_text(canonical({'source_id':'tim','sha256':hashlib.sha256(raw).hexdigest(),'observed_at':'2024-01-03T12:00:00+08:00'}),'utf-8')
    config=root/'sources.json';config.write_text(canonical({'version':1,'sources':{'tim':profile}}),'utf-8')
    cli('config-check','--config',str(config));source(2)
    first=cli('collect','--config',str(config),'--source','tim')
    second=cli('collect','--config',str(config),'--source','tim')
    source(3);third=cli('collect','--config',str(config),'--source','tim')
    fourth=cli('collect','--config',str(config),'--source','tim')
    assert [x['new_records'] for x in (first,second,third,fourth)]==[2,0,1,0]
    assert cli('query')['count']==0
    messages=cli('query','--include-synthetic','--full')
    assert messages['count']==3 and len({x['message_key'] for x in messages['items']})==3
    exported=cli('export-all','--include-synthetic')
    assert exported['records']==3 and exported['query_complete'] is True
    original=cli('verify');assert original['records_verified']==3
    backed=cli('backup','--output',str(root/'backup.zip'))
    restored=cli('restore','--input',str(root/'backup.zip'),'--destination',str(root/'restored'))
    assert backed['records_verified']==3 and restored['records_verified']==3
    before={x['message_key'] for x in messages['items']}
    proc=subprocess.run([str(a.python),'-m','im_hub','--home',str(root/'restored'),'query','--include-synthetic','--full'],
                         capture_output=True,env=env,timeout=30)
    after=json.loads(proc.stdout.decode('utf-8'));assert proc.returncode==0 and before=={x['message_key'] for x in after['data']['items']}
    run=cli('run','--config',str(config));assert run['success'] and run['results'][0]['new_records']==0
    report={'ok':True,'actual_backend':'chatlab-cli0.37.1','data':'synthetic_only',
            'tim_full_export_initial_replay_append_replay':[2,0,1,0],
            'same_second_same_text_distinct':True,'complete_export_records':3,
            'restored_records':3,'restored_message_keys_exact':True,'batch_run_replay_new':0,
            'core_llm_calls':0,'ui_performed':False,'production_desktop_accepted':False}
    (root/'RESULT.json').write_text(canonical(report),'utf-8');print(canonical(report))


if __name__=='__main__':main()
