"""저장된 교과서 정리본 찾기. Python 3.10+, 표준 라이브러리만 사용."""
import argparse
import json
import os
from pathlib import Path
import sys


def data_home():
    return Path(os.environ.get('TEACHER_EXAM_HOME') or Path.home() / '.teacher-exam-hwpx')


def normalized(value):
    return ''.join(value.split()).casefold()


def find(args):
    home = data_home().resolve()
    path = home / 'textbooks/index.json'
    if not path.exists():
        return {'status': 'NONE', 'path': str(path), 'textbooks': []}, 0
    try:
        index = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(index, dict) or not isinstance(index.get('textbooks'), list):
            raise ValueError()
        found = []
        for book in index['textbooks']:
            if any(value is not None and normalized(book[key]) != normalized(value)
                   for key, value in (('subject', args.subject), ('grade', args.grade))):
                continue
            units = []
            for unit in book['units']:
                if args.keyword is not None and not any(
                        normalized(args.keyword) in normalized(value)
                        for value in (unit['title'], unit['no'], book['id'])):
                    continue
                folder = (home / book['folder'] / unit['folder']).resolve()
                paths = {k: folder / name for k, name in
                         (('card', '범위카드.md'), ('source_md', '원문.md'), ('source_json', '원문.json'))}
                units.append({**unit, **{k: str(v) for k, v in paths.items()},
                              'exists': all(p.is_file() for p in paths.values())})
            if units:
                found.append({**book, 'units': units})
        return {'status': 'FOUND' if found else 'NONE', 'textbooks': found}, 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'status': 'BROKEN', 'error': f'교과서 정리본 목록을 읽을 수 없습니다: {path}'}, 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['find'])
    for name in ('subject', 'grade', 'keyword'):
        parser.add_argument('--' + name)
    result, code = find(parser.parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
