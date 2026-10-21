"""Prepare, compile, export and audit exams using only this skill and Python stdlib.

The agent authors content and a bounded layout map; it never writes per-run code.
Native export requires Windows PowerShell and Hancom Hangul.
"""
import argparse
import copy
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

import exam_check as C
from wrap_boxes import NS, signature, expected

HERE = Path(__file__).resolve().parent
ANCHOR = '{{BODY}}'


def save(path, value):
    path = Path(path)
    if path.exists():
        raise ValueError('기존 결과를 보존했습니다: ' + str(path))
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def native(source, output, plan=None, pdf=None):
    command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
               str(HERE/'hancom_fill.ps1'), '-Source', str(source), '-Output', str(output),
               '-Receipt', str(output.with_suffix('.native.json')), '-Python', sys.executable]
    if plan:
        command += ['-Spec', str(plan)]
    if pdf:
        command += ['-Pdf', str(pdf)]
    result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise ValueError('한글 실행 실패: ' + result.stderr[-3000:] + result.stdout[-1000:])
    return C.load(output.with_suffix('.native.json'))


def roots(path):
    with zipfile.ZipFile(path) as z:
        return {n: ET.fromstring(z.read(n)) for n in z.namelist()
                if re.fullmatch(r'Contents/(header|section\d+)\.xml', n)}


def inspect(path):
    result = C.inspect(path)
    result['paragraphs'] = {}
    for name, root in roots(path).items():
        if 'section' not in name:
            continue
        result['paragraphs'][name] = [
            {'index': i, 'style_id': p.get('styleIDRef'), 'text': signature(p)[0],
             'controls': [C.local(x.tag) for x in p.iter()
                          if C.local(x.tag) in ('secPr', 'colPr', 'header', 'footer', 'tbl')],
             'tables': [{'id': x.get('id'), 'text': signature(x)[0][:300]}
                        for x in p.iter() if C.local(x.tag) == 'tbl']}
            for i, p in enumerate(root)]
    return result


def write_xml_copy(source, output, changed):
    if output.exists():
        raise ValueError('기존 파일을 덮어쓰지 않습니다.')
    for root in changed.values():
        for parent in root.iter():
            for child in list(parent):
                if C.local(child.tag) == 'linesegarray':
                    parent.remove(child)
    with zipfile.ZipFile(source) as z, zipfile.ZipFile(output, 'w') as target:
        for entry in z.infolist():
            data = (ET.tostring(changed[entry.filename], encoding='utf-8', xml_declaration=True)
                    if entry.filename in changed else z.read(entry.filename))
            target.writestr(entry, data)


def paragraph_layout(paragraph):
    """Only the outer flow line, not changed text inside a table cell."""
    arrays = [x for x in paragraph if C.local(x.tag).lower() == 'linesegarray']
    return [[line.get(key) for key in ('vertpos','vertsize','spacing','horzpos','horzsize')]
            for array in arrays for line in array]


def preserve_single_cell_heights(paragraphs):
    """Convert a calculated auto height into the same visible minimum height.

    Pagination must precede saving the source. A short field replacement must
    not collapse a fixed notice box whose old cell minimum was much smaller.
    Multirow tables are left intact; the prefix-layout gate rejects any reflow.
    """
    locks = []
    for paragraph in paragraphs:
        for table in reversed(list(paragraph.iter())):
            if C.local(table.tag) != 'tbl':
                continue
            rows = [x for x in table if C.local(x.tag) == 'tr']
            cells = [x for row in rows for x in row if C.local(x.tag) == 'tc']
            if len(rows) != 1 or len(cells) != 1:
                continue
            size = next(x for x in table if C.local(x.tag) == 'sz')
            cell_size = next(x for x in cells[0] if C.local(x.tag) == 'cellSz')
            actual, minimum = int(size.get('height')), int(cell_size.get('height'))
            if actual > minimum:
                cell_size.set('height', str(actual))
                locks.append({'table_id':table.get('id'), 'old_minimum':minimum,
                              'source_rendered_height':actual, 'new_minimum':actual})
    return locks


