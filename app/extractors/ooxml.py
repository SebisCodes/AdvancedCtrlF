"""Microsoft Office formats (docx, pptx, xlsx) - read directly from the ZIP/XML.

Deliberately without python-docx/python-pptx (lxml), so it runs on any Python version
and we keep full control over pages, slides and image alt texts.
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path

from .. import lang as langmod
from ..ocr import ImageOCR
from .base import Chunker, Context, ExtractError, Result, safe_xml

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
SSML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
VML = "urn:schemas-microsoft-com:vml"

MAX_PART = 100 * 1024 * 1024


def q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _read(z: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART:
        raise ExtractError(f"Part {name} is too large")
    return z.read(info)


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    """Relationships of a part: rId -> absolute part path inside the ZIP."""
    folder, fname = posixpath.split(part)
    data = _read(z, posixpath.join(folder, "_rels", fname + ".rels"))
    out = {}
    if data:
        for rel in safe_xml(data):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
            out[rel.get("Id")] = path
    return out


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ExtractError("Not a valid Office file (possibly a legacy binary format or encrypted)") from e


def _image_rids(el) -> list[str]:
    """Relationship ids of all images embedded below `el` (DrawingML and legacy VML)."""
    rids = [b.get(q(R, "embed")) for b in el.iter(q(A, "blip"))]
    rids += [v.get(q(R, "id")) for v in el.iter(f"{{{VML}}}imagedata")]
    return [r for r in rids if r]


def _ocr_media(ch: Chunker, z: zipfile.ZipFile, rels: dict, rids: list[str], loc: dict, images: ImageOCR) -> None:
    """OCR the images behind `rids` and add their text at `loc`."""
    for rid in rids:
        target = rels.get(rid)
        if not target or target not in z.NameToInfo:
            continue  # external/linked image
        text = images.read(target, lambda t=target: _read(z, t))
        if text:
            ch.add(text, dict(loc, part="Image (OCR)"))


# ----------------------------------------------------------------------- DOCX
CAPTION_STYLES = re.compile(r"caption|beschriftung|legende", re.I)


class _DocxWalker:
    def __init__(self, use_rendered_breaks: bool) -> None:
        self.page = 1
        self.para = 0
        self.rendered = use_rendered_breaks
        self.images: list[tuple[list[str], dict]] = []   # (relationship ids, location)

    def para_text(self, p) -> tuple[str, list[str]]:
        """Text of a paragraph plus image alt texts found in it. Counts page breaks."""
        parts: list[str] = []
        alts: list[str] = []
        for el in p.iter():
            tag = el.tag
            if tag == q(W, "t"):
                parts.append(el.text or "")
            elif tag == q(W, "tab"):
                parts.append("\t")
            elif tag == q(W, "br"):
                if el.get(q(W, "type")) == "page" and not self.rendered:
                    self.page += 1
                else:
                    parts.append("\n")
            elif tag == q(W, "lastRenderedPageBreak") and self.rendered:
                self.page += 1
            elif tag == q(WP, "docPr"):
                alt = " ".join(x for x in (el.get("title"), el.get("descr")) if x)
                if alt.strip():
                    alts.append(alt)
        return "".join(parts), alts


def _docx_body(ch: Chunker, root, walker: _DocxWalker, part: str | None) -> None:
    body = root.find(q(W, "body"))
    container = body if body is not None else root
    for el in container:
        _docx_block(ch, el, walker, part)


def _docx_block(ch: Chunker, el, walker: _DocxWalker, part: str | None) -> None:
    def loc(extra_part=None):
        d = {"kind": "page", "page": walker.page, "para": walker.para, "approx": True}
        if extra_part or part:
            d["part"] = extra_part or part
        return d

    if el.tag == q(W, "p"):
        walker.para += 1
        start_loc = loc()
        text, alts = walker.para_text(el)
        style = el.find(f"{q(W, 'pPr')}/{q(W, 'pStyle')}")
        is_caption = style is not None and CAPTION_STYLES.search(style.get(q(W, "val"), "") or "")
        if is_caption:
            ch.add(text, dict(start_loc, part="Caption"))
        else:
            ch.add(text, start_loc, key=("docx", part, start_loc["page"]))
        for alt in alts:
            ch.add(alt, dict(start_loc, part="Image (alt text)"))
        rids = _image_rids(el)
        if rids:
            walker.images.append((rids, start_loc))
    elif el.tag == q(W, "tbl"):
        for row in el.iter(q(W, "tr")):
            cells = []
            row_loc = loc("Table" if not part else part)
            for tc in row.findall(q(W, "tc")):
                cell_txt = []
                for p in tc.iter(q(W, "p")):
                    t, _ = walker.para_text(p)
                    cell_txt.append(t)
                cells.append(" ".join(cell_txt).strip())
            ch.add(" | ".join(c for c in cells if c), row_loc, key=("tbl", part, row_loc["page"]))
            rids = _image_rids(row)
            if rids:
                walker.images.append((rids, row_loc))
    elif el.tag == q(W, "sdt"):
        content = el.find(q(W, "sdtContent"))
        if content is not None:
            for sub in content:
                _docx_block(ch, sub, walker, part)


def extract_docx(path: Path, ctx: Context) -> Result:
    ch = Chunker()
    with _open_zip(path) as z:
        rels = _rels(z, "_rels/.rels")
        main = next((p for p in rels.values() if p.endswith(".xml") and "document" in p), "word/document.xml")
        data = _read(z, main)
        if data is None:
            raise ExtractError("word/document.xml is missing")
        rendered = b"lastRenderedPageBreak" in data
        walker = _DocxWalker(rendered)
        _docx_body(ch, safe_xml(data), walker, None)
        pages = walker.page
        images = ImageOCR(ctx)
        if images.enabled and walker.images:
            main_rels = _rels(z, main)
            for rids, loc in walker.images:
                if ctx.cancelled():
                    raise ExtractError("Cancelled")
                _ocr_media(ch, z, main_rels, rids, {k: v for k, v in loc.items() if k != "part"}, images)
        extra = [("header", "Header"), ("footer", "Footer"), ("footnotes", "Footnote"),
                 ("endnotes", "Endnote"), ("comments", "Comment")]
        for name in sorted(z.namelist()):
            for key, label in extra:
                if re.fullmatch(rf"word/{key}\d*\.xml", name):
                    w2 = _DocxWalker(False)
                    root = safe_xml(_read(z, name))
                    for el in root.iter():
                        if el.tag in (q(W, "p"),):
                            t, alts = w2.para_text(el)
                            ch.add(t, {"kind": "part", "part": label}, key=("x", label))
    chunks = ch.result()
    return Result(chunks, {"pages": pages, "pages_approx": True, "ocr_images": images.count,
                           "language": langmod.detect(" ".join(c["text"] for c in chunks[:200]))[0]})


# ----------------------------------------------------------------------- PPTX
def _shape_box(sp):
    off = sp.find(f".//{q(A, 'off')}")
    ext = sp.find(f".//{q(A, 'ext')}")
    if off is None or ext is None:
        return None
    try:
        x, y = int(off.get("x")), int(off.get("y"))
        return x, y, x + int(ext.get("cx")), y + int(ext.get("cy"))
    except (TypeError, ValueError):
        return None


def _txbody_text(el) -> str:
    paras = []
    for p in el.iter(q(A, "p")):
        paras.append("".join((t.text or "") for t in p.iter(q(A, "t"))))
    return "\n".join(paras)


def _walk_shapes(tree, out_text: list, pics: list, title: list) -> None:
    for sp in tree:
        tag = sp.tag
        if tag == q(P, "grpSp"):
            _walk_shapes(sp, out_text, pics, title)
        elif tag == q(P, "sp"):
            ph = sp.find(f".//{q(P, 'ph')}")
            ph_type = ph.get("type") if ph is not None else None
            if ph_type in ("sldNum", "dt", "ftr"):
                continue
            txt = _txbody_text(sp)
            if not txt.strip():
                continue
            if ph_type in ("title", "ctrTitle"):
                title.append(txt)
            out_text.append((txt, _shape_box(sp)))
        elif tag == q(P, "pic"):
            nv = sp.find(f".//{q(P, 'cNvPr')}")
            alt = ""
            if nv is not None:
                alt = " ".join(x for x in (nv.get("title"), nv.get("descr")) if x)
            pics.append((alt, _shape_box(sp)))
        elif tag == q(P, "graphicFrame"):
            for tr in sp.iter(q(A, "tr")):
                cells = [_txbody_text(tc).replace("\n", " ") for tc in tr.findall(q(A, "tc"))]
                row = " | ".join(c for c in cells if c.strip())
                if row:
                    out_text.append((row, None))


def _is_caption(text_box, pic_box) -> bool:
    """Is the text box directly below (or above) a picture and horizontally overlapping?"""
    if not text_box or not pic_box:
        return False
    tx0, ty0, tx1, ty1 = text_box
    px0, py0, px1, py1 = pic_box
    overlap = min(tx1, px1) - max(tx0, px0)
    if overlap <= 0.3 * min(tx1 - tx0, px1 - px0):
        return False
    gap_below = ty0 - py1
    gap_above = py0 - ty1
    tol = 0.25 * (py1 - py0) + 300000  # ~0.8 cm
    small = (ty1 - ty0) < 0.6 * (py1 - py0) + 1
    return small and (-tol * 0.3 <= gap_below <= tol or -tol * 0.3 <= gap_above <= tol * 0.5)


def extract_pptx(path: Path, ctx: Context) -> Result:
    ch = Chunker()
    images = ImageOCR(ctx)
    with _open_zip(path) as z:
        rels = _rels(z, "_rels/.rels")
        pres = next((p for p in rels.values() if p.endswith("presentation.xml")), "ppt/presentation.xml")
        pres_rels = _rels(z, pres)
        root = safe_xml(_read(z, pres) or b"<x/>")
        slide_parts = [pres_rels.get(s.get(q(R, "id"))) for s in root.iter(q(P, "sldId"))]
        slide_parts = [s for s in slide_parts if s]
        if not slide_parts:  # fallback: sort by file name
            slide_parts = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                                 key=lambda n: int(re.findall(r"\d+", n)[-1]))
        n = len(slide_parts)
        for idx, part in enumerate(slide_parts, 1):
            if ctx.cancelled():
                raise ExtractError("Cancelled")
            ctx.progress(f"Slide {idx}/{n}")
            data = _read(z, part)
            if not data:
                continue
            sroot = safe_xml(data)
            tree = sroot.find(f".//{q(P, 'spTree')}")
            texts, pics, title = [], [], []
            if tree is not None:
                _walk_shapes(tree, texts, pics, title)
            base = {"kind": "slide", "slide": idx}
            if title:
                base["title"] = title[0].replace("\n", " ")[:120]
            caption_ids = set()
            for alt, pbox in pics:
                for i, (t, tbox) in enumerate(texts):
                    if i not in caption_ids and _is_caption(tbox, pbox):
                        caption_ids.add(i)
                        ch.add(t, dict(base, part="Caption"))
                if alt.strip():
                    ch.add(alt, dict(base, part="Image (alt text)"))
            for i, (t, _) in enumerate(texts):
                if i not in caption_ids:
                    ch.add(t, base, key=("slide", idx))
            slide_rels = _rels(z, part)
            if images.enabled:  # pictures, picture fills and slide backgrounds
                _ocr_media(ch, z, slide_rels, _image_rids(sroot), base, images)
            # speaker notes
            for target in slide_rels.values():
                if "notesSlide" in target:
                    nd = _read(z, target)
                    if nd:
                        ntree = safe_xml(nd).find(f".//{q(P, 'spTree')}")
                        nt = []
                        if ntree is not None:
                            _walk_shapes(ntree, nt, [], [])
                        ch.add("\n".join(t for t, _ in nt), dict(base, part="Notes"))
    chunks = ch.result()
    return Result(chunks, {"pages": n, "ocr_images": images.count,
                           "language": langmod.detect(" ".join(c["text"] for c in chunks[:200]))[0]})


# ----------------------------------------------------------------------- XLSX
def col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def add_row(ch: Chunker, sheet: str, rownum: int, values: list) -> None:
    """One table row as a chunk; loc.cells records where each cell starts in the text."""
    parts, cells, pos = [], [], 0
    for ci, v in enumerate(values):
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        ref = f"{col_letter(ci)}{rownum}"
        cells.append([pos, ref])
        parts.append(s)
        pos += len(s) + 3
    if parts:
        ch.chunks.append({"loc": {"kind": "cell", "sheet": sheet, "row": rownum, "cells": cells},
                          "text": " | ".join(parts)[:20000]})


MAX_ROWS = 300_000


def _xlsx_images(path: Path, ch: Chunker, ctx: Context, images: ImageOCR) -> None:
    """OCR pictures placed on worksheets, located at the cell they are anchored to."""
    with _open_zip(path) as z:
        wb = next((p for p in _rels(z, "_rels/.rels").values() if p.endswith("workbook.xml")), "xl/workbook.xml")
        wb_rels = _rels(z, wb)
        for sheet in safe_xml(_read(z, wb) or b"<x/>").iter(q(SSML, "sheet")):
            name, part = sheet.get("name", ""), wb_rels.get(sheet.get(q(R, "id")))
            if not part:
                continue
            for drawing in _rels(z, part).values():
                if "/drawings/" not in drawing or not drawing.endswith(".xml"):
                    continue
                drawing_rels = _rels(z, drawing)
                for anchor in safe_xml(_read(z, drawing) or b"<x/>"):
                    if ctx.cancelled():
                        raise ExtractError("Cancelled")
                    loc = {"kind": "cell", "sheet": name}
                    frm = anchor.find(q(XDR, "from"))
                    try:
                        col, row = int(frm.findtext(q(XDR, "col"))), int(frm.findtext(q(XDR, "row"))) + 1
                        loc.update(row=row, cells=[[0, f"{col_letter(col)}{row}"]])
                    except (AttributeError, TypeError, ValueError):
                        pass  # absolute anchor: sheet only
                    _ocr_media(ch, z, drawing_rels, _image_rids(anchor), loc, images)


def extract_xlsx(path: Path, ctx: Context) -> Result:
    try:
        import openpyxl
    except ImportError as e:
        raise ExtractError("openpyxl is missing") from e
    ch = Chunker()
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:
        raise ExtractError(f"Excel file could not be read: {e}") from e
    try:
        for ws in wb.worksheets:
            ctx.progress(f"Sheet {ws.title}")
            for r, row in enumerate(ws.iter_rows(values_only=True), 1):
                if r > MAX_ROWS or ctx.cancelled():
                    break
                add_row(ch, ws.title, r, list(row))
        sheets = len(wb.worksheets)
    finally:
        wb.close()
    images = ImageOCR(ctx)
    if images.enabled:
        try:
            _xlsx_images(path, ch, ctx, images)
        except ExtractError:
            raise
        except Exception:
            pass  # pictures are a bonus - never fail the whole workbook because of them
    return Result(ch.result(), {"pages": sheets, "ocr_images": images.count})


def extract_xls(path: Path, ctx: Context) -> Result:
    try:
        import xlrd
    except ImportError as e:
        raise ExtractError("xlrd is missing") from e
    ch = Chunker()
    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except Exception as e:
        raise ExtractError(f"Excel file could not be read: {e}") from e
    for sh in book.sheets():
        for r in range(min(sh.nrows, MAX_ROWS)):
            add_row(ch, sh.name, r + 1, sh.row_values(r))
    return Result(ch.result(), {"pages": book.nsheets})
