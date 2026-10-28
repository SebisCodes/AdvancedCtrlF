"""Format detection and dispatch to the matching extractor."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from .base import Context, ExtractError, Result, binary_strings, looks_like_text
from . import ooxml, odf, pdf, textual

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml",
    ".json", ".jsonl", ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs",
    ".php", ".rb", ".sh", ".bat", ".ps1", ".sql", ".css", ".scss", ".tex", ".bib", ".srt", ".vtt",
    ".properties", ".env", ".kt", ".swift", ".lua", ".r", ".m", ".pl", ".vb", ".ics", ".vcf",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".pnm", ".jxr", ".jp2"}
ARCHIVE_EXT = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
LEGACY = {".doc": "docx", ".dot": "docx", ".wps": "docx", ".wpd": "docx", ".pages": "docx",
          ".ppt": "pptx", ".pps": "pptx", ".pot": "pptx", ".key": "pptx"}

HANDLERS = {
    ".pdf": pdf.extract_pdf, ".xps": pdf.extract_pdf, ".oxps": pdf.extract_pdf, ".cbz": pdf.extract_pdf,
    ".fb2": pdf.extract_pdf, ".mobi": pdf.extract_pdf,
    ".docx": ooxml.extract_docx, ".docm": ooxml.extract_docx, ".dotx": ooxml.extract_docx, ".dotm": ooxml.extract_docx,
    ".pptx": ooxml.extract_pptx, ".pptm": ooxml.extract_pptx, ".ppsx": ooxml.extract_pptx, ".potx": ooxml.extract_pptx,
    ".xlsx": ooxml.extract_xlsx, ".xlsm": ooxml.extract_xlsx, ".xltx": ooxml.extract_xlsx, ".xltm": ooxml.extract_xlsx,
    ".xls": ooxml.extract_xls,
    ".odt": odf.extract_odt, ".ott": odf.extract_odt, ".fodt": textual.extract_xml,
    ".odp": odf.extract_odp, ".otp": odf.extract_odp,
    ".ods": odf.extract_ods, ".ots": odf.extract_ods,
    ".csv": textual.extract_csv, ".tsv": textual.extract_csv,
    ".html": textual.extract_html, ".htm": textual.extract_html, ".xhtml": textual.extract_html, ".mhtml": textual.extract_eml, ".mht": textual.extract_eml,
    ".xml": textual.extract_xml, ".svg": textual.extract_xml, ".xsd": textual.extract_xml, ".xsl": textual.extract_xml,
    ".kml": textual.extract_xml, ".gpx": textual.extract_xml, ".rss": textual.extract_xml, ".plist": textual.extract_xml,
    ".rtf": textual.extract_rtf,
    ".eml": textual.extract_eml,
    ".epub": textual.extract_epub,
    ".ipynb": textual.extract_ipynb,
}


def suffix(path: Path) -> str:
    name = path.name.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if name.endswith(ext):
            return ext
    return path.suffix.lower()


def is_archive(path: Path) -> bool:
    ext = suffix(path)
    if ext in ARCHIVE_EXT:
        # do not unpack a .zip that really is an Office document
        return not (ext == ".zip" and _ooxml_kind(path))
    return False


def _ooxml_kind(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "[Content_Types].xml" in names:
                if any(n.startswith("word/") for n in names):
                    return ".docx"
                if any(n.startswith("ppt/") for n in names):
                    return ".pptx"
                if any(n.startswith("xl/") for n in names):
                    return ".xlsx"
            if "mimetype" in names:
                mt = z.read("mimetype")[:100].decode("ascii", "ignore")
                for key, ext in (("text", ".odt"), ("presentation", ".odp"), ("spreadsheet", ".ods"), ("epub", ".epub")):
                    if key in mt:
                        return ext
    except (zipfile.BadZipFile, OSError):
        pass
    return None


def sniff(path: Path) -> str:
    """Determine the file type from its extension and, if needed, its content."""
    ext = suffix(path)
    if ext in HANDLERS or ext in TEXT_EXT or ext in IMAGE_EXT or ext in LEGACY:
        return ext
    with open(path, "rb") as f:
        head = f.read(8192)
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK"):
        return _ooxml_kind(path) or ".zip"
    if head.startswith(b"{\\rtf"):
        return ".rtf"
    if head[:8] in (b"\x89PNG\r\n\x1a\n",) or head[:3] == b"\xff\xd8\xff" or head[:4] in (b"II*\x00", b"MM\x00*"):
        return ".png"
    low = head.lower()
    if b"<html" in low or b"<!doctype html" in low:
        return ".html"
    if head.lstrip().startswith(b"<?xml"):
        return ".xml"
    if looks_like_text(head):
        return ".txt"
    return ext or ".bin"


def _soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for p in (r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if os.path.isfile(p):
            return p
    return None


def _convert_legacy(path: Path, target: str, ctx: Context) -> Result | None:
    """Convert legacy binary formats (.doc/.ppt …) with LibreOffice, if installed."""
    exe = _soffice()
    if not exe:
        return None
    ctx.progress("Converting with LibreOffice …")
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp, "profile").as_uri()
        try:
            subprocess.run([exe, f"-env:UserInstallation={profile}", "--headless", "--norestore",
                            "--convert-to", target, "--outdir", tmp, str(path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = Path(tmp, path.stem + "." + target)
        if not out.is_file():
            return None
        return HANDLERS["." + target](out, ctx)


def extract(path: Path, ctx: Context, kind: str | None = None) -> Result:
    kind = kind or sniff(path)
    if kind in HANDLERS:
        return HANDLERS[kind](path, ctx)
    if kind in IMAGE_EXT:
        return pdf.extract_image(path, ctx)
    if kind in TEXT_EXT:
        return textual.extract_text(path, ctx)
    if kind in LEGACY:
        res = _convert_legacy(path, LEGACY[kind], ctx)
        if res is not None:
            if kind in (".doc", ".dot", ".wps", ".wpd", ".pages"):
                res.meta["warning"] = "Page numbers come from a conversion and may differ slightly"
            return res
        text = binary_strings(path.read_bytes()[:200 * 1024 * 1024])
        from .base import Chunker, add_lines
        ch = Chunker()
        add_lines(ch, text, {"kind": "part", "part": "Raw text"})
        return Result(ch.chunks, {"warning": "Legacy binary format: raw text only, without page numbers (install LibreOffice for more)"})
    with open(path, "rb") as f:
        head = f.read(8192)
    if looks_like_text(head):
        return textual.extract_text(path, ctx)
    return extract_binary(path, head)


# Magic numbers of common binary formats - only used to describe the file
_MAGIC = [
    (b"MZ", "Windows program or library"), (b"\x7fELF", "Linux program or library"),
    (b"\xcf\xfa\xed\xfe", "macOS program or library"), (b"\xca\xfe\xba\xbe", "macOS program or Java class"),
    (b"SQLite format 3\x00", "SQLite database"), (b"\x00\x01\x00\x00\x00", "font"), (b"OTTO", "font"),
    (b"wOFF", "web font"), (b"wOF2", "web font"), (b"ID3", "MP3 audio"), (b"\xff\xfb", "MP3 audio"),
    (b"fLaC", "FLAC audio"), (b"OggS", "Ogg audio/video"), (b"RIFF", "RIFF media (WAV/AVI)"),
    (b"\x1aE\xdf\xa3", "Matroska/WebM video"), (b"7z\xbc\xaf\x27\x1c", "7-Zip archive"), (b"Rar!", "RAR archive"),
    (b"\x1f\x8b", "gzip-compressed file"), (b"BZh", "bzip2-compressed file"), (b"\xfd7zXZ", "xz-compressed file"),
    (b"PK", "ZIP-based file"), (b"\xd0\xcf\x11\xe0", "legacy Microsoft Office/OLE file"),
    (b"GIF8", "GIF image"), (b"8BPS", "Photoshop image"),
]


def describe_binary(head: bytes) -> str:
    if head[4:8] == b"ftyp":
        return "MP4/MOV/HEIC media"
    for magic, label in _MAGIC:
        if head.startswith(magic):
            return label
    return "binary file"


def extract_binary(path: Path, head: bytes) -> Result:
    """Any other file: the file name is searchable (see store) plus readable text
    strings found inside (e.g. messages in programs, metadata in media files)."""
    from .base import Chunker, add_lines
    with open(path, "rb") as f:
        data = f.read(50 * 1024 * 1024)
    text = binary_strings(data)[:300_000]
    ch = Chunker()
    add_lines(ch, text, {"kind": "part", "part": "Readable strings"}, lines_per_chunk=20)
    note = f"{describe_binary(head).capitalize()} - file name" + (" and readable text strings" if ch.chunks else "") + " searchable"
    return Result(ch.chunks, {"note": note})


__all__ = ["Context", "ExtractError", "Result", "extract", "sniff", "is_archive", "suffix"]
