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
  4. checks the version inside the archive and keeps the archive together with the signed
     description in data/updates/staged-<version>/;
  5. is applied only when the user restarts: signature, checksum and version are checked
     again, the archive is unpacked afresh, the program files move into
     data/updates/backup-<old version>/ and the new ones take their place (data/, config/
     and python/ are never touched). The backup is restored if anything fails; the file
     data/updates/applying lets the launcher roll back an install that was interrupted.
The newest version ever seen is remembered: an older description offered later (an
attacker replaying an old, genuinely signed release) is reported instead of being trusted.
A program folder that is a Git checkout is never changed.
"""
from __future__ import annotations

import errno
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
    """Parse latest.json after checking its signature and contents. The signed bytes are kept
    in the private keys "_raw"/"_sig" so a staged update can be verified again later."""
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
    m["_raw"], m["_sig"] = raw, signature_hex.strip()
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


def _write_atomic(path: Path, data: bytes) -> None:
    """Complete or not at all, and on the disk before anything that depends on it happens."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def remember_version(root: Path, manifest: dict, current: str = __version__) -> None:
    """Refuse a description older than the newest one seen before or than the installed version
    (replay of an old release by whoever controls the server or the connection); otherwise
    remember its version. A Git checkout may be ahead of the releases - only its history counts."""
    seen_file = _work(root) / "newest-seen"
    try:
        seen = parse_version(seen_file.read_text(encoding="utf-8"))
    except OSError:
        seen = None
    floor = max(v for v in (seen, None if is_git_checkout(root) else parse_version(current), (0, 0, 0)) if v)
    offered = parse_version(manifest["version"])
    if offered < floor:
        raise UpdateError(f"the update server offers {manifest['version']} although "
                          f"{'.'.join(map(str, floor))} is known - outdated or replayed description")
    if seen is None or offered > seen:
        _write_atomic(seen_file, manifest["version"].lstrip("v").encode())


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
    elif path.exists() or path.is_symlink():
        path.unlink()


def _check_archive(archive: Path, manifest: dict) -> None:
    if not archive.is_file() or archive.stat().st_size != manifest["size"] \
            or sha256_file(archive) != manifest["sha256"]:
        raise UpdateError("the downloaded archive does not match its checksum - update rejected")


def _unpack_checked(archive: Path, into: Path, version: str) -> Path:
    """Unpack the archive into the empty folder `into`; returns the program folder inside."""
    from .store import unpack_archive   # heavy imports only when an update really runs
    unpack_archive(archive, into)
    src = _source_root(into)
    found = _version_in(src)
    if found is None or parse_version(found) != parse_version(version) \
            or not (src / "advanced_ctrl_f.py").is_file():
        raise UpdateError(f"the archive is not Advanced Ctrl+F {version} (found {found})")
    return src


def stage(root: Path, manifest: dict, progress=lambda msg: None, cancelled=None,
          public_key: bytes = PUBLIC_KEY) -> Path:
    """Download and check the update; keeps archive + signed description for apply()."""
    if is_git_checkout(root):
        raise UpdateError("the program folder is a Git checkout - update it with git pull")
    if "_raw" not in manifest:
        raise UpdateError("the update description carries no signature")
    version = manifest["version"].lstrip("v")
    work = _work(root)
    target = work / f"staged-{version}"
    if (target / "ok").is_file():
        try:                                 # prepared earlier: reuse it only if it is still intact
            _check_archive(target / "update.zip", _verified(target, public_key))
            return target
        except UpdateError:
            log.info("Prepared update %s is incomplete or changed - downloading it again", version)
    for old in list(work.glob("staged-*")) + list(work.glob("tmp*")):   # also leftovers of interrupted downloads
        _remove(old)
    with tempfile.TemporaryDirectory(dir=work) as tmp:
        tmp = Path(tmp)
        keep = tmp / "staged"
        keep.mkdir()
        archive = keep / "update.zip"
        progress(f"Downloading version {version} …")
        net.download(manifest["url"], archive, lambda m: progress(f"Downloading version {version}: {m}"),
                     cancelled, manifest["size"])
        _check_archive(archive, manifest)
        progress(f"Checking version {version} …")
        (tmp / "new").mkdir()
        _unpack_checked(archive, tmp / "new", version)
        (keep / "latest.json").write_bytes(manifest["_raw"])
        (keep / "latest.json.sig").write_text(manifest["_sig"], encoding="ascii")
        (keep / "ok").write_text("checked\n", encoding="utf-8")
        os.replace(keep, target)
    log.info("Update %s downloaded and checked", version)
    return target


