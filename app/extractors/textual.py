"""Text-based formats: txt/md/code/log/json, csv/tsv, html, xml, rtf, eml/mhtml, epub."""
from __future__ import annotations

import csv
import io
import json
import posixpath
import re
import zipfile
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from xml.parsers import expat

from .. import lang as langmod
from .base import (Chunker, Context, ExtractError, Result, add_lines, clean, decode_bytes,
                   safe_xml)
from .ooxml import col_letter

MAX_TEXT_BYTES = 300 * 1024 * 1024


def _read(path: Path) -> bytes:
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ExtractError("File too large for text processing")
    return path.read_bytes()


def _lang(chunks) -> str:
    return langmod.detect(" ".join(c["text"] for c in chunks[:200]))[0]


def chunk_lines(pairs: list[tuple[int, str]], base: dict, max_len: int = 700) -> list[dict]:
    """Merge (line number, text) pairs into chunks. Source lines are not consecutive
    (markup, blank lines), so loc["lines"] maps text offsets to source lines."""
    out: list[dict] = []
    buf: list[str] = []
    marks: list[list[int]] = []
    size = 0

    def flush():
        loc = dict(base, line=marks[0][1])
        if any(ln != marks[0][1] + i for i, (_, ln) in enumerate(marks)):
            loc["lines"] = [m[:] for m in marks]
        out.append({"loc": loc, "text": "\n".join(buf)})

    for line, text in pairs:
        text = clean(text)
        if not text:
            continue
        if buf and size + len(text) > max_len:
            flush()
            buf, marks, size = [], [], 0
        marks.append([size, line])
        buf.append(text)
        size += len(text) + 1
    if buf:
        flush()
    return out


# ------------------------------------------------------------------ Plain text
def extract_text(path: Path, ctx: Context) -> Result:
    text = decode_bytes(_read(path))
    ch = Chunker()
    add_lines(ch, text, {"kind": "line"})
    return Result(ch.chunks, {"pages": text.count("\n") + 1, "language": _lang(ch.chunks)})


# ------------------------------------------------------------------ CSV/TSV
def extract_csv(path: Path, ctx: Context) -> Result:
    text = decode_bytes(_read(path))
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header: list[str] = []
    try:
        has_header = csv.Sniffer().has_header(sample)
    except csv.Error:
        has_header = False
    chunks = []
    for row in reader:
        line = reader.line_num
        if line == 1 and has_header:
            header = [h.strip() for h in row]
        parts, cells, pos = [], [], 0
        for ci, v in enumerate(row):
            v = v.strip()
            if not v:
                continue
            name = col_letter(ci)
            if ci < len(header) and header[ci] and line > 1:
                name += f" ({header[ci][:30]})"
            cells.append([pos, name])
            parts.append(v)
            pos += len(v) + 3
        if parts:
            chunks.append({"loc": {"kind": "row", "line": line, "cells": cells}, "text": " | ".join(parts)[:20000]})
        if ctx.cancelled():
            raise ExtractError("Cancelled")
    return Result(chunks, {"pages": len(chunks), "language": _lang(chunks)})


