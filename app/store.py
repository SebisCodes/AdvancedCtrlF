"""Document store: copy to data/documents/, extraction, JSON to data/index/."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import config as cfg
from .extractors import Context, ExtractError, extract, is_archive, sniff, suffix

log = logging.getLogger(__name__)

MAX_ARCHIVE_TOTAL = 4 * 1024 ** 3   # protection against zip bombs
MAX_ARCHIVE_MEMBERS = 20000
MAX_ARCHIVE_DEPTH = 3
SCHEMA = 1

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
        if rel is None:
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
                log.warning("Index %s defekt: %s", f, e)
                continue
            if isinstance(d, dict) and "id" in d:
                docs.append(d)
                self._by_sha[d.get("sha1", "")] = d["id"]
        return docs

    def stored_path(self, doc: dict) -> Path:
        # accept only a file name, never a path from the JSON
        return cfg.DOCS_DIR / Path(doc["stored"]).name

    # ------------------------------------------------------------ Adding
    def add_path(self, src: Path, ctx: Context, display_prefix: str = "", depth: int = 0) -> list[dict]:
        """Add a file, folder or archive. Returns new/updated documents."""
        if src.is_dir():
            docs = []
            for p in sorted(src.rglob("*")):
                if ctx.cancelled():
                    break
                if p.is_file() and not p.is_symlink():
                    folder = p.parent.relative_to(src.parent).as_posix()
                    docs += self.add_path(p, ctx, f"{display_prefix}{folder}/", depth)
            return docs
        size = src.stat().st_size
        if size > self.config.get("max_file_mb", 500) * 1024 * 1024:
            return [self._error_doc(src, display_prefix, f"File larger than {self.config.get('max_file_mb')} MB")]
        if is_archive(src) and depth < MAX_ARCHIVE_DEPTH:
            ctx.progress(f"Unpacking {src.name} …")
            docs = []
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
                    docs += self.add_path(f, ctx, prefix, depth + 1)
            return docs
        return [self._add_file(src, ctx, display_prefix)]

    def _add_file(self, src: Path, ctx: Context, display_prefix: str) -> dict:
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
        doc = {
            "schema": SCHEMA, "id": doc_id, "sha1": sha, "name": src.name,
            "display_name": display_prefix + src.name, "stored": stored_name,
            "size": src.stat().st_size, "added": datetime.now().isoformat(timespec="seconds"),
        }
        self._extract_into(doc, target, ctx)
        self._save(doc)
        with self._lock:
            self._by_sha[sha] = doc_id
        return doc

    def _extract_into(self, doc: dict, path: Path, ctx: Context) -> None:
        ctx.progress(f"Reading {doc['name']} …")
        doc.update(kind="", status="ok", message="", chunks=[], pages=None, language="und", ocr_pages=0)
        try:
            kind = sniff(path)
            doc["kind"] = kind.lstrip(".")
            res = extract(path, ctx, kind)
            doc["chunks"] = res.chunks
            doc["pages"] = res.meta.get("pages")
            doc["pages_approx"] = res.meta.get("pages_approx", False)
            doc["language"] = res.meta.get("language", "und")
            doc["ocr_pages"] = res.meta.get("ocr_pages", 0)
            if res.meta.get("warning"):
                doc["status"], doc["message"] = "warn", res.meta["warning"]
            elif not res.chunks:
                doc["status"], doc["message"] = "warn", "No text found"
        except ExtractError as e:
            doc["status"], doc["message"] = "error", str(e)
        except Exception as e:  # broken files must never stop the program
            log.exception("Error in %s", path)
            doc["status"], doc["message"] = "error", f"{type(e).__name__}: {e}"
        doc["indexed"] = datetime.now().isoformat(timespec="seconds")

    def _error_doc(self, src: Path, prefix: str, msg: str) -> dict:
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
