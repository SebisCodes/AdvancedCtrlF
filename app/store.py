"""Document store: copy to data/documents/, extraction, JSON to data/index/."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import config as cfg
from .extractors import Context, ExtractError, extract, is_archive, sniff, suffix

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- Mark of the Web (Windows)
# Windows remembers in the stream "Zone.Identifier" that a file came from the internet; Office
# and PDF readers then open it in protected view and block macros. A plain copy loses that
# stream, so it is carried over to the stored copy (from the original, from the archive a file
# came out of, or set to "internet" for downloads).
def read_zone(path: Path) -> str | None:
    if os.name != "nt":
        return None
    try:
        with open(f"{path}:Zone.Identifier", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def internet_zone(url: str) -> str:
    return f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={url}\r\n"


def write_zone(path: Path, zone: str | None) -> None:
    if os.name != "nt" or not zone:
        return
    try:
        with open(f"{path}:Zone.Identifier", "w", encoding="utf-8") as f:
            f.write(zone)
    except OSError as e:                 # e.g. FAT/exFAT drives have no streams
        log.info("Zone.Identifier for %s not written: %s", path.name, e)

MAX_ARCHIVE_TOTAL = 4 * 1024 ** 3   # protection against zip bombs
MAX_ARCHIVE_MEMBERS = 100_000      # large repositories have tens of thousands of files
MAX_ARCHIVE_DEPTH = 3
SCHEMA = 1

# Folders that hold version control data, installed dependencies, caches or virtual
# environments - never documents. Skipped in folders and archives.
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".nox",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".gradle", ".terraform", "__MACOSX",
             ".ipynb_checkpoints", "bower_components", ".next", ".nuxt", ".parcel-cache", ".cache"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized"}


def skipped(rel_parts) -> bool:
    *dirs, name = rel_parts
    return name in SKIP_FILES or any(d in SKIP_DIRS for d in dirs)


def _git_files(folder: Path) -> list[Path] | None:
    """Files of a Git working tree as Git sees them (tracked + untracked, .gitignore respected).
    None if `folder` is not a repository root or git is not available."""
    git = shutil.which("git")
    if not git or not (folder / ".git").exists():
        return None
    try:
        # core.fsmonitor could run a program configured by the (untrusted) repository - disabled
        r = subprocess.run([git, "-c", "core.fsmonitor=false", "-C", str(folder), "ls-files", "-z",
                            "--cached", "--others", "--exclude-standard"],
                           capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                           creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    files = []
    for rel in r.stdout.decode("utf-8", "replace").split("\0"):
        if not rel:
            continue
        p = folder / rel
        if p.is_file() and not p.is_symlink() and not skipped(Path(rel).parts):
            files.append(p)
    log.info("Git repository %s: %d files from git ls-files", folder.name, len(files))
    return files


def list_folder(folder: Path) -> list[Path]:
    """All document files below `folder` (Git-aware, without dependency/cache folders)."""
    files = _git_files(folder)
    if files is not None:
        return sorted(files)
    files = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            p = Path(dirpath, name)
            if name not in SKIP_FILES and p.is_file() and not p.is_symlink():
                files.append(p)
    return files

_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_filename(name: str) -> str:
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .") or "file"
    stem, dot, ext = name.rpartition(".")
    if (stem or name).lower() in _WIN_RESERVED:
        name = "_" + name
    if len(name) > 120:
        ext = ("." + ext[:15]) if dot else ""
        name = name[:120 - len(ext)] + ext
    return name


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _safe_member_path(name: str) -> PurePosixPath | None:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or any(part in ("..", "") for part in p.parts) or re.match(r"^[a-zA-Z]:", name):
        return None
    if "__MACOSX" in p.parts:
        return None
    return p


def unpack_archive(path: Path, dest: Path) -> list[Path]:
    """Unpack an archive safely (no zip slip, no links, size limits)."""
    out: list[Path] = []
    total = 0

    def write(member_name: str, size: int, opener) -> None:
        nonlocal total
        rel = _safe_member_path(member_name)
        if rel is None or skipped(rel.parts):
            return
        total += size
        if total > MAX_ARCHIVE_TOTAL or len(out) >= MAX_ARCHIVE_MEMBERS:
            raise ExtractError("Archive too large (zip bomb protection)")
        target = dest.joinpath(*rel.parts)
        if dest.resolve() not in target.resolve().parents:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with opener() as src, open(target, "wb") as dst:
            for block in iter(lambda: src.read(1 << 20), b""):
                written += len(block)
                if written > size + 1024 or written > MAX_ARCHIVE_TOTAL:
                    raise ExtractError("Archive entry larger than declared")
                dst.write(block)
        out.append(target)

    if suffix(path) == ".zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode and not stat.S_ISREG(mode):
                    continue  # skip symlinks and the like
                write(info.filename, info.file_size, lambda i=info: z.open(i))
    else:
        with tarfile.open(path) as t:
            for m in t:
                if m.isfile():
                    write(m.name, m.size, lambda m=m: t.extractfile(m))
    return out


class Store:
    def __init__(self, config: dict) -> None:
        cfg.ensure_dirs()
        self.config = config
        self._lock = threading.Lock()
        self._by_sha: dict[str, str] = {}

    # ------------------------------------------------------------ Loading
    def load_all(self) -> list[dict]:
        docs = []
        for f in sorted(cfg.INDEX_DIR.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("Index %s is broken: %s", f, e)
                continue
            if isinstance(d, dict) and "id" in d:
                docs.append(d)
                self._by_sha[d.get("sha1", "")] = d["id"]
        return docs

    def stored_path(self, doc: dict) -> Path:
        # accept only a file name, never a path from the JSON
        return cfg.DOCS_DIR / Path(doc["stored"]).name

    # ------------------------------------------------------------ Adding
    def add_path(self, src: Path, ctx: Context, display_prefix: str = "", depth: int = 0,
                 extra: dict | None = None, base: Path | None = None, zone: str | None = None) -> list[dict]:
        """Add a file, folder or archive. Returns new/updated documents.
        `extra` is stored with every document (e.g. the source URL); for folders, display
        names are relative to `base` (default: the folder's parent, i.e. include its name)."""
        if src.is_dir():
            docs = []
            base = base or src.parent
            files = list_folder(src)
            for n, p in enumerate(files, 1):
                if ctx.cancelled():
                    break
                ctx.progress(f"File {n}/{len(files)}")
                folder = p.parent.relative_to(base).as_posix()
                prefix = display_prefix + ("" if folder == "." else f"{folder}/")
                docs += self.add_path(p, ctx, prefix, depth, extra, zone=zone)
            return docs
        size = src.stat().st_size
        if size > self.config.get("max_file_mb", 500) * 1024 * 1024:
            return [self._error_doc(src, display_prefix, f"File larger than {self.config.get('max_file_mb')} MB")]
        if is_archive(src) and depth < MAX_ARCHIVE_DEPTH:
            ctx.progress(f"Unpacking {src.name} …")
            docs = []
            member_zone = zone or read_zone(src) or (internet_zone(extra["source"]) if (extra or {}).get("source") else None)
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    files = unpack_archive(src, Path(tmp))
                except (ExtractError, zipfile.BadZipFile, tarfile.TarError, OSError) as e:
                    return [self._error_doc(src, display_prefix, f"Archive could not be read: {e}")]
                for f in files:
                    if ctx.cancelled():
                        break
                    rel = f.relative_to(tmp).as_posix()
                    prefix = f"{display_prefix}{src.name} › {rel.rsplit('/', 1)[0] + '/' if '/' in rel else ''}"
                    docs += self.add_path(f, ctx, prefix, depth + 1, extra, zone=member_zone)
            return docs
        return [self._add_file(src, ctx, display_prefix, extra, zone)]

    def add_repository_archive(self, archive: Path, label: str, ctx: Context, subpath: str = "",
                               extra: dict | None = None) -> list[dict]:
        """A downloaded repository snapshot (e.g. GitHub ZIP): files are shown as
        "label › path/in/repo", optionally only those below `subpath`."""
        ctx.progress(f"Unpacking {label} …")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                unpack_archive(archive, Path(tmp))
            except (ExtractError, zipfile.BadZipFile, tarfile.TarError, OSError) as e:
                return [self._error_doc(archive, f"{label} › ", f"Archive could not be read: {e}")]
            tops = [p for p in Path(tmp).iterdir()]
            root = tops[0] if len(tops) == 1 and tops[0].is_dir() else Path(tmp)   # "repo-main/" folder
            start = root / subpath if subpath else root
            if not start.exists():
                return [self._error_doc(archive, f"{label} › ", f"Folder {subpath!r} not found in the repository")]
            if start.is_file():
                rel = start.parent.relative_to(root).as_posix()
                return self.add_path(start, ctx, f"{label} › " + ("" if rel == "." else f"{rel}/"), 0, extra)
            return self.add_path(start, ctx, f"{label} › ", 0, extra, base=root)

    def _add_file(self, src: Path, ctx: Context, display_prefix: str, extra: dict | None = None,
                  zone: str | None = None) -> dict:
        sha = sha1_file(src)
        with self._lock:
            existing = self._by_sha.get(sha)
        if existing and (cfg.INDEX_DIR / f"{existing}.json").exists():
            return {"id": existing, "duplicate": True, "name": src.name}
        doc_id = sha[:16]
        stored_name = f"{doc_id[:8]}_{safe_filename(src.name)}"
        target = cfg.DOCS_DIR / stored_name
        shutil.copyfile(src, target)   # deliberately without permissions/metadata (no exec bit)
        if os.name != "nt":
            os.chmod(target, 0o644)
        write_zone(target, zone or read_zone(src)
                   or (internet_zone(extra["source"]) if (extra or {}).get("source") else None))
        doc = {
            "schema": SCHEMA, "id": doc_id, "sha1": sha, "name": src.name,
            "display_name": display_prefix + src.name, "stored": stored_name,
            "size": src.stat().st_size, "added": datetime.now().isoformat(timespec="seconds"),
            **(extra or {}),
        }
        self._extract_into(doc, target, ctx)
        self._save(doc)
        with self._lock:
            self._by_sha[sha] = doc_id
        return doc

    def _extract_into(self, doc: dict, path: Path, ctx: Context) -> None:
        ctx.progress(f"Reading {doc['name']} …")
        doc.update(kind="", status="ok", message="", chunks=[], pages=None, language="und", ocr_pages=0, ocr_images=0)
        try:
            kind = sniff(path)
            doc["kind"] = kind.lstrip(".")
            res = extract(path, ctx, kind)
            doc["chunks"] = res.chunks
            doc["pages"] = res.meta.get("pages")
            doc["pages_approx"] = res.meta.get("pages_approx", False)
            doc["language"] = res.meta.get("language", "und")
            doc["ocr_pages"] = res.meta.get("ocr_pages", 0)
            doc["ocr_images"] = res.meta.get("ocr_images", 0)
            doc["needs_ocr"] = bool(res.meta.get("ocr_skipped"))
            if res.meta.get("warning"):
                doc["status"], doc["message"] = "warn", res.meta["warning"]
            elif res.meta.get("note"):
                doc["message"] = res.meta["note"]
            elif doc["needs_ocr"]:
                doc["status"], doc["message"] = "warn", (f"{res.meta['ocr_skipped']} image(s) not read: "
                                                         "OCR language data missing - ⚙ Menu → Languages and packs")
            elif not res.chunks:
                doc["status"], doc["message"] = "warn", "No text found"
        except ExtractError as e:
            doc["status"], doc["message"] = "error", str(e)
        except Exception as e:  # broken files must never stop the program
            log.exception("Error in %s", path)
            doc["status"], doc["message"] = "error", f"{type(e).__name__}: {e}"
        if not doc["chunks"]:
            # Every file can at least be found by its name (and folder), even without any text
            doc["chunks"] = [{"loc": {"kind": "file"}, "text": doc.get("display_name") or doc["name"]}]
        doc["indexed"] = datetime.now().isoformat(timespec="seconds")
        level = {"ok": logging.INFO, "warn": logging.WARNING}.get(doc["status"], logging.ERROR)
        log.log(level, "Read %s [%s]: %s chunks, %s OCR page(s), %s OCR image(s)%s", doc.get("display_name") or doc["name"],
                doc["kind"] or "?", len(doc["chunks"]), doc.get("ocr_pages", 0), doc.get("ocr_images", 0),
                f" - {doc['message']}" if doc["message"] else "")

    def _error_doc(self, src: Path, prefix: str, msg: str) -> dict:
        log.error("Skipped %s: %s", prefix + src.name, msg)
        return {"id": None, "name": src.name, "display_name": prefix + src.name, "status": "error", "message": msg}

    def _save(self, doc: dict) -> None:
        cfg.atomic_write_text(cfg.INDEX_DIR / f"{doc['id']}.json", json.dumps(doc, ensure_ascii=False))

    # ------------------------------------------------------------ Maintenance
    def reindex(self, doc: dict, ctx: Context) -> dict:
        new = {k: v for k, v in doc.items() if k not in ("chunks",)}
        self._extract_into(new, self.stored_path(doc), ctx)
        self._save(new)
        return new

    def remove(self, doc: dict) -> None:
        for p in (self.stored_path(doc), cfg.INDEX_DIR / f"{doc['id']}.json"):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        with self._lock:
            self._by_sha.pop(doc.get("sha1", ""), None)