def blank_paragraph(original, anchor):
    node = copy.deepcopy(original)
    # Keep paragraph/section/column properties; remove only authored body content.
    for run in list(node):
        if C.local(run.tag) != 'run':
            node.remove(run)
            continue
        for child in list(run):
            if C.local(child.tag) not in ('secPr', 'ctrl'):
                run.remove(child)
            elif C.local(child.tag) == 'ctrl':
                for ctrl in list(child):
                    if C.local(ctrl.tag) not in ('colPr', 'header', 'footer'):
                        child.remove(ctrl)
        if not len(run):
            node.remove(run)
    run = ET.SubElement(node, '{'+NS['hp']+'}run', {'charPrIDRef': '0'})
    ET.SubElement(run, '{'+NS['hp']+'}t').text = anchor
    return node


def teacher_styles(header, base_id):
    collections = {C.local(x.tag): x for x in header.iter()}
    chars, paras, styles = [collections[k] for k in ('charProperties', 'paraProperties', 'styles')]
    base = next(x for x in styles if x.get('id') == str(base_id))
    ids = {}
    for name, bold in [('teacher_body', False), ('teacher_heading', True)]:
        char = copy.deepcopy(next(x for x in chars if x.get('id') == base.get('charPrIDRef')))
        para = copy.deepcopy(next(x for x in paras if x.get('id') == base.get('paraPrIDRef')))
        style = copy.deepcopy(base)
        for node, collection in [(char, chars), (para, paras), (style, styles)]:
            node.set('id', str(max(int(x.get('id')) for x in collection)+1))
        if bold and not any(C.local(x.tag) == 'bold' for x in char):
            ET.SubElement(char, '{'+NS['hh']+'}bold')
        next(x for x in para if C.local(x.tag) == 'align').set('horizontal', 'LEFT')
        if bold:
            next(x for x in para if C.local(x.tag) == 'breakSetting').set('keepWithNext', '1')
        style.set('name', '해설 항목' if bold else '해설 본문')
        style.set('engName', name)
        style.set('charPrIDRef', char.get('id'))
        style.set('paraPrIDRef', para.get('id'))
        style.set('nextStyleIDRef', style.get('id'))
        chars.append(char); paras.append(para); styles.append(style)
        ids[name] = int(style.get('id'))
    for collection in (chars, paras, styles):
        collection.set('itemCnt', str(len(collection)))
    return ids


