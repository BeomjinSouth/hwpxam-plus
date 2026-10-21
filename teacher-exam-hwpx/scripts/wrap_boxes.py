"""Wrap native-authored paragraphs in a preserved condition-box table. Stdlib only."""
import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

NS = {"hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
      "hh": "http://www.hancom.co.kr/hwpml/2011/head",
      "hc": "http://www.hancom.co.kr/hwpml/2011/core",
      "hs": "http://www.hancom.co.kr/hwpml/2011/section"}
for key, value in NS.items():
    ET.register_namespace(key, value)


def local(node):
    return node.tag.rsplit("}", 1)[-1]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(node):
    value = copy.deepcopy(node)
    value.attrib.pop("id", None)
    return ET.tostring(value, encoding="unicode")


def signature(node):
    return ("".join(x.text or "" for x in node.iter() if local(x) == "t"),
            [x.text for x in node.iter() if local(x) == "script"])


def expected(block):
    if block.get("runs"):
        return ("".join(x.get("text", "") for x in block["runs"] if x["kind"] == "text"),
                [x["script"] for x in block["runs"] if x["kind"] == "equation"])
    return block.get("text", ""), []


def resize_caption(table, width_mm):
    if width_mm is None:
        return
    width = round(float(width_mm)*7200/25.4)
    total = int(table.find("hp:sz", NS).get("width"))
    if not 2000 <= width <= total//2:
        raise ValueError("상자 제목 폭이 잘못됐습니다.")
    first_row = table.find("hp:tr", NS)
    old = int(list(first_row)[1].find("hp:cellSz", NS).get("width"))
    delta = width-old
    for cell in table.iter():
        if local(cell) != "tc":
            continue
        addr = cell.find("hp:cellAddr", NS)
        if int(addr.get("rowAddr")) > 2:
            continue
        size = cell.find("hp:cellSz", NS)
        col = addr.get("colAddr")
        size.set("width", str(width if col == "1" else int(size.get("width"))-(delta//2 if col == "0" else delta-delta//2)))


def resize_table(table, width_mm):
    if width_mm is None:
        return
    if not 45 <= float(width_mm) <= 300:
        raise ValueError("확인한 상자 폭은 45~300mm 범위여야 합니다.")
    size = table.find("hp:sz", NS)
    old, new = int(size.get("width")), round(float(width_mm)*7200/25.4)
    # Preserve merged-cell boundaries exactly after integer rounding.
    row = table.find("hp:tr", NS)
    boundaries = [0]
    for cell in row:
        boundaries.append(boundaries[-1]+int(cell.find("hp:cellSz", NS).get("width")))
    scaled = [round(value*new/old) for value in boundaries]
    for cell in table.iter():
        if local(cell) != 'tc':
            continue
        col = int(cell.find('hp:cellAddr', NS).get('colAddr'))
        span = int(cell.find('hp:cellSpan', NS).get('colSpan'))
        cell.find('hp:cellSz', NS).set('width', str(scaled[col+span]-scaled[col]))
    size.set('width', str(new))


def apply(source, output, template, plan, profile_path):
    if output.exists() or source.resolve() == output.resolve():
        raise ValueError("기존 파일은 덮어쓰지 않습니다.")
    source_hash, template_hash = sha(source), sha(template)
    if plan["source_sha256"] != template_hash:
        raise ValueError("현재 기준 양식과 명세 해시가 다릅니다.")
    selected_profile = plan["body"].get("box_profile")
    if selected_profile:
        profile_path = Path(selected_profile["path"])
        if not profile_path.is_absolute() or sha(profile_path) != selected_profile["sha256"]:
            raise ValueError("확인한 상자 프로필의 절대 경로와 해시가 필요합니다.")
    profile = ET.parse(profile_path).getroot()
    blocks, boxes = plan["body"]["blocks"], plan["body"].get("boxes", {})
    if not boxes:
        raise ValueError("상자 명세가 없습니다.")
    anchor = plan["body"]["anchor"]
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(template) as baseline:
        header = ET.fromstring(archive.read("Contents/header.xml"))
        matches = []
        for name in baseline.namelist():
            if name.startswith("Contents/section") and name.endswith(".xml"):
                root = ET.fromstring(baseline.read(name))
                for index, p in enumerate(root):
                    if anchor in signature(p)[0]:
                        matches.append((name, index, len(root) - index - 1))
        if len(matches) != 1:
            raise ValueError("상자는 단일 본문 표시자가 있는 양식에서만 자동 처리합니다.")
        section_name, offset, suffix = matches[0]
        section = ET.fromstring(archive.read(section_name))
        body = list(section)[offset:len(section)-suffix if suffix else len(section)]
        if [signature(p) for p in body] != [expected(b) for b in blocks]:
            actual, wanted = [signature(p) for p in body], [expected(b) for b in blocks]
            difference = next(((i, x, y) for i, (x, y) in enumerate(zip(actual, wanted)) if x != y), None)
            raise ValueError(f"한글 문단·수식 불일치: 실제 {len(actual)}, 명세 {len(wanted)}, 첫 차이 {difference!r}")
        collections = {local(x): x for x in header.iter()}

        def merge(kind, nodes, prepare=lambda x: None):
            target = collections[kind]
            mapping = {}
            for original in nodes:
                node = copy.deepcopy(original)
                prepare(node)
                same = next((x for x in target if identity(x) == identity(node)), None)
                if same is None:
                    node.set("id", str(max((int(x.get("id")) for x in target), default=-1)+1))
                    target.append(node)
                    same = node
                mapping[original.get("id")] = same.get("id")
            target.set("itemCnt", str(len(target)))
            return mapping

        resources = profile.find("resources")
        font_map = {}
        for source_face in resources.find("fontfaces"):
            target_face = next(x for x in header.iter() if local(x) == "fontface" and x.get("lang") == source_face.get("lang"))
            ids = {}
            for original in source_face:
                same = next((x for x in target_face if identity(x) == identity(original)), None)
                if same is None:
                    same = copy.deepcopy(original)
                    same.set("id", str(max(int(x.get("id")) for x in target_face)+1))
                    target_face.append(same)
                ids[original.get("id")] = same.get("id")
            target_face.set("fontCnt", str(len(target_face)))
            font_map[source_face.get("lang").lower()] = ids
        border_map = merge("borderFills", resources.find("borderFills"))
        tab_map = merge("tabProperties", resources.find("tabProperties"))

        def remap(node, maps):
            for element in node.iter():
                for key, mapping in maps.items():
                    if key in element.attrib:
                        element.set(key, mapping[element.get(key)])

        def prepare_char(node):
            remap(node, {"borderFillIDRef": border_map})
            for element in node:
                if local(element) == "fontRef":
                    for lang, value in list(element.attrib.items()):
                        element.set(lang, font_map[lang][value])

        char_map = merge("charProperties", resources.find("charProperties"), prepare_char)
        para_map = merge("paraProperties", resources.find("paraProperties"),
                         lambda node: remap(node, {"borderFillIDRef": border_map, "tabPrIDRef": tab_map}))
        all_ids = [int(x.get("id")) for x in section.iter() if x.get("id", "").isdigit()]
        next_id = max(all_ids, default=1000000000)+1
        result, seen = [], set()
        index = 0
        while index < len(blocks):
            group = blocks[index].get("box_id")
            if not group:
                result.append(body[index])
                index += 1
                continue
            if group in seen or group not in boxes:
                raise ValueError("상자 ID는 명세에 있고 한 번의 연속 묶음이어야 합니다.")
            seen.add(group)
            end = index+1
            while end < len(blocks) and blocks[end].get("box_id") == group:
                end += 1
            table = copy.deepcopy(profile.find("hp:tbl", NS))
            remap(table, {"borderFillIDRef": border_map, "charPrIDRef": char_map, "paraPrIDRef": para_map})
            table.set("id", str(next_id))
            next_id += 1
            table.set("zOrder", str(next_id % 100000))
            caption = boxes[group].get("caption", "조건")
            if not caption or len(caption) > 12 or "\n" in caption:
                raise ValueError("짧은 상자 제목이 필요합니다.")
            caption_text = next(x for x in table.iter() if local(x) == "t")
            caption_text.text = "＜" + ("조 건" if caption == "조건" else caption) + "＞"
            resize_table(table, plan['body'].get('box_width_mm'))
            resize_caption(table, boxes[group].get("caption_width_mm"))
            cell = next(x for x in table.iter() if local(x) == "tc" and
                        x.find("hp:cellAddr", NS).get("rowAddr") == profile.get("body_row") and
                        x.find("hp:cellAddr", NS).get("colAddr") == profile.get("body_col"))
            contents = cell.find("hp:subList", NS)
            for p in list(contents):
                contents.remove(p)
            for p in body[index:end]:
                if p.get("columnBreak") != "0" or p.get("pageBreak") != "0":
                    raise ValueError("상자 안 문단에는 단·쪽 나눔을 넣지 않습니다.")
                contents.append(p)
            cell.find("hp:cellSz", NS).set("height", "1")
            table.find("hp:sz", NS).set("height", "1881")
            outer = ET.Element(body[index].tag, dict(body[index].attrib))
            outer.set("styleIDRef", "0")
            base_style = next(x for x in header.iter() if local(x) == "style" and x.get("id") == "0")
            outer.set("paraPrIDRef", base_style.get("paraPrIDRef"))
            run = ET.SubElement(outer, "{"+NS["hp"]+"}run", {"charPrIDRef": base_style.get("charPrIDRef")})
            run.append(table)
            ET.SubElement(run, "{"+NS["hp"]+"}t")
            result.append(outer)
            index = end
        if seen != set(boxes):
            raise ValueError("실제로 사용하지 않은 상자 명세가 있습니다.")
        children = list(section)
        for p in children[offset:len(children)-suffix if suffix else len(children)]:
            section.remove(p)
        for index, p in enumerate(result, offset):
            section.insert(index, p)
        for root in [header, section]:
            for parent in root.iter():
                for child in list(parent):
                    if local(child) == "linesegarray":
                        parent.remove(child)
        with zipfile.ZipFile(output, "w") as target:
            for entry in archive.infolist():
                data = ET.tostring(header, encoding="utf-8", xml_declaration=True) if entry.filename == "Contents/header.xml" else ET.tostring(section, encoding="utf-8", xml_declaration=True) if entry.filename == section_name else archive.read(entry.filename)
                target.writestr(entry, data)
    if sha(source) != source_hash or sha(template) != template_hash:
        raise ValueError("입력 파일의 해시가 달라졌습니다.")
    return {"status": "BOXES_WRAPPED", "boxes": len(seen), "profile_sha256": sha(profile_path),
            "source_sha256": source_hash, "output_sha256": sha(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["source", "output", "template", "plan"]:
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=Path(__file__).resolve().parents[1]/"assets/condition-box.xml")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    print(json.dumps(apply(args.source, args.output, args.template, plan, args.profile), ensure_ascii=False))


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    main()
