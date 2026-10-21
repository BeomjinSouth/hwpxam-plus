"""Read-only content and HWPX checks. Python 3.10+, standard library only."""
import argparse
import copy
from collections import Counter
import hashlib
import json
import re
import sys
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def local(tag):
    return tag.rsplit('}', 1)[-1]


def xmlfiles(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip():
            raise ValueError('ZIP CRC 검사 실패')
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('중복 ZIP 항목')
        result = {n: ET.fromstring(archive.read(n)) for n in names
                  if re.fullmatch(r'Contents/(header|section\d+)\.xml', n)}
    if 'Contents/header.xml' not in result or not any('section' in x for x in result):
        raise ValueError('HWPX 본문 또는 스타일 누락')
    return result


def canonical(element, skip=()):
    return [local(element.tag), sorted(element.attrib.items()), (element.text or '').strip(),
            [canonical(e, skip) for e in element if local(e.tag) not in skip]]


def table_shape(element):
    # Paragraph contents can change, while table/cell sizes and properties remain fixed.
    return canonical(element, ('p', 'linesegarray'))


def without_table_height_cache(element):
    result = copy.deepcopy(element)
    for table in result.iter():
        if local(table.tag) == 'tbl':
            for child in table:
                if local(child.tag) == 'sz':
                    child.attrib.pop('height', None)
    return result


def font_identity(font):
    node = copy.deepcopy(font)
    node.attrib.pop('id', None)
    return json.dumps(canonical(node), ensure_ascii=False, sort_keys=True)


def semantic_char(element, header):
    node = copy.deepcopy(element)
    fonts = {face.attrib['lang'].lower(): {f.attrib['id']: font_identity(f) for f in face}
             for face in header.iter() if local(face.tag) == 'fontface'}
    for ref in node.iter():
        if local(ref.tag) == 'fontRef':
            for lang, value in list(ref.attrib.items()):
                ref.attrib[lang] = fonts.get(lang, {}).get(value, 'MISSING:' + value)
    return canonical(node)


def body_range(root, anchor, block_count=None):
    children = list(root)
    found = [i for i, p in enumerate(children) if anchor in ''.join(p.itertext())]
    if len(found) != 1:
        raise ValueError('단일 본문 표시자를 찾지 못했습니다.')
    return found[0], len(children)-found[0]-1


def box_signature(table, border_definitions):
    node = without_table_height_cache(table)
    for key in ('id', 'zOrder'):
        node.attrib.pop(key, None)
    for e in node.iter():
        if local(e.tag) == 'cellSz':
            e.attrib.pop('height', None)
        if 'borderFillIDRef' in e.attrib:
            definition = copy.deepcopy(border_definitions[e.attrib['borderFillIDRef']])
            definition.attrib.pop('id', None)
            e.attrib['borderFillIDRef'] = json.dumps(canonical(definition), ensure_ascii=False)
    return table_shape(node)


def inspect(path):
    files = xmlfiles(path)
    header = files['Contents/header.xml']
    sections = {n: rt for n, rt in files.items() if 'section' in n}
    return {'sha256': digest(path),
            'styles': [dict(e.attrib) for e in header.iter() if local(e.tag) == 'style'],
            'sections': {n: {
                'page': [canonical(e) for e in rt.iter() if local(e.tag) == 'pagePr'],
                'columns': [canonical(e) for e in rt.iter() if local(e.tag) == 'colPr'],
                'tables': sum(local(e.tag) == 'tbl' for e in rt.iter()),
                'texts': [''.join(e.itertext()) for e in rt.iter() if local(e.tag) == 't'],
                'fields': [e.attrib for e in rt.iter() if local(e.tag) == 'fieldBegin']
            } for n, rt in sections.items()}}


def protected_paragraph(element, replacements):
    attrs = {k: v for k, v in element.attrib.items() if not (local(element.tag) == 'p' and k == 'id')}
    text = element.text or ''
    if local(element.tag) == 't':
        mapping = {change['find']: change['replace'] for change in replacements}
        if any(not key for key in mapping):
            raise ValueError('빈 치환 문자열')
        if mapping:
            pattern = '|'.join(re.escape(key) for key in sorted(mapping, key=len, reverse=True))
            text = re.sub(pattern, lambda match: mapping[match.group()], text)
    return [local(element.tag), sorted(attrs.items()), text.strip(),
            [protected_paragraph(e, replacements) for e in element if local(e.tag) != 'linesegarray']]


def compare(before, after, plan=None):
    a, b = xmlfiles(before), xmlfiles(after)
    errors = []
    cache_reviews = []
    if set(a) != set(b):
        errors.append('구역 파일 목록 변경')
    for name in sorted(set(a) & set(b)):
        if 'section' in name:
            source_fixed, output_fixed = a[name], b[name]
            planned_boxes = (plan or {}).get('body', {}).get('boxes', {})
            anchor = (plan or {}).get('body', {}).get('anchor')
            if planned_boxes and anchor and any(anchor in ''.join(p.itertext()) for p in a[name]):
                from wrap_boxes import resize_caption, resize_table
                offset, suffix = body_range(a[name], anchor)
                old, new = list(a[name]), list(b[name])
                source_fixed, output_fixed = ET.Element('fixed'), ET.Element('fixed')
                source_fixed.extend(old[:offset] + (old[-suffix:] if suffix else []))
                output_fixed.extend(new[:offset] + (new[-suffix:] if suffix else []))
                body = new[offset:len(new)-suffix if suffix else len(new)]
                tables = [t for p in body for t in p.iter() if local(t.tag) == 'tbl']
                groups = []
                for block in plan['body']['blocks']:
                    group = block.get('box_id')
                    if group and (not groups or groups[-1] != group):
                        groups.append(group)
                profile_path = Path(__file__).resolve().parents[1]/'assets/condition-box.xml'
                selected = plan['body'].get('box_profile')
                if selected:
                    profile_path = Path(selected['path'])
                    if not profile_path.is_absolute() or digest(profile_path) != selected['sha256']:
                        errors.append('상자 프로필 경로·해시 불일치')
                profile = ET.parse(profile_path).getroot()
                border_a = {x.attrib['id']: x for x in profile.iter() if local(x.tag) == 'borderFill'}
                border_b = {x.attrib['id']: x for x in b['Contents/header.xml'].iter() if local(x.tag) == 'borderFill'}
                source_table = next(x for x in profile if local(x.tag) == 'tbl')
                if len(tables) != len(groups) or set(groups) != set(planned_boxes):
                    errors.append(f'{name}: 명세와 본문 상자 수/ID 불일치')
                for group, table in zip(groups, tables):
                    expected = copy.deepcopy(source_table)
                    resize_table(expected, plan['body'].get('box_width_mm'))
                    resize_caption(expected, planned_boxes[group].get('caption_width_mm'))
                    if box_signature(expected, border_a) != box_signature(table, border_b):
                        errors.append(f'{name}: {group} 상자 폭·셀·선·여백 변경')
                    caption = next((x.text for x in table.iter() if local(x.tag) == 't' and x.text), '')
                    label = planned_boxes[group].get('caption', '조건')
                    if caption != '＜' + ('조 건' if label == '조건' else label) + '＞':
                        errors.append(f'{name}: {group} 상자 제목 불일치')
            for kind in ('secPr', 'colPr', 'tbl'):
                get = lambda rt: [table_shape(e) if kind == 'tbl' else canonical(e)
                                  for e in rt.iter() if local(e.tag) == kind]
                left, right = (source_fixed, output_fixed) if kind == 'tbl' else (a[name], b[name])
                if get(left) != get(right):
                    if kind == 'tbl' and get(without_table_height_cache(left)) == get(without_table_height_cache(right)):
                        cache_reviews.append(f'{name}: 표 집계 높이만 달라짐. 원본과 최종 PDF에서 고정 표·셀 경계의 실제 위치 대조 필요')
                    else:
                        errors.append(f'{name}: {kind} 고정 구조 변경')
        else:
            for kind in ('style', 'charPr', 'paraPr', 'borderFill', 'fontface', 'numbering', 'bullet', 'tabPr'):
                aa = [e for e in a[name].iter() if local(e.tag) == kind]
                bb = [e for e in b[name].iter() if local(e.tag) == kind]
                index = {e.attrib.get('id', str(i)): canonical(e) for i, e in enumerate(bb)}
                for i, e in enumerate(aa):
                    key = e.attrib.get('id', str(i))
                    if kind == 'fontface' and i < len(bb):
                        target = bb[i]
                        old_fonts = Counter(font_identity(x) for x in e)
                        new_fonts = Counter(font_identity(x) for x in target)
                        old_attrs = {k: v for k, v in e.attrib.items() if k != 'fontCnt'}
                        new_attrs = {k: v for k, v in target.attrib.items() if k != 'fontCnt'}
                        allowed_faces = {'Consolas', '맑은 고딕'} if any(x.get('code') for x in (plan or {}).get('body', {}).get('blocks', [])) else set()
                        additions = new_fonts - old_fonts
                        if old_attrs != new_attrs or old_fonts - new_fonts or any(json.loads(x)[1] and dict(json.loads(x)[1]).get('face') not in allowed_faces for x in additions):
                            errors.append(f'{name}: 기존 fontface/{key} 변경 또는 승인되지 않은 글꼴 추가')
                        continue
                    target = next((x for j, x in enumerate(bb) if x.attrib.get('id', str(j)) == key), None)
                    equal = (target is not None and semantic_char(e, a[name]) == semantic_char(target, b[name])) if kind == 'charPr' else canonical(e) == index.get(key)
                    if not equal:
                        errors.append(f'{name}: 기존 {kind}/{key} 변경')
    with zipfile.ZipFile(before) as za, zipfile.ZipFile(after) as zb:
        protected = {n for n in za.namelist() if n.startswith('BinData/')}
        current = {n for n in zb.namelist() if n.startswith('BinData/')}
        authorized = {x['image']['sha256'] for x in (plan or {}).get('body', {}).get('blocks', []) if x.get('image')}
        added = current - protected
        added_hashes = {hashlib.sha256(zb.read(n)).hexdigest() for n in added}
        if protected - current or added_hashes != authorized:
            errors.append('기존 첨부 바이너리 목록 변경')
        for name in protected & set(zb.namelist()):
            if za.read(name) != zb.read(name):
                errors.append(f'{name}: 기존 바이너리 변경')
    if plan:
        if plan.get('source_sha256') != digest(before):
            errors.append('기준 양식과 채움 명세의 원본 해시 불일치')
        replacements = plan.get('replacements', [])
        anchor = plan.get('body', {}).get('anchor')
        if anchor:
            anchor_count = sum(''.join(e.itertext()).count(anchor)
                               for name, rt in a.items() if 'section' in name
                               for e in rt.iter() if local(e.tag) == 't')
            if anchor_count != 1:
                errors.append('본문 표시자는 문서 전체에 정확히 하나여야 함')
        for name in sorted(set(a) & set(b)):
            if 'section' not in name:
                continue
            old, new = list(a[name]), list(b[name])
            matching = [i for i, p in enumerate(old) if anchor and anchor in ''.join(p.itertext())]
            if len(matching) == 1:
                index = matching[0]
                # Only a plain top-level content slot is covered by this strict mode.
                candidate = old[index]
                visible = ''.join(''.join(e.itertext()) for e in candidate.iter() if local(e.tag) == 't')
                allowed = {'p', 'run', 't', 'linesegarray', 'lineseg', 'ctrl', 'colPr', 'colLine'}
                if visible != anchor or any(local(e.tag) not in allowed for e in candidate.iter()):
                    errors.append(f'{name}: 본문 표시자 문단에 고정 문구나 개체가 있음. 별도 검토 필요')
                    continue
                prefix, suffix = old[:index], old[index+1:]
                pairs = list(zip(prefix, new[:len(prefix)]))
                if suffix:
                    pairs += list(zip(suffix, new[-len(suffix):]))
                if len(new) < len(prefix)+len(suffix):
                    errors.append(f'{name}: 고정 문단 누락')
            elif not matching:
                pairs = list(zip(old, new))
                if len(old) != len(new):
                    errors.append(f'{name}: 지정 본문 밖 문단 수 변경')
            else:
                errors.append(f'{name}: 본문 표시자 중복')
                continue
            for index, (p, q) in enumerate(pairs):
                if protected_paragraph(p, replacements) != protected_paragraph(q, []):
                    if protected_paragraph(without_table_height_cache(p), replacements) != protected_paragraph(without_table_height_cache(q), []):
                        errors.append(f'{name}: 지정 본문 밖 고정 문단 {index} 변경')
    return {'status': 'FAIL' if errors else ('NATIVE_LAYOUT_REVIEW_REQUIRED' if cache_reviews else 'PASS'), 'baseline_sha256': digest(before),
            'output_sha256': digest(after), 'errors': errors,
            'native_layout_review_required': cache_reviews,
            'protected_content_checked': bool(plan),
            'scope': '용지·다단·표/셀 속성·스타일/번호 정의·바이너리. --plan이 있으면 지정 본문 밖 문단도 대조. 본문 의미·인쇄 모양은 별도 검토'}


def check(spec):
    errors = []
    if 'textbook_reference' in spec:
        errors.extend(check_textbook(spec))
    items = spec.get('items', [])
    if not items:
        errors.append('문항 없음')
    seen = set()
    total = 0
    for item in items:
        name = item.get('id', '?')
        if name in seen:
            errors.append(f'{name}: 중복 ID')
        seen.add(name)
        for key in ('id', 'stem', 'objective', 'derivation', 'answer', 'source_status', 'shortest_solution'):
            if not item.get(key):
                errors.append(f'{name}: {key} 누락')
        points = item.get('points')
        if type(points) not in (int, float) or points <= 0:
            errors.append(f'{name}: 양수 배점 필요')
            points = 0
        total += points
        if item.get('type') == 'mc':
            choices = item.get('choices', [])
            ids = [c.get('id') for c in choices]
            review = item.get('option_review', [])
            if len(ids) < 2 or len(ids) != len(set(ids)):
                errors.append(f'{name}: 선택지 ID 오류')
            if sorted(str(x) for x in ids) != sorted(str(r.get('id')) for r in review):
                errors.append(f'{name}: 전체 선택지 검토 누락/중복')
            correct = [r.get('id') for r in review if r.get('satisfies_prompt') is True]
            answer = item.get('answer', [])
            if not isinstance(answer, list) or set(answer) != set(correct) or not answer:
                errors.append(f'{name}: 정답과 보기 판정 불일치')
            if item.get('selection', 'single') == 'single' and len(correct) != 1:
                errors.append(f'{name}: 단일 선택의 정답 개수 오류')
            for r in review:
                if type(r.get('satisfies_prompt')) is not bool or not r.get('reason'):
                    errors.append(f'{name}: 선택지 근거 누락')
                if r.get('satisfies_prompt') is False and not r.get('misconception'):
                    errors.append(f'{name}: 오답 진단 근거 누락')
        elif item.get('type') == 'essay':
            rubric = item.get('rubric', [])
            if sum(r.get('points', 0) for r in rubric) != points:
                errors.append(f'{name}: 부분점수 합계 오류')
            if not rubric or any(not r.get('criterion') or not r.get('evidence') or r.get('points', 0) <= 0 for r in rubric):
                errors.append(f'{name}: 채점 항목/근거 누락')
            for key in ('answer_only', 'alternative_answers', 'partial_combinations'):
                if not item.get(key):
                    errors.append(f'{name}: {key} 누락')
        else:
            errors.append(f'{name}: 문항 유형 오류')
        shortcuts = item.get('shortcut_tests', [])
        if {s.get('id') for s in shortcuts} != set(range(1, 8)) or len(shortcuts) != 7:
            errors.append(f'{name}: 우회 점검 7개 필요')
        for s in shortcuts:
            disposition = s.get('resolution', {})
            resolved_reclassification = (s.get('result') == 'found' and
                disposition.get('decision') == 'reclassified' and
                item.get('assessment_role') == 'support' and
                disposition.get('reason') and disposition.get('new_level') == '중')
            if (s.get('result') not in ('blocked', 'not_applicable') and not resolved_reclassification) or not s.get('evidence'):
                errors.append(f'{name}: 우회로 미해결 또는 근거 누락')
            if item.get('type') == 'mc' and s.get('id') in (1, 2) and s.get('result') == 'not_applicable':
                errors.append(f'{name}: 선택형 필수 우회 점검 생략')
    if total != spec.get('total_points'):
        errors.append('시험 총점 불일치')
    if not spec.get('cross_item_review'):
        errors.append('문항 간 답 노출 검토 누락')
    return {'status': 'PASS' if not errors else 'FAIL', 'items': len(items), 'total_points': total,
            'errors': errors, 'scope': '기록·합계·답 연결 검사. 교과 내용·난도·인쇄 검증은 아님'}


def string_array(value, empty=True):
    return (isinstance(value, list) and (empty or bool(value))
            and all(isinstance(v, str) for v in value))


def check_textbook(spec):
    errors = []
    refs = spec['textbook_reference']
    if (not isinstance(refs, list) or not refs or any(
            not isinstance(r, dict) or not isinstance(r.get('id'), str)
            or not string_array(r.get('units')) for r in refs)):
        errors.append('textbook_reference는 {"id", "units"} 항목의 배열이어야 합니다.')
    for item in spec.get('items', []):
        name = item.get('id', '?')
        use = item.get('textbook_use')
        if not isinstance(use, dict):
            errors.append(f'{name}: textbook_reference가 있으면 textbook_use가 필요합니다.')
            continue
        for key in ('terms_checked', 'definitions_applied', 'scope_check',
                    'design_origin', 'differs_from_textbook'):
            value = use.get(key)
            valid = (string_array(value, key == 'definitions_applied')
                     if key in ('terms_checked', 'definitions_applied')
                     else isinstance(value, str) and bool(value.strip()))
            if not valid:
                errors.append(f'{name}: textbook_use.{key}가 비어 있거나 형식이 맞지 않습니다.')
        if 'overlap_resolution' in use and not isinstance(use['overlap_resolution'], str):
            errors.append(f'{name}: textbook_use.overlap_resolution가 비어 있거나 형식이 맞지 않습니다.')
    return errors


def normalize_text(text, question=False):
    text = unicodedata.normalize('NFKC', text).replace('−', '-')
    text = text.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))
    if question:
        text = re.sub(r'(?<![a-z])(?:over|rm|times|left|right|sqrt|deg)(?![a-z])', '', text)
        text = text.translate(str.maketrans('', '', '~`{}'))
    return ''.join(text.split())


