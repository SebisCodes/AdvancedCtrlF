"""PDF, XPS and other MuPDF formats plus images - including OCR for scanned pages
and for every image embedded in a page."""
from __future__ import annotations

from pathlib import Path

from .. import lang as langmod
from ..ocr import ImageOCR, LanguageTracker, native_dpi
from .base import Chunker, Context, ExtractError, Result


def _pymupdf():
    try:
        import pymupdf
    except ImportError:  # older versions
        import fitz as pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)
    return pymupdf


def _ocr_page(page, ctx: Context, tracker: LanguageTracker):
    dpi = native_dpi(page, ctx.ocr.dpi)  # never enlarge beyond the scan's own resolution
    tp = ctx.ocr.textpage(page, tracker.current(), dpi)
    text = page.get_text("text", textpage=tp)
    better = tracker.learn(text)
    if better:
        tp = ctx.ocr.textpage(page, better, dpi)
    return tp


def _add_page(ch: Chunker, page, pno: int, textpage=None, loc_kind: str = "page") -> int:
    line = 1
    chars = 0
    for b in page.get_text("blocks", textpage=textpage, sort=True):
        if b[6] != 0:  # text blocks only
            continue
        text = b[4]
        ch.add(text, {"kind": loc_kind, "page": pno, "line": line}, key=("p", pno))
        line += text.count("\n") + (0 if text.endswith("\n") else 1)
        chars += len(text.strip())
    # comments/notes in PDF annotations
    try:
        for a in page.annots() or []:
            content = (a.info or {}).get("content", "")
            if content.strip():
                ch.add(content, {"kind": loc_kind, "page": pno, "part": "Comment"})
    except Exception:
        pass
    return chars


def _ocr_page_images(ch: Chunker, doc, page, pno: int, images: ImageOCR) -> None:
    """OCR every image placed on a page that otherwise has a normal text layer
    (screenshots, diagrams, photographed notes ...)."""
    if not images.wanted:
        return
    pymupdf = _pymupdf()
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return
    for info in infos:
        xref = info.get("xref") or 0
        bbox = pymupdf.Rect(info["bbox"]) & page.rect
        if bbox.is_empty or bbox.width < 20 or bbox.height < 10:
            continue
        if xref:   # stored image: read in full resolution, once per document
            key, source = ("xref", xref), (lambda x=xref: pymupdf.Pixmap(doc, x))
        else:      # inline image: render its area of the page
            key = ("inline", pno, tuple(round(v) for v in bbox))
            source = (lambda b=bbox: page.get_pixmap(clip=b, dpi=300))
        text = images.read(key, source)
        if text:
            ch.add(text, {"kind": "page", "page": pno, "part": "Image (OCR)"})


def extract_pdf(path: Path, ctx: Context) -> Result:
    pymupdf = _pymupdf()
    try:
        doc = pymupdf.open(str(path))
    except Exception as e:
        raise ExtractError(f"File could not be read: {e}") from e
    if doc.needs_pass and not doc.authenticate(""):
        raise ExtractError("Document is password protected")
    cfg = ctx.config
    ch = Chunker()
    tracker = LanguageTracker(ctx.ocr)
    ocr_ok = bool(cfg.get("ocr_enabled")) and ctx.ocr is not None and ctx.ocr.available()
    ocr_pages, skipped_ocr, sample = 0, 0, []
    images = ImageOCR(ctx)
    n = doc.page_count
    with doc:
        for i, page in enumerate(doc):
            if ctx.cancelled():
                raise ExtractError("Cancelled")
            ctx.progress(f"Page {i + 1}/{n}")
            text = page.get_text("text")
            scanned = len(text.strip()) < cfg.get("ocr_min_chars", 25) and bool(page.get_images(full=False))
            tp = None
            if scanned and doc.is_pdf:
                if ocr_ok:
                    ctx.progress(f"OCR page {i + 1}/{n}")
                    try:
                        tp = _ocr_page(page, ctx, tracker)
                        ocr_pages += 1
                    except Exception:
                        tp = None
                elif scanned:
                    skipped_ocr += 1
            _add_page(ch, page, i + 1, tp)
            if tp is None:
                _ocr_page_images(ch, doc, page, i + 1, images)
            if len(sample) < 30:
                sample.append(page.get_text("text", textpage=tp)[:2000] if tp else text[:2000])
    meta = {"pages": n, "ocr_pages": ocr_pages, "ocr_images": images.count,
            "ocr_skipped": images.skipped + (skipped_ocr if cfg.get("ocr_enabled") else 0)}
    if skipped_ocr:
        meta["warning"] = (f"{skipped_ocr} scanned page(s) not read: " +
                           ("OCR language data missing - ⚙ Menu → Languages and packs" if cfg.get("ocr_enabled") else "OCR is switched off in the menu"))
    meta["language"] = langmod.detect("\n".join(sample))[0]
    return Result(ch.result(), meta)


def extract_image(path: Path, ctx: Context) -> Result:
    pymupdf = _pymupdf()
    if not ctx.config.get("ocr_enabled"):
        return Result([], {"warning": "Image not read: OCR is switched off in the menu"})
    if ctx.ocr is None or not ctx.ocr.available():
        return Result([], {"warning": "Image not read: OCR language data missing - ⚙ Menu → Languages and packs", "ocr_skipped": 1})
    ch = Chunker()
    tracker = LanguageTracker(ctx.ocr)
    texts = []
    with pymupdf.open(str(path)) as img:
        n = img.page_count
        for i in range(n):
            if ctx.cancelled():
                raise ExtractError("Cancelled")
            ctx.progress(f"OCR image {i + 1}/{n}")
            with pymupdf.open("pdf", img.convert_to_pdf(from_page=i, to_page=i)) as pdf:
                page = pdf[0]
                tp = _ocr_page(page, ctx, tracker)
                _add_page(ch, page, i + 1, tp, loc_kind="page" if n > 1 else "image")
                texts.append(page.get_text("text", textpage=tp))
    return Result(ch.result(), {"pages": n, "ocr_pages": n, "language": langmod.detect("\n".join(texts))[0]})
