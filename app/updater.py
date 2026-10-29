"""Automatic updates.

Every release is described by a small signed file on the program's web page:
    updates/latest.json      {"version", "url", "sha256", "size", "published"}
    updates/latest.json.sig  Ed25519 signature of exactly these bytes (hex)
    updates/SHA256SUMS       checksums of all release archives (for manual downloads)

An update
  1. fetches latest.json and its signature (short timeout, in the background) and rejects
     it unless the signature matches the public key below - nobody without the
     maintainer's private key can publish an update, even with access to the web server;
  2. accepts only a higher version and an archive below updates/ on the program's site;
  3. downloads the archive and checks size and SHA-256 against the signed values;
  4. unpacks it into data/updates/staged-<version>/ and checks the version inside;
  5. is applied only when the user restarts: the program files move into
     data/updates/backup-<old version>/, the new ones take their place (data/, config/
     and python/ are never touched), and the backup is restored if anything fails.
A program folder that is a Git checkout is never changed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import __version__, ed25519, net

log = logging.getLogger(__name__)

SITE = "https://advanced-ctrl-f.giit.ch/updates/"
MANIFEST_URL = SITE + "latest.json"
PUBLIC_KEY = bytes.fromhex("bcf56b2aa564da3eaad79197ea2380ba17b31b641bc2869e51a34df19a55e280")
PAGE_URL = "https://github.com/SebisCodes/AdvancedCtrlF"
PROTECTED = {"data", "config", "python", ".git"}   # never replaced
MAX_ARCHIVE = 200 * 1024 ** 2
MAX_MANIFEST = 64 * 1024
TIMEOUT = 8                                        # seconds for the small manifest requests
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateError(Exception):
    pass


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION.match(str(text).strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_git_checkout(root: Path) -> bool:
    return (root / ".git").exists()


# ---------------------------------------------------------------- Manifest
def _get(url: str, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AdvancedCtrlF", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        if not r.geturl().startswith("https://"):
            raise UpdateError("redirected to a non-HTTPS address")
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("response too large")
    return data


def check_manifest(raw: bytes, signature_hex: str, public_key: bytes = PUBLIC_KEY) -> dict:
    """Parse latest.json after checking its signature and contents."""
    try:
        sig = bytes.fromhex(signature_hex.strip())
    except ValueError as e:
        raise UpdateError("the update signature is unreadable") from e
    if not ed25519.verify(public_key, raw, sig):
        raise UpdateError("the update signature is not valid - update rejected")
    try:
        m = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise UpdateError("the update description is unreadable") from e
    if not isinstance(m, dict) or parse_version(m.get("version", "")) is None:
        raise UpdateError("the update description has no valid version")
    if not str(m.get("url", "")).startswith(SITE) or ".." in str(m["url"]):
        raise UpdateError("the update archive is not on the program's web page")
    if not re.fullmatch(r"[0-9a-f]{64}", str(m.get("sha256", ""))):
        raise UpdateError("the update description has no valid checksum")
    if not isinstance(m.get("size"), int) or not 0 < m["size"] <= MAX_ARCHIVE:
        raise UpdateError("the update description has no valid size")
    return m


def fetch_manifest() -> dict:
    try:
        raw = _get(MANIFEST_URL, MAX_MANIFEST)
        sig = _get(MANIFEST_URL + ".sig", 1024).decode("ascii", "replace")
    except UpdateError:
        raise
    except Exception as e:
        raise UpdateError(f"no connection to the update server ({e})") from e
    return check_manifest(raw, sig)


def newer(manifest: dict, current: str = __version__) -> bool:
    cur = parse_version(current)
    return bool(cur) and parse_version(manifest["version"]) > cur


# ---------------------------------------------------------------- Staging
def _work(root: Path) -> Path:
    d = root / "data" / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _source_root(folder: Path) -> Path:
    """The archive contains one top folder ("AdvancedCtrlF-1.5.0/")."""
    entries = list(folder.iterdir())
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else folder


def _version_in(src: Path) -> str | None:
    init = src / "app" / "__init__.py"
    if not init.is_file():
        return None
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()


def stage(root: Path, manifest: dict, progress=lambda msg: None, cancelled=None) -> Path:
    """Download, check and unpack the update; returns the folder with the new program files."""
    from .store import unpack_archive   # heavy imports only when an update really runs

    if is_git_checkout(root):
        raise UpdateError("the program folder is a Git checkout - update it with git pull")
    version = manifest["version"].lstrip("v")
    work = _work(root)
    target = work / f"staged-{version}"
    if (target / "ok").is_file():
        return target
    for old in list(work.glob("staged-*")) + list(work.glob("tmp*")):   # also leftovers of interrupted downloads
        _remove(old)
    with tempfile.TemporaryDirectory(dir=work) as tmp:
        tmp = Path(tmp)
        archive = tmp / "update.zip"
        progress(f"Downloading version {version} …")
        net.download(manifest["url"], archive, lambda m: progress(f"Downloading version {version}: {m}"),
                     cancelled, manifest["size"])
        if archive.stat().st_size != manifest["size"] or sha256_file(archive) != manifest["sha256"]:
            raise UpdateError("the downloaded archive does not match its checksum - update rejected")
        progress(f"Checking version {version} …")
        unpacked = tmp / "new"
        unpacked.mkdir()
        unpack_archive(archive, unpacked)
        src = _source_root(unpacked)
        found = _version_in(src)
        if found is None or parse_version(found) != parse_version(version) \
                or not (src / "advanced_ctrl_f.py").is_file():
            raise UpdateError(f"the archive is not Advanced Ctrl+F {version} (found {found})")
        shutil.move(str(src), str(target))
    (target / "ok").write_text(json.dumps(manifest), encoding="utf-8")
    log.info("Update %s downloaded and checked", version)
    return target


def staged(root: Path, current: str = __version__) -> tuple[str, Path] | None:
    """A complete, checked update that is newer than the running version."""
    work = root / "data" / "updates"
    best = None
    for d in work.glob("staged-*") if work.is_dir() else ():
        v = d.name.split("-", 1)[1]
        if (d / "ok").is_file() and parse_version(v) and parse_version(current) \
                and parse_version(v) > parse_version(current) and (best is None or parse_version(v) > parse_version(best[0])):
            best = (v, d)
    return best


def discard(root: Path) -> None:
    for d in (root / "data" / "updates").glob("staged-*"):
        _remove(d)


# ---------------------------------------------------------------- Applying
def apply(root: Path, staged_dir: Path) -> str:
    """Replace the program files with the staged ones. Returns the new version."""
    if is_git_checkout(root):
        raise UpdateError("the program folder is a Git checkout - update it with git pull")
    new = _version_in(staged_dir)
    if not (staged_dir / "ok").is_file() or new is None:
        raise UpdateError("the prepared update is incomplete")
    work = _work(root)
    names = [p.name for p in staged_dir.iterdir() if p.name not in PROTECTED and p.name != "ok"]
    for old in work.glob("backup-*"):            # only the backup of the last update is kept
        _remove(old)
    backup = work / f"backup-{__version__}"
    backup.mkdir(parents=True)
    moved: list[str] = []
    placed: list[str] = []
    try:
        for name in names:                       # old program files out of the way ...
            if (root / name).exists():
                shutil.move(str(root / name), str(backup / name))
                moved.append(name)
        for name in names:                       # ... and the new ones in
            shutil.move(str(staged_dir / name), str(root / name))
            placed.append(name)
    except Exception as e:
        log.exception("Update failed - restoring the previous version")
        for name in placed:
            _remove(root / name)
        for name in moved:
            shutil.move(str(backup / name), str(root / name))
        raise UpdateError(f"installing failed, the previous version was restored: {e}") from e
    _remove(staged_dir)
    log.info("Updated from %s to %s", __version__, new)
    return new


def restart(root: Path) -> None:
    """Start the program again through its launcher (installs changed packages first)."""
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(["cmd", "/c", str(root / "start.cmd")], cwd=str(root), creationflags=flags,
                         close_fds=True)
    else:
        subprocess.Popen(["/bin/bash", str(root / "start.sh")], cwd=str(root), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
    log.info("Restarting through the launcher (%s)", sys.platform)
