"""HTTPS downloads shared by OCR data, GitHub repositories and file links.

Tries Python (urllib) first and, on Windows, falls back to PowerShell, which uses the
Windows network stack incl. company proxies that require Windows authentication.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from email.message import Message
from pathlib import Path

log = logging.getLogger(__name__)

CHUNK = 1 << 20
MAX_DOWNLOAD = 2 * 1024 ** 3   # 2 GB


class DownloadError(Exception):
    pass


def _check_url(url: str) -> None:
    if not url.lower().startswith("https://"):
        raise DownloadError("only HTTPS downloads are allowed")


def filename_from(url: str, content_disposition: str | None = None) -> str:
    if content_disposition:
        msg = Message()
        msg["content-disposition"] = content_disposition
        name = msg.get_filename()
        if name:
            return Path(name.replace("\\", "/")).name
    name = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    return name or "download"


def fetch_python(url: str, dest: Path, progress=None, cancelled=None, max_bytes: int = MAX_DOWNLOAD) -> str | None:
    """Download `url` to `dest`. Returns the server-suggested file name (if any)."""
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "AdvancedCtrlF"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        if not r.geturl().lower().startswith("https://"):
            raise DownloadError("redirected to a non-HTTPS address")
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, 0.0
        while True:
            block = r.read(CHUNK)
            if not block:
                break
            done += len(block)
            if done > max_bytes:
                raise DownloadError(f"download larger than {max_bytes // 1024 ** 2} MB")
            if cancelled and cancelled():
                raise DownloadError("cancelled")
            f.write(block)
            if progress and time.monotonic() - last > 0.5:
                last = time.monotonic()
                progress(f"{done / 1024 ** 2:.1f} MB" + (f" of {total / 1024 ** 2:.1f} MB" if total else ""))
        return r.headers.get("Content-Disposition")


def fetch_powershell(url: str, dest: Path, progress=None, cancelled=None, max_bytes: int = MAX_DOWNLOAD) -> None:
    """Windows only: download with PowerShell (system proxy + Windows credentials).
    URL and target are passed via environment variables - nothing is pasted into the command."""
    _check_url(url)
    script = ("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
              "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
              "$p=[Net.WebRequest]::DefaultWebProxy; if ($p) { $p.Credentials=[Net.CredentialCache]::DefaultNetworkCredentials }; "
              "Invoke-WebRequest -UseBasicParsing -UseDefaultCredentials -Uri $env:ACF_URL -OutFile $env:ACF_OUT")
    env = dict(os.environ, ACF_URL=url, ACF_OUT=str(dest))
    if progress:
        progress("downloading via PowerShell …")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                       env=env, capture_output=True, text=True, timeout=3600, creationflags=0x08000000)  # no window
    if r.returncode != 0:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", (r.stderr or r.stdout)).strip()   # drop colour codes
        raise DownloadError(msg.splitlines()[0] if msg else f"PowerShell exit code {r.returncode}")
    if dest.stat().st_size > max_bytes:
        raise DownloadError(f"download larger than {max_bytes // 1024 ** 2} MB")


def download(url: str, dest: Path, progress=None, cancelled=None, max_bytes: int = MAX_DOWNLOAD) -> str:
    """Download with all available methods. Returns the file name to use for the download."""
    methods = [("Python", fetch_python)] + ([("PowerShell", fetch_powershell)] if os.name == "nt" else [])
    errors = []
    for name, fetch in methods:
        try:
            disposition = fetch(url, dest, progress, cancelled, max_bytes)
            log.info("Downloaded %s via %s (%d bytes)", url, name, dest.stat().st_size)
            return filename_from(url, disposition if isinstance(disposition, str) else None)
        except Exception as e:
            if cancelled and cancelled():
                raise DownloadError("cancelled") from e
            errors.append(f"{name}: {e}")
            log.warning("Download of %s via %s failed: %s", url, name, e)
            try:
                dest.unlink()
            except OSError:
                pass
    raise DownloadError("; ".join(errors))