def prepare(source, layout_path, out):
    layout = C.load(layout_path)
    if C.digest(source) != layout['source_sha256']:
        raise ValueError('원본과 양식 위치표의 해시가 다릅니다.')
    out.mkdir(parents=True, exist_ok=False)
    normalized = out/'source-native.hwpx'
    native(source, normalized, pdf=out/'source-native.pdf')
    files = roots(normalized)
    section_name = layout.get('section', 'Contents/section0.xml')
    section = files[section_name]
    children = list(section)
    body = layout['body']
    start, end = body['start'], body['end']
    if not 0 <= start <= end < len(children):
        raise ValueError('본문 범위가 잘못됐습니다.')
    if 'expected_start_text' not in body or signature(children[start])[0] != body['expected_start_text']:
        raise ValueError('저장 후 본문 시작 문단이 위치표의 확인 문구와 다릅니다.')
    if not layout.get('body_range_reason') or not layout.get('teacher_layout_reason'):
        raise ValueError('확인한 본문 경계와 교사용 구성의 근거가 필요합니다.')
    original_prefix_layout = [paragraph_layout(p) for p in children[:start]]
    template_body = blank_paragraph(children[start], ANCHOR)
    for p in children[start:end+1]:
        section.remove(p)
    section.insert(start, template_body)
    retained = list(section)[:start]+list(section)[start+1:]
    locks = preserve_single_cell_heights(retained)
    # All retained content is protected; the layout map names only the body range.
    student_draft = out/'student-draft.hwpx'
    write_xml_copy(normalized, student_draft, {section_name: section})
    native(student_draft, out/'student-template.hwpx', pdf=out/'student-template.pdf')
    saved_student = roots(out/'student-template.hwpx')[section_name]
    old_prefix = original_prefix_layout
    new_prefix = [paragraph_layout(p) for p in list(saved_student)[:start]]
    if not all(old_prefix) or old_prefix != new_prefix:
        raise ValueError('학생용 기준본 준비 중 본문 앞 고정 영역의 실제 배치가 달라졌습니다.')
    teacher_files = roots(normalized)
    teacher_section = teacher_files[section_name]
    original = list(teacher_section)
    for p in original:
        teacher_section.remove(p)
    structural = copy.deepcopy(original[0])
    for run in list(structural):
        for child in list(run):
            if C.local(child.tag) == 'ctrl':
                for ctrl in list(child):
                    if C.local(ctrl.tag) != 'colPr':
                        child.remove(ctrl)
                if not len(child):
                    run.remove(child)
            elif C.local(child.tag) != 'secPr':
                run.remove(child)
        if not len(run):
            structural.remove(run)
    # Hangul ignores a section-only paragraph without its initial column control
    # and text terminator, silently replacing the supplied paper with defaults.
    structural[-1].append(ET.Element('{'+NS['hp']+'}t'))
    if not any(C.local(x.tag) == 'secPr' for x in structural.iter()):
        raise ValueError('첫 문단에서 구역 설정을 확인하지 못했습니다.')
    teacher_section.append(structural)
    keep = layout['teacher_keep']
    if len(keep) != len(set(keep)) or any(not 0 < i < start for i in keep):
        raise ValueError('교사용에는 본문 앞의 제목·머리말 문단만 지정합니다.')
    for i in keep:
        teacher_section.append(copy.deepcopy(original[i]))
    teacher_section.append(blank_paragraph(original[start], ANCHOR))
    ids = teacher_styles(teacher_files['Contents/header.xml'], layout['styles']['body'])
    teacher_draft = out/'teacher-draft.hwpx'
    write_xml_copy(normalized, teacher_draft, {
        section_name: teacher_section, 'Contents/header.xml': teacher_files['Contents/header.xml']})
    native(teacher_draft, out/'teacher-template.hwpx', pdf=out/'teacher-template.pdf')
    teacher_saved = roots(out/'teacher-template.hwpx')[section_name]
    geometry = lambda root: [C.canonical(x) for x in root.iter() if C.local(x.tag) == 'pagePr']
    if geometry(teacher_saved) != geometry(teacher_files[section_name]):
        raise ValueError('교사용 기준본 저장 중 원본 용지·여백이 달라졌습니다.')
    receipt = {'status': 'TEMPLATES_PREPARED_REVIEW_REQUIRED', 'source': str(source.resolve()),
               'source_sha256': C.digest(source), 'layout_sha256': C.digest(layout_path),
               'teacher_styles': ids, 'section': section_name,
               'fixed_height_locks':locks, 'student_prefix_layout':'PASS',
               'student_sha256': C.digest(out/'student-template.hwpx'),
               'teacher_sha256': C.digest(out/'teacher-template.hwpx')}
    save(out/'templates.json', receipt)
    return receipt


def paragraph(text, style, rich=None):
    if '\n' in text or '\r' in text:
        raise ValueError('한 문단 안에 줄바꿈을 넣지 않습니다.')
    block = {'style_id': int(style), 'text': text}
    if rich is not None:
        block['runs'] = copy.deepcopy(rich)
    return block


