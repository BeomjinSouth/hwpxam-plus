"""Conservative, standard-library PDF text extractor (Python 3.10+).

No OCR, decryption, font-shape inference, or reading-order rearrangement.
Missing character mappings produce one U+FFFD per source code. Streams and
objects are fetched lazily from a read-only mmap; image data is not decoded.
"""
from __future__ import annotations

import argparse
import base64
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
import json
import math
import mmap
import os
from pathlib import Path
import re
import sys
import time
import zlib


class PDFError(ValueError):
    pass


class EncryptedPDF(PDFError):
    pass


class Name(str):
    pass


class Word(str):
    pass


@dataclass(frozen=True)
class Ref:
    number: int
    generation: int = 0


WS = b"\x00\t\n\x0c\r "
DELIM = WS + b"()<>[]{}/%"
NUMBER = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)\Z")
OBJ = re.compile(rb"(?<![A-Za-z0-9])([0-9]+)\s+([0-9]+)\s+obj\b")


class Parser:
    """PDF lexical/object parser, working on bytes or directly on mmap."""
    def __init__(self, data, pos=0, refs=True):
        self.data, self.pos, self.refs = data, pos, refs

    def skip(self):
        d, n = self.data, len(self.data)
        while self.pos < n:
            c = d[self.pos]
            if c in WS:
                self.pos += 1
            elif c == 37:
                while self.pos < n and d[self.pos] not in (10, 13):
                    self.pos += 1
            else:
                break

    def value(self, depth=0):
        if depth > 100:
            raise PDFError("PDF object nesting exceeds 100")
        self.skip()
        d, p, n = self.data, self.pos, len(self.data)
        if p >= n:
            raise EOFError("End of PDF tokens")
        c = d[p]
        if c == 47:
            self.pos += 1
            start = self.pos
            while self.pos < n and d[self.pos] not in DELIM:
                self.pos += 1
            b = bytes(d[start:self.pos])
            b = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m[1], 16)]), b)
            return Name(b.decode("latin1"))
        if c == 40:
            self.pos += 1
            out, nesting = bytearray(), 1
            escapes = {110: 10, 114: 13, 116: 9, 98: 8, 102: 12}
            while self.pos < n:
                c = d[self.pos]
                self.pos += 1
                if c == 92:
                    if self.pos >= n:
                        break
                    c = d[self.pos]
                    self.pos += 1
                    if c in (10, 13):
                        if c == 13 and self.pos < n and d[self.pos] == 10:
                            self.pos += 1
                    elif 48 <= c <= 55:
                        octal = bytes([c])
                        for _ in range(2):
                            if self.pos < n and 48 <= d[self.pos] <= 55:
                                octal += bytes([d[self.pos]])
                                self.pos += 1
                            else:
                                break
                        out.append(int(octal, 8) & 255)
                    else:
                        out.append(escapes.get(c, c))
                elif c == 40:
                    nesting += 1
                    out.append(c)
                elif c == 41:
                    nesting -= 1
                    if not nesting:
                        return bytes(out)
                    out.append(c)
                elif c == 13:
                    if self.pos < n and d[self.pos] == 10:
                        self.pos += 1
                    out.append(10)
                else:
                    out.append(c)
            raise PDFError("Unterminated literal string")
        if d[p:p+2] == b"<<":
            self.pos += 2
            result = {}
            while True:
                self.skip()
                if d[self.pos:self.pos+2] == b">>":
                    self.pos += 2
                    return result
                key = self.value(depth + 1)
                if not isinstance(key, Name):
                    raise PDFError("Dictionary key is not a name")
                result[str(key)] = self.value(depth + 1)
        if c == 60:
            end = d.find(b">", p + 1)
            if end < 0:
                raise PDFError("Unterminated hex string")
            b = re.sub(rb"\s+", b"", bytes(d[p+1:end])).replace(b"\x00", b"")
            if len(b) % 2:
                b += b"0"
            self.pos = end + 1
            try:
                return bytes.fromhex(b.decode("ascii"))
            except ValueError as exc:
                raise PDFError("Invalid hex string") from exc
        if c == 91:
            self.pos += 1
            result = []
            while True:
                self.skip()
                if self.pos >= n:
                    raise PDFError("Unterminated array")
                if self.pos < n and d[self.pos] == 93:
                    self.pos += 1
                    return result
                result.append(self.value(depth + 1))
        while self.pos < n and d[self.pos] not in DELIM:
            self.pos += 1
        if self.pos == p:
            raise PDFError(f"Unexpected delimiter at {p}")
        word = bytes(d[p:self.pos])
        if NUMBER.fullmatch(word):
            v = float(word) if b"." in word else int(word)
            if self.refs and isinstance(v, int):
                saved = self.pos
                self.skip()
                m = re.match(rb"(\d+)\s+R\b", d[self.pos:self.pos+40])
                if m:
                    self.pos += m.end()
                    return Ref(v, int(m[1]))
                self.pos = saved
            return v
        return {b"true": True, b"false": False, b"null": None}.get(
            word, Word(word.decode("latin1")))


