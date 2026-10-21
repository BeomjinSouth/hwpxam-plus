"""Local textbook digest tools; Python 3.10+, standard library only."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata

import pdf_text


KINDS = dict(objective='학습목표', definition='정의', summary='정리',
             property='성질·법칙·공식', notation='표기 약속', passage='지문',
             example='예제·문제', activity='탐구 활동', scope_note='범위 메모 (AI 판단)')
FORBIDDEN = '\\/:*?"<>|'
IGNORED = '\u00ad\u200b\u200c\u200d\ufeff'
INVALID_ID = '정리본 id는 2~60자이며 \\ / : * ? " < > | 를 쓸 수 없습니다.'
ENCRYPTED = '암호가 걸리거나 복사가 제한된 PDF는 정리하지 않습니다. 제한 없는 파일을 받아 주세요.'
PDF_CHANGED = '등록된 교과서와 다른 PDF입니다. 새 id로 등록하세요.'
NOT_VERIFIED = '검증을 통과한 원문만 문서로 만듭니다. verify를 먼저 실행하세요.'
SOURCE_NOTICE = '교과서 원문 사본입니다. 교사 개인의 평가 준비용이므로 공유하거나 인터넷에 올리지 마세요. 시험지를 만들 때는 용어·정의·표기·범위를 검산하는 사전으로만 쓰고, 문장이나 예제를 문항·보기로 옮기지 마세요. 쪽수는 책에 인쇄된 쪽수입니다.'
CARD_NOTICE = '출제 설계 단계에서 읽는 범위 카드입니다. 문항의 상황·자료·물음은 평가목표에서 새로 만드세요. 정의 전문·정리 문장·예제는 일부러 넣지 않았습니다. 용어와 정의는 설계가 끝난 뒤 원문.md로 검산하세요.'


class ToolError(Exception):
    def __init__(self, status, error, code=1):
        self.status, self.error, self.code = status, error, code
        super().__init__(error)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def data_home():
    return Path(os.environ.get('TEACHER_EXAM_HOME') or Path.home() / '.teacher-exam-hwpx').resolve()


def sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def book_folder(identifier):
    if (not 2 <= len(identifier) <= 60 or identifier.startswith('.') or
            any(c in FORBIDDEN or unicodedata.category(c) == 'Cc' for c in identifier)):
        raise ToolError('INVALID', INVALID_ID, 2)
    return data_home() / 'textbooks' / identifier


def load_book(identifier):
    folder = book_folder(identifier)
    return folder, read_json(folder / 'meta.json')


def matching_pdf(meta, path):
    path = Path(path or meta['pdf_path']).resolve()
    if sha256(path) != meta['pdf_sha256']:
        raise ToolError('PDF_CHANGED', PDF_CHANGED, 3)
    return path


def init(args):
    folder = book_folder(args.id)
    path = folder / 'meta.json'
    if path.exists():
        meta = read_json(path)
        if args.pdf is not None:
            meta['pdf_path'] = str(matching_pdf(meta, args.pdf))
        status = 'UPDATED'
    else:
        if not args.pdf or not args.subject or not args.grade:
            raise ToolError('INVALID', '처음 등록할 때는 --pdf, --subject, --grade가 모두 필요합니다.', 2)
        pdf = Path(args.pdf).resolve()
        pages = pdf_text.page_count(pdf)  # Reject encryption before creating any folder.
        meta = dict(version=1, id=args.id, subject='', grade='', publisher='', authors='',
                    curriculum='', pdf_path=str(pdf), pdf_name=pdf.name,
                    pdf_sha256=sha256(pdf), pdf_pages=pages, page_offset=None, created_at=now())
        status = 'REGISTERED'
    for key in ('subject', 'grade', 'publisher', 'authors', 'curriculum', 'page_offset'):
        value = getattr(args, key)
        if value is not None:
            meta[key] = value
    meta['updated_at'] = now()
    write_json(path, meta)
    return {'status': status, 'meta': meta}, 0


def load_pages(folder):
    path = folder / 'pages.jsonl'
    if not path.exists():
        return {}
    return {row['page']: row for row in
            (json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip())}


def extract(args):
    folder, meta = load_book(args.id)
    path = matching_pdf(meta, args.pdf)
    rows = load_pages(folder)
    rows.update({row['page']: row for row in pdf_text.extract_pages(path, args.pages)})
    ordered = [rows[n] for n in sorted(rows)]
    chars = sum(r['chars'] for r in ordered)
    unmapped = sum(r['unmapped'] for r in ordered)
    high = []
    for row in ordered:
        total = row['chars'] + row['unmapped']
        ratio = row['unmapped'] / total if total else 0
        if ratio >= 0.05:
            high.append({'page': row['page'], 'ratio': ratio})
    summary = dict(pages_total=meta['pdf_pages'], pages_extracted=len(ordered),
                   text_pages=sum(r['chars'] >= 20 for r in ordered),
                   empty_pages=[r['page'] for r in ordered if r['chars'] < 20],
                   high_unmapped_pages=high, unmapped_ratio=unmapped / (chars + unmapped) if chars + unmapped else 0,
                   fonts_without_unicode=sorted({font for r in ordered for font in r['fonts_without_unicode']}),
                   extracted_at=now())
    atomic_write(folder / 'pages.jsonl', ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in ordered))
    write_json(folder / 'extract.json', summary)
    return {'status': 'EXTRACTED', **summary}, 0


def page(args):
    folder, meta = load_book(args.id)
    end = args.to if args.to is not None else args.page
    if not 1 <= args.page <= end <= meta['pdf_pages'] or end - args.page >= 10:
        raise ToolError('INVALID', f'쪽 범위가 맞지 않습니다. --page와 --to는 1~{meta["pdf_pages"]} 안에서 고르고, 한 번에 10쪽까지 읽습니다.', 2)
    rows = load_pages(folder)
    output = []
    for n in range(args.page, end + 1):
        if n not in rows:
            raise ToolError('NOT_EXTRACTED', f'먼저 extract로 그 쪽 글자를 뽑으세요: {n}')
        book = f'책 {n - meta["page_offset"]}쪽' if meta['page_offset'] is not None else '책 쪽수 미정'
        output.append(f'=== PDF {n}쪽 ({book}) ===\n{rows[n]["text"]}')
    return '\n'.join(output), 0


def is_int(value):
    return type(value) is int


def valid_range(value, total):
    return (isinstance(value, list) and len(value) == 2 and all(is_int(n) for n in value)
            and 1 <= value[0] <= value[1] <= total)


def load_toc(folder, meta):
    try:
        toc = read_json(folder / 'toc.json')
        if not isinstance(toc, dict) or not isinstance(toc.get('units'), list):
            raise ValueError('units')
        seen = set()
        def check(node):
            if (not isinstance(node, dict) or not isinstance(node.get('no'), str)
                    or not isinstance(node.get('title'), str) or not node['title'].strip()
                    or not valid_range(node.get('pdf_pages'), meta['pdf_pages'])):
                raise ValueError('no/title/pdf_pages')
            sections = node.get('sections', [])
            if not isinstance(sections, list):
                raise ValueError('sections')
            for section in sections:
                check(section)
        for unit in toc['units']:
            check(unit)
            order = unit.get('order')
            if not is_int(order) or order < 1 or order in seen:
                raise ValueError('order')
            seen.add(order)
        return sorted(toc['units'], key=lambda u: u['order'])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ToolError('TOC_INVALID', f'toc.json 형식이 맞지 않습니다({exc}). 정리본-형식.md의 toc.json 규칙을 확인하세요.') from exc


def unit_folder(folder, unit):
    title = ''.join(c for c in unit['title'] if c not in FORBIDDEN)[:40]
    return folder / 'units' / f'{unit["order"]:02d}-{title}'


def selected_unit(args):
    folder, meta = load_book(args.id)
    units = load_toc(folder, meta)
    selector = args.unit
    matches = [u for u in units if (selector.isdecimal() and int(selector) == u['order'])
               or selector == u['no'] or selector in u['title']]
    if len(matches) != 1:
        status = 'AMBIGUOUS' if matches else 'NOT_FOUND'
        raise ToolError(status, (f'대단원을 하나로 정할 수 없습니다. 번호나 제목을 더 구체적으로 주세요: {selector}' if matches
                                 else f'차례(toc.json)에서 대단원을 찾지 못했습니다: {selector}'), 2 if matches else 1)
    unit = matches[0]
    return folder, meta, unit, unit_folder(folder, unit)


def unit_dir(args):
    _, _, unit, path = selected_unit(args)
    path.mkdir(parents=True, exist_ok=True)
    return {'status': 'OK', 'path': str(path.resolve()), 'unit': unit}, 0


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFC', text) if not c.isspace() and c not in IGNORED)


def closest_match(raw, target):
    canonical = unicodedata.normalize('NFC', raw)
    # Map NFC positions back to the original spelling, including decomposed Hangul.
    spans = [(0, 0)] * len(canonical)
    for tag, a, b, c, d in difflib.SequenceMatcher(None, raw, canonical, autojunk=False).get_opcodes():
        for i in range(c, d):
            spans[i] = (a + i - c, a + i - c + 1) if tag == 'equal' else (a, b)
    positions = [spans[i] for i, c in enumerate(canonical) if not c.isspace() and c not in IGNORED]
    normalized = normalize(raw)
    size = len(target)
    best, start = -1.0, 0
    matcher = difflib.SequenceMatcher(None, '', target, autojunk=False)
    for i in range(max(1, len(normalized) - size + 1)):
        matcher.set_seq1(normalized[i:i + size])
        if matcher.quick_ratio() <= best:
            continue
        ratio = matcher.ratio()
        if ratio > best:
            best, start = ratio, i
    if not positions:
        return {'closest': raw[:80], 'ratio': max(0.0, best)}
    left = positions[start][0]
    right = positions[min(start + size, len(positions)) - 1][1]
    left = max(0, left - min(12, max(0, (80 - (right - left)) // 2)))
    return {'closest': raw[left:min(len(raw), left + 80)], 'ratio': best}


def entry_pages(entry, total):
    if 'pdf_page' in entry:
        value = entry['pdf_page']
        if not is_int(value) or not 1 <= value <= total:
            return [], '쪽이 PDF 범위를 벗어났습니다.'
        # Validate both fields if an author supplies both.
        if 'pdf_pages' in entry and not valid_range(entry['pdf_pages'], total):
            return [], '쪽이 PDF 범위를 벗어났습니다.'
        return [value], None
    if 'pdf_pages' in entry:
        value = entry['pdf_pages']
        if not valid_range(value, total):
            return [], '쪽이 PDF 범위를 벗어났습니다.'
        return list(range(value[0], value[1] + 1)), None
    return [], '쪽 정보가 없습니다.'


def nonempty(value):
    return isinstance(value, str) and bool(normalize(value))


def verify(args):
    folder, meta, unit, path = selected_unit(args)
    source_path = path / '원문.json'
    raw = source_path.read_bytes()
    source = json.loads(raw.decode('utf-8-sig'))
    entries = source.get('entries') if isinstance(source, dict) else None
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise ToolError('FAIL', '원문.json에 entries 배열이 없거나 형식이 맞지 않습니다.')
    pages = load_pages(folder)
    failures, warnings, seen = [], [], set()
    # IDs belong to the whole textbook, not merely the current unit.
    for other in sorted((folder / 'units').glob('*/원문.json')):
        if other.resolve() != source_path.resolve():
            for e in read_json(other).get('entries', []):
                if isinstance(e, dict) and isinstance(e.get('id'), str):
                    seen.add(e['id'])
    for entry in entries:
        identifier = entry.get('id')
        def fail(reason, **extra):
            failures.append({'id': identifier, 'reason': reason, **extra})
        if not nonempty(identifier):
            fail('내용이 비어 있습니다.')
        elif identifier in seen:
            fail('id가 겹칩니다.')
        else:
            seen.add(identifier)
        kind, source_type = entry.get('kind'), entry.get('source')
        if not isinstance(kind, str) or kind not in KINDS:
            fail('kind가 허용값이 아닙니다.')
        elif source_type not in (('judgment',) if kind == 'scope_note' else ('text', 'visual')):
            fail('이 kind에는 이 source를 쓸 수 없습니다.')
        text = entry.get('text')
        if kind == 'example':
            if not nonempty(entry.get('task')):
                fail('예제에는 task가 필요합니다.')
            if 'text' in entry and not nonempty(text):
                fail('내용이 비어 있습니다.')
        elif not nonempty(text):
            fail('내용이 비어 있습니다.')
        if source_type == 'visual' and not nonempty(entry.get('note')):
            fail('눈으로 옮긴 항목에는 note가 필요합니다.')
        numbers, error = entry_pages(entry, meta['pdf_pages'])
        if error and (source_type in ('text', 'visual') or 'pdf_page' in entry or 'pdf_pages' in entry):
            fail(error)
        if any(n < unit['pdf_pages'][0] or n > unit['pdf_pages'][1] for n in numbers):
            warnings.append({'id': identifier, 'pdf_pages': numbers, 'unit_pdf_pages': unit['pdf_pages']})
        if source_type == 'text' and not error:
            if any(n not in pages for n in numbers):
                fail('아직 추출하지 않은 쪽입니다.')
            elif nonempty(text):
                original = '\n'.join(pages[n]['text'] for n in numbers)
                target = normalize(text)
                if target not in normalize(original):
                    fail('PDF 글자에 이 원문이 없습니다.', **closest_match(original, target))
    counts = dict(entries=len(entries), **{s: sum(e.get('source') == s for e in entries)
                                         for s in ('text', 'visual', 'judgment')},
                  by_kind=dict(Counter(e['kind'] for e in entries if isinstance(e.get('kind'), str))))
    result = dict(status='FAIL' if failures else 'PASS', unit={k: unit[k] for k in ('order', 'no', 'title')},
                  source_sha256=hashlib.sha256(raw).hexdigest(), checked_at=now(), counts=counts,
                  failures=failures, warnings=warnings)
    write_json(path / '검증.json', result)
    return result, int(bool(failures))


def current_verification(path):
    try:
        report = read_json(path / '검증.json')
        if report['status'] == 'PASS' and report['source_sha256'] == sha256(path / '원문.json'):
            datetime.fromisoformat(report['checked_at'])
            return report
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def page_label(entry, meta):
    value = entry.get('book_page')
    if value is not None:
        if isinstance(value, list):
            return f'{value[0]}~{value[-1]}쪽'
        return f'{value}쪽'
    values = entry.get('pdf_pages') or [entry['pdf_page']]
    offset = meta['page_offset']
    prefix = 'PDF ' if offset is None else ''
    values = [n - (offset or 0) for n in values]
    return prefix + (str(values[0]) if values[0] == values[-1] else f'{values[0]}~{values[-1]}') + '쪽'


def sections_of(unit):
    for section in unit.get('sections', []):
        yield section
        yield from sections_of(section)


def render(args):
    _, meta, unit, path = selected_unit(args)
    report = current_verification(path)
    if report is None:
        raise ToolError('NOT_VERIFIED', NOT_VERIFIED)
    entries = read_json(path / '원문.json')['entries']
    details = [meta[k] for k in ('publisher', 'authors') if meta[k]]
    if meta['curriculum']:
        details.append(f'{meta["curriculum"]} 교육과정')
    name = f'{meta["subject"]} {meta["grade"]}' + (f' ({", ".join(details)})' if details else '')
    heading = f'{name} — {unit["no"]}. {unit["title"]}'
    source = [f'# {heading} 원문', '', '> ' + SOURCE_NOTICE, '']
    sections = list(sections_of(unit))
    groups = [(f'{s["no"]}. {s["title"]} (교과서 {page_label(s, meta)})',
               [e for e in entries if str(e.get('section', '')) == s['no']]) for s in sections]
    groups.append(('단원 전체', [e for e in entries if not e.get('section')]))
    unknown = [e for e in entries if e.get('section') and str(e['section']) not in {s['no'] for s in sections}]
    if any(e['kind'] != 'scope_note' for e in unknown):
        raise ToolError('FAIL', '원문.json의 section 값이 toc.json의 중단원 번호와 맞지 않습니다.')
    for title, grouped in groups:
        grouped = [e for e in grouped if e['kind'] != 'scope_note']
        if not grouped:
            continue
        source += ['## ' + title, '']
        for kind, caption in KINDS.items():
            selected = [e for e in grouped if e['kind'] == kind]
            if not selected:
                continue
            source += ['### ' + caption, '']
            for e in selected:
                location = f'(교과서 {page_label(e, meta)})'
                note = f' [눈으로 옮김: {e["note"]}]' if e['source'] == 'visual' else ''
                label = e.get('label', '')
                if kind == 'example':
                    source.append(f'- {label} {location}: {e["task"]}' + note)
                elif kind == 'passage':
                    if label:
                        source += ['#### ' + label, '']
                    source += ['> ' + line for line in e['text'].splitlines()]
                    source += ['> ' + location + note]
                else:
                    lead = f'**{label}**: ' if label else ''
                    source.append(f'- {lead}{e["text"]} {location}' + note)
            source.append('')
    scope = ['- ' + e['text'] for e in entries if e['kind'] == 'scope_note']
    visual_pages = list(dict.fromkeys(page_label(e, meta) for e in entries if e['source'] == 'visual'))
    source += ['## 범위 메모 (AI 판단)', *scope, '', '## 검증',
               f'- PDF 글자와 대조한 원문 {report["counts"]["text"]}개 일치, 눈으로 옮긴 항목 {report["counts"]["visual"]}개({", ".join(visual_pages)}), 검증 시각 {report["checked_at"]}']
    card = [f'# {heading} 범위 카드', '', '> ' + CARD_NOTICE, '', '## 단원 구성']
    card += [f'- {s["no"]}. {s["title"]} (교과서 {page_label(s, meta)})' for s in sections]
    card += ['', '## 학습목표'] + ['- ' + e['text'] for e in entries if e['kind'] == 'objective']
    card += ['', '## 핵심 용어', ', '.join(e['label'] for e in entries
              if e['kind'] in ('definition', 'property') and e.get('label'))]
    card += ['', '## 표기 약속'] + ['- ' + e['text'] for e in entries if e['kind'] == 'notation']
    card += ['', '## 범위 메모 (AI 판단)', *scope]
    files = [path / '원문.md', path / '범위카드.md']
    for target, lines in zip(files, (source, card)):
        atomic_write(target, '\n'.join(lines) + '\n')
    return {'status': 'RENDERED', 'files': [str(p.resolve()) for p in files]}, 0


def unit_state(folder, unit):
    path = unit_folder(folder, unit)
    result = {k: unit[k] for k in ('order', 'no', 'title')}
    result['state'] = 'todo'
    report = None
    if (path / '원문.json').exists():
        entries = read_json(path / '원문.json')['entries']
        result.update(state='drafted', entries=len(entries), visual_entries=sum(e.get('source') == 'visual' for e in entries))
        report = current_verification(path)
        if report:
            result['state'] = 'verified'
            checked = datetime.fromisoformat(report['checked_at']).timestamp()
            if all(p.is_file() and p.stat().st_mtime > checked for p in (path / '원문.md', path / '범위카드.md')):
                result['state'] = 'rendered'
    return result, report


def status(args):
    folder, meta = load_book(args.id)
    present = (folder / 'toc.json').exists()
    units = load_toc(folder, meta) if present else []
    return dict(status='OK', meta=meta, toc=present, units=[unit_state(folder, u)[0] for u in units]), 0


def index(args):
    home = data_home()
    books = []
    for meta_path in sorted((home / 'textbooks').glob('*/meta.json')):
        folder, meta = meta_path.parent, read_json(meta_path)
        book = {k: meta[k] for k in ('id', 'subject', 'grade', 'publisher', 'authors', 'curriculum',
                                     'pdf_name', 'pdf_sha256', 'page_offset')}
        book.update(folder=folder.relative_to(home).as_posix(), units=[])
        for unit in load_toc(folder, meta) if (folder / 'toc.json').exists() else []:
            state, report = unit_state(folder, unit)
            if state['state'] == 'todo':
                continue
            book['units'].append({**{k: state[k] for k in ('order', 'no', 'title', 'entries', 'visual_entries')},
                                  'folder': unit_folder(folder, unit).relative_to(folder).as_posix(),
                                  'verified': report is not None, 'verified_at': report['checked_at'] if report else None})
        books.append(book)
    path = home / 'textbooks/index.json'
    write_json(path, dict(version=1, updated_at=now(), textbooks=books))
    return dict(status='INDEXED', textbooks=len(books), path=str(path)), 0


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolError('INVALID', '명령 인수가 맞지 않습니다: ' + message, 2)


def main(argv=None):
    parser = ArgumentParser()
    modes = parser.add_subparsers(dest='mode', required=True)
    for mode in ('init', 'extract', 'page', 'unit-dir', 'verify', 'render', 'status', 'index'):
        sub = modes.add_parser(mode)
        if mode != 'index':
            sub.add_argument('--id', required=True)
        if mode in ('init', 'extract'):
            sub.add_argument('--pdf')
        if mode == 'init':
            for key in ('subject', 'grade', 'publisher', 'authors', 'curriculum'):
                sub.add_argument('--' + key)
            sub.add_argument('--page-offset', type=int)
        if mode == 'extract':
            sub.add_argument('--pages')
        if mode == 'page':
            sub.add_argument('--page', type=int, required=True)
            sub.add_argument('--to', type=int)
        if mode in ('unit-dir', 'verify', 'render'):
            sub.add_argument('--unit', required=True)
    try:
        args = parser.parse_args(argv)
        result, code = globals()[args.mode.replace('-', '_')](args)
    except pdf_text.EncryptedPDF:
        result, code = dict(status='ENCRYPTED', error=ENCRYPTED), 2
    except ToolError as exc:
        result, code = dict(status=exc.status, error=exc.error), exc.code
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result, code = dict(status='FAIL', error='처리 중 오류가 났습니다: ' + str(exc)), 1
    print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    raise SystemExit(main())
