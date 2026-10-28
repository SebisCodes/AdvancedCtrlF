"""OpenDocument: odt (text), odp (presentation), ods (spreadsheet) - including image titles/descriptions."""
from __future__ import annotations

import zipfile
from pathlib import Path

from .. import lang as langmod
from .base import Chunker, Context, ExtractError, Result, safe_xml
from .ooxml import MAX_ROWS, add_row

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
}


def q(prefix: str, tag: str) -> str:
    return f"{{{NS[prefix]}}}{tag}"


TEXT_BLOCKS = {q("text", "p"), q("text", "h")}
SKIP = {q("text", "tracked-changes"), q("office", "annotation")}


def _inline_text(el) -> str:
    """Text of a paragraph including <text:s/>, <text:tab/>, <text:line-break/>."""
    out = [el.text or ""]
    for child in el:
        if child.tag in SKIP:
            pass
        elif child.tag == q("text", "s"):
            out.append(" " * int(child.get(q("text", "c"), "1") or 1))
        elif child.tag == q("text", "tab"):
            out.append("\t")
        elif child.tag == q("text", "line-break"):
            out.append("\n")
        elif child.tag in (q("draw", "frame"), q("text", "note")):
            pass  # handled separately
        else:
            out.append(_inline_text(child))
        out.append(child.tail or "")
    return "".join(out)


def _frame_texts(el) -> list[tuple[str, str]]:
    """(part, text) for image titles/descriptions in draw:frame."""
    res = []
    for fr in el.iter(q("draw", "frame")):
        for tag, label in ((q("svg", "title"), "Image (title)"), (q("svg", "desc"), "Image (alt text)")):
            t = fr.find(tag)
            if t is not None and (t.text or "").strip():
                res.append((label, t.text))
    return res


def _content(path: Path) -> object:
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("content.xml")
            if info.file_size > 200 * 1024 * 1024:
                raise ExtractError("content.xml too large")
            return safe_xml(z.read(info))
    except (zipfile.BadZipFile, KeyError) as e:
        raise ExtractError("Not a valid OpenDocument file") from e


def extract_odt(path: Path, ctx: Context) -> Result:
    root = _content(path)
    ch = Chunker()
    body = root.find(f"{q('office', 'body')}/{q('office', 'text')}")
    para = 0

    def walk(el):
        nonlocal para
        for child in el:
            if child.tag in SKIP:
                continue
            if child.tag in TEXT_BLOCKS:
                para += 1
                loc = {"kind": "paragraph", "para": para}
                is_cap = "caption" in (child.get(q("text", "style-name")) or "").lower() or \
                         "illustration" in (child.get(q("text", "style-name")) or "").lower()
                ch.add(_inline_text(child), dict(loc, part="Caption") if is_cap else loc,
                       key=None if is_cap else ("odt",))
                for label, t in _frame_texts(child):
                    ch.add(t, dict(loc, part=label))
                for note in child.iter(q("text", "note")):
                    body_el = note.find(q("text", "note-body"))
                    if body_el is not None:
                        ch.add(" ".join(_inline_text(p) for p in body_el.iter(q("text", "p"))),
                               dict(loc, part="Footnote"))
            elif child.tag == q("table", "table"):
                for row in child.iter(q("table", "table-row")):
                    para += 1
                    cells = [" ".join(_inline_text(p) for p in c.iter(q("text", "p")))
                             for c in row.findall(q("table", "table-cell"))]
                    ch.add(" | ".join(c for c in cells if c.strip()), {"kind": "paragraph", "para": para, "part": "Table"})
            else:
                walk(child)

    if body is not None:
        walk(body)
    chunks = ch.result()
    return Result(chunks, {"language": langmod.detect(" ".join(c["text"] for c in chunks[:200]))[0]})


def extract_odp(path: Path, ctx: Context) -> Result:
    root = _content(path)
    ch = Chunker()
    pages = list(root.iter(q("draw", "page")))
    for idx, page in enumerate(pages, 1):
        base = {"kind": "slide", "slide": idx}
        notes = page.find(q("presentation", "notes"))
        for frame in page.iter(q("draw", "frame")):
            if notes is not None and any(frame is f for f in notes.iter(q("draw", "frame"))):
                continue
            texts = [_inline_text(p) for p in frame.iter() if p.tag in TEXT_BLOCKS]
            cls = frame.get(q("presentation", "class")) or ""
            if cls == "title" and texts and "title" not in base:
                base["title"] = " ".join(texts)[:120]
            ch.add("\n".join(texts), base, key=("slide", idx))
            for label, t in _frame_texts(frame):
                ch.add(t, dict(base, part=label))
        if notes is not None:
            ch.add("\n".join(_inline_text(p) for p in notes.iter() if p.tag in TEXT_BLOCKS), dict(base, part="Notes"))
    chunks = ch.result()
    return Result(chunks, {"pages": len(pages), "language": langmod.detect(" ".join(c["text"] for c in chunks[:200]))[0]})


def extract_ods(path: Path, ctx: Context) -> Result:
    root = _content(path)
    ch = Chunker()
    tables = list(root.iter(q("table", "table")))
    for tbl in tables:
        name = tbl.get(q("table", "name")) or "Table"
        r = 0
        for row in tbl.iter(q("table", "table-row")):
            rep = int(row.get(q("table", "number-rows-repeated"), "1") or 1)
            values = []
            for cell in row:
                if cell.tag not in (q("table", "table-cell"), q("table", "covered-table-cell")):
                    continue
                crep = min(int(cell.get(q("table", "number-columns-repeated"), "1") or 1), 1024)
                txt = "\n".join(_inline_text(p) for p in cell.iter(q("text", "p")))
                values.extend([txt or None] * crep)
            if any(values):  # repeated rows with content (rare) are indexed only once
                add_row(ch, name, r + 1, values)
            r += rep
            if r > MAX_ROWS:
                break
    return Result(ch.result(), {"pages": len(tables)})
