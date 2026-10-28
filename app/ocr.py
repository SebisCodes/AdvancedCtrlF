"""OCR via the Tesseract engine built into MuPDF/PyMuPDF.

No Tesseract installation is required - only the language data (eng/deu.traineddata).
If missing, it can be downloaded to data/tessdata/ after asking the user.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
import urllib.parse
from pathlib import Path

from . import lang as langmod
from . import net

log = logging.getLogger(__name__)

FIRST_PASS_MAX = 4          # languages combined in the first OCR pass (more = slower)
NOT_LANGUAGES = {"osd", "equ"}   # helper models in tessdata folders, not languages


def _min_size(code: str) -> int:
    """A downloaded pack must have at least half of its known size (else: broken/HTML page)."""
    lang = langmod.BY_TESS.get(code)
    return int(lang.size_mb * 1e6 * 0.5) if lang else 100_000


def _packs_in(folder: Path) -> list[str]:
    try:
        return sorted(p.name[:-len(".traineddata")] for p in folder.glob("*.traineddata")
                      if p.name[:-len(".traineddata")] not in NOT_LANGUAGES and p.stat().st_size > 1000)
    except OSError:
        return []


def _catalog_order(codes) -> list[str]:
    order = {l.tess: i for i, l in enumerate(langmod.LANGUAGES)}
    return sorted(codes, key=lambda c: (order.get(c, 999), c))


def _system_tessdata() -> list[Path]:
    cands = []
    if os.environ.get("TESSDATA_PREFIX"):
        cands.append(Path(os.environ["TESSDATA_PREFIX"]))
    cands += [Path(p) for p in (
        "/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tesseract-ocr/4.00/tessdata",
        "/usr/share/tessdata", "/usr/local/share/tessdata",
        "/opt/homebrew/share/tessdata", "/opt/local/share/tessdata",          # macOS: Homebrew, MacPorts
        r"C:\Program Files\Tesseract-OCR\tessdata", r"C:\Program Files (x86)\Tesseract-OCR\tessdata",
    )]
    if os.environ.get("LOCALAPPDATA"):
        cands.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Tesseract-OCR" / "tessdata")
    return cands


MIRROR = "https://cdn.jsdelivr.net/gh/tesseract-ocr/tessdata_fast@main/{lang}.traineddata"


def _fetch_python(url: str, dest: Path) -> None:
    net.fetch_python(url, dest)


def _fetch_powershell(url: str, dest: Path) -> None:
    net.fetch_powershell(url, dest)


def _check_traineddata(path: Path, min_size: int = 100_000) -> None:
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(64).lstrip()
    if size < min_size or head[:1] == b"<":   # an HTML error/login page instead of the model
        raise RuntimeError(f"received an invalid file ({size} bytes) - possibly a proxy or login page")


def _replace(src: Path, dst: Path) -> None:
    """os.replace with retries: virus scanners briefly lock freshly written files on Windows."""
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.5)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


class OcrEngine:
    """OCR language packs (data/tessdata) and OCR calls. Packs from an existing Tesseract
    installation are used as long as none are installed here; installing copies them."""

    def __init__(self, own_dir: Path, dpi: int = 300, url_template: str = "") -> None:
        self.own_dir = own_dir
        self.dpi = dpi
        self.url_template = url_template

    # --- Language packs ---------------------------------------------------
    def tessdata(self) -> Path | None:
        for d in [self.own_dir, *_system_tessdata()]:
            if _packs_in(d):
                return d
        return None

    def installed(self) -> list[str]:
        """Language packs OCR can use right now (Tesseract codes, catalog order)."""
        td = self.tessdata()
        return _catalog_order(_packs_in(td)) if td else []

    def own_packs(self) -> list[str]:
        return _catalog_order(_packs_in(self.own_dir))

    def system_packs(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for d in _system_tessdata():
            for code in _packs_in(d):
                found.setdefault(code, d / f"{code}.traineddata")
        return found

    def available(self) -> bool:
        try:
            import pymupdf  # noqa: F401
        except ImportError:
            return False
        return bool(self.installed())

    def first_pass(self) -> str:
        """Language string for the first OCR pass: up to FIRST_PASS_MAX installed packs."""
        return "+".join(self.installed()[:FIRST_PASS_MAX]) or "eng"

    def remove(self, codes) -> None:
        for code in codes:
            _remove(self.own_dir / f"{code}.traineddata")
            log.info("Removed OCR language pack %s", code)

    def download(self, codes=langmod.DEFAULT_PACKS, progress=lambda msg: None) -> None:
        """Install language packs. A pack found in a local Tesseract installation is copied;
        otherwise it is downloaded - trying Python (urllib), then on Windows PowerShell (Windows
        network stack incl. company proxies with Windows authentication), then a CDN mirror if
        github.com is blocked."""
        self.own_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.own_dir.glob("*.part"):   # leftovers of interrupted downloads
            _remove(stale)
        urls = [u for u in dict.fromkeys([self.url_template, MIRROR]) if u]
        methods = [("Python", _fetch_python)] + ([("PowerShell", _fetch_powershell)] if os.name == "nt" else [])
        system = self.system_packs()
        for n, l in enumerate(codes, 1):
            target = self.own_dir / f"{l}.traineddata"
            if target.is_file() and target.stat().st_size >= _min_size(l):
                continue
            part = self.own_dir / f"{l}.traineddata.part"
            if l in system:
                progress(f"Copying OCR language pack {langmod.name_of(l)} ({n}/{len(codes)}) …")
                shutil.copyfile(system[l], part)
                _replace(part, target)
                log.info("Copied %s from %s", target.name, system[l])
                continue
            errors = []
            for template in urls:
                url = template.format(lang=l)
                if not url.startswith("https://"):
                    errors.append(f"{url}: only HTTPS downloads are allowed")
                    continue
                for name, fetch in methods:
                    progress(f"Downloading OCR language pack {langmod.name_of(l)} ({n}/{len(codes)}, "
                             f"{name}, {urllib.parse.urlsplit(url).hostname}) …")
                    try:
                        fetch(url, part)
                        _check_traineddata(part, _min_size(l))
                        _replace(part, target)
                        log.info("Downloaded %s via %s from %s", target.name, name, url)
                        break
                    except Exception as e:
                        errors.append(f"{name} via {urllib.parse.urlsplit(url).hostname}: {e}")
                        log.warning("Download of %s via %s from %s failed: %s", target.name, name, url, e)
                        _remove(part)
                else:
                    continue
                break
            else:
                raise RuntimeError(f"{langmod.name_of(l)} ({l}.traineddata) could not be downloaded.\n"
                                   + "\n".join(f"- {e}" for e in errors))

    # --- OCR -------------------------------------------------------------
    def textpage(self, page, language: str | None = None, dpi: int | None = None):
        td = self.tessdata()
        if td is None:
            raise RuntimeError("OCR language data is missing")
        return page.get_textpage_ocr(language=language or self.first_pass(), dpi=dpi or self.dpi, full=True,
                                     tessdata=str(td))


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
    """Learns the document language: the first OCR pages run with up to FIRST_PASS_MAX
    installed packs combined; once the language is detected (and its pack is installed)
    only that pack is used - more accurate and faster."""

    def __init__(self, ocr) -> None:
        self.first = ocr.first_pass() if ocr is not None else "eng"
        self.installed = set(ocr.installed()) if ocr is not None else set()
        self.code: str | None = None
        self.tries = 0

    def current(self) -> str:
        return self.code or self.first

    def learn(self, text: str) -> str | None:
        """Return the pack to re-recognise the page with, if the language is now known."""
        if self.code is not None:
            return None
        lang, conf = langmod.detect(text)
        self.tries += 1
        tess = langmod.tesseract_code(lang)
        if tess in self.installed and conf >= 0.6:
            self.code = tess
            return tess if tess != self.first else None
        if self.tries >= 3:
            self.code = self.first
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
        self.tracker = LanguageTracker(ctx.ocr)
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
            if better:
                text = self._ocr(pix, better)
        except Exception:
            log.warning("OCR of image %s failed", key, exc_info=True)
            return ""
        text = text.strip()
        return text if sum(c.isalnum() for c in text) >= 3 else ""
