"""OCR via the Tesseract engine built into MuPDF/PyMuPDF.

No Tesseract installation is required - only the language data (eng/deu.traineddata).
If missing, it can be downloaded to data/tessdata/ after asking the user.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import urllib.request
from pathlib import Path

from . import lang as langmod

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
    def textpage(self, page, language: str = "deu+eng"):
        td = self.tessdata()
        if td is None:
            raise RuntimeError("OCR language data is missing")
        return page.get_textpage_ocr(language=language, dpi=self.dpi, full=True, tessdata=str(td))


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