def predictor(data, params):
    p = int(params.get("Predictor", 1))
    if p == 1:
        return data
    colors = int(params.get("Colors", 1))
    bits = int(params.get("BitsPerComponent", 8))
    columns = int(params.get("Columns", 1))
    if colors < 1 or columns < 1 or bits not in (1, 2, 4, 8, 16):
        raise PDFError("Invalid predictor parameters")
    rowlen = (colors * columns * bits + 7) // 8
    bpp = max(1, (colors * bits + 7) // 8)
    out = bytearray()
    if p == 2:
        if len(data) % rowlen:
            raise PDFError("Truncated TIFF predictor row")
        mask = (1 << bits) - 1
        for start in range(0, len(data), rowlen):
            row = data[start:start+rowlen]
            packed = int.from_bytes(row, "big")
            samples = [(packed >> (rowlen*8 - bits*(i+1))) & mask
                       for i in range(colors*columns)]
            for i in range(colors, len(samples)):
                samples[i] = (samples[i] + samples[i-colors]) & mask
            decoded = 0
            for sample in samples:
                decoded = (decoded << bits) | sample
            padding = rowlen*8 - len(samples)*bits
            decoded = (decoded << padding) | (packed & ((1 << padding)-1))
            out.extend(decoded.to_bytes(rowlen, "big"))
        return bytes(out)
    if not 10 <= p <= 15:
        raise PDFError(f"Unsupported predictor {p}")
    if len(data) % (rowlen+1):
        raise PDFError("Truncated PNG predictor row")
    previous = bytearray(rowlen)
    for start in range(0, len(data), rowlen+1):
        kind = data[start]
        row = bytearray(data[start+1:start+1+rowlen])
        if kind > 4:
            raise PDFError("Invalid PNG predictor filter")
        for i in range(rowlen):
            a = row[i-bpp] if i >= bpp else 0
            b = previous[i]
            c = previous[i-bpp] if i >= bpp else 0
            if kind == 1:
                add = a
            elif kind == 2:
                add = b
            elif kind == 3:
                add = (a+b)//2
            elif kind == 4:
                q = a+b-c
                distances = (abs(q-a), abs(q-b), abs(q-c))
                add = (a, b, c)[distances.index(min(distances))]
            else:
                add = 0
            row[i] = (row[i]+add) & 255
        out.extend(row)
        previous = row
    return bytes(out)


def lzw(data, early=1):
    if early not in (0, 1):
        raise PDFError("Invalid LZW EarlyChange")
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    width, bitpos, previous = 9, 0, None
    out = bytearray()
    while bitpos + width <= len(data)*8:
        code = 0
        for _ in range(width):
            code = (code << 1) | ((data[bitpos//8] >> (7-bitpos%8)) & 1)
            bitpos += 1
        if code == 256:
            table = [bytes([i]) for i in range(256)] + [b"", b""]
            width, previous = 9, None
            continue
        if code == 257:
            return bytes(out)
        if code < len(table):
            entry = table[code]
        elif code == len(table) and previous is not None:
            entry = previous + previous[:1]
        else:
            raise PDFError("Invalid LZW code")
        out.extend(entry)
        if previous is not None and len(table) < 4096:
            table.append(previous + entry[:1])
            if len(table) + early == 1 << width and width < 12:
                width += 1
        previous = entry
    raise PDFError("LZW stream has no end code")


def decode_filters(data, filters=None, params=None):
    if not filters:
        return data
    filters = filters if isinstance(filters, list) else [filters]
    params = params if isinstance(params, list) else [params]
    for i, f in enumerate(filters):
        p = params[i] if i < len(params) and isinstance(params[i], dict) else {}
        if f in ("FlateDecode", "Fl"):
            data = predictor(zlib.decompress(data), p)
        elif f in ("ASCIIHexDecode", "AHx"):
            s = data.split(b">", 1)[0]
            s = re.sub(rb"[\x00\t\n\f\r ]", b"", s)
            if len(s) % 2:
                s += b"0"
            data = bytes.fromhex(s.decode("ascii"))
        elif f in ("ASCII85Decode", "A85"):
            s = re.sub(rb"[\x00\t\n\f\r ]", b"", data)
            if s.startswith(b"<~"):
                s = s[2:]
            if not s.endswith(b"~>"):
                raise PDFError("ASCII85 end marker missing")
            data = base64.a85decode(s[:-2])
        elif f in ("LZWDecode", "LZW"):
            data = predictor(lzw(data, int(p.get("EarlyChange", 1))), p)
        elif f in ("RunLengthDecode", "RL"):
            out, pos = bytearray(), 0
            while pos < len(data):
                n = data[pos]
                pos += 1
                if n == 128:
                    break
                count = n+1 if n < 128 else 1
                if pos+count > len(data):
                    raise PDFError("Truncated RunLength data")
                out.extend(data[pos:pos+count] if n < 128 else data[pos:pos+1]*(257-n))
                pos += count
            else:
                raise PDFError("RunLength end marker missing")
            data = bytes(out)
        else:
            raise PDFError(f"Unsupported non-image stream filter: {f}")
    return data


@dataclass
class Stream:
    attrs: dict
    offset: int
    length: object
    number: int


class PDF:
    def __init__(self, path):
        self.file = open(path, "rb")
        self.mm = None
        try:
            self.mm = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
            self.xref, self.trailer = {}, {}
            self.cache, self.objstreams = OrderedDict(), OrderedDict()
            self.fonts, self.resolving = {}, set()
            self.recovered = False
            tail = self.mm[max(0, len(self.mm)-65536):]
            starts = list(re.finditer(rb"startxref\s+(\d+)", tail))
            try:
                if not starts:
                    raise PDFError("startxref missing")
                self.read_xref(int(starts[-1][1]), set())
                if not self.trailer.get("Root"):
                    raise PDFError("Root missing from trailer")
            except EncryptedPDF:
                raise
            except (PDFError, EOFError, ValueError, IndexError, zlib.error):
                self.recover()
            self.check_encryption(self.trailer)
            self.pages = []
            root = self.resolve(self.trailer.get("Root"))
            if not isinstance(root, dict):
                raise PDFError("Catalog missing")
            try:
                self.walk_pages(root.get("Pages"), {}, set())
            except PDFError:
                if self.recovered:
                    raise
                self.recover()
                self.pages.clear()
                root = self.resolve(self.trailer.get("Root"))
                self.walk_pages(root.get("Pages"), {}, set())
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.mm is not None:
            self.mm.close()
        self.file.close()

    @staticmethod
    def check_encryption(trailer):
        if trailer.get("Encrypt") is not None:
            raise EncryptedPDF("Encrypted PDF")

    @staticmethod
    def remember(cache, key, value, limit):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
        return value

    def indirect(self, offset):
        p = Parser(self.mm, offset)
        p.skip()
        m = OBJ.match(self.mm, p.pos)
        if not m:
            raise PDFError(f"Invalid object offset {offset}")
        number, generation = int(m[1]), int(m[2])
        p.pos = m.end()
        value = p.value()
        p.skip()
        if isinstance(value, dict) and self.mm[p.pos:p.pos+6] == b"stream":
            p.pos += 6
            while self.mm[p.pos:p.pos+1] in (b" ", b"\t"):
                p.pos += 1
            if self.mm[p.pos:p.pos+2] == b"\r\n":
                p.pos += 2
            elif self.mm[p.pos:p.pos+1] in (b"\r", b"\n"):
                p.pos += 1
            else:
                raise PDFError("Missing stream line ending")
            value = Stream(value, p.pos, value.get("Length"), number)
        return number, generation, value

    def stream_data(self, stream):
        length = self.resolve(stream.length)
        if not isinstance(length, int) or length < 0 or stream.offset+length > len(self.mm):
            raise PDFError("Missing or invalid stream Length")
        raw = self.mm[stream.offset:stream.offset+length]
        filters = self.resolve(stream.attrs.get("Filter"))
        if isinstance(filters, list):
            filters = [self.resolve(f) for f in filters]
        params = self.resolve(stream.attrs.get("DecodeParms"))
        if isinstance(params, list):
            params = [self.resolve(p) for p in params]
            params = [{k: self.resolve(v) for k, v in p.items()} if isinstance(p, dict) else p for p in params]
        elif isinstance(params, dict):
            params = {k: self.resolve(v) for k, v in params.items()}
        return decode_filters(raw, filters, params)

    def read_xref(self, offset, visited, follow_prev=True):
        if offset in visited:
            raise PDFError("Cyclic xref chain")
        visited.add(offset)
        p = Parser(self.mm, offset)
        p.skip()
        entries = {}
        if self.mm[p.pos:p.pos+4] == b"xref":
            p.pos += 4
            while True:
                first = p.value()
                if first == "trailer":
                    trailer = p.value()
                    break
                count = p.value()
                if not isinstance(first, int) or not isinstance(count, int) or count < 0:
                    raise PDFError("Invalid xref subsection")
                for n in range(first, first+count):
                    off, gen, state = p.value(), p.value(), p.value()
                    entries[n] = (1, off, gen) if state == "n" else (0, off, gen)
        else:
            _, _, stream = self.indirect(p.pos)
            if not isinstance(stream, Stream) or stream.attrs.get("Type") != "XRef":
                raise PDFError("startxref does not point to xref")
            trailer = stream.attrs
            self.check_encryption(trailer)
            widths = trailer.get("W")
            if not isinstance(widths, list) or len(widths) != 3 or any(w < 0 or w > 8 for w in widths):
                raise PDFError("Invalid xref W")
            data = self.stream_data(stream)
            index = trailer.get("Index", [0, trailer["Size"]])
            if len(index) % 2 or sum(widths) == 0:
                raise PDFError("Invalid xref Index")
            pos = 0
            for i in range(0, len(index), 2):
                for n in range(index[i], index[i]+index[i+1]):
                    fields = []
                    for j, w in enumerate(widths):
                        if pos+w > len(data):
                            raise PDFError("Truncated xref stream")
                        fields.append(int.from_bytes(data[pos:pos+w], "big") if w else (1 if j == 0 else 0))
                        pos += w
                    entries[n] = tuple(fields)
        self.check_encryption(trailer)
        # A companion stream has precedence over entries in its classic table.
        hybrid = trailer.get("XRefStm")
        if isinstance(hybrid, int) and hybrid not in visited:
            self.read_xref(hybrid, visited, follow_prev=False)
        for n, entry in entries.items():
            self.xref.setdefault(n, entry)
        for key, value in trailer.items():
            self.trailer.setdefault(key, value)
        prev = trailer.get("Prev")
        if follow_prev and isinstance(prev, int) and prev not in visited:
            self.read_xref(prev, visited)

    def recover(self):
        """Scan object headers without copying the file; skip known stream bodies."""
        self.recovered = True
        self.xref.clear()
        self.cache.clear()
        self.objstreams.clear()
        self.trailer = {}
        pos, streams, catalogs = 0, [], []
        while True:
            m = OBJ.search(self.mm, pos)
            if not m:
                break
            pos = m.end()
            try:
                n, g, value = self.indirect(m.start())
            except (PDFError, EOFError, ValueError, IndexError):
                continue
            self.xref[n] = (1, m.start(), g)
            attrs = value.attrs if isinstance(value, Stream) else value
            if isinstance(attrs, dict):
                if attrs.get("Type") == "Catalog":
                    catalogs.append(Ref(n, g))
                if attrs.get("Type") == "XRef":
                    self.check_encryption(attrs)
                    self.trailer.update(attrs)
                if attrs.get("Type") == "ObjStm":
                    streams.append(n)
            if isinstance(value, Stream):
                length = value.length
                if isinstance(length, int) and length >= 0:
                    pos = value.offset+length
                else:
                    end = self.mm.find(b"endstream", value.offset)
                    if end >= 0:
                        pos = end+9
        # Trailers are small; parse every candidate, never trust an Encrypt match
        # inside a string alone. An actual trailer dictionary always blocks.
        for m in re.finditer(rb"\btrailer\s*<<", self.mm):
            try:
                tr = Parser(self.mm, m.end()-2).value()
                self.check_encryption(tr)
                self.trailer.update(tr)
            except (PDFError, EOFError) as exc:
                if isinstance(exc, EncryptedPDF):
                    raise
        for n in streams:
            self.load_objstream(n, recovery=True)
        if not self.trailer.get("Root"):
            if catalogs:
                self.trailer["Root"] = catalogs[-1]
            else:
                for n, entry in list(self.xref.items()):
                    if entry[0] == 2:
                        v = self.resolve(Ref(n))
                        if isinstance(v, dict) and v.get("Type") == "Catalog":
                            self.trailer["Root"] = Ref(n)
        if not self.trailer.get("Root"):
            raise PDFError("Cannot recover PDF catalog")

    def resolve(self, value):
        if not isinstance(value, Ref):
            return value
        if value in self.cache:
            self.cache.move_to_end(value)
            return self.cache[value]
        if value in self.resolving:
            raise PDFError("Cyclic indirect object")
        entry = self.xref.get(value.number)
        if entry and entry[0] == 1 and not self.recovered:
            probe = Parser(self.mm, entry[1])
            probe.skip()
            header = OBJ.match(self.mm, probe.pos)
            if not header or (int(header[1]), int(header[2])) != (value.number, value.generation):
                self.recover()
                return self.resolve(value)
        self.resolving.add(value)
        try:
            e = self.xref.get(value.number)
            if not e or e[0] == 0:
                raise PDFError(f"Object {value.number} is unavailable")
            if e[0] == 1:
                n, g, result = self.indirect(e[1])
                if n != value.number or g != value.generation:
                    raise PDFError("Object identity mismatch")
            elif e[0] == 2:
                if value.generation != 0:
                    raise PDFError("Compressed object has nonzero generation")
                data, pairs, first = self.load_objstream(e[1])
                if e[2] >= len(pairs) or pairs[e[2]][0] != value.number:
                    raise PDFError("Object stream index mismatch")
                result = Parser(data, first+pairs[e[2]][1]).value()
            else:
                raise PDFError("Unsupported xref entry type")
            return self.remember(self.cache, value, result, 512)
        finally:
            self.resolving.remove(value)

    def load_objstream(self, number, recovery=False):
        if number in self.objstreams:
            self.objstreams.move_to_end(number)
            return self.objstreams[number]
        e = self.xref.get(number)
        if not e or e[0] != 1:
            raise PDFError("Object stream must be an uncompressed object")
        stream = self.resolve(Ref(number, e[2]))
        if not isinstance(stream, Stream) or stream.attrs.get("Type") != "ObjStm":
            raise PDFError("Invalid object stream")
        data = self.stream_data(stream)
        first, count = stream.attrs["First"], stream.attrs["N"]
        p = Parser(data, refs=False)
        pairs = [(p.value(), p.value()) for _ in range(count)]
        if p.pos > first or first > len(data):
            raise PDFError("Invalid object stream header")
        if recovery:
            for i, (n, _) in enumerate(pairs):
                self.xref.setdefault(n, (2, number, i))
        return self.remember(self.objstreams, number, (data, pairs, first), 4)

    def walk_pages(self, node, inherited, active, depth=0):
        if depth > 100 or (isinstance(node, Ref) and node in active):
            raise PDFError("Cyclic or excessively deep page tree")
        active = active | ({node} if isinstance(node, Ref) else set())
        obj = self.resolve(node)
        if not isinstance(obj, dict):
            raise PDFError("Invalid page tree node")
        inherited = dict(inherited)
        for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
            if key in obj:
                inherited[key] = obj[key]
        if obj.get("Type") == "Page":
            self.pages.append(dict(inherited, **obj))
        else:
            kids = self.resolve(obj.get("Kids"))
            if not isinstance(kids, list):
                raise PDFError("Page tree Kids missing")
            for kid in kids:
                self.walk_pages(kid, inherited, active, depth+1)

    def content(self, value):
        value = self.resolve(value)
        if value is None:
            return b""
        if isinstance(value, list):
            return b" ".join(self.content(v) for v in value)
        if not isinstance(value, Stream):
            raise PDFError("Contents is not a stream")
        return self.stream_data(value)

    def font(self, ref):
        key = ref if isinstance(ref, Ref) else id(ref)
        if key not in self.fonts:
            self.fonts[key] = Font(self, self.resolve(ref))
        return self.fonts[key]

    def extract(self, number):
        if not 1 <= number <= len(self.pages):
            raise PDFError(f"Page out of range: {number}")
        page = self.pages[number-1]
        renderer = TextRenderer(self)
        renderer.run(self.content(page.get("Contents")), self.resolve(page.get("Resources")) or {})
        text = "".join(renderer.output).strip()
        return {"page": number, "text": text,
                "chars": sum(not c.isspace() for c in text),
                "unmapped": text.count("\ufffd"),
                "fonts_without_unicode": sorted(renderer.missing),
                "images": renderer.images}, list(renderer.used.values())


def unicode_bytes(data):
    if not data or len(data) % 2:
        return "\ufffd"
    try:
        return data.decode("utf-16-be", errors="strict")
    except UnicodeDecodeError:
        return "\ufffd"


class CMap:
    def __init__(self, data=b""):
        self.mapping, self.cid, self.spaces = {}, {}, []
        self.vertical = False
        p, tokens = Parser(data, refs=False), []
        while True:
            try:
                tokens.append(p.value())
            except EOFError:
                break
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t == Name("WMode") and i+1 < len(tokens):
                self.vertical = tokens[i+1] == 1
            sections = {"begincodespacerange": (2, "space"), "beginbfchar": (2, "char"),
                        "beginbfrange": (3, "range"), "begincidchar": (2, "cidchar"),
                        "begincidrange": (3, "cidrange")}
            if isinstance(t, Word) and t in sections:
                step, kind = sections[t]
                count = tokens[i-1] if i else None
                if not isinstance(count, int) or count < 0:
                    raise PDFError("Invalid CMap entry count")
                i += 1
                for _ in range(count):
                    group = tokens[i:i+step]
                    if len(group) != step:
                        raise PDFError("Truncated CMap")
                    a, b = group[:2]
                    if not isinstance(a, bytes):
                        raise PDFError("Invalid CMap source")
                    if kind == "space":
                        if not isinstance(b, bytes) or len(a) != len(b):
                            raise PDFError("Invalid codespace range")
                        self.spaces.append((len(a), int.from_bytes(a, "big"), int.from_bytes(b, "big")))
                    elif kind == "char":
                        self.mapping[a] = unicode_bytes(b)
                    elif kind == "cidchar":
                        self.cid[a] = int(b)
                    else:
                        if not isinstance(b, bytes) or len(a) != len(b):
                            raise PDFError("Invalid CMap range")
                        lo, hi = int.from_bytes(a, "big"), int.from_bytes(b, "big")
                        dest = group[2]
                        if hi < lo or hi-lo > 1000000:
                            raise PDFError("Excessive CMap range")
                        for k, code in enumerate(range(lo, hi+1)):
                            source = code.to_bytes(len(a), "big")
                            if kind == "cidrange":
                                self.cid[source] = int(dest)+k
                            elif isinstance(dest, list):
                                if k >= len(dest):
                                    raise PDFError("Truncated bfrange array")
                                self.mapping[source] = unicode_bytes(dest[k])
                            elif isinstance(dest, bytes):
                                try:
                                    target = (int.from_bytes(dest, "big")+k).to_bytes(len(dest), "big")
                                except OverflowError as exc:
                                    raise PDFError("CMap destination overflow") from exc
                                self.mapping[source] = unicode_bytes(target)
                            else:
                                raise PDFError("Invalid bfrange destination")
                    i += step
                continue
            i += 1

    def split(self, data, default=1):
        pos = 0
        lengths = sorted({s[0] for s in self.spaces})
        if not lengths:
            lengths = sorted({len(k) for k in self.mapping} | {len(k) for k in self.cid})
        while pos < len(data):
            size = None
            for n in lengths:
                raw = data[pos:pos+n]
                if len(raw) != n:
                    continue
                value = int.from_bytes(raw, "big")
                if self.spaces:
                    valid = any(w == n and lo <= value <= hi for w, lo, hi in self.spaces)
                else:
                    valid = raw in self.mapping or raw in self.cid
                if valid:
                    size = n
                    break
            if size is None:
                size = default
            yield data[pos:pos+size]
            pos += size


# Published Latin glyph names; no guesses from font outlines or CID numbers.
GLYPHS = {c: c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"}
GLYPHS.update(dict(zip(
    "zero one two three four five six seven eight nine".split(), "0123456789")))
GLYPHS.update(dict(zip(
    "space exclam quotedbl numbersign dollar percent ampersand quotesingle parenleft parenright asterisk plus comma hyphen period slash colon semicolon less equal greater question at bracketleft backslash bracketright asciicircum underscore grave braceleft bar braceright asciitilde".split(),
    " !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")))
GLYPHS.update({
    "quoteleft": "‘", "quoteright": "’", "quotedblleft": "“", "quotedblright": "”",
    "quotesinglbase": "‚", "quotedblbase": "„", "guillemotleft": "«", "guillemotright": "»",
    "guilsinglleft": "‹", "guilsinglright": "›", "endash": "–", "emdash": "—",
    "bullet": "•", "ellipsis": "…", "dagger": "†", "daggerdbl": "‡",
    "Euro": "€", "cent": "¢", "sterling": "£", "currency": "¤", "yen": "¥",
    "copyright": "©", "registered": "®", "trademark": "™", "degree": "°",
    "plusminus": "±", "multiply": "×", "divide": "÷", "minus": "−",
    "paragraph": "¶", "section": "§", "mu": "µ", "periodcentered": "·",
    "exclamdown": "¡", "questiondown": "¿", "ordfeminine": "ª", "ordmasculine": "º",
    "onequarter": "¼", "onehalf": "½", "threequarters": "¾", "onesuperior": "¹",
    "twosuperior": "²", "threesuperior": "³", "fraction": "⁄", "perthousand": "‰",
    "fi": "ﬁ", "fl": "ﬂ", "ff": "ﬀ", "ffi": "ﬃ", "ffl": "ﬄ",
    "AE": "Æ", "ae": "æ", "OE": "Œ", "oe": "œ", "Oslash": "Ø", "oslash": "ø",
    "Lslash": "Ł", "lslash": "ł", "germandbls": "ß", "dotlessi": "ı",
    "Eth": "Ð", "eth": "ð", "Thorn": "Þ", "thorn": "þ", "Scaron": "Š", "scaron": "š",
    "Zcaron": "Ž", "zcaron": "ž", "Ydieresis": "Ÿ", "florin": "ƒ",
    "breve": "˘", "caron": "ˇ", "circumflex": "ˆ", "dotaccent": "˙", "hungarumlaut": "˝",
    "ogonek": "˛", "ring": "˚", "tilde": "˜", "acute": "´", "dieresis": "¨",
    "macron": "¯", "cedilla": "¸", "brokenbar": "¦", "logicalnot": "¬",
})
for _prefix, _chars in {
    "A": "ÀÁÂÃÄÅ", "a": "àáâãäå", "E": "ÈÉÊË", "e": "èéêë",
    "I": "ÌÍÎÏ", "i": "ìíîï", "O": "ÒÓÔÕÖ", "o": "òóôõö",
    "U": "ÙÚÛÜ", "u": "ùúûü",
}.items():
    _accents = ["grave", "acute", "circumflex"]
    if _prefix.lower() in "ao":
        _accents += ["tilde"]
    _accents += ["dieresis"]
    if _prefix.lower() == "a":
        _accents += ["ring"]
    GLYPHS.update({_prefix+a: c for a, c in zip(_accents, _chars)})
GLYPHS.update({"Ccedilla": "Ç", "ccedilla": "ç", "Ntilde": "Ñ", "ntilde": "ñ",
               "Yacute": "Ý", "yacute": "ý", "ydieresis": "ÿ"})


def glyph_unicode(name):
    name = str(name).split(".", 1)[0]
    if "_" in name:
        values = [glyph_unicode(s) for s in name.split("_")]
        return "".join(values) if all(v != "\ufffd" for v in values) else "\ufffd"
    if name in GLYPHS:
        return GLYPHS[name]
    if re.fullmatch(r"uni(?:[0-9A-Fa-f]{4})+", name):
        return unicode_bytes(bytes.fromhex(name[3:]))
    if re.fullmatch(r"u[0-9A-Fa-f]{4,6}", name):
        n = int(name[1:], 16)
        if n <= 0x10ffff and not 0xd800 <= n <= 0xdfff:
            return chr(n)
    return "\ufffd"


def encoding_table(name):
    if name in ("WinAnsiEncoding", "MacRomanEncoding"):
        codec = "cp1252" if name == "WinAnsiEncoding" else "mac_roman"
        table = {i: bytes([i]).decode(codec, "replace") for i in range(32, 256)}
        if name == "WinAnsiEncoding":
            for i in (127, 129, 141, 143, 144, 157):
                table[i] = "•"
            table[160], table[173] = " ", "-"
        else:
            # Unassigned slots in the PDF MacRoman encoding are not Apple glyphs.
            for i in (127, 173, 176, 178, 179, 182, 183, 184, 185, 186, 189, 195, 197, 198, 215, 240):
                table.pop(i, None)
            table[202] = " "
            table[219] = "¤"
        return table
    table = {i: chr(i) for i in range(32, 127)}
    if name == "StandardEncoding":
        table[39], table[96] = "’", "‘"
        extra = {161: "¡",162: "¢",163: "£",164: "⁄",165: "¥",166: "ƒ",167: "§",168: "¤",
                 169: "'",170: "“",171: "«",172: "‹",173: "›",174: "ﬁ",175: "ﬂ",
                 177: "–",178: "†",179: "‡",180: "·",182: "¶",183: "•",184: "‚",185: "„",
                 186: "”",187: "»",188: "…",189: "‰",191: "¿",193: "`",194: "´",195: "ˆ",
                 196: "˜",197: "¯",198: "˘",199: "˙",200: "¨",202: "˚",203: "¸",205: "˝",
                 206: "˛",207: "ˇ",208: "—",225: "Æ",227: "ª",232: "Ł",233: "Ø",234: "Œ",
                 235: "º",241: "æ",245: "ı",248: "ł",249: "ø",250: "œ",251: "ß"}
        table.update(extra)
        return table
    if name == "PDFDocEncoding":
        table.update({i: chr(i) for i in range(161, 256) if i != 173})
        table.update(dict(zip(range(24, 32), "˘ˇˆ˙˝˛˚˜")))
        table.update(dict(zip(range(128, 159), "•†‡…—–ƒ⁄‹›−‰„“”‘’‚™ﬁﬂŁŒŠŸŽıłœšž")))
        table[160] = "€"
        return table
    return {}


@lru_cache(maxsize=1)
def korea1_table():
    """Load the optional standard CID table once, including a missing-file result."""
    try:
        data = json.loads(Path(__file__).with_name('adobe_korea1_unicode.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return ()
    return tuple(data['cids'])


class Font:
    def __init__(self, pdf, attrs):
        if not isinstance(attrs, dict):
            raise PDFError("Invalid font dictionary")
        self.name = str(attrs.get("BaseFont", attrs.get("Name", "unnamed")))
        self.subtype = str(attrs.get("Subtype", "unknown"))
        enc = pdf.resolve(attrs.get("Encoding"))
        tu = pdf.resolve(attrs.get("ToUnicode"))
        self.tounicode = isinstance(tu, Stream)
        self.cmap = CMap(pdf.stream_data(tu)) if self.tounicode else CMap()
        self.composite = self.subtype == "Type0"
        self.identity = enc in ("Identity-H", "Identity-V") if isinstance(enc, str) else False
        self.encoding_cmap = CMap(pdf.stream_data(enc)) if isinstance(enc, Stream) else None
        self.vertical = enc == "Identity-V" or bool(self.encoding_cmap and self.encoding_cmap.vertical)
        self.encoding_name = ("embedded CMap" if isinstance(enc, Stream) else
                              str(enc.get("BaseEncoding", "Differences")) if isinstance(enc, dict) else
                              str(enc) if enc is not None else "unspecified")
        self.widths, self.default_width, self.table = {}, 0, {}
        self.korea1 = ()
        if self.composite:
            descendants = pdf.resolve(attrs.get("DescendantFonts", []))
            descendant = pdf.resolve(descendants[0]) if descendants else {}
            system = pdf.resolve(descendant.get("CIDSystemInfo")) or {}
            if self.identity and isinstance(system, dict):
                registry = pdf.resolve(system.get("Registry"))
                ordering = pdf.resolve(system.get("Ordering"))
                if isinstance(registry, bytes):
                    registry = registry.decode("latin-1")
                if isinstance(ordering, bytes):
                    ordering = ordering.decode("latin-1")
                if registry == "Adobe" and ordering == "Korea1":
                    self.korea1 = korea1_table()
            self.default_width = pdf.resolve(descendant.get("DW", 1000))
            widths = pdf.resolve(descendant.get("W", []))
            i = 0
            while i < len(widths):
                start = widths[i]
                nxt = pdf.resolve(widths[i+1])
                if isinstance(nxt, list):
                    self.widths.update((start+j, pdf.resolve(w)) for j, w in enumerate(nxt))
                    i += 2
                else:
                    w = pdf.resolve(widths[i+2])
                    self.widths.update((j, w) for j in range(start, nxt+1))
                    i += 3
        else:
            desc = pdf.resolve(attrs.get("FontDescriptor")) or {}
            self.default_width = pdf.resolve(desc.get("MissingWidth", 0))
            first = attrs.get("FirstChar", 0)
            self.widths = {first+i: pdf.resolve(w) for i, w in enumerate(pdf.resolve(attrs.get("Widths", [])))}
            base = enc.get("BaseEncoding") if isinstance(enc, dict) else enc
            if base is None:
                bare = self.name.split("+")[-1]
                if self.subtype in ("Type1", "MMType1") and (
                    bare.startswith(("Helvetica", "Times-", "Courier")) or
                    (desc.get("Flags", 0) & 32 and not desc.get("Flags", 0) & 4)):
                    base = "StandardEncoding"
            self.table = encoding_table(base)
            if isinstance(enc, dict):
                code = 0
                for item in pdf.resolve(enc.get("Differences", [])):
                    if isinstance(item, int):
                        code = item
                    elif isinstance(item, Name):
                        self.table[code] = glyph_unicode(item)
                        code += 1

    def info(self):
        source = "ToUnicode" if self.tounicode else None
        if self.korea1:
            source = "ToUnicode+Adobe-Korea1" if self.tounicode else "Adobe-Korea1"
        elif not self.composite and not self.tounicode and self.table:
            source = "encoding"
        return {"name": self.name, "Subtype": self.subtype, "Encoding": self.encoding_name,
                "ToUnicode": self.tounicode, "unicode_source": source}

    def decode(self, data):
        if self.composite and self.identity:
            codes = (data[i:i+2] for i in range(0, len(data), 2))
        elif self.composite and self.encoding_cmap:
            codes = self.encoding_cmap.split(data)
        elif self.composite:
            codes = self.cmap.split(data, default=2)
        else:
            codes = (data[i:i+1] for i in range(len(data)))
        for raw in codes:
            number = int.from_bytes(raw, "big")
            cid = self.encoding_cmap.cid.get(raw) if self.encoding_cmap else number
            if self.tounicode:
                text = self.cmap.mapping.get(raw)
            elif self.composite:
                text = None
            else:
                text = self.table.get(number, "\ufffd")
            if text is None:
                codepoint = self.korea1[cid] if self.korea1 and len(raw) == 2 and 0 <= cid < len(self.korea1) else 0
                text = chr(codepoint) if codepoint else "\ufffd"
            yield text, self.widths.get(cid, self.default_width), (not self.composite and raw == b" ")


IDENTITY = (1., 0., 0., 1., 0., 0.)


def compose(a, b):
    """Transform by b, then by a (column-vector affine convention)."""
    return (a[0]*b[0]+a[2]*b[1], a[1]*b[0]+a[3]*b[1],
            a[0]*b[2]+a[2]*b[3], a[1]*b[2]+a[3]*b[3],
            a[0]*b[4]+a[2]*b[5]+a[4], a[1]*b[4]+a[3]*b[5]+a[5])


def point(m, x, y):
    return (m[0]*x+m[2]*y+m[4], m[1]*x+m[3]*y+m[5])


class TextRenderer:
    def __init__(self, pdf):
        self.pdf = pdf
        self.state = {"ctm": IDENTITY, "font": None, "size": 0., "leading": 0.,
                      "charspace": 0., "wordspace": 0., "scale": 1., "rise": 0.}
        self.tm = self.lm = IDENTITY
        self.output, self.missing, self.used = [], set(), {}
        self.last = None
        self.images = 0
        self.pending_space = False

    def separator(self, start, end, direction, size, text):
        if self.last and text:
            prev_end, prev_start, prev_dir, prev_size = self.last
            dx, dy = start[0]-prev_end[0], start[1]-prev_end[1]
            along, across = dx*prev_dir[0]+dy*prev_dir[1], -dx*prev_dir[1]+dy*prev_dir[0]
            threshold = max(size, prev_size, 0.001)
            backward = (start[0]-prev_start[0])*prev_dir[0] + (start[1]-prev_start[1])*prev_dir[1]
            if abs(across) > threshold*0.5 or backward < -threshold or sum(a*b for a,b in zip(direction,prev_dir)) < 0.5:
                if self.output and not self.output[-1].endswith("\n"):
                    self.output.append("\n")
            elif (along > threshold*0.25 or self.pending_space) and self.output and not self.output[-1][-1:].isspace() and not text[0].isspace():
                self.output.append(" ")
        self.pending_space = False
        self.output.append(text)
        self.last = (end, start, direction, size)

    def advance(self, x, y=0):
        self.tm = compose(self.tm, (1, 0, 0, 1, x, y))

    def show(self, data):
        if not isinstance(data, bytes):
            raise PDFError("Text operand is not a string")
        s, font = self.state, self.state["font"]
        if font is None:
            raise PDFError("Text drawn without a font")
        self.used[id(font)] = font.info()
        for text, width, word in font.decode(data):
            if "\ufffd" in text:
                self.missing.add(font.name)
            matrix = compose(s["ctm"], self.tm)
            start = point(matrix, 0, s["rise"])
            advance = width/1000*s["size"]
            if font.vertical:
                vx, vy = 0, -(advance+s["charspace"]+(s["wordspace"] if word else 0))
                end = point(matrix, 0, s["rise"]-advance)
                direction = (-matrix[2], -matrix[3])
            else:
                vx, vy = (advance+s["charspace"]+(s["wordspace"] if word else 0))*s["scale"], 0
                end = point(matrix, advance*s["scale"], s["rise"])
                direction = (matrix[0], matrix[1])
            norm = math.hypot(*direction) or 1
            direction = tuple(x/norm for x in direction)
            size = abs(s["size"])*math.hypot(matrix[2], matrix[3])
            self.separator(start, end, direction, size, text)
            self.advance(vx, vy)

    def move_line(self, x, y):
        self.lm = compose(self.lm, (1, 0, 0, 1, x, y))
        self.tm = self.lm

    def inline_image(self, p):
        attrs = {}
        while True:
            key = p.value()
            if key == Word("ID"):
                break
            if not isinstance(key, Name):
                raise PDFError("Invalid inline image dictionary")
            attrs[str(key)] = p.value()
        if p.pos < len(p.data) and p.data[p.pos] in WS:
            if p.data[p.pos:p.pos+2] == b"\r\n":
                p.pos += 2
            else:
                p.pos += 1
        start = p.pos
        length = attrs.get("Length", attrs.get("L"))
        if not isinstance(length, int) and not attrs.get("F", attrs.get("Filter")):
            colorspace = attrs.get("CS", attrs.get("ColorSpace"))
            colors = {"G": 1, "DeviceGray": 1, "RGB": 3, "DeviceRGB": 3, "CMYK": 4, "DeviceCMYK": 4}.get(colorspace) if isinstance(colorspace, str) else None
            if attrs.get("IM", attrs.get("ImageMask")):
                colors = 1
            width, height = attrs.get("W", attrs.get("Width")), attrs.get("H", attrs.get("Height"))
            bits = attrs.get("BPC", attrs.get("BitsPerComponent", 1))
            if colors and isinstance(width, int) and isinstance(height, int):
                length = ((width*colors*bits+7)//8)*height
        if isinstance(length, int):
            p.pos = start+length
            p.skip()
            if p.data[p.pos:p.pos+2] != b"EI":
                raise PDFError("Inline image length does not end at EI")
            p.pos += 2
            return
        # For encoded inline images, EI must be whitespace-delimited and the
        # following bytes must start syntactically valid content tokens.
        for m in re.finditer(rb"[\x00\t\n\f\r ]EI(?=[\x00\t\n\f\r /]|$)", p.data[start:]):
            candidate = start+m.end()
            probe = Parser(p.data, candidate, refs=False)
            try:
                for _ in range(12):
                    token = probe.value()
                    if isinstance(token, Word):
                        if token in CONTENT_OPERATORS:
                            p.pos = candidate
                            return
                        break
            except EOFError:
                p.pos = candidate
                return
            except PDFError:
                pass
        raise PDFError("Cannot locate inline image EI safely")

    def run(self, data, resources, depth=0, active=frozenset()):
        if depth > 10:
            raise PDFError("Form XObject depth exceeds 10")
        p, args, stack = Parser(data, refs=False), [], []
        resources = self.pdf.resolve(resources) or {}
        while True:
            try:
                token = p.value()
            except EOFError:
                break
            if not isinstance(token, Word):
                args.append(token)
                continue
            op, a, s = str(token), args, self.state
            args = []
            try:
                if op == "BI":
                    self.inline_image(p)
                elif op == "BT":
                    self.tm = self.lm = IDENTITY
                elif op == "Tf":
                    fonts = self.pdf.resolve(resources.get("Font")) or {}
                    if a[-2] not in fonts:
                        raise PDFError(f"Missing font resource {a[-2]}")
                    s["font"], s["size"] = self.pdf.font(fonts[a[-2]]), a[-1]
                    self.used[id(s["font"])] = s["font"].info()
                elif op in ("Td", "TD"):
                    if op == "TD":
                        s["leading"] = -a[-1]
                    self.move_line(a[-2], a[-1])
                elif op == "Tm":
                    self.tm = self.lm = tuple(a[-6:])
                    if len(self.tm) != 6:
                        raise PDFError("Invalid Tm")
                elif op == "T*":
                    self.move_line(0, -s["leading"])
                elif op in ("TL", "Tc", "Tw", "Tz", "Ts"):
                    key = {"TL": "leading", "Tc": "charspace", "Tw": "wordspace", "Tz": "scale", "Ts": "rise"}[op]
                    s[key] = a[-1]/100 if op == "Tz" else a[-1]
                elif op in ("Tj", "'", '"'):
                    if op == '"':
                        s["wordspace"], s["charspace"] = a[-3], a[-2]
                    if op != "Tj":
                        self.move_line(0, -s["leading"])
                    self.show(a[-1])
                elif op == "TJ":
                    for item in a[-1]:
                        if isinstance(item, bytes):
                            self.show(item)
                        elif isinstance(item, (int, float)):
                            adjust = -item/1000*s["size"]
                            if item <= -250:
                                self.pending_space = True
                            if s["font"] and s["font"].vertical:
                                self.advance(0, -adjust)
                            else:
                                self.advance(adjust*s["scale"])
                        else:
                            raise PDFError("Invalid TJ element")
                elif op == "q":
                    stack.append(dict(s))
                elif op == "Q":
                    if not stack:
                        raise PDFError("Unbalanced Q")
                    self.state = stack.pop()
                elif op == "cm":
                    s["ctm"] = compose(s["ctm"], tuple(a[-6:]))
                elif op == "Do":
                    objects = self.pdf.resolve(resources.get("XObject")) or {}
                    ref = objects.get(a[-1])
                    obj = self.pdf.resolve(ref)
                    if not isinstance(obj, Stream):
                        raise PDFError("Missing XObject")
                    if obj.attrs.get("Subtype") == "Image":
                        self.images += 1
                    elif obj.attrs.get("Subtype") == "Form":
                        key = ref if isinstance(ref, Ref) else id(obj)
                        if key in active:
                            raise PDFError("Cyclic Form XObject")
                        saved, tm, lm = dict(s), self.tm, self.lm
                        self.state = dict(s)
                        self.state["ctm"] = compose(s["ctm"], tuple(obj.attrs.get("Matrix", IDENTITY)))
                        try:
                            self.run(self.pdf.stream_data(obj), obj.attrs.get("Resources", resources), depth+1, active | {key})
                        finally:
                            self.state, self.tm, self.lm = saved, tm, lm
                # ET and non-text painting operators do not change text positions.
            except (IndexError, TypeError, KeyError) as exc:
                raise PDFError(f"Invalid operands for {op}") from exc


CONTENT_OPERATORS = set("b B b* B* BDC BI BMC BT BX c cm CS cs d d0 d1 Do DP EI EMC ET EX f F f* G g gs h i ID j J K k l m M MP n q Q re RG rg ri s S SC sc SCN scn sh T* Tc Td TD Tf Tj TJ TL Tm Tr Ts Tw Tz v w W W* y ' \"".split())


def selected_pages(pages, total):
    if pages is None:
        return list(range(1, total+1))
    if isinstance(pages, str):
        result = set()
        for part in pages.split(","):
            m = re.fullmatch(r"\s*(\d+)(?:-(\d+))?\s*", part)
            if not m:
                raise PDFError(f"Invalid page selection: {part}")
            lo, hi = int(m[1]), int(m[2] or m[1])
            if lo < 1 or hi < lo or hi > total:
                raise PDFError(f"Page selection out of range: {part}")
            result.update(range(lo, hi+1))
        return sorted(result)
    result = sorted(set(pages))
    if any(not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= total for n in result):
        raise PDFError("Invalid page selection")
    return result


def extract_pages(path, pages=None) -> list[dict]:
    with PDF(path) as pdf:
        return [pdf.extract(n)[0] for n in selected_pages(pages, len(pdf.pages))]


def page_count(path) -> int:
    with PDF(path) as pdf:
        return len(pdf.pages)


class PDFArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise PDFError(message)


def main(argv=None):
    parser = PDFArgumentParser(description=__doc__)
    parser.add_argument("pdf")
    parser.add_argument("--pages")
    parser.add_argument("--out")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--fonts", action="store_true")
    start = time.perf_counter()
    output = None
    try:
        args = parser.parse_args(argv)
        if args.out and os.path.realpath(args.out) == os.path.realpath(args.pdf):
            raise PDFError("Output must not overwrite input PDF")
        if args.out and os.path.exists(args.out) and os.path.samefile(args.out, args.pdf):
            raise PDFError("Output must not overwrite input PDF")
        with PDF(args.pdf) as pdf:
            numbers = selected_pages(args.pages, len(pdf.pages))
            output = open(args.out, "w", encoding="utf-8", newline="\n") if args.out else sys.stdout
            chars = unmapped = text_pages = 0
            fonts = {}
            def emit(record):
                print(json.dumps(record, ensure_ascii=False), file=output)
            for n in numbers:
                record, used = pdf.extract(n)
                emit(record)
                chars += record["chars"]
                unmapped += record["unmapped"]
                text_pages += record["chars"] >= 20
                for font in used:
                    fonts[json.dumps(font, sort_keys=True)] = font
            if args.fonts:
                emit({"fonts": sorted(fonts.values(), key=lambda f: (f["name"], f["Subtype"], f["Encoding"]))})
            if args.summary:
                emit({"pages": len(pdf.pages), "processed": len(numbers), "text_pages": text_pages,
                      "unmapped_ratio": unmapped/(chars+unmapped) if chars+unmapped else 0,
                      "encrypted": False, "seconds": round(time.perf_counter()-start, 6)})
        return 0
    except EncryptedPDF:
        print(json.dumps({"status": "ENCRYPTED"}))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if output is not None and output is not sys.stdout:
            output.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