def substring_index(text):
    # Suffix automaton: linear construction and matching, even for repeated text.
    edges, links, lengths = [{}], [-1], [0]
    last = 0
    for char in text:
        current = len(edges)
        edges.append({})
        lengths.append(lengths[last] + 1)
        links.append(0)
        parent = last
        while parent >= 0 and char not in edges[parent]:
            edges[parent][char] = current
            parent = links[parent]
        if parent >= 0:
            target = edges[parent][char]
            if lengths[parent] + 1 == lengths[target]:
                links[current] = target
            else:
                clone = len(edges)
                edges.append(edges[target].copy())
                lengths.append(lengths[parent] + 1)
                links.append(links[target])
                while parent >= 0 and edges[parent].get(char) == target:
                    edges[parent][char] = clone
                    parent = links[parent]
                links[target] = links[current] = clone
        last = current
    return edges, links, lengths


def longest_span(index, text):
    edges, links, lengths = index
    state = size = best = end = 0
    for position, char in enumerate(text):
        while state and char not in edges[state]:
            state = links[state]
            size = lengths[state]
        if char in edges[state]:
            state = edges[state][char]
            size += 1
        else:
            size = 0
        if size > best:
            best, end = size, position + 1
    return best, text[end-best:end][:30]


def grams(text):
    text = re.sub(r'\d', '0', text)
    return {text[i:i+4] for i in range(len(text)-3)}


