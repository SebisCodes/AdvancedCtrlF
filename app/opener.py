"""Open documents as close to the hit as possible (page / line / text), per platform.

Security: only files inside data/documents/ are opened, external programs are
started with argument lists (never through a shell), and file types that could
execute code when opened are never launched.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from . import config as cfg

log = logging.getLogger(__name__)
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
MAC_APPS = [Path("/Applications"), Path.home() / "Applications"]

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
        log.info("Started %s", args)
        return True
    except OSError as e:
        log.warning("Could not start %s: %s", args, e)
        return False


def _default_open(path: Path) -> bool:
    if IS_WIN:
        try:
            os.startfile(str(path))  # noqa: S606 - only allow-listed document types get here
            log.info("Opened %s with its default application", path.name)
            return True
        except OSError as e:
            log.warning("Default application for %s failed: %s", path.name, e)
            return False
    opener = "/usr/bin/open" if IS_MAC else _which("xdg-open", "gio", "open")
    if not opener:
        return False
    return _spawn([opener, "open", str(path)] if opener.endswith("gio") else [opener, str(path)])


def _mac_app(*names: str) -> str | None:
    """Executable inside an installed macOS app bundle, e.g. Google Chrome.app/Contents/MacOS/Google Chrome."""
    for base in MAC_APPS:
        for name in names:
            exe = base / f"{name}.app" / "Contents" / "MacOS" / name
            if exe.is_file():
                return str(exe)
    return None


def _browsers() -> list[str]:
    if IS_MAC:
        found = [_mac_app("Google Chrome"), _mac_app("Microsoft Edge"), _mac_app("Firefox"), _mac_app("Brave Browser")]
    elif IS_WIN:
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
    if IS_MAC:
        pass  # Preview cannot jump to a page from outside - browsers (below) can
    elif IS_WIN:
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
    code = (_which("code", "codium") or (IS_WIN and _win_paths(r"Programs\Microsoft VS Code\Code.exe"))
            or (IS_MAC and _mac_bundle_cli("Visual Studio Code", "Contents/Resources/app/bin/code")))
    if code:
        cmds.append([code, "-g", f"{p}:{n}"])
    if IS_WIN:
        npp = _win_paths(r"Notepad++\notepad++.exe")
        if npp:
            cmds.append([npp, f"-n{n}", p])
    elif IS_MAC:
        bbedit = _which("bbedit")
        if bbedit:
            cmds.append([bbedit, f"+{n}", p])
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


def _mac_bundle_cli(app: str, rel: str) -> str | None:
    for base in MAC_APPS:
        exe = base / f"{app}.app" / rel
        if exe.is_file():
            return str(exe)
    return None


def show_in_folder(path: Path) -> None:
    if IS_WIN:
        # explorer.exe has its own command-line parsing: it needs /select,"C:\path with spaces".
        # An argument list would be quoted as a whole ("/select,C:\...") - explorer does not
        # understand that and opens the default "Documents" folder instead (e.g. with OneDrive paths).
        # Windows paths cannot contain quotes, so this string is safe.
        target = str(path.resolve())
        cmd = f'explorer /select,"{target}"' if path.is_file() else f'explorer "{target}"'
        try:
            subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200)
            log.info("Started %s", cmd)
        except OSError as e:
            log.warning("Could not start %s: %s", cmd, e)
        return
    if IS_MAC:  # reveal the file in Finder
        _spawn(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
        return
    opener = _which("xdg-open", "gio")
    if opener:
        folder = str(path if path.is_dir() else path.parent)
        _spawn([opener, "open", folder] if opener.endswith("gio") else [opener, folder])
