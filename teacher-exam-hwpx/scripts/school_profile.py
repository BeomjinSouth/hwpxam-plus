"""학교명 저장과 조회. Python 3.10+, 표준 라이브러리만 사용."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata


def data_home():
    return Path(os.environ.get('TEACHER_EXAM_HOME') or Path.home() / '.teacher-exam-hwpx')


def valid_name(value):
    return (isinstance(value, str) and 2 <= len(value) <= 30
            and not any(unicodedata.category(c).startswith('C') for c in value)
            and not re.search(r'[\\/:*?"<>|]', value)
            and re.search(r'[가-힣ㄱ-ㅎㅏ-ㅣA-Za-z]', value) is not None)


def read_profile(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or value.get('version') != 1
            or not all(valid_name(value.get(k)) for k in ('school_name', 'official_name'))
            or not isinstance(value.get('saved_at'), str) or not value['saved_at']):
        raise ValueError()
    return value


def save_profile(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def run(args):
    path = data_home() / 'school.json'
    broken = {'status': 'BROKEN', 'path': str(path), 'error':
              '학교 정보 파일을 읽을 수 없습니다. 파일을 고치거나 지운 뒤 다시 저장하세요.'}
    if args.mode == 'set':
        name = args.name.strip()
        official = args.official.strip() if args.official is not None else name
        if not all(valid_name(v) for v in (name, official)):
            return {'status': 'INVALID', 'error':
                    '학교명은 2~30자이며 한글이나 영문을 포함하고 \\ / : * ? " < > | 를 쓸 수 없습니다.'}, 2
    exists = path.exists()
    current = None
    if exists:
        try:
            current = read_profile(path)
        except (ValueError, OSError):
            if args.mode == 'show' or not args.replace:
                return broken, 1
    if args.mode == 'show':
        if not exists:
            return {'status': 'MISSING', 'path': str(path)}, 0
        return {'status': 'OK', **{k: current[k] for k in
                ('school_name', 'official_name', 'saved_at')}, 'path': str(path)}, 0
    if current and (current['school_name'], current['official_name']) == (name, official):
        return {'status': 'UNCHANGED', **current, 'path': str(path)}, 0
    if current and not args.replace:
        return {'status': 'EXISTS', 'current': current, 'error':
                '이미 다른 학교명이 저장되어 있습니다. 바꾸려면 --replace를 붙이세요.'}, 3
    value = {'version': 1, 'school_name': name, 'official_name': official,
             'saved_at': datetime.now().astimezone().isoformat()}
    save_profile(path, value)
    result = {'status': 'REPLACED' if exists else 'SAVED', **value, 'path': str(path)}
    if exists:
        result['previous'] = current
    return result, 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    sub.add_parser('show')
    setter = sub.add_parser('set')
    setter.add_argument('--name', required=True)
    setter.add_argument('--official')
    setter.add_argument('--replace', action='store_true')
    result, code = run(parser.parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