def overlap_items(spec):
    for item in spec.get('items', []):
        text = item.get('stem', '') + '\n' + '\n'.join(c.get('text', '') for c in item.get('choices', []))
        for paragraph in item.get('presentation', {}).get('student', []):
            if paragraph.get('textbook_passage') is True and paragraph.get('text'):
                text = text.replace(paragraph['text'], '')
        yield item.get('id', '?'), text, item.get('textbook_use', {}).get('overlap_resolution')
    for number, passage in enumerate(spec.get('passages', []), 1):
        if passage.get('textbook_passage') is not True:
            yield f'PASSAGE:{number}', passage.get('text', ''), passage.get('textbook_use', {}).get('overlap_resolution')


def overlap(spec, paths, min_span=20, example_ratio=0.5):
    entries = []
    for path in paths:
        try:
            source = load(path)
            if not isinstance(source, dict) or not isinstance(source.get('entries'), list):
                raise ValueError()
            for entry in source['entries']:
                if not isinstance(entry, dict):
                    raise ValueError()
                if entry.get('kind') not in ('definition', 'summary', 'property', 'passage', 'example'):
                    continue
                text = entry.get('text', '')
                if not isinstance(text, str):
                    raise ValueError()
                if text:
                    normalized = normalize_text(text)
                    entries.append((entry, substring_index(normalized), grams(normalized)))
        except (OSError, ValueError, TypeError, KeyError):
            return {'status': 'FAIL', 'error': f'정리본을 읽을 수 없습니다: {path}'}
    items = []
    for name, raw, resolution in overlap_items(spec):
        text = normalize_text(raw, question=True)
        question_grams = grams(text)
        blank = re.search(r'\(\s*\)|□|_{3,}|빈칸', unicodedata.normalize('NFKC', raw))
        flags = []
        for entry, index, example_grams in entries:
            span, excerpt = longest_span(index, text)
            common = {k: entry[k] for k in ('pdf_page', 'pdf_pages') if k in entry}
            common.update(entry_id=entry['id'], kind=entry['kind'], label=entry.get('label'), excerpt=excerpt)
            if span >= min_span:
                flags.append({'type': 'COPY_SPAN', **common, 'span': span})
            if entry['kind'] == 'example' and example_grams:
                ratio = len(example_grams & question_grams) / len(example_grams)
                if ratio >= example_ratio:
                    flags.append({'type': 'EXAMPLE_REUSE', **common, 'ratio': ratio})
            if blank and entry['kind'] in ('definition', 'summary', 'property') and span >= 12:
                flags.append({'type': 'DEFINITION_BLANK', **common, 'span': span})
        items.append({'id': name, 'flags': flags, 'resolution': resolution})
    return {'status': 'FLAGGED' if any(i['flags'] for i in items) else 'PASS',
            'digests': [str(p) for p in paths], 'items': items}