def compile_plan(content, content_path, layout, template, role, teacher_ids):
    styles = dict(layout['styles'], **teacher_ids)
    is_teacher = role == 'teacher'
    base_style = styles['teacher_body'] if is_teacher else styles['body']
    blocks, boxes = [], {}
    rich = content.get('rich_text', {})

    def add(text, style=None):
        for line in str(text).split('\n'):
            blocks.append(paragraph(line, base_style if style is None else style, rich.get(line)))

    def gap():
        if blocks and blocks[-1].get('text') != '':
            add('')

    def parts(parts_list, prefix, question_style=None):
        active = None
        for i, part in enumerate(parts_list):
            kind = part.get('kind', 'body')
            text = part.get('text', '')
            if kind not in ('question', 'body', 'passage', 'condition', 'code', 'image'):
                raise ValueError('지원하지 않는 문단 종류: '+kind)
            # A passage is already wrapped in a real table. Reuse the existing
            # plain body style so a legacy bordered passage style cannot add
            # a second frame inside that table. Never alter source styles.
            style = question_style if kind == 'question' else base_style
            b = paragraph(text, style, part.get('runs', rich.get(text)))
            if kind == 'image':
                b['image'] = copy.deepcopy(part['image'])
            group = part.get('group', kind)
            if kind in ('condition', 'code', 'passage'):
                gid = prefix+'-'+group
                if active != gid:
                    gap()
                b['box_id'] = gid
                b.pop('runs', None) if kind == 'code' else None
                if kind == 'code':
                    b['code'] = True
                if gid not in boxes:
                    boxes[gid] = {'caption': part.get('caption', {'condition':'조건', 'code':'코드', 'passage':'자료'}[kind])}
                elif part.get('caption'):
                    boxes[gid]['caption'] = part['caption']
                if kind == 'code':
                    boxes[gid]['caption_width_mm'] = 32
                active = gid
            else:
                active = None
            blocks.append(b)

    if not is_teacher:
        for index, passage in enumerate(content.get('passages', [])):
            parts([{'kind':'passage','caption':passage['title'],'text':line}
                   for line in passage['text'].split('\n')], 'common-'+str(index))
    for index, item in enumerate(content['items']):
        gap()
        presentation = item.get('presentation', {})
        if not is_teacher:
            source_parts = presentation.get('student')
            if not source_parts or source_parts[0].get('kind') != 'question':
                raise ValueError(item['id']+': 학생용 구조의 첫 문단은 question이어야 합니다.')
            if '\n'.join(p.get('text','') for p in source_parts if p.get('kind') != 'image') != item['stem']:
                raise ValueError(item['id']+': 학생용 구조와 검토한 stem이 다릅니다.')
            parts_list = copy.deepcopy(source_parts)
            first = parts_list[0]
            suffix = ' ['+str(item['points'])+'점]'
            first['text'] += suffix
            if first.get('runs'):
                first['runs'].append({'kind':'text','text':suffix})
            elif item['stem'].split('\n')[0] in rich:
                first['runs'] = copy.deepcopy(rich[item['stem'].split('\n')[0]])+[{'kind':'text','text':suffix}]
            start = len(blocks)
            parts(parts_list, item['id'], styles['mc'] if item['type']=='mc' else styles['essay'])
            if index and presentation.get('break_before'):
                blocks[start]['break_before'] = presentation['break_before']
            for choice in item.get('choices', []):
                text = chr(0x2460+int(choice['id'])-1)+' '+choice['text']
                add(text, styles['choice'])
                if choice['text'] in rich:
                    blocks[-1]['runs'] = [{'kind':'text','text':text[:2]}]+copy.deepcopy(rich[choice['text']])
            for _ in range(presentation.get('answer_lines', 0)):
                add('')
        else:
            start = len(blocks)
            add('정답 '+('·'.join(chr(0x2460+int(x)-1) for x in item['answer']) if item['type']=='mc' else '및 해설'),
                styles['mc'] if item['type']=='mc' else styles['essay'])
            if index and presentation.get('teacher_break_before'):
                blocks[start]['break_before'] = presentation['teacher_break_before']
            if item['type'] == 'essay':
                add('예시 답안', styles['teacher_heading'])
                answer_parts = presentation.get('answer')
                if answer_parts:
                    if '\n'.join(p.get('text','') for p in answer_parts) != item['answer']:
                        raise ValueError(item['id']+': 교사 답안 구조와 정답 원고가 다릅니다.')
                    parts(answer_parts, item['id']+'-answer')
                else:
                    add(item['answer'])
            gap(); add('해설', styles['teacher_heading'])
            derivation_parts = presentation.get('derivation')
            if derivation_parts:
                if '\n'.join(p.get('text','') for p in derivation_parts) != item['derivation']:
                    raise ValueError(item['id']+': 해설 구조와 추론 원고가 다릅니다.')
                parts(derivation_parts, item['id']+'-derivation')
            else:
                add(item['derivation'])
            if item['type'] == 'mc':
                gap(); add('선택지 풀이', styles['teacher_heading'])
                for review in item['option_review']:
                    add(chr(0x2460+int(review['id'])-1)+' '+review['reason'])
            else:
                gap()
                parts([{'kind':'condition','caption':'채점','text':str(r['points'])+'점 | '+r['criterion']+' — '+r['evidence']}
                       for r in item['rubric']], item['id']+'-grading')
                for heading, field in [('인정 답안','alternative_answers'),('부분점수','partial_combinations'),('답만 쓴 경우','answer_only')]:
                    gap(); add(heading, styles['teacher_heading']); add(item[field])
                add('평가 요소별 점수를 합산하며 같은 오류를 중복 감점하지 않는다.')
    printed = '\n'.join(b['text'] for b in blocks)
    if re.search(r'창작 검증 예시|중상 예상|제공 PDF.*발췌|\b(?:MC|E)\d+\b', printed):
        raise ValueError('인쇄 본문에 제작용 메타 설명이나 내부 문항 ID가 남았습니다.')
    texts = '\n'.join(x.text or '' for root in roots(template).values()
                      for x in root.iter() if C.local(x.tag)=='t')
    values = content.get('metadata', {}).get(role, {})
    replacements = [{'find': key, 'replace': str(value), 'count': texts.count(key)}
                    for key, value in values.items() if key in texts]
    unresolved = set(re.findall(r'\{\{[^{}]+\}\}', texts))-{ANCHOR}-set(values)
    if unresolved:
        raise ValueError('미기입 시험 정보: '+', '.join(sorted(unresolved)))
    return {'source_sha256': C.digest(template), 'content_source_sha256': C.digest(content_path),
            'replacements': replacements, 'body': {'anchor':ANCHOR,'blocks':blocks,'boxes':boxes,
                                                   'box_width_mm':layout.get('box_width_mm')}}


