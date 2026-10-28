"""Open documents as close to the hit as possible (page / line / text), per platform.

Security: only files inside data/documents/ are opened, external programs are
started with argument lists (never through a shell), and file types that could
execute code when opened are never launched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from . import config as cfg

IS_WIN = sys.platform.startswith("win")

# Only these extensions may be handed to the default application.
SAFE_OPEN = {
    ".pdf", ".xps", ".oxps", ".epub", ".docx", ".docm", ".doc", ".dotx", ".odt", ".ott", ".rtf",
    ".pptx", ".pptm", ".ppt", ".ppsx", ".pps", ".odp", ".xlsx", ".xlsm", ".xls", ".ods", ".csv", ".tsv",
    ".txt", ".md", ".log", ".json", ".xml", ".html", ".htm", ".xhtml", ".eml", ".mht", ".mhtml", ".png",
    ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".svg", ".yaml", ".yml", ".ini", ".toml",
    ".tex", ".pages", ".key",
}
# Plain-text files that can safely be opened in an editor at a given line
TEXT_LINE = {".txt", ".md", ".log", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".ini", ".toml", ".py",
             ".js", ".ts", ".sql", ".css", ".tex", ".sh", ".bat", ".ps1", ".c", ".h", ".cpp", ".java", ".cs",
             ".go", ".rs", ".php", ".rb", ".conf", ".cfg"}
HTML = {".html", ".htm", ".xhtml"}


def _which(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _win_paths(*rel: str) -> str | None:
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        for r in rel:
            p = Path(base, r)
            if p.is_file():
                return str(p)
    return None


def _spawn(args: list[str]) -> bool:
    try:
        kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
        if IS_WIN:
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen(args, **kw)
        return True
    except OSError:
        return False


def _default_open(path: Path) -> bool:
    if IS_WIN:
        try:
            os.startfile(str(path))  # noqa: S606 - only allow-listed document types get here
            return True
        except OSError:
            return False
    opener = _which("xdg-open", "gio", "open")
    if not opener:
        return False
    return _spawn([opener, "open", str(path)] if opener.endswith("gio") else [opener, str(path)])


def _browsers() -> list[str]:
    if IS_WIN:
        found = [_win_paths(r"Microsoft\Edge\Application\msedge.exe"), _win_paths(r"Google\Chrome\Application\chrome.exe"),
                 _win_paths(r"Mozilla Firefox\firefox.exe")]
    else:
        found = [_which(n) for n in ("firefox", "chromium", "chromium-browser", "google-chrome", "microsoft-edge",
                                     "brave-browser")]
    return [b for b in found if b]


def _pdf_viewers(path: Path, page: int, preferred: str) -> list[list[str]]:
    p, n = str(path), str(page)
    cmds = []
    if preferred and Path(preferred).is_file():
        name = Path(preferred).name.lower()
        if "sumatra" in name:
            cmds.append([preferred, "-page", n, p])
        elif "acro" in name:
            cmds.append([preferred, "/A", f"page={n}", p])
        elif "okular" in name:
            cmds.append([preferred, "-p", n, p])
        elif "evince" in name or "papers" in name:
            cmds.append([preferred, "-i", n, p])
        else:
            cmds.append([preferred, p])
    if IS_WIN:
        sumatra = _win_paths(r"SumatraPDF\SumatraPDF.exe")
        if sumatra:
            cmds.append([sumatra, "-page", n, p])
        acro = _win_paths(r"Adobe\Acrobat DC\Acrobat\Acrobat.exe", r"Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
                          r"Adobe\Acrobat Reader\Reader\AcroRd32.exe", r"Adobe\Acrobat DC\Acrobat\AcroRd32.exe")
        if acro:
            cmds.append([acro, "/A", f"page={n}", p])
    else:
        for exe, args in (("okular", ["-p", n, p]), ("evince", ["-i", n, p]), ("papers", ["-i", n, p]),
                          ("zathura", ["-P", n, p]), ("qpdfview", [f"{p}#{n}"]), ("atril", ["-i", n, p]),
                          ("xreader", ["-i", n, p])):
            e = _which(exe)
            if e:
                cmds.append([e, *args])
    for b in _browsers():
        cmds.append([b, path.as_uri() + f"#page={n}"])
    return cmds


def _editors(path: Path, line: int) -> list[list[str]]:
    p, n = str(path), str(line)
    cmds = []
    code = _which("code", "codium") or (IS_WIN and _win_paths(r"Programs\Microsoft VS Code\Code.exe"))
    if code:
        cmds.append([code, "-g", f"{p}:{n}"])
    if IS_WIN:
        npp = _win_paths(r"Notepad++\notepad++.exe")
        if npp:
            cmds.append([npp, f"-n{n}", p])
    else:
        for exe in ("kate", "gnome-text-editor", "gedit", "mousepad", "pluma", "xed", "cosmic-edit"):
            e = _which(exe)
            if e:
                cmds.append([e, "-l", n, p] if exe == "kate" else [e, f"+{n}", p])
                break
    return cmds


def open_document(path: Path, target: dict | None, preferred_pdf: str = "", highlight: str = "") -> tuple[bool, str]:
    """Open `path` at `target` (page/line) if possible. `highlight` is a matched word
    used to jump to the hit in web pages. Returns (ok, message)."""
    path = path.resolve()
    if cfg.DOCS_DIR.resolve() not in path.parents or not path.is_file():
        return False, "File is not inside the document store"
    ext = path.suffix.lower()
    target = target or {}
    page = target.get("page") or target.get("slide")
    line = target.get("line")
    if ext in (".pdf", ".xps") and page:
        for cmd in _pdf_viewers(path, int(page), preferred_pdf):
            if _spawn(cmd):
                return True, f"Opened at page {page}"
    if ext in HTML:
        # Text fragments (#:~:text=) scroll to and highlight the hit in Chrome, Edge and Firefox
        url = path.as_uri() + (f"#:~:text={quote(highlight)}" if highlight else "")
        for b in _browsers():
            if _spawn([b, url]):
                return True, "Opened in browser" + (" at the hit" if highlight else "")
    if ext in TEXT_LINE and line and target.get("kind") in ("line", "row"):
        for cmd in _editors(path, int(line)):
            if _spawn(cmd):
                return True, f"Opened at line {line}"
    if ext not in SAFE_OPEN:
        show_in_folder(path)
        return True, "This file type is never launched for safety reasons - folder opened instead"
    if _default_open(path):
        return True, "Opened - please navigate to the hit manually" if (page or line) else "Opened"
    return False, "No suitable application found"


def show_in_folder(path: Path) -> None:
    if IS_WIN:
        _spawn(["explorer", f"/select,{path}"])
        return
    opener = _which("xdg-open", "gio")
    if opener:
        folder = str(path if path.is_dir() else path.parent)
        _spawn([opener, "open", folder] if opener.endswith("gio") else [opener, folder])
