"""Keeps the program's own Python up to date.

The program first only asks whether a newer version exists (check(), a few small requests
with a short timeout, silent without internet). The user then decides: update, later, or
skip this version. Only after "update" the preparation runs as its own background process
(tools/python_update.py), so the download and the package installation never delay the
program and continue when its window is closed:
  1. only the Python the launcher downloaded is updated (python/python/), never a system
     Python, a venv or PYTHON_BIN;
  2. the newest stable CPython is looked up - Linux/macOS: python-build-standalone,
     Windows: the Python Software Foundation's NuGet package;
  3. if it is newer, it is downloaded and verified - Linux/macOS: SHA-256 against the
     release's SHA256SUMS; Windows: SHA-512 against the NuGet catalog and the code
     signature of the Python Software Foundation on python.exe;
  4. it is unpacked into python/_next/, the packages from requirements.txt are installed
     into it and loaded once; only then the file python/_next/READY is written.
The launcher switches to python/_next/ at the next start and deletes the old Python.
PY_VERSION (e.g. 3.13), if set, limits updates to that series; otherwise no Python newer than
"# python-max" in requirements.txt is chosen - the newest series the locked packages have wheels for.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import net

log = logging.getLogger(__name__)

PBS = "https://github.com/astral-sh/python-build-standalone/releases"
NUGET = "https://api.nuget.org/v3-flatcontainer"
NUGET_REGISTRATION = "https://api.nuget.org/v3/registration5-semver1"
TIMEOUT = 8                          # seconds per request: without internet the check gives up quickly
MIN_FREE = 1500 * 1024 ** 2          # new Python + packages, with room to spare
LOCK_AGE = 3 * 3600                  # an older lock is left over from a stopped run
NO_WINDOW = 0x08000000               # Windows: CREATE_NO_WINDOW


class PythonUpdateError(Exception):
    pass


def managed_prefix(root: Path) -> Path:
    return root / "python" / "python"


def is_managed(root: Path, prefix: str = sys.prefix) -> bool:
    """True if `prefix` is the Python the launcher downloaded into python/python/."""
    try:
        return Path(prefix).resolve() == managed_prefix(root).resolve()
    except OSError:
        return False


def version_tuple(text: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def python_max(root: Path) -> int | None:
    """Newest 3.X the locked packages have wheels for: "# python-max: 3.15" in requirements.txt -> 15."""
    try:
        m = re.search(r"^# python-max: 3\.(\d+)\s*$", (root / "requirements.txt").read_text(encoding="utf-8"),
                      re.M)
    except OSError:
        return None
    return int(m.group(1)) if m else None


def _series_ok(version: tuple[int, int, int], series: str, upto: int | None = None) -> bool:
    """In the PY_VERSION series if one is set; otherwise not newer than 3.<upto>."""
    if not series and upto is not None and version[:2] > (3, upto):
        return False
    return not series or ".".join(map(str, version)).startswith(series.rstrip(".") + ".") \
        or ".".join(map(str, version)) == series


def _get(url: str, limit: int = 8 * 1024 ** 2) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AdvancedCtrlF"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        if not r.geturl().startswith("https://"):
            raise PythonUpdateError("redirected to a non-HTTPS address")
        data = r.read(limit + 1)
    if len(data) > limit:
        raise PythonUpdateError("response too large")
    return data


# ---------------------------------------------------------------- Linux / macOS
def pbs_triple() -> str | None:
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
        platform.machine().lower())
    if arch is None:
        return None
    return f"{arch}-apple-darwin" if sys.platform == "darwin" else f"{arch}-unknown-linux-gnu"


def pbs_latest(sums: str, triple: str, series: str = "", upto: int | None = None) -> tuple[tuple[int, int, int], str, str] | None:
    """(version, file name, SHA-256) of the newest stable install_only build in SHA256SUMS."""
    pat = re.compile(rf"^cpython-(\d+)\.(\d+)\.(\d+)\+(\d+)-{re.escape(triple)}-install_only\.tar\.gz$")
    best = None
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            continue
        m = pat.match(parts[1])
        if not m:
            continue
        v = tuple(int(x) for x in m.groups()[:3])
        if _series_ok(v, series, upto) and (best is None or v > best[0]):
            best = (v, parts[1], parts[0])
    return best


def _pbs_download(work: Path, name: str, sha256: str, progress) -> None:
    """Into work/python/ (the archive's top folder is "python/")."""
    tag = name.split("+", 1)[1].split("-", 1)[0]          # build date, e.g. 20261026
    url = f"{PBS}/download/{tag}/{urllib.parse.quote(name)}"
    archive = work / "dl" / "python.tar.gz"
    progress(f"Downloading {name} …")
    net.download(url, archive, max_bytes=300 * 1024 ** 2)
    if _sha256(archive) != sha256:
        raise PythonUpdateError("checksum mismatch - download discarded")
    progress("Checksum OK, unpacking …")
    with tarfile.open(archive) as tf:
        tf.extractall(work, filter="data")                 # no absolute paths, links out, devices


# ---------------------------------------------------------------- Windows
def nuget_id() -> str:
    return "pythonarm64" if platform.machine().upper() == "ARM64" else "python"


def nuget_latest(versions: list[str], series: str = "", upto: int | None = None) -> str | None:
    stable = [(version_tuple(v), v) for v in versions if version_tuple(v)]
    stable = [(t, v) for t, v in stable if _series_ok(t, series, upto)]
    return max(stable)[1] if stable else None


def check_sha512_b64(path: Path, expected_b64: str) -> bool:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return base64.b64encode(h.digest()).decode() == expected_b64


def _authenticode_ok(exe: Path) -> bool:
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:ACF_FILE; "
              "if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Python Software Foundation') "
              "{ exit 1 }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=dict(os.environ, ACF_FILE=str(exe)), capture_output=True, timeout=120,
                       creationflags=NO_WINDOW)
    return r.returncode == 0


