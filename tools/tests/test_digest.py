"""Synthetic, offline integration tests for textbook-digest (Python 3.10+)."""
import copy
from datetime import datetime
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'textbook-digest/scripts'
TEMP = Path(os.environ.get('HWPXAM_TEST_ROOT') or tempfile.gettempdir()) / 'hwpxam-plus-test'
# 실제 교과서 성능 검사는 HWPXAM_LARGE_PDF에 PDF 경로를 줄 때만 실행한다.
LARGE_PDF = Path(os.environ['HWPXAM_LARGE_PDF']) if os.environ.get('HWPXAM_LARGE_PDF') else None
DEFINITION = '가나 수를 두 배로 늘린 값을 다나 수라고 한다.'
PROPERTY = '다나 수는 항상 짝수이다.'
EXAMPLE = '예제 1 다나 수를 구하시오.'
OBJECTIVE = '다나 수의 뜻을 설명할 수 있다.'
NOTATION = '다나 수를 기호 D로 나타낸다.'
SUMMARY = '이 단원에서는 가나 수와 다나 수의 관계를 정리하였다.'
PASSAGE = '초록 새가 둥근 돌 위에 앉았다.\n노란 새가 그 곁에 내려앉았다.'
ACTIVITY = '두 묶음의 조각을 나란히 놓고 관찰한다.'
NOTICE = '교과서 원문 사본입니다. 교사 개인의 평가 준비용이므로 공유하거나 인터넷에 올리지 마세요. 시험지를 만들 때는 용어·정의·표기·범위를 검산하는 사전으로만 쓰고, 문장이나 예제를 문항·보기로 옮기지 마세요. 쪽수는 책에 인쇄된 쪽수입니다.'
CARD_NOTICE = '출제 설계 단계에서 읽는 범위 카드입니다. 문항의 상황·자료·물음은 평가목표에서 새로 만드세요. 정의 전문·정리 문장·예제는 일부러 넣지 않았습니다. 용어와 정의는 설계가 끝난 뒤 원문.md로 검산하세요.'