def audit(template, output, plan):
    fixed = C.compare(template, output, plan)
    if fixed['status'] == 'FAIL':
        raise ValueError('고정 양식 변경: '+json.dumps(fixed, ensure_ascii=False))
    before, after = roots(template), roots(output)
    matches = [(name, *C.body_range(root, ANCHOR)) for name, root in before.items()
               if 'section' in name and any(ANCHOR in signature(p)[0] for p in root)]
    if len(matches) != 1:
        raise ValueError('본문 표시자는 하나여야 합니다.')
    name, offset, suffix = matches[0]
    children = list(after[name]); body = children[offset:len(children)-suffix if suffix else len(children)]
    old_prefix = [paragraph_layout(p) for p in list(before[name])[:offset]]
    new_prefix = [paragraph_layout(p) for p in children[:offset]]
    if not all(old_prefix) or old_prefix != new_prefix:
        raise ValueError('시험정보/본문 입력 후 고정 영역의 줄 위치·높이·쪽 흐름이 달라졌습니다.')
    flat = []
    for p in body:
        tables = [x for x in p.iter() if C.local(x.tag)=='tbl']
        if not tables:
            flat.append(p); continue
        if len(tables) != 1:
            raise ValueError('중첩/추가 표가 있습니다.')
        cell = next(x for x in tables[0].iter() if C.local(x.tag)=='tc'
                    and x.find('hp:cellAddr',NS).get('rowAddr')=='3'
                    and x.find('hp:cellAddr',NS).get('colAddr')=='0')
        flat.extend(cell.find('hp:subList',NS))
    if [signature(p) for p in flat] != [expected(b) for b in plan['body']['blocks']]:
        raise ValueError('최종 상자 안팎 문단·수식·코드가 원고와 다릅니다.')
    whole = '\n'.join(signature(root)[0] for root in after.values())
    if re.search(r'\{\{[^{}]+\}\}', whole):
        raise ValueError('미치환 표시자가 남았습니다.')
    return {'content_match':'PASS','fixed_prefix_layout':'PASS','paragraphs':len(flat),
            'equations':sum(len(signature(p)[1]) for p in flat),
            'code_lines':sum(bool(b.get('code')) for b in plan['body']['blocks']),
            'boxes':len(plan['body']['boxes']), 'template':fixed}


