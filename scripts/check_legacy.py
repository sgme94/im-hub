"""Read an existing authorized store and print counts only; never copy messages."""
import argparse
import hashlib
import json
from pathlib import Path
from im_hub.query import query_messages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    args = parser.parse_args()
    home = args.home.resolve()
    files = [home / 'index.sqlite3', *sorted((home / 'chatlab/data/databases').glob('*.db'))]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    counts, keys, cursor, pages = {}, set(), None, 0
    while True:
        result = query_messages(home, limit=200, cursor=cursor)
        pages += 1
        for item in result['items']:
            key = item['message_key']
            if key in keys:
                raise RuntimeError('DUPLICATE_QUERY_ID')
            keys.add(key)
            platform = item['platform']
            counts[platform] = counts.get(platform, 0) + 1
        if not result['has_more']:
            break
        if pages >= 1000:
            raise RuntimeError('LEGACY_CHECK_PAGE_BOUND')
        cursor = result['next_cursor']
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != h for p, h in before.items()):
        raise RuntimeError('SOURCE_CHANGED_DURING_READ_CHECK')
    print(json.dumps({'ok': True, 'records': len(keys), 'by_platform': counts,
                      'unique': True, 'files_unchanged': True, 'copied_messages': False,
                      'llm_calls': 0, 'client_access_performed': False}))


if __name__ == '__main__':
    main()
