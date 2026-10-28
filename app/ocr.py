"""OCR via the Tesseract engine built into MuPDF/PyMuPDF.

No Tesseract installation is required - only the language data (eng/deu.traineddata).
If missing, it can be downloaded to data/tessdata/ after asking the user.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path

from . import lang as langmod

log = logging.getLogger(__name__)

LANGS = ("eng", "deu")
MIN_SIZE = 500_000  # smaller files are certainly not a valid language model


def _system_tessdata() -> list[Path]:
    cands = []
    if os.environ.get("TESSDATA_PREFIX"):
        cands.append(Path(os.environ["TESSDATA_PREFIX"]))
    cands += [Path(p) for p in (
        "/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tesseract-ocr/4.00/tessdata",
        "/usr/share/tessdata", "/usr/local/share/tessdata",
        r"C:\Program Files\Tesseract-OCR\tessdata", r"C:\Program Files (x86)\Tesseract-OCR\tessdata",
    )]
    if os.environ.get("LOCALAPPDATA"):
        cands.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Tesseract-OCR" / "tessdata")
    return cands


class OcrEngine:
    def __init__(self, own_dir: Path, dpi: int = 300, url_template: str = "") -> None:
        self.own_dir = own_dir
        self.dpi = dpi
        self.url_template = url_template

    # --- Language data -----------------------------------------------------
    def tessdata(self) -> Path | None:
        for d in [self.own_dir, *_system_tessdata()]:
            if all((d / f"{l}.traineddata").is_file() for l in LANGS):
                return d
        return None

    def available(self) -> bool:
        try:
            import pymupdf  # noqa: F401
        except ImportError:
            return False
        return self.tessdata() is not None

    def download(self, progress=lambda msg: None) -> None:
        self.own_dir.mkdir(parents=True, exist_ok=True)
        for l in LANGS:
            target = self.own_dir / f"{l}.traineddata"
            if target.is_file() and target.stat().st_size >= MIN_SIZE:
                continue
            url = self.url_template.format(lang=l)
            if not url.startswith("https://"):
                raise RuntimeError("Downloads are only allowed via HTTPS")
            progress(f"Downloading OCR language data {l} …")
            fd, tmp = tempfile.mkstemp(dir=self.own_dir, suffix=".part")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "AdvancedCtrlF"})
                with urllib.request.urlopen(req, timeout=60) as r, os.fdopen(fd, "wb") as f:
                    shutil.copyfileobj(r, f)
                if os.path.getsize(tmp) < MIN_SIZE:
                    raise RuntimeError(f"Download of {l}.traineddata is incomplete")
                os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    # --- OCR -------------------------------------------------------------
    def textpage(self, page, language: str = "deu+eng", dpi: int | None = None):
        td = self.tessdata()
        if td is None:
            raise RuntimeError("OCR language data is missing")
        return page.get_textpage_ocr(language=language, dpi=dpi or self.dpi, full=True, tessdata=str(td))


def native_dpi(page, fallback: int = 300) -> int:
    """Resolution at which a page should be OCR'd: that of its largest image.

    Rendering above the image's own resolution only enlarges the letters - Tesseract
    recognises noticeably less when glyphs get very big (e.g. a screenshot saved as PDF
    rendered at 300 dpi is enlarged ~4x)."""
    best, area = None, 0.0
    try:
        for info in page.get_image_info():
            w_pt = info["bbox"][2] - info["bbox"][0]
            if w_pt > 0 and info.get("width") and w_pt * (info["bbox"][3] - info["bbox"][1]) > area:
                area = w_pt * (info["bbox"][3] - info["bbox"][1])
                best = info["width"] / (w_pt / 72)
    except Exception:
        best = None
    if not best:
        return fallback
    return int(max(72, min(fallback, best)))


class LanguageTracker:
    """Remembers the detected document language: the first OCR pages run with
    deu+eng, afterwards the detected single language is used (more accurate)."""

    def __init__(self) -> None:
        self.code: str | None = None   # Tesseract code once confident
        self.tries = 0

    def current(self) -> str:
        return self.code or "deu+eng"

    def learn(self, text: str) -> str | None:
        """Return a new language code if the page should be recognized again."""
        if self.code is not None:
            return None
        lang, conf = langmod.detect(text)
        self.tries += 1
        if lang != "und" and conf >= 0.8:
            self.code = langmod.tesseract_code(lang)
            return self.code
        if self.tries >= 3:
            self.code = "deu+eng"
        return None


class ImageOCR:
    """OCR for images embedded in documents (PDF, Office, OpenDocument).

    One instance per document: each distinct image is read only once (a logo on
    every slide would otherwise produce dozens of identical hits), tiny images
    such as icons are skipped, and the document language is learned on the way.
    """
    MIN_SIDE = 24          # px - smaller images cannot contain readable text
    MIN_AREA = 12_000      # px² - skips icons and bullets
    SMALL_WIDTH = 600      # only really small images are upscaled (max. 2x) -
                           # enlarging normal images makes the letters too big and recognition worse
    MAX_PIXELS = 25_000_000
    MAX_IMAGES = 2000      # safety limit per document

    def __init__(self, ctx) -> None:
        cfg = ctx.config
        self.ctx = ctx
        self.wanted = bool(cfg.get("ocr_enabled") and cfg.get("ocr_all_images"))
        self.enabled = self.wanted and ctx.ocr is not None and ctx.ocr.available()
        self.tracker = LanguageTracker()
        self.seen: set = set()
        self.count = 0
        self.skipped = 0   # images that would need OCR but the language data is missing

    def _prepare(self, pix):
        import pymupdf
        if pix.width < self.MIN_SIDE or pix.height < self.MIN_SIDE or pix.width * pix.height < self.MIN_AREA:
            return None
        if pix.colorspace is None or pix.colorspace.n != 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)       # gray, CMYK, indexed ... -> RGB
                                                            # (MuPDF's OCR returns nothing for gray pixmaps)
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)                    # drop transparency
        scale = 1.0
        if pix.width < self.SMALL_WIDTH:
            scale = min(2.0, self.SMALL_WIDTH / pix.width)
        if pix.width * pix.height * scale * scale > self.MAX_PIXELS:
            scale = (self.MAX_PIXELS / (pix.width * pix.height)) ** 0.5
        if abs(scale - 1.0) > 0.05:
            pix = pymupdf.Pixmap(pix, int(pix.width * scale), int(pix.height * scale), None)
        return pix

    def _ocr(self, pix, language: str) -> str:
        import pymupdf
        td = self.ctx.ocr.tessdata()
        with pymupdf.open("pdf", pix.pdfocr_tobytes(language=language, tessdata=str(td))) as doc:
            return doc[0].get_text("text")

    def read(self, key, source) -> str:
        """OCR text of an image. `source` is image bytes or a pymupdf.Pixmap (or a
        callable returning one). Returns "" for duplicates, tiny or unsupported images."""
        if not self.wanted or key in self.seen or self.count >= self.MAX_IMAGES or self.ctx.cancelled():
            return ""
        self.seen.add(key)
        try:
            import pymupdf
            if callable(source):
                source = source()
            pix = pymupdf.Pixmap(source) if isinstance(source, (bytes, bytearray)) else source
            pix = self._prepare(pix)
        except Exception as e:
            log.debug("Image %s skipped: %s", key, e)
            return ""  # unsupported format (e.g. EMF/WMF/SVG) or broken image
        if pix is None:
            return ""
        if not self.enabled:   # language data missing: remember that this document needs OCR later
            self.skipped += 1
            return ""
        self.count += 1
        self.ctx.progress(f"OCR image {self.count}")
        try:
            text = self._ocr(pix, self.tracker.current())
            better = self.tracker.learn(text)
            if better and better != "deu+eng":
                text = self._ocr(pix, better)
        except Exception:
            log.warning("OCR of image %s failed", key, exc_info=True)
            return ""
        text = text.strip()
        return text if sum(c.isalnum() for c in text) >= 3 else ""