def _nuget_download(work: Path, pkg_id: str, version: str, progress) -> None:
    """Into work/python/ (the package's folder "tools/" holds python.exe)."""
    pkg = work / "dl" / "python.nupkg"
    progress(f"Downloading official Python {version} …")
    net.download(f"{NUGET}/{pkg_id}/{version}/{pkg_id}.{version}.nupkg", pkg, max_bytes=300 * 1024 ** 2)
    reg = json.loads(_get(f"{NUGET_REGISTRATION}/{pkg_id}/{version}.json"))
    leaf = json.loads(_get(reg["catalogEntry"]))
    if leaf.get("packageHashAlgorithm") != "SHA512" or not check_sha512_b64(pkg, leaf.get("packageHash", "")):
        raise PythonUpdateError("checksum mismatch - download discarded")
    progress("Checksum OK, unpacking …")
    unpacked = work / "dl" / "pkg"
    with zipfile.ZipFile(pkg) as z:
        for info in z.infolist():
            name = Path(info.filename)
            if name.is_absolute() or ".." in name.parts:
                raise PythonUpdateError("unsafe path in the package")
        z.extractall(unpacked)
    exe = unpacked / "tools" / "python.exe"
    if not exe.is_file() or not _authenticode_ok(exe):
        raise PythonUpdateError("python.exe is not validly signed by the Python Software Foundation")
    shutil.move(str(unpacked / "tools"), str(work / "python"))


# ---------------------------------------------------------------- Common
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def latest(series: str = "", upto: int | None = None) -> tuple[tuple[int, int, int], dict]:
    """Newest available version (not newer than 3.<upto>) and what is needed to fetch it."""
    if os.name == "nt":
        pkg_id = nuget_id()
        versions = json.loads(_get(f"{NUGET}/{pkg_id}/index.json")).get("versions", [])
        v = nuget_latest(versions, series, upto)
        if v is None:
            raise PythonUpdateError("no matching Python version found")
        return version_tuple(v), {"kind": "nuget", "id": pkg_id, "version": v}
    triple = pbs_triple()
    if triple is None:
        raise PythonUpdateError(f"architecture {platform.machine()} is not supported")
    sums = _get(f"{PBS}/latest/download/SHA256SUMS").decode("utf-8", "replace")
    found = pbs_latest(sums, triple, series, upto)
    if found is None:
        raise PythonUpdateError("no matching Python version found")
    return found[0], {"kind": "pbs", "name": found[1], "sha256": found[2]}