def run(content_path, layout_path, templates, out):
    content, layout = C.load(content_path), C.load(layout_path)
    quality = C.check(content)
    if quality['status'] != 'PASS':
        raise ValueError('원고 검토 실패: '+json.dumps(quality, ensure_ascii=False))
    prepared = C.load(templates/'templates.json')
    if prepared['layout_sha256'] != C.digest(layout_path):
        raise ValueError('준비 후 양식 위치표가 달라졌습니다.')
    if C.digest(prepared['source']) != prepared['source_sha256']:
        raise ValueError('원본 양식이 준비 후 달라졌습니다.')
    out.mkdir(parents=True, exist_ok=False)
    records = []
    for role, label in [('student','학생용'),('teacher','교사용')]:
        template = templates/(role+'-template.hwpx')
        if C.digest(template) != prepared[role+'_sha256']:
            raise ValueError('준비한 기준 양식이 달라졌습니다.')
        plan = compile_plan(content, content_path, layout, template, role, prepared['teacher_styles'])
        spec = out/(label+'-채움.json'); save(spec, plan)
        # Separate authorized field reflow from the effect of authored body
        # insertion. Keep the original template comparison as well.
        field_plan = {'source_sha256': C.digest(template),
                      'replacements': copy.deepcopy(plan['replacements'])}
        field_spec = out/(label+'-시험정보기준.json')
        save(field_spec, field_plan)
        field_hwpx = out/(label+'-시험정보기준.hwpx')
        field_pdf = out/(label+'-시험정보기준.pdf')
        field_receipt = native(template, field_hwpx, field_spec, field_pdf)
        output, pdf = out/(label+'.hwpx'), out/(label+'.pdf')
        receipt = native(template, output, spec, pdf)
        declared_pages = content.get('metadata', {}).get(role, {}).get('{{PAGES}}')
        if str(declared_pages).isdecimal() and int(declared_pages) != receipt['pages']:
            raise ValueError(label+' 실제 '+str(receipt['pages'])+'쪽과 원고의 전체 쪽수 '+str(declared_pages)+'가 다릅니다. 쪽수 정보를 고쳐 다시 출력하세요.')
        checked = audit(template, output, plan)
        records.append({'role':role,'hwpx':str(output.resolve()),'pdf':str(pdf.resolve()),
                        'hwpx_sha256':C.digest(output),'pdf_sha256':C.digest(pdf),
                        'pages':receipt['pages'], 'audit':checked,
                        'authorized_fields_baseline': {
                            'hwpx':str(field_hwpx.resolve()), 'pdf':str(field_pdf.resolve()),
                            'hwpx_sha256':C.digest(field_hwpx), 'pdf_sha256':C.digest(field_pdf),
                            'pages':field_receipt['pages'],
                            'scope':'승인된 시험정보만 치환. 본문 미삽입. 원본 비교를 대체하지 않음'}})
    result = {'status':'EXPORTED_REVIEW_REQUIRED','content_sha256':C.digest(content_path),
              'item_ids':[item['id'] for item in content['items']],
              'layout_sha256':C.digest(layout_path),'source_sha256':prepared['source_sha256'],
              'source':prepared['source'],
              'quality_record_check':quality,'files':records}
    save(out/'실행결과.json', result)
    return result


def render(receipt_path, out):
    receipt = C.load(receipt_path); out.mkdir(parents=True, exist_ok=False)
    images = []
    render_records = list(receipt['files'])
    for record in receipt['files']:
        if record.get('authorized_fields_baseline'):
            render_records.append(dict(record['authorized_fields_baseline'],
                                       role=record['role']+'-fields-baseline'))
    for record in render_records:
        if C.digest(record['pdf']) != record['pdf_sha256']:
            raise ValueError('검수할 PDF가 달라졌습니다.')
        for page in range(1, record['pages']+1):
            image = out/(record['role']+'-'+str(page)+'.png')
            command = ['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(HERE/'pdf_crop.ps1'),
                       '-Source',record['pdf'],'-Page',str(page),
                       '-Dpi','110','-Output',str(image)]
            completed = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace')
            if completed.returncode:
                raise ValueError('PDF 미리보기 실패: '+completed.stderr[-2000:])
            images.append({'pdf_sha256':record['pdf_sha256'],'role':record['role'],'page':page,
                           'image':str(image.resolve()),'sha256':C.digest(image)})
    save(out/'pages.json', images)
    return {'status':'PAGES_RENDERED_REVIEW_PENDING','pages':len(images)}