def _verified(staged_dir: Path, public_key: bytes) -> dict:
    """The signed description kept with a staged update, checked again (nothing in
    data/updates/ is trusted just because it is there)."""
    try:
        raw = (staged_dir / "latest.json").read_bytes()
        sig = (staged_dir / "latest.json.sig").read_text(encoding="ascii", errors="replace")
    except OSError as e:
        raise UpdateError("the prepared update is incomplete") from e
    m = check_manifest(raw, sig, public_key)
    if not (staged_dir / "ok").is_file() \
            or parse_version(staged_dir.name.split("-", 1)[-1]) != parse_version(m["version"]):
        raise UpdateError("the prepared update is incomplete")
    return m


def staged(root: Path, current: str = __version__, public_key: bytes = PUBLIC_KEY) -> tuple[str, Path] | None:
    """A complete, signed update that is newer than the running version."""
    work = root / "data" / "updates"
    best = None
    for d in work.glob("staged-*") if work.is_dir() else ():
        try:
            v = _verified(d, public_key)["version"].lstrip("v")
        except UpdateError:
            continue
        if parse_version(current) and parse_version(v) > parse_version(current) \
                and (best is None or parse_version(v) > parse_version(best[0])):
            best = (v, d)
    return best


def discard(root: Path) -> None:
    for d in (root / "data" / "updates").glob("staged-*"):
        _remove(d)


# ---------------------------------------------------------------- Applying
JOURNAL = "applying"   # data/updates/applying: line 1 backup folder, then "old <name>" / "new <name>"
_BACKUP_NAME = re.compile(r"backup-[0-9A-Za-z][0-9A-Za-z._+-]*")
_ENTRY_NAME = re.compile(r"[0-9A-Za-z._+-]+")


def _entry_ok(name: str) -> bool:
    """A single top-level name of the program folder (no path, never data/config/python/.git)."""
    return bool(_ENTRY_NAME.fullmatch(name)) and name not in (".", "..") and name not in PROTECTED


def _exists(p: Path) -> bool:
    return p.exists() or p.is_symlink()