def check(root: Path, series: str | None = None) -> str | None:
    """Newer Python version than the running one, or None. Only looks, downloads nothing."""
    series = os.environ.get("PY_VERSION", "") if series is None else series
    if not is_managed(root) or prepared(root):
        return None
    version, _ = latest(series, python_max(root))
    return ".".join(map(str, version)) if version > sys.version_info[:3] else None


def new_interpreter(folder: Path) -> Path:
    return folder / "python" / ("python.exe" if os.name == "nt" else "bin/python3")


def prepared(root: Path) -> str | None:
    """Version waiting in python/_next/ for the next start, if any."""
    ready = root / "python" / "_next" / "READY"
    return ready.read_text(encoding="utf-8").strip() if ready.is_file() else None


def _lock(root: Path):
    lock = root / "python" / "_next.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_AGE:
            lock.unlink()
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return lock


def prepare(root: Path, progress=lambda msg: None, series: str | None = None) -> str | None:
    """Prepare a newer Python in python/_next/. Returns its version, or None if there is none."""
    series = os.environ.get("PY_VERSION", "") if series is None else series
    if not is_managed(root):
        progress("This program does not use its own downloaded Python - nothing to update.")
        return None
    current = sys.version_info[:3]
    waiting = prepared(root)
    if waiting and version_tuple(waiting) and version_tuple(waiting) > current:
        return waiting
    version, how = latest(series, python_max(root))
    if version <= current:
        progress(f"Python {'.'.join(map(str, current))} is the newest version.")
        return None
    if shutil.disk_usage(root).free < MIN_FREE:
        raise PythonUpdateError("not enough free disk space for the new Python")
    lock = _lock(root)
    if lock is None:
        progress("A Python update is already being prepared.")
        return None
    work = root / "python" / "_next.tmp"
    try:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(root / "python" / "_next", ignore_errors=True)
        (work / "dl").mkdir(parents=True)
        if how["kind"] == "nuget":
            _nuget_download(work, how["id"], how["version"], progress)
        else:
            _pbs_download(work, how["name"], how["sha256"], progress)
        shutil.rmtree(work / "dl", ignore_errors=True)
        exe = new_interpreter(work)
        text = ".".join(map(str, version))
        flags = NO_WINDOW if os.name == "nt" else 0
        out = subprocess.run([str(exe), "-I", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                             capture_output=True, text=True, timeout=120, creationflags=flags)
        if out.returncode != 0 or out.stdout.strip() != text:
            raise PythonUpdateError(f"the new Python does not start correctly ({out.stdout.strip() or out.stderr.strip()})")
        progress(f"Installing the packages for Python {text} …")
        logfile = root / "data" / "python-update.log"
        deps = subprocess.run([str(exe), "-I", str(root / "tools" / "ensure_deps.py"), "--log", str(logfile)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600,
                              env=dict(os.environ, ACF_NO_WINDOW="1"), creationflags=flags)
        if deps.returncode != 0:
            raise PythonUpdateError(f"the packages could not be installed for Python {text} (see {logfile.name})")
        (work / "READY").write_text(text + "\n", encoding="utf-8")
        os.replace(work, root / "python" / "_next")
        progress(f"Python {text} is ready - it is used from the next start.")
        return text
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        lock.unlink(missing_ok=True)


def spawn(root: Path) -> None:
    """Start the preparation as an independent background process with low priority."""
    script = root / "tools" / "python_update.py"
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | NO_WINDOW | 0x00004000   # detached, own group, no window, below normal
        subprocess.Popen([sys.executable, "-I", str(script)], cwd=str(root), creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["nice", "-n", "10", sys.executable, "-I", str(script)] if shutil.which("nice")
                         else [sys.executable, "-I", str(script)], cwd=str(root), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