def finalize(receipt_path, review_path, output):
    receipt, review = C.load(receipt_path), C.load(review_path)
    if review.get('receipt_sha256') != C.digest(receipt_path):
        raise ValueError('현재 실행결과에 연결된 검수 기록이 필요합니다.')
    if C.digest(receipt['source']) != receipt['source_sha256']:
        raise ValueError('검수 중 원본 양식이 달라졌습니다.')
    for record in receipt['files']:
        baseline = record.get('authorized_fields_baseline')
        if baseline:
            for kind in ('pdf', 'hwpx'):
                if C.digest(baseline[kind]) != baseline[kind+'_sha256']:
                    raise ValueError('시험정보만 치환한 대조 기준본이 변경됐습니다.')
        for kind in ('pdf','hwpx'):
            if C.digest(record[kind]) != record[kind+'_sha256']:
                raise ValueError('검수 후 파일이 변경됐습니다.')
        if record['audit'].get('fixed_prefix_layout') != 'PASS':
            raise ValueError('현재 도구의 원본 고정 영역 배치 검사를 통과한 결과가 필요합니다.')
        role_review = review.get(record['role'], {})
        pages = role_review.get('pages', [])
        if len(pages) != record['pages'] or {p['page'] for p in pages} != set(range(1,record['pages']+1)):
            raise ValueError('모든 쪽의 개별 시각 검수가 필요합니다.')
        if any(p.get('status') != 'PASS' or not p.get('evidence') or p.get('pdf_sha256') != record['pdf_sha256'] for p in pages):
            raise ValueError('쪽별 검수 근거/현재 PDF 해시가 부족합니다.')
        if record['audit']['template']['status'] != 'PASS':
            proof = role_review.get('fixed_geometry_proof', {})
            if not proof.get('path') or C.digest(proof['path']) != proof.get('sha256') or not proof.get('finding'):
                raise ValueError('양식 높이 재계산은 원본/최종 PDF의 실제 고정선 비교 증거가 필요합니다.')
    item_reviews = review.get('item_review', [])
    if (len(item_reviews) != len(receipt['item_ids']) or
        {item.get('id') for item in item_reviews} != set(receipt['item_ids']) or
        any(not item.get(key) for item in item_reviews for key in ('answer_check','difficulty_check','rubric_check')) or
        not review.get('limits')):
        raise ValueError('문항별 정답·풀이·난도 재검토와 검증 한계를 남기세요.')
    result = dict(receipt, status='VERIFIED_WITH_DECLARED_LIMITS', review_sha256=C.digest(review_path),
                  limits=review['limits'])
    save(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p=sub.add_parser('inspect'); p.add_argument('source',type=Path)
    p=sub.add_parser('prepare'); p.add_argument('--source',type=Path,required=True); p.add_argument('--layout',type=Path,required=True); p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('run'); p.add_argument('--content',type=Path,required=True); p.add_argument('--layout',type=Path,required=True); p.add_argument('--templates',type=Path,required=True); p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('render'); p.add_argument('--receipt',type=Path,required=True); p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('finalize'); p.add_argument('--receipt',type=Path,required=True); p.add_argument('--review',type=Path,required=True); p.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='inspect': result=inspect(args.source)
    elif args.command=='prepare': result=prepare(args.source,args.layout,args.out)
    elif args.command=='run': result=run(args.content,args.layout,args.templates,args.out)
    elif args.command=='render': result=render(args.receipt,args.out)
    else: result=finalize(args.receipt,args.review,args.out)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass
    try:
        main()
    except (ValueError, KeyError, StopIteration, OSError) as error:
        print(json.dumps({'status':'FAIL','error':str(error)},ensure_ascii=False),file=sys.stderr)
        sys.exit(1)
