"""Paths and settings of Advanced Ctrl+F.

All data lives next to the program (portable):
    config/config.json   settings
    data/documents/      copies of the added documents
    data/index/          one JSON per document (text + locations)
    data/tessdata/       OCR language data (deu/eng)
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "config.json"
DATA_DIR = ROOT / "data"
DOCS_DIR = DATA_DIR / "documents"
INDEX_DIR = DATA_DIR / "index"
TESSDATA_DIR = DATA_DIR / "tessdata"
LOG_FILE = DATA_DIR / "advanced_ctrl_f.log"

DEFAULTS: dict = {
    "ocr_enabled": True,          # read scanned pages / images via OCR
    "ocr_all_images": True,       # also OCR every image inside PDFs and Office files (slower)
    "ocr_dpi": 300,
    "ocr_min_chars": 25,          # less text on a page -> OCR
    "ocr_download_declined": False,
    "ocr_first_start_asked": False,  # the download question is asked once on the first start
    "tessdata_url": "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata",
    "max_file_mb": 500,
    "max_results": 300,
    "one_hit_per_doc": False,
    "pdf_viewer": "",             # optional: path to a preferred PDF viewer
    "window": {},
}


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DOCS_DIR, INDEX_DIR, TESSDATA_DIR):
        d.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """Write to a temp file first, then rename (no half-written JSON after a crash)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Config(dict):
    def __init__(self) -> None:
        super().__init__(DEFAULTS)
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.update({k: v for k, v in data.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        atomic_write_text(CONFIG_FILE, json.dumps(self, indent=2, ensure_ascii=False))