# ------------------------------------------------------------------ HTML
class _HtmlText(HTMLParser):
    """Collects visible text with source line numbers. Tolerant of sloppy HTML
    (missing </head>, </p>, </body> ... are all legal or common)."""
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "main",
             "header", "footer", "nav", "aside", "table", "blockquote", "pre", "figcaption", "figure", "dt", "dd",
             "ul", "ol", "dl", "form", "hr", "caption", "address", "details", "summary"}
    SKIP = {"script", "style", "noscript", "template", "svg", "math", "object", "iframe"}
    HEAD_ONLY = {"head", "title", "meta", "link", "base", "style", "script", "noscript", "template", "html"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[tuple[int, str]] = []
        self.cur: list[str] = []
        self.cur_line = 1
        self.skip: dict[str, int] = {}
        self.in_head = False
        self.in_title = False
        self.title = ""
        self.scripts: list[tuple[int, str]] = []

    def _flush(self):
        t = "".join(self.cur).strip()
        if t:
            self.lines.append((self.cur_line, t))
        self.cur = []

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.in_head = True
        elif self.in_head and tag not in self.HEAD_ONLY:
            self.in_head = False  # <body> or content started without </head>
        if tag == "title" and not any(self.skip.values()):
            self.in_title = True
        if tag in self.SKIP:
            self.skip[tag] = self.skip.get(tag, 0) + 1
        if tag in self.BLOCK:
            self._flush()
        if tag in ("img", "area", "input") and not any(self.skip.values()):
            a = dict(attrs)
            alt = a.get("alt") or a.get("title")
            if alt and alt.strip():
                self._flush()
                self.lines.append((self.getpos()[0], f"[Image: {alt.strip()}]"))
        if tag in ("td", "th") and "".join(self.cur).strip():
            self.cur.append(" | ")

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False
        if tag == "title":
            self.in_title = False
        if self.skip.get(tag):
            self.skip[tag] -= 1
        if tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self.skip.get("script") and sum(self.skip.values()) == self.skip["script"]:
            self.scripts.append((self.getpos()[0], data))  # analysed later for dynamic pages
            return
        if self.in_title:
            self.title += data
            return
        if self.in_head or any(self.skip.values()):
            return
        if not self.cur:
            lead = len(data) - len(data.lstrip())
            self.cur_line = self.getpos()[0] + data[:lead].count("\n")
        self.cur.append(data)

    def close(self):
        super().close()
        self._flush()


# --- Text that only exists inside JavaScript -------------------------------
# Many "app-like" HTML files (editors, exported reports, single-page apps) ship an
# empty container plus a script that builds the page. A browser's Ctrl+F finds that
# text because it runs the script; we approximate it by reading the string literals.
_JS_TOKEN = re.compile(r"""//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`""", re.S)
_JS_ESC = re.compile(r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
_JS_SIMPLE = {"n": "\n", "t": "\t", "r": "", "b": "", "f": "", "v": "", "0": ""}
_CODE_LIKE = re.compile(r"=>|=\"|\bon[a-z]+=|[{};]\s*$|^\s*[.#]?[\w-]+\s*\{|\bfunction\b|\breturn\b|^[\w.$-]+\(|"
                        r"^\s*[\w-]+\s*:\s*[^:]+;|^(https?:|data:|mailto:|#|\.|/)|^[\w-]+(=|\[)")
_WORD = re.compile(r"[^\W\d_]{2,}")


def _js_unescape(m: re.Match) -> str:
    e = m.group(1)
    if e[0] == "u":
        return chr(int(e[2:-1] if e[1] == "{" else e[1:], 16))
    if e[0] == "x":
        return chr(int(e[1:], 16))
    return _JS_SIMPLE.get(e, e)


def _looks_like_text(s: str) -> bool:
    s = s.strip()
    if len(s) < 3 or len(s) > 20000 or _CODE_LIKE.search(s):
        return False
    words = _WORD.findall(s)
    if not words:
        return False
    letters = sum(len(w) for w in words)
    if " " not in s:
        # single token: keep real words (e.g. "Wohnfläche"), drop identifiers like "btnSave", "display"
        return len(s) >= 4 and s[0].isupper() and s[1:].islower() and letters == len(s)
    return letters / len(s) >= 0.55


_TAG_HEAD = re.compile(r"<[a-zA-Z/!][^<>]*$")              # '...<div class="'  (tag continues in next string)
_TAG_TAIL = re.compile(r"^[^<>]*?(?:[\"')=;]|\d%?)\s*>")      # '%" onclick="f(x)">'  (rest of a tag)


def _strip_tag_fragments(s: str) -> str:
    """Scripts often build markup from pieces: '<a href="' + url + '">' + label. Drop the
    half tags at both ends so only real text (and complete tags) remain."""
    s = _TAG_TAIL.sub(" ", s, count=1)
    return _TAG_HEAD.sub(" ", s)


def script_pairs(scripts: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Readable text from string literals inside <script> blocks, with source lines."""
    out: list[tuple[int, str]] = []
    for start_line, code in scripts:
        for m in _JS_TOKEN.finditer(code):
            tok = m.group()
            if tok[0] not in "'\"`":
                continue  # comment
            body = tok[1:-1]
            if tok[0] == "`":
                body = re.sub(r"\$\{[^}]*\}", " ", body)
            if "base64," in body[:200] or len(body) < 3:
                continue
            body = _strip_tag_fragments(_JS_ESC.sub(_js_unescape, body))
            line = start_line + code.count("\n", 0, m.start())
            if "<" in body and ">" in body:  # HTML snippet inside a string
                inner = _HtmlText()
                inner.feed(body)
                inner.close()
                texts = [t for _, t in inner.lines]
            else:
                texts = [body]
            for t in texts:
                if _looks_like_text(t):
                    out.append((line, t.strip()))
    return out


def html_pairs(text: str) -> tuple[list[tuple[int, str]], str]:
    p = _HtmlText()
    p.feed(text)
    p.close()
    pairs = p.lines
    if p.title.strip():
        pairs.insert(0, (1, p.title.strip()))
    visible = sum(len(t) for _, t in pairs)
    if p.scripts:
        extra = script_pairs(p.scripts)
        # Only for pages whose content is generated by scripts - keeps normal web pages free of script noise
        if extra and (visible < 3000 or sum(len(t) for _, t in extra) > visible):
            seen = {t for _, t in pairs}
            pairs += [(ln, t) for ln, t in extra if t not in seen and not seen.add(t)]
    return pairs, p.title.strip()


def extract_html(path: Path, ctx: Context) -> Result:
    pairs, _ = html_pairs(decode_bytes(_read(path)))
    chunks = chunk_lines(pairs, {"kind": "line"})
    return Result(chunks, {"language": _lang(chunks)})


# ------------------------------------------------------------------ XML
def extract_xml(path: Path, ctx: Context) -> Result:
    data = _read(path)
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    pairs: list[tuple[int, str]] = []
    cur: list[str] = []
    cur_line = [1]

    def entity_decl(*_):
        raise ExtractError("XML with ENTITY definitions is not read for security reasons")

    def flush():
        t = "".join(cur).strip()
        if t:
            pairs.append((cur_line[0], t))
        cur.clear()

    def start(name, attrs):
        flush()
        vals = [v for v in attrs.values() if isinstance(v, str) and len(v) > 2 and not v.startswith(("http:", "https:", "#"))]
        if vals:
            pairs.append((parser.CurrentLineNumber, " ".join(vals)))

    def chardata(data):
        if not cur:
            cur_line[0] = parser.CurrentLineNumber
        cur.append(data)

    parser.EntityDeclHandler = entity_decl
    parser.StartElementHandler = start
    parser.EndElementHandler = lambda name: flush()
    parser.CharacterDataHandler = chardata
    try:
        parser.Parse(data, True)
    except expat.ExpatError:
        # broken XML: treat as plain text
        return extract_text(path, ctx)
    flush()
    chunks = chunk_lines(pairs, {"kind": "line"})
    return Result(chunks, {"language": _lang(chunks)})


# ------------------------------------------------------------------ RTF
_RTF_SKIP_DEST = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header", "footer",
                  "listtable", "listoverridetable", "rsidtbl", "generator", "themedata", "datastore",
                  "latentstyles", "xmlnstbl", "mmathPr", "filetbl", "revtbl"}
_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}]+)", re.S)


def rtf_to_text(rtf: str) -> str:
    out: list[str] = []
    stack: list[tuple[bool, int]] = []
    skip, uc, pending_skip = False, 1, 0
    for m in _RTF_TOKEN.finditer(rtf):
        word, arg, hexc, sym, brace, text = m.groups()
        if brace == "{":
            stack.append((skip, uc))
        elif brace == "}":
            skip, uc = stack.pop() if stack else (False, 1)
        elif skip:
            if sym == "*":
                pass
            continue
        elif word:
            if word in _RTF_SKIP_DEST:
                skip = True
            elif word in ("par", "line", "row", "sect", "page"):
                out.append("\n")
            elif word in ("tab", "cell"):
                out.append("\t")
            elif word == "uc":
                uc = int(arg or 1)
            elif word == "u":
                code = int(arg or 0)
                out.append(chr(code + 65536 if code < 0 else code))
                pending_skip = uc
        elif hexc:
            if pending_skip:
                pending_skip -= 1
                continue
            out.append(bytes([int(hexc, 16)]).decode("cp1252", "replace"))
        elif sym:
            if sym == "*":
                skip = True
            elif sym in "\\{}":
                out.append(sym)
            elif sym == "~":
                out.append(" ")
        elif text:
            if pending_skip:
                drop = min(pending_skip, len(text))
                text, pending_skip = text[drop:], pending_skip - drop
            out.append(text.replace("\r", "").replace("\n", ""))
    return "".join(out)


def extract_rtf(path: Path, ctx: Context) -> Result:
    text = rtf_to_text(_read(path).decode("latin-1"))
    ch = Chunker()
    add_lines(ch, text, {"kind": "line"})
    return Result(ch.chunks, {"language": _lang(ch.chunks)})


# ------------------------------------------------------------------ E-mail / MHTML
def extract_eml(path: Path, ctx: Context) -> Result:
    msg = BytesParser(policy=policy.default).parsebytes(_read(path))
    head = [f"{h}: {msg.get(h)}" for h in ("Subject", "From", "To", "Cc", "Date") if msg.get(h)]
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            text = body.get_content()
        except Exception:
            text = ""
        if body.get_content_type() == "text/html":
            text = "\n".join(t for _, t in html_pairs(text)[0])
    atts = [a.get_filename() for a in msg.iter_attachments() if a.get_filename()]
    if atts:
        text += "\n\nAttachments: " + ", ".join(atts)
    full = "\n".join(head) + "\n\n" + text
    ch = Chunker()
    add_lines(ch, full, {"kind": "line"})
    return Result(ch.chunks, {"language": _lang(ch.chunks)})


# ------------------------------------------------------------------ EPUB
def extract_epub(path: Path, ctx: Context) -> Result:
    chunks: list[dict] = []
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ExtractError("Not a valid EPUB file") from e
    with z:
        names = set(z.namelist())
        order: list[str] = []
        try:
            cont = safe_xml(z.read("META-INF/container.xml"))
            opf_path = next(el.get("full-path") for el in cont.iter() if el.tag.endswith("rootfile"))
            opf = safe_xml(z.read(opf_path))
            base = posixpath.dirname(opf_path)
            items = {el.get("id"): el.get("href") for el in opf.iter() if el.tag.endswith("}item")}
            for ref in opf.iter():
                if ref.tag.endswith("itemref") and ref.get("idref") in items:
                    order.append(posixpath.normpath(posixpath.join(base, items[ref.get("idref")])))
        except Exception:
            order = sorted(n for n in names if n.endswith((".xhtml", ".html", ".htm")))
        for chap, name in enumerate((n for n in order if n in names), 1):
            pairs, _ = html_pairs(decode_bytes(z.read(name)))
            chunks += chunk_lines(pairs, {"kind": "chapter", "chapter": chap})
    return Result(chunks, {"pages": len(order), "language": _lang(chunks)})


# ------------------------------------------------------------------ Jupyter notebook
def extract_ipynb(path: Path, ctx: Context) -> Result:
    try:
        nb = json.loads(decode_bytes(_read(path)))
    except ValueError:
        return extract_text(path, ctx)   # not valid JSON: index as plain text
    cells = nb.get("cells")
    if cells is None:   # nbformat 3
        cells = [c for ws in nb.get("worksheets", []) for c in ws.get("cells", [])]

    def text_of(value) -> str:
        return "".join(value) if isinstance(value, list) else str(value or "")

    chunks = []
    for i, cell in enumerate(cells, 1):
        kind = cell.get("cell_type", "code")
        src = text_of(cell.get("source", cell.get("input", "")))
        if src.strip():
            chunks.append({"loc": {"kind": "nbcell", "cell": i, "line": 1, "part": kind.capitalize()},
                           "text": src[:20000]})
        outputs = []
        for out in cell.get("outputs", []):
            outputs.append(text_of(out.get("text") or (out.get("data") or {}).get("text/plain")
                                   or out.get("evalue") or ""))
        out_text = "\n".join(o for o in outputs if o.strip())
        if out_text:
            chunks.append({"loc": {"kind": "nbcell", "cell": i, "part": "Output"}, "text": out_text[:20000]})
    return Result(chunks, {"pages": len(cells), "language": _lang(chunks)})
