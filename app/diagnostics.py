"""Logging setup and a diagnostics report that users can copy and send when
something does not work (versions, OCR status, failed documents, log tails)."""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
from collections import Counter
from pathlib import Path

from . import __version__
from . import config as cfg

SETUP_LOG = cfg.DATA_DIR / "setup.log"


def setup_logging() -> None:
    """Application log in data/advanced_ctrl_f.log (max. 1 MB, 2 older files kept)."""
    handler = logging.handlers.RotatingFileHandler(cfg.LOG_FILE, maxBytes=1_000_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    log = logging.getLogger("startup")
    log.info("=" * 60)
    for line in environment():
        log.info(line)


def _versions() -> list[str]:
    out = []
    for label, mod, attr in (("PySide6", "PySide6", "__version__"), ("PyMuPDF", "pymupdf", "VersionBind"),
                             ("MuPDF", "pymupdf", "VersionFitz"), ("openpyxl", "openpyxl", "__version__"),
                             ("xlrd", "xlrd", "__version__")):
        try:
            out.append(f"{label} {getattr(__import__(mod), attr)}")
        except Exception as e:
            out.append(f"{label} MISSING ({type(e).__name__})")
    return out


def environment() -> list[str]:
    embeddable = any(Path(sys.executable).parent.glob("python*._pth"))
    return [
        f"Advanced Ctrl+F {__version__}",
        f"OS: {platform.platform()} ({platform.machine()})",
        f"Python: {sys.version.split()[0]} {'(embeddable package) ' if embeddable else ''}{sys.executable}",
        f"Packages: {', '.join(_versions())}",
        f"Program folder: {cfg.ROOT} (writable: {os.access(cfg.ROOT, os.W_OK)})",
    ]


def _tail(path: Path, lines: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[-200_000:].splitlines()[-lines:]
    except OSError:
        return ["(not found)"]


def report(config: dict, ocr, docs: dict) -> str:
    """Plain-text diagnostics for support requests."""
    lines = ["### Advanced Ctrl+F diagnostics", *environment(), ""]
    tess = ocr.tessdata() if ocr else None
    lines += [
        f"OCR enabled: {config.get('ocr_enabled')}, OCR all images: {config.get('ocr_all_images')}",
        f"OCR language data: {tess or 'MISSING'}"
        + (f" ({', '.join(sorted(p.name for p in Path(tess).glob('*.traineddata')))})" if tess else ""),
        f"OCR download declined: {config.get('ocr_download_declined')}",
        "",
    ]
    status = Counter(d.get("status", "ok") for d in docs.values())
    lines.append(f"Documents: {len(docs)} (ok {status['ok']}, warnings {status['warn']}, errors {status['error']})")
    problems = [d for d in docs.values() if d.get("status") in ("warn", "error")]
    for d in problems[:30]:
        lines.append(f"  [{d.get('status')}] {d.get('kind', '?')}: {d.get('display_name') or d.get('name')} - {d.get('message')}")
    if len(problems) > 30:
        lines.append(f"  ... {len(problems) - 30} more")
    lines += ["", f"--- last lines of {cfg.LOG_FILE.name} ---", *_tail(cfg.LOG_FILE, 40),
              "", f"--- last lines of {SETUP_LOG.name} ---", *_tail(SETUP_LOG, 40)]
    return "\n".join(lines)
