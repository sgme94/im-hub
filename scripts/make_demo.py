"""Create a new synthetic-only example folder. Never read an IM client."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error('Use a new output directory; existing evidence is never overwritten.')
    output.mkdir(parents=True, mode=0o700)
    raw = ('消息对象:测试群\n2024-01-02 上午 08:00:00 Example(10001)\n'
           '[合成测试] 请跟进网关验证\n2024-01-02 上午 08:00:01 Example(10001)\n'
           '[合成测试] 已收到，结果待确认\n').encode('utf-8-sig')
    (output / 'tim.txt').write_bytes(raw)
    manifest = {'source_id': 'demo-tim', 'sha256': hashlib.sha256(raw).hexdigest(),
                'observed_at': '2024-01-02T12:00:00+08:00'}
    profile = {'version': 1, 'sources': {'demo-tim': {
        'enabled': True, 'transport': 'file', 'platform': 'qq', 'adapter': 'tim-txt',
        'account_namespace': 'demo-account', 'conversation_id': 'demo-group',
        'conversation_name': '测试群', 'source_epoch': 'demo-generation',
        'data_class': 'synthetic', 'input': 'tim.txt', 'manifest': 'tim.meta.json'}}}
    for name, obj in [('tim.meta.json', manifest), ('sources.json', profile)]:
        (output / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({'ok': True, 'synthetic_only': True, 'config': str(output / 'sources.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