def make_pdf(path, encrypted=False, variant=''):
    """Write a real xref PDF with Type0/Identity-H and an explicit ToUnicode map."""
    pages = [['차례 Ⅰ. 가나 단원 2' + variant],
             [DEFINITION, PROPERTY, OBJECTIVE, NOTATION, SUMMARY, '① 기호 A − 가'],
             [EXAMPLE, *PASSAGE.splitlines(), ACTIVITY]]
    chars = sorted(set(''.join(''.join(lines) for lines in pages)))
    codes = {char: i + 1 for i, char in enumerate(chars)}
    mapping = '\n'.join(f'<{code:04X}> <{char.encode("utf-16-be").hex().upper()}>' for char, code in codes.items())
    cmap = ('/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
            '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
            '/CMapName /Synthetic def\n/CMapType 2 def\n'
            '1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n'
            f'{len(codes)} beginbfchar\n{mapping}\nendbfchar\nendcmap\nend\nend').encode('ascii')
    def stream(data):
        return f'<< /Length {len(data)} >>\nstream\n'.encode() + data + b'\nendstream'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [7 0 R 9 0 R 11 0 R] /Count 3 >>',
               b'<< /Type /Font /Subtype /Type0 /BaseFont /Synthetic /Encoding /Identity-H /DescendantFonts [4 0 R] /ToUnicode 5 0 R >>',
               b'<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Synthetic /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 1000 >>',
               stream(cmap), b'<< >>']
    for n, lines in enumerate(pages):
        content_id = 8 + n * 2
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>'.encode())
        instructions = ['BT /F1 10 Tf 14 TL 1 0 0 1 20 760 Tm']
        for line in lines:
            instructions += ['<' + ''.join(f'{codes[c]:04X}' for c in line) + '> Tj', 'T*']
        objects.append(stream(('\n'.join(instructions) + '\nET').encode('ascii')))
    data = bytearray(b'%PDF-1.7\n')
    offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f'{n} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(data)
    data.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        data.extend(f'{offset:010d} 00000 n \n'.encode())
    encrypt = ' /Encrypt 6 0 R' if encrypted else ''
    data.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R{encrypt} >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    path.write_bytes(data)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class DigestTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        # Inherit the permitted root's ACL; mkdtemp's owner-only ACL cannot be
        # used by the restricted Windows execution token on Python 3.12.15.
        self.base = (TEMP / ('digest-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex)).resolve()
        self.base.mkdir()
        self.addCleanup(self.cleanup)
        self.home = self.base / '한글 교사 홈'
        self.env = {**os.environ, 'TEACHER_EXAM_HOME': str(self.home), 'PYTHONUTF8': '1'}
        self.pdf = self.base / '지어낸 교과서.pdf'
        make_pdf(self.pdf)
        self.identifier = '수학2-가상-2015'
        self.folder = self.home / 'textbooks' / self.identifier

    def cleanup(self):
        self.assertTrue(self.base.is_relative_to(TEMP.resolve()))
        shutil.rmtree(self.base)

    def run_cli(self, *args, code=0, plain=False, script=None):
        result = subprocess.run([sys.executable, '-B', str(script or SCRIPTS / 'textbook_tool.py'), *map(str, args)],
                                env=self.env, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        return result.stdout if plain else json.loads(result.stdout)

    def cli(self, command, *args, **kwargs):
        return self.run_cli(command, '--id', self.identifier, *args, **kwargs)

    def register(self):
        return self.cli('init', '--pdf', self.pdf, '--subject', '수학', '--grade', '중2',
                        '--publisher', '가상 출판사', '--authors', '가상 저자', '--curriculum', '2015', '--page-offset', '1')

    def prepare(self):
        self.register()
        self.cli('extract')
        self.toc = {'textbook_id': self.identifier, 'units': [
            {'order': 1, 'no': 'Ⅰ', 'title': '가나 단원', 'pdf_pages': [2, 3], 'sections': [
                {'no': '1', 'title': '다나 수', 'pdf_pages': [2, 3]}]}]}
        write_json(self.folder / 'toc.json', self.toc)
        self.unit = Path(self.cli('unit-dir', '--unit', '01')['path'])
        self.source = {'textbook_id': self.identifier, 'unit_order': 1, 'entries': [
            {'id': 'U01-001', 'kind': 'definition', 'section': '1', 'label': '다나 수', 'text': DEFINITION, 'pdf_page': 2, 'source': 'text'},
            {'id': 'U01-002', 'kind': 'property', 'section': '1', 'label': '짝수', 'text': PROPERTY, 'pdf_page': 2, 'source': 'text'},
            {'id': 'U01-003', 'kind': 'example', 'section': '1', 'label': '예제 1', 'task': '다나 수 구하기', 'text': EXAMPLE, 'pdf_page': 3, 'source': 'text'},
            {'id': 'U01-004', 'kind': 'scope_note', 'section': '', 'text': '가나 수에서 다나 수를 만드는 관계를 다룬다.', 'source': 'judgment'}]}
        self.save()

    def save(self):
        write_json(self.unit / '원문.json', self.source)

    def verify(self, code=0):
        self.save()
        return self.cli('verify', '--unit', 'Ⅰ', code=code)

    def test_init_identity_encryption_and_invalid_ids(self):
        first = self.register()
        self.assertEqual(first['status'], 'REGISTERED')
        self.assertEqual(first['meta']['pdf_pages'], 3)
        self.assertTrue(Path(first['meta']['pdf_path']).is_absolute())
        self.assertEqual(first['meta']['pdf_sha256'], hashlib.sha256(self.pdf.read_bytes()).hexdigest())
        result = self.cli('init', '--pdf', self.pdf)
        self.assertEqual(result['status'], 'UPDATED')
        self.assertEqual(result['meta']['created_at'], first['meta']['created_at'])
        before = (self.folder / 'meta.json').read_bytes()
        different = self.base / '다른.pdf'
        make_pdf(different, variant=' 다름')
        self.assertEqual(self.cli('init', '--pdf', different, '--subject', '변경', code=3)['status'], 'PDF_CHANGED')
        self.assertEqual((self.folder / 'meta.json').read_bytes(), before)
        self.assertEqual(self.cli('extract', '--pdf', different, code=3)['status'], 'PDF_CHANGED')
        moved = self.base / '옮긴.pdf'
        self.pdf.rename(moved)
        self.assertEqual(self.cli('extract', '--pdf', moved)['pages_extracted'], 3)
        updated = self.cli('init', '--pdf', moved)['meta']
        self.assertEqual(updated['pdf_path'], str(moved.resolve()))
        self.assertEqual(updated['pdf_name'], first['meta']['pdf_name'])
        for invalid in ('a', '.가나', '가' * 61, '가/나', '가\\나', '가:나', '가*나', '가?나', '가"나', '가<나', '가>나', '가|나', '가\x01나', '가\x7f나'):
            self.assertEqual(self.run_cli('init', '--id', invalid, code=2)['status'], 'INVALID')
        locked = self.base / '잠긴.pdf'
        make_pdf(locked, encrypted=True)
        result = self.run_cli('init', '--id', '암호화', '--pdf', locked, '--subject', '수학', '--grade', '중2', code=2)
        self.assertEqual(result['status'], 'ENCRYPTED')
        self.assertFalse((self.home / 'textbooks/암호화').exists())

    def test_extract_page_and_partial_merge(self):
        self.register()
        self.assertEqual(self.cli('page', '--page', 1, code=1)['status'], 'NOT_EXTRACTED')
        partial = self.cli('extract', '--pages', '2')
        self.assertEqual(partial['pages_extracted'], 1)
        self.assertEqual(self.cli('page', '--page', 1, '--to', 2, code=1)['status'], 'NOT_EXTRACTED')
        result = self.cli('extract')
        self.assertEqual(result['status'], 'EXTRACTED')
        self.assertEqual(result['pages_total'], 3)
        self.assertEqual(result['empty_pages'], [1])
        self.assertEqual(result['unmapped_ratio'], 0)
        self.assertEqual(result['high_unmapped_pages'], [])
        rows = (self.folder / 'pages.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(rows), 3)
        self.assertEqual(self.cli('extract', '--pages', '2,2-3')['pages_extracted'], 3)
        self.assertEqual(rows, (self.folder / 'pages.jsonl').read_text(encoding='utf-8').splitlines())
        text = self.cli('page', '--page', 2, '--to', 3, plain=True)
        self.assertIn('=== PDF 2쪽 (책 1쪽) ===', text)
        self.assertIn(DEFINITION, text)
        self.assertIn('=== PDF 3쪽 (책 2쪽) ===', text)
        meta = json.loads((self.folder / 'meta.json').read_text(encoding='utf-8'))
        meta['page_offset'] = None
        write_json(self.folder / 'meta.json', meta)
        self.assertIn('(책 쪽수 미정)', self.cli('page', '--page', 2, plain=True))
        self.assertEqual(self.cli('page', '--page', 2, '--to', 1, code=2)['status'], 'INVALID')
        self.assertEqual(self.cli('page', '--page', 1, '--to', 11, code=2)['status'], 'INVALID')
        self.assertEqual(self.cli('extract', '--pages', '0', code=1)['status'], 'FAIL')

    def test_toc_selectors_validation_and_folder_name(self):
        self.prepare()
        for selector in ('1', '01', 'Ⅰ', '가나'):
            self.assertEqual(Path(self.cli('unit-dir', '--unit', selector)['path']), self.unit)
        self.assertEqual(self.cli('unit-dir', '--unit', '없음', code=1)['status'], 'NOT_FOUND')
        second = {'order': 2, 'no': 'Ⅱ', 'title': '가나 심화', 'pdf_pages': [3, 3], 'sections': []}
        self.toc['units'].append(second)
        write_json(self.folder / 'toc.json', self.toc)
        self.assertEqual(self.cli('unit-dir', '--unit', '가나', code=2)['status'], 'AMBIGUOUS')
        for change in ({'order': 1}, {'order': 0}, {'order': True}, {'pdf_pages': [0, 2]},
                       {'pdf_pages': [3, 2]}, {'pdf_pages': [1, 4]}, {'pdf_pages': [True, 2]},
                       {'sections': [{'no': '1', 'title': '오류', 'pdf_pages': [1, 9]}]}):
            bad = copy.deepcopy(self.toc)
            bad['units'][1].update(change)
            write_json(self.folder / 'toc.json', bad)
            self.assertEqual(self.cli('unit-dir', '--unit', '1', code=1)['status'], 'TOC_INVALID')
        second['title'] = '\\/:*?"<>|' + '가' * 50
        write_json(self.folder / 'toc.json', self.toc)
        self.assertEqual(Path(self.cli('unit-dir', '--unit', '2')['path']).name, '02-' + '가' * 40)
        write_json(self.folder / 'toc.json', {'units': {}})
        self.assertEqual(self.cli('unit-dir', '--unit', '1', code=1)['status'], 'TOC_INVALID')

    def test_verification_normalization_and_closest(self):
        self.prepare()
        self.assertEqual(self.verify()['status'], 'PASS')
        entry = self.source['entries'][0]
        entry['text'] = DEFINITION.replace('두', '세')
        result = self.verify(1)
        failure = result['failures'][0]
        self.assertEqual(failure['reason'], 'PDF 글자에 이 원문이 없습니다.')
        self.assertIn(DEFINITION, failure['closest'])
        self.assertLessEqual(len(failure['closest']), 80)
        self.assertGreater(failure['ratio'], 0.9)
        entry['text'] = '\u200b\u200c\u200d\ufeff\u00ad' + unicodedata.normalize('NFD', DEFINITION.replace(' ', '\n\t\u00a0'))
        self.assertEqual(self.verify()['status'], 'PASS')
        for original, changed in [('①', '1'), ('A', 'a'), ('A', 'Ａ'), ('−', '-')]:
            entry['text'] = original
            self.assertEqual(self.verify()['status'], 'PASS')
            entry['text'] = changed
            self.assertEqual(self.verify(1)['failures'][0]['reason'], 'PDF 글자에 이 원문이 없습니다.')
        entry['text'] = PROPERTY + EXAMPLE
        entry.pop('pdf_page')
        entry['pdf_pages'] = [2, 3]
        # The two strings are not adjacent across the page boundary.
        self.assertEqual(self.verify(1)['status'], 'FAIL')
        entry['text'] = '① 기호 A − 가' + EXAMPLE
        self.assertEqual(self.verify()['status'], 'PASS')

    def test_verification_rules_and_warnings(self):
        self.prepare()
        base = copy.deepcopy(self.source)
        cases = [({'id': 'U01-002'}, 'id가 겹칩니다.'),
                 ({'kind': 'unknown'}, 'kind가 허용값이 아닙니다.'),
                 ({'source': 'judgment'}, '이 kind에는 이 source를 쓸 수 없습니다.'),
                 ({'text': ' \u200b'}, '내용이 비어 있습니다.'),
                 ({'pdf_page': 9}, '쪽이 PDF 범위를 벗어났습니다.'),
                 ({'pdf_page': True}, '쪽이 PDF 범위를 벗어났습니다.'),
                 ({'source': 'visual'}, '눈으로 옮긴 항목에는 note가 필요합니다.')]
        for change, reason in cases:
            self.source = copy.deepcopy(base)
            self.source['entries'][0].update(change)
            self.assertIn(reason, [f['reason'] for f in self.verify(1)['failures']])
        self.source = copy.deepcopy(base)
        self.source['entries'][0].pop('pdf_page')
        self.assertIn('쪽 정보가 없습니다.', [f['reason'] for f in self.verify(1)['failures']])
        self.source = copy.deepcopy(base)
        self.source['entries'][2].pop('task')
        self.assertIn('예제에는 task가 필요합니다.', [f['reason'] for f in self.verify(1)['failures']])
        self.source = copy.deepcopy(base)
        self.source['entries'][2].pop('text')
        self.assertEqual(self.verify()['status'], 'PASS')
        self.source['entries'][0].update(source='visual', note='합성 그림 대조')
        self.assertEqual(self.verify()['counts']['visual'], 1)
        self.source['entries'][0].update(pdf_page=1)
        result = self.verify()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['warnings'][0]['id'], 'U01-001')
        self.source = copy.deepcopy(base)
        rows = (self.folder / 'pages.jsonl').read_text(encoding='utf-8').splitlines()
        (self.folder / 'pages.jsonl').write_text(rows[0] + '\n', encoding='utf-8')
        self.assertIn('아직 추출하지 않은 쪽입니다.', [f['reason'] for f in self.verify(1)['failures']])

    def test_global_entry_ids(self):
        self.prepare()
        write_json(self.folder / 'units/02-다른 단원/원문.json', {'entries': [{'id': 'U01-001'}]})
        self.assertIn('id가 겹칩니다.', [f['reason'] for f in self.verify(1)['failures']])

    def test_render_content_status_and_lookup(self):
        self.prepare()
        self.assertEqual(self.cli('status')['units'][0]['state'], 'drafted')
        self.assertEqual(self.cli('render', '--unit', '1', code=1)['status'], 'NOT_VERIFIED')
        for kind, text, p in [('objective', OBJECTIVE, 2), ('notation', NOTATION, 2),
                              ('summary', SUMMARY, 2), ('passage', PASSAGE, 3), ('activity', ACTIVITY, 3)]:
            self.source['entries'].append({'id': kind, 'kind': kind, 'text': text, 'pdf_page': p,
                                           'section': '1', 'source': 'text', 'label': kind})
        self.source['entries'].append({'id': 'visual', 'kind': 'notation', 'text': '그림 속 기호',
                                       'pdf_page': 2, 'source': 'visual', 'note': '합성 그림 대조', 'book_page': 99})
        report = self.verify()
        self.assertEqual(self.cli('status')['units'][0]['state'], 'verified')
        result = self.cli('render', '--unit', '1')
        self.assertEqual(result['status'], 'RENDERED')
        source_md, card_md = [Path(p).read_text(encoding='utf-8') for p in result['files']]
        self.assertEqual(source_md.splitlines()[2], '> ' + NOTICE)
        self.assertEqual(card_md.splitlines()[2], '> ' + CARD_NOTICE)
        self.assertIn('# 수학 중2 (가상 출판사, 가상 저자, 2015 교육과정) — Ⅰ. 가나 단원 원문', source_md)
        self.assertIn('## 1. 다나 수 (교과서 1~2쪽)', source_md)
        self.assertIn('- **다나 수**: ' + DEFINITION + ' (교과서 1쪽)', source_md)
        self.assertIn('- 예제 1 (교과서 2쪽): 다나 수 구하기', source_md)
        self.assertIn('#### passage\n\n> 초록 새가 둥근 돌 위에 앉았다.\n> 노란 새가 그 곁에 내려앉았다.', source_md)
        self.assertIn('## 단원 전체', source_md)
        self.assertIn('(교과서 99쪽) [눈으로 옮김: 합성 그림 대조]', source_md)
        self.assertNotIn(EXAMPLE, source_md)
        for text in (DEFINITION, PROPERTY, EXAMPLE, SUMMARY, *PASSAGE.splitlines()):
            self.assertNotIn(text, card_md)
        for text in (OBJECTIVE, NOTATION, '다나 수, 짝수'):
            self.assertIn(text, card_md)
        self.assertEqual(self.cli('status')['units'][0]['state'], 'rendered')
        old = datetime.fromisoformat(report['checked_at']).timestamp() - 1
        os.utime(self.unit / '원문.md', (old, old))
        self.assertEqual(self.cli('status')['units'][0]['state'], 'verified')
        self.cli('render', '--unit', '1')
        self.assertEqual(self.run_cli('index')['status'], 'INDEXED')
        index = json.loads((self.home / 'textbooks/index.json').read_text(encoding='utf-8'))
        self.assertEqual(index['version'], 1)
        self.assertTrue(index['textbooks'][0]['units'][0]['verified'])
        self.assertEqual(index['textbooks'][0]['units'][0]['entries'], 10)
        self.assertEqual(index['textbooks'][0]['units'][0]['visual_entries'], 1)
        found = self.run_cli('find', '--subject', '수학', '--grade', '중2', script=ROOT / 'teacher-exam-hwpx/scripts/textbook_lookup.py')
        self.assertEqual(found['status'], 'FOUND')
        found_unit = found['textbooks'][0]['units'][0]
        self.assertTrue(found_unit['exists'])
        for key in ('card', 'source_md', 'source_json'):
            self.assertTrue(Path(found_unit[key]).is_absolute())
        self.source['entries'][0]['text'] += ' 변경'
        self.save()
        self.assertEqual(self.cli('status')['units'][0]['state'], 'drafted')
        self.assertEqual(self.cli('render', '--unit', '1', code=1)['status'], 'NOT_VERIFIED')
        self.run_cli('index')
        index = json.loads((self.home / 'textbooks/index.json').read_text(encoding='utf-8'))
        self.assertFalse(index['textbooks'][0]['units'][0]['verified'])
        self.assertIsNone(index['textbooks'][0]['units'][0]['verified_at'])

    def test_empty_status_index_and_optional_metadata(self):
        result = self.cli('init', '--pdf', self.pdf, '--subject', '수학', '--grade', '중2')
        self.assertIsNone(result['meta']['page_offset'])
        self.assertEqual(self.cli('status')['units'], [])
        self.assertFalse(self.cli('status')['toc'])
        self.run_cli('index')
        index = json.loads((self.home / 'textbooks/index.json').read_text(encoding='utf-8'))
        self.assertEqual(index['textbooks'][0]['units'], [])

    def test_atomic_write_and_exact_window(self):
        sys.path.insert(0, str(SCRIPTS))
        try:
            spec = importlib.util.spec_from_file_location('digest_under_test', SCRIPTS / 'textbook_tool.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
        path = self.base / 'atomic.json'
        path.write_text('original', encoding='utf-8')
        before = set(self.base.iterdir())
        with patch.object(module.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                module.atomic_write(path, 'changed')
        self.assertEqual(path.read_text(encoding='utf-8'), 'original')
        self.assertEqual(set(self.base.iterdir()), before)
        raw = '가 나 다 라 마 가 다 나 다 라'
        target = '가나다마'
        normalized = module.normalize(raw)
        expected = max(difflib.SequenceMatcher(None, normalized[i:i + len(target)], target, autojunk=False).ratio()
                       for i in range(len(normalized) - len(target) + 1))
        self.assertEqual(module.closest_match(raw, target)['ratio'], expected)
        self.assertEqual(module.normalize('①Ａ−A'), '①Ａ−A')
        self.assertEqual(module.page_label({'pdf_pages': [1, 3]}, {'page_offset': None}), 'PDF 1~3쪽')
        self.assertEqual(module.page_label({'book_page': [4, 5]}, {'page_offset': None}), '4~5쪽')

    def test_large_pdf_performance(self):
        if LARGE_PDF is None or not LARGE_PDF.is_file():
            self.skipTest('지정한 큰 PDF가 없어 성능 검사를 건너뜁니다.')
        start = time.perf_counter()
        self.cli('init', '--pdf', LARGE_PDF, '--subject', '수학', '--grade', '중2')
        init_seconds = time.perf_counter() - start
        start = time.perf_counter()
        result = self.cli('extract', '--pages', '1-30')
        self.assertEqual(result['pages_extracted'], 30)
        print(json.dumps({'performance': {'init_seconds': round(init_seconds, 3),
              'extract_30_seconds': round(time.perf_counter() - start, 3),
              'pages_total': result['pages_total'], 'unmapped_ratio': result['unmapped_ratio']}}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    for output in (sys.stdout, sys.stderr):
        try:
            output.reconfigure(encoding='utf-8')
        except Exception:
            pass
    unittest.main(verbosity=2)