def _remove_strict(path: Path) -> None:
    """Like _remove, but an error is an error: a rollback must not continue on a half-deleted folder."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif _exists(path):
        path.unlink()
    if _exists(path):
        raise OSError(f"{path} could not be removed")


def _replace_file(src: Path, dst: Path) -> None:
    """os.replace (one step); if data/ lies on another drive, copy next to the target first."""
    try:
        os.replace(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        part = dst.with_name(dst.name + ".partial")
        shutil.copy2(src, part)
        os.replace(part, dst)
        src.unlink()


def _put_aside(src: Path, backup: Path) -> None:
    """Move the old file/folder into the backup. Files are linked (or copied under a temporary
    name first - a backup is never incomplete), so the launchers are never missing: os.replace
    then swaps them in one step."""
    if src.is_dir() and not src.is_symlink():
        shutil.move(str(src), str(backup))   # a rename on the same drive
        return
    try:
        os.link(src, backup)
    except OSError:
        part = backup.with_name(backup.name + ".partial")
        shutil.copy2(src, part)
        os.replace(part, backup)


def apply(root: Path, staged_dir: Path, public_key: bytes = PUBLIC_KEY) -> str:
    """Replace the program files with the staged ones. Returns the new version."""
    if is_git_checkout(root):
        raise UpdateError("the program folder is a Git checkout - update it with git pull")
    work = _work(root)
    if (work / JOURNAL).exists():            # an earlier install was interrupted: restore that first
        rollback(root)
    m = _verified(staged_dir, public_key)
    version = m["version"].lstrip("v")
    current = _version_in(root) or __version__
    if not parse_version(current) or parse_version(version) <= parse_version(current):
        raise UpdateError(f"the prepared update ({version}) is not newer than {current}")
    fresh = Path(tempfile.mkdtemp(dir=work, prefix="tmp-apply-"))
    try:
        archive = fresh / "update.zip"       # private copy: checked and unpacked from the same file
        shutil.copyfile(staged_dir / "update.zip", archive)
        _check_archive(archive, m)
        (fresh / "new").mkdir()
        src = _unpack_checked(archive, fresh / "new", version)
        names = sorted(p.name for p in src.iterdir() if p.name not in PROTECTED)
        if not all(_entry_ok(n) for n in names):
            raise UpdateError("the archive contains unexpected file names")
        for old in work.glob("backup-*"):            # only the backup of the last update is kept
            _remove(old)
        backup = work / f"backup-{current}"
        backup.mkdir(parents=True)
        journal = work / JOURNAL
        nl = "\r\n" if os.name == "nt" else "\n"   # start.cmd reads the first line with "set /p"
        _write_atomic(journal, (backup.name + nl + "".join(
            f"{'old' if _exists(root / n) else 'new'} {n}{nl}" for n in names)).encode("utf-8"))
        try:
            for name in names:                       # one name at a time: old aside, new in
                if _exists(root / name):
                    _put_aside(root / name, backup / name)
                if (src / name).is_dir() or not _exists(root / name):
                    if _exists(root / name):         # a file that became a folder
                        _remove_strict(root / name)
                    shutil.move(str(src / name), str(root / name))
                else:
                    _replace_file(src / name, root / name)
        except Exception as e:
            log.exception("Update failed - restoring the previous version")
            try:
                rollback(root)
            except Exception as e2:
                raise UpdateError(f"installing failed ({e}) and restoring the previous version stopped ({e2}) - "
                                  "close other programs using the folder and start Advanced Ctrl+F again") from e
            raise UpdateError(f"installing failed, the previous version was restored: {e}") from e
        journal.unlink()
    finally:
        _remove(fresh)
    _remove(staged_dir)
    log.info("Updated from %s to %s", current, version)
    return version


def rollback(root: Path) -> bool:
    """Undo an interrupted install described by data/updates/applying (the launchers do the
    same before they start anything). Files are put back with one atomic rename each, so a
    launcher is never missing; any error stops the rollback and keeps the journal for the next
    attempt. Returns True if there was something to undo."""
    work = root / "data" / "updates"
    journal = work / JOURNAL
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    first = lines[0].strip() if lines else ""
    backup = work / first if _BACKUP_NAME.fullmatch(first) and ".." not in first else None
    for line in lines[1:]:
        state, _, name = line.strip().partition(" ")
        if not _entry_ok(name):
            continue
        target = root / name
        saved = backup / name if backup else None
        if state == "old" and saved and _exists(saved):
            if saved.is_dir() and not saved.is_symlink():
                if _exists(target):
                    _remove_strict(target)   # target is gone now: the move cannot nest the folder
                shutil.move(str(saved), str(target))
            else:
                if target.is_dir() and not target.is_symlink():
                    _remove_strict(target)
                _replace_file(saved, target)  # the old file takes the place of the new one in one step
        elif state == "new" and _exists(target):
            _remove_strict(target)
    journal.unlink()
    log.warning("An interrupted update was rolled back")
    return True


def restart(root: Path) -> None:
    """Start the program again through its launcher (installs changed packages first)."""
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        # cmd.exe parses its command line: a folder like "C:\a&b" must not become two commands.
        # The path goes in through a variable, expanded inside quotes (/s keeps them as they are).
        env = dict(os.environ, ACF_LAUNCHER=str(root / "start.cmd"))
        cmd = f'"{net.windows_exe("System32", "cmd.exe")}" /d /s /c ""%ACF_LAUNCHER%""'
        subprocess.Popen(cmd, cwd=str(root), env=env, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(["/bin/bash", str(root / "start.sh")], cwd=str(root), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
    log.info("Restarting through the launcher (%s)", sys.platform)