def packet(spec):
    lines = [spec.get('title', '학생용 검토'), '']
    for passage in spec.get('passages', []):
        lines.extend([passage['title'], passage['text'], ''])
    for item in spec['items']:
        lines.extend([f"{item['id']}. {item['stem']} [{item['points']}점]"])
        lines.extend(f"{c['id']} {c['text']}" for c in item.get('choices', []))
        lines.append('')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['inspect', 'compare', 'check', 'packet', 'overlap'])
    parser.add_argument('source')
    parser.add_argument('target', nargs='?')
    parser.add_argument('--digest', action='append', default=[])
    parser.add_argument('--min-span', type=int, default=20)
    parser.add_argument('--example-ratio', type=float, default=0.5)
    parser.add_argument('--plan', help='승인된 치환과 본문 위치를 담은 채움 명세')
    args = parser.parse_args()
    if args.mode == 'inspect':
        result = inspect(args.source)
    elif args.mode == 'compare':
        if not args.target: parser.error('비교할 파일이 필요합니다.')
        result = compare(args.source, args.target, load(args.plan) if args.plan else None)
    elif args.mode == 'check':
        result = check(load(args.source))
    elif args.mode == 'overlap':
        if not args.digest:
            parser.error('overlap에는 --digest로 정리본 원문.json 경로가 필요합니다.')
        result = overlap(load(args.source), args.digest, args.min_span, args.example_ratio)
    else:
        if not args.target: parser.error('학생용 출력 경로가 필요합니다.')
        target = Path(args.target)
        if target.exists(): raise ValueError('기존 출력은 덮어쓰지 않습니다.')
        target.write_text(packet(load(args.source)), encoding='utf-8')
        result = {'status': 'PACKET_CREATED', 'source_sha256': digest(args.source), 'packet_sha256': digest(target)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get('status') == 'FLAGGED':
        return int(any(i['flags'] and not (isinstance(i['resolution'], str) and i['resolution'].strip())
                       for i in result['items']))
    return 1 if result.get('status') == 'FAIL' else (2 if result.get('status') == 'NATIVE_LAYOUT_REVIEW_REQUIRED' else 0)


if __name__ == '__main__':
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(json.dumps({'status': 'FAIL', 'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)
