"""hwpxam-plus 회귀 검사. 테스트 데이터는 지정된 임시 폴더에만 생성한다."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'teacher-exam-hwpx/scripts'
# 임시 폴더는 HWPXAM_TEST_ROOT가 있으면 그 아래, 없으면 시스템 임시 폴더 아래에 만든다.
TEMP = Path(os.environ.get('HWPXAM_TEST_ROOT') or tempfile.gettempdir()) / 'hwpxam-plus-test'
# 1차판 exam_check.py 사본과 기존 원고(파일·폴더, os.pathsep로 구분)를 주면 check 결과가 같은지 비교한다.
BASELINE = Path(os.environ['HWPXAM_BASELINE_EXAM_CHECK']) if os.environ.get('HWPXAM_BASELINE_EXAM_CHECK') else None
REFERENCES = [Path(p) for p in os.environ.get('HWPXAM_REGRESSION_MANUSCRIPTS', '').split(os.pathsep) if p]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class PlusTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='검사 실행 ', dir=TEMP)
        self.home = Path(self.temporary.name)
        self.env = {**os.environ, 'TEACHER_EXAM_HOME': str(self.home), 'PYTHONUTF8': '1'}

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, script, *args, code=0):
        result = subprocess.run([sys.executable, '-B', str(SCRIPTS / script), *map(str, args)],
                                env=self.env, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_school_profile(self):
        def call(*args, code=0):
            return self.cli('school_profile.py', *args, code=code)
        self.assertEqual(call('show')['status'], 'MISSING')
        first = call('set', '--name', ' 길동중 ')
        self.assertEqual(first['status'], 'SAVED')
        self.assertEqual(first['official_name'], '길동중')
        self.assertEqual(call('show')['status'], 'OK')
        path = self.home / 'school.json'
        original = path.read_bytes()
        self.assertEqual(call('set', '--name', '길동중')['status'], 'UNCHANGED')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(call('set', '--name', '다른중', code=3)['status'], 'EXISTS')
        self.assertEqual(path.read_bytes(), original)
        replaced = call('set', '--name', '다른중', '--official', '다른중학교', '--replace')
        self.assertEqual(replaced['status'], 'REPLACED')
        self.assertEqual(replaced['previous']['saved_at'], first['saved_at'])
        for name in ('a', '길동/중', '가' * 31, '12', '길\x01동'):
            self.assertEqual(call('set', '--name', name, code=2)['status'], 'INVALID')
        self.assertEqual(call('set', '--name', '길동중', '--official', 'a', code=2)['status'], 'INVALID')
        for broken in ('{', '{}'):
            path.write_text(broken, encoding='utf-8')
            self.assertEqual(call('show', code=1)['status'], 'BROKEN')
            self.assertEqual(call('set', '--name', '길동중', code=1)['status'], 'BROKEN')
            self.assertEqual(path.read_text(encoding='utf-8'), broken)
            result = call('set', '--name', '길동중', '--replace')
            self.assertEqual(result['status'], 'REPLACED')
            self.assertIsNone(result['previous'])

    def test_lookup(self):
        def call(*args, code=0):
            return self.cli('textbook_lookup.py', 'find', *args, code=code)
        self.assertEqual(call()['status'], 'NONE')
        index = self.home / 'textbooks/index.json'
        unit = {'order': 3, 'no': 'Ⅲ', 'title': '일차함수', 'folder': 'units/03-일차함수', 'verified': True}
        book = {'id': '수학2-가상-2015', 'subject': '수학', 'grade': '중2',
                'folder': 'textbooks/수학2-가상-2015', 'units': [unit, {**unit, 'title': '도형', 'no': 'Ⅳ'}]}
        write(index, {'version': 1, 'textbooks': [book]})
        folder = self.home / book['folder'] / unit['folder']
        folder.mkdir(parents=True)
        for name in ('범위카드.md', '원문.md', '원문.json'):
            (folder / name).write_text('{}', encoding='utf-8')
        result = call('--subject', ' 수 학 ', '--grade', '중 2', '--keyword', '일차')
        units = result['textbooks'][0]['units']
        self.assertEqual(result['status'], 'FOUND')
        self.assertEqual(len(units), 1)
        self.assertTrue(units[0]['exists'])
        for key in ('card', 'source_md', 'source_json'):
            self.assertTrue(Path(units[0][key]).is_absolute())
        for keyword in ('Ⅲ', '가상'):
            self.assertEqual(call('--keyword', keyword)['status'], 'FOUND')
        self.assertEqual(call('--grade', '중3')['status'], 'NONE')
        (folder / '원문.md').unlink()
        self.assertFalse(call()['textbooks'][0]['units'][0]['exists'])
        index.write_text('{', encoding='utf-8')
        self.assertEqual(call(code=1)['status'], 'BROKEN')

    def test_check_regression(self):
        if BASELINE is None or not BASELINE.is_file():
            self.skipTest('HWPXAM_BASELINE_EXAM_CHECK가 없어 기존 원고 비교를 건너뜁니다.')
        references = []
        for item in REFERENCES:
            references += sorted(item.rglob('원고.json')) if item.is_dir() else [item]
        references = [p for p in references if p.is_file()]
        if not references:
            self.skipTest('HWPXAM_REGRESSION_MANUSCRIPTS에 비교할 원고가 없어 건너뜁니다.')
        for path in references:
            results = [subprocess.run([sys.executable, '-B', str(script), 'check', str(path)],
                       env=self.env, capture_output=True) for script in (BASELINE, SCRIPTS / 'exam_check.py')]
            self.assertEqual((results[0].returncode, results[0].stdout, results[0].stderr),
                             (results[1].returncode, results[1].stdout, results[1].stderr), str(path))
        print(f'기존 원고 {len(references)}개: 출력·종료 코드 일치', flush=True)

    def test_check_textbook(self):
        item = {'id': '가', 'type': 'mc', 'stem': '가상 문항', 'objective': '기록 검사',
                'derivation': '가상 도출', 'answer': ['1'], 'source_status': '창작',
                'shortest_solution': '가상 풀이', 'points': 1,
                'choices': [{'id': '1', 'text': '가'}, {'id': '2', 'text': '나'}],
                'option_review': [{'id': '1', 'satisfies_prompt': True, 'reason': '가상 근거'},
                                  {'id': '2', 'satisfies_prompt': False, 'reason': '가상 근거', 'misconception': '가상 오류'}],
                'shortcut_tests': [{'id': i, 'result': 'blocked', 'evidence': '가상 증거'} for i in range(1, 8)]}
        spec = {'items': [item], 'total_points': 1, 'cross_item_review': '가상 검토'}
        path = self.home / '원고.json'
        def check(code=0):
            write(path, spec)
            return self.cli('exam_check.py', 'check', path, code=code)
        self.assertEqual(check()['status'], 'PASS')
        spec['textbook_reference'] = [{'id': '가상', 'units': ['단원']}]
        self.assertIn('textbook_use가 필요합니다', check(1)['errors'][0])
        use = {'terms_checked': ['가나 수'], 'definitions_applied': [], 'scope_check': '가상 범위',
               'design_origin': '새 상황', 'differs_from_textbook': '상황 변경'}
        item['textbook_use'] = use.copy()
        self.assertEqual(check()['status'], 'PASS')
        for key in use:
            item['textbook_use'] = {**use, key: None}
            self.assertTrue(any('textbook_use.' + key in e for e in check(1)['errors']))
        item['textbook_use'] = {**use, 'terms_checked': []}
        check(1)
        item['textbook_use'] = use
        for invalid in ([], {}, None, [{'id': 1, 'units': []}], [{'id': '가', 'units': '나'}]):
            spec['textbook_reference'] = invalid
            self.assertIn('textbook_reference는', check(1)['errors'][0])

    def test_overlap(self):
        definition = '가나 수를 두 배로 늘린 값에 세모 조각을 하나 더 붙인 값을 다나 수라고 한다.'
        second = '보라 상자의 왼쪽 칸과 오른쪽 칸에 놓인 구슬의 합을 모둠 수라고 한다.'
        example = '상자에 가나 조각이 12개 있고 매일 3개씩 넣을 때 7일 뒤의 조각 수를 구하여라.'
        passage = '초록 마을의 작은 새는 새벽마다 둥근 언덕을 돌아 노란 창가에 앉아 노래하였다.'
        entries = [{'id': str(i), 'kind': kind, 'text': text, 'source': '창작', 'pdf_page': i + 1}
                   for i, (kind, text) in enumerate((('definition', definition), ('definition', second),
                                                    ('example', example), ('passage', passage)))]
        digest = self.home / '원문.json'
        write(digest, {'textbook_id': '가상', 'unit_order': 1, 'entries': entries})
        spec = {'items': [{'id': '1', 'stem': '설명을 고르시오.', 'choices': [{'text': definition}]},
                          {'id': '2', 'stem': example.replace('12', '24').replace('3', '6').replace('7', '9')},
                          {'id': '3', 'stem': definition.replace('다나 수', '(  )')},
                          {'id': '4', 'stem': '리본 두 묶음을 펼쳐 길이를 비교하는 방법을 설명하시오.'}],
                'passages': [{'text': passage, 'textbook_passage': True}]}
        manuscript = self.home / '원고.json'
        def call(code=1, *extra):
            write(manuscript, spec)
            return self.cli('exam_check.py', 'overlap', manuscript, '--digest', digest, *extra, code=code)
        result = call()
        for item, flag in zip(result['items'], ('COPY_SPAN', 'EXAMPLE_REUSE', 'DEFINITION_BLANK')):
            self.assertIn(flag, {f['type'] for f in item['flags']})
        self.assertEqual(result['items'][3]['flags'], [])
        self.assertEqual(len(result['items']), 4)
        spec['passages'][0].pop('textbook_passage')
        self.assertIn('COPY_SPAN', {f['type'] for f in call()['items'][-1]['flags']})
        spec['passages'][0]['textbook_passage'] = True
        for item in spec['items']:
            item['textbook_use'] = {'overlap_resolution': '창작 검사 자료의 의도한 중복'}
        self.assertEqual(call(0)['status'], 'FLAGGED')
        spec['items'] = [{'id': '제외', 'stem': passage, 'presentation': {'student': [
            {'text': passage, 'textbook_passage': True}]}}]
        self.assertEqual(call(0)['status'], 'PASS')
        spec['items'][0]['presentation']['student'][0]['textbook_passage'] = False
        self.assertEqual(call()['status'], 'FLAGGED')
        self.assertEqual(call(1, '--digest', self.home / '없는.json')['status'], 'FAIL')
        for kind in ('objective', 'notation', 'activity', 'scope_note'):
            write(digest, {'entries': [{**entries[3], 'kind': kind}]})
            self.assertEqual(call(0)['status'], 'PASS')

    def test_overlap_speed_and_exactness(self):
        module_spec = importlib.util.spec_from_file_location('exam_check', SCRIPTS / 'exam_check.py')
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        rng = random.Random(23)
        for _ in range(80):
            a = ''.join(rng.choices('가나다라', k=30))
            b = ''.join(rng.choices('가나다라', k=30))
            expected = max((j-i for i in range(len(a)) for j in range(i+1, len(a)+1)
                            if a[i:j] in b), default=0)
            self.assertEqual(module.longest_span(module.substring_index(a), b)[0], expected)
        self.assertEqual(module.normalize_text('Ａ − {X} ~ ` over rm times left right sqrt deg', True), 'a-x')
        digest = self.home / '긴 원문.json'
        text = '가나다라마' * 1000
        write(digest, {'entries': [{'id': '긴 문장', 'kind': 'definition', 'text': text}]})
        manuscript = self.home / '긴 원고.json'
        write(manuscript, {'items': [{'id': str(i), 'stem': text} for i in range(20)]})
        start = time.perf_counter()
        result = self.cli('exam_check.py', 'overlap', manuscript, '--digest', digest, code=1)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(result['items']), 20)
        self.assertLess(elapsed, 2)
        print(f'5,000자 × 20문항: {elapsed:.3f}초', flush=True)

    def test_package_manifest(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/build_package.py'), 'check'],
                                env=self.env, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    unittest.main(verbosity=2)
