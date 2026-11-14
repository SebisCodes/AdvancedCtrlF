"""HTTPS downloads shared by OCR data, GitHub repositories and file links.

Tries Python (urllib) first and, on Windows, falls back to PowerShell, which uses the
Windows network stack incl. company proxies that require Windows authentication (the Windows
credentials go only to the system proxy, never to the server of the download). On macOS
and Linux the fallback is curl, which uses the system certificate store - a Python without
its own certificates (e.g. python.org builds on macOS) fails with CERTIFICATE_VERIFY_FAILED.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
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


class TooLarge(DownloadError):
    """Final: another download method would receive just as much."""


def windows_exe(*parts: str) -> str:
    """Absolute path of a Windows system program (never looked up in the current folder)."""
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", *parts)


def powershell_exe() -> str:
    return windows_exe("System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def redact(url: str) -> str:
    """URL without user name/password, query and fragment - for logs and messages (tokens of
    pre-signed links must not end up in data/advanced_ctrl_f.log or copied diagnostics)."""
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname or ""
        if u.port:
            host += f":{u.port}"
        return urllib.parse.urlunsplit((u.scheme, host, u.path, "…" if u.query else "", ""))
    except ValueError:
        return "(address)"


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
                raise TooLarge(f"download larger than {max_bytes // 1024 ** 2} MB")
            if cancelled and cancelled():
                raise DownloadError("cancelled")
            f.write(block)
            if progress and time.monotonic() - last > 0.5:
                last = time.monotonic()
                progress(f"{done / 1024 ** 2:.1f} MB" + (f" of {total / 1024 ** 2:.1f} MB" if total else ""))
        return r.headers.get("Content-Disposition")


def fetch_powershell(url: str, dest: Path, progress=None, cancelled=None, max_bytes: int = MAX_DOWNLOAD) -> None:
    """Windows only: download with PowerShell (system proxy + Windows credentials for the proxy).
    URL, target and limit are passed via environment variables - nothing is pasted into the command.
    The response is streamed and stops at `max_bytes`; cancelling ends the process."""
    _check_url(url)
    script = ("$ErrorActionPreference='Stop'; "
              "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
              "$q=[Net.WebRequest]::Create($env:ACF_URL); $q.UserAgent='AdvancedCtrlF'; "
              "$p=[Net.WebRequest]::DefaultWebProxy; "
              "if ($p) { $p.Credentials=[Net.CredentialCache]::DefaultNetworkCredentials; $q.Proxy=$p }; "
              "$r=$q.GetResponse(); "
              "try { if ($r.ResponseUri.Scheme -ne 'https') { throw 'redirected to a non-HTTPS address' }; "
              "$max=[int64]$env:ACF_MAX; $in=$r.GetResponseStream(); $out=[IO.File]::Create($env:ACF_OUT); "
              "try { $buf=New-Object byte[] 1048576; $n=[int64]0; "
              "while (($k=$in.Read($buf,0,$buf.Length)) -gt 0) { $n+=$k; "
              "if ($n -gt $max) { [Console]::Error.WriteLine('ACF_TOO_LARGE'); exit 3 }; $out.Write($buf,0,$k) } } "
              "finally { $out.Close(); $in.Close() } } finally { $r.Close() }")
    env = dict(os.environ, ACF_URL=url, ACF_OUT=str(dest), ACF_MAX=str(max_bytes))
    if progress:
        progress("downloading via PowerShell …")
    p = subprocess.Popen([powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-Command", script], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, creationflags=0x08000000)  # no window
    start = last = time.monotonic()
    while True:
        try:
            out, err = p.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            pass
        if (cancelled and cancelled()) or time.monotonic() - start > 3600:
            p.kill()
            p.communicate()
            raise DownloadError("cancelled" if cancelled and cancelled() else "download took too long")
        if progress and time.monotonic() - last > 0.5 and dest.exists():
            last = time.monotonic()
            progress(f"{dest.stat().st_size / 1024 ** 2:.1f} MB")
    if p.returncode != 0:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", (err or out or "")).strip()   # drop colour codes
        if "ACF_TOO_LARGE" in msg:
            raise TooLarge(f"download larger than {max_bytes // 1024 ** 2} MB")
        raise DownloadError(msg.splitlines()[0] if msg else f"PowerShell exit code {p.returncode}")


def fetch_curl(url: str, dest: Path, progress=None, cancelled=None, max_bytes: int = MAX_DOWNLOAD) -> None:
    """macOS/Linux: download with curl (system certificate store, proxy environment variables)."""
    _check_url(url)
    if progress:
        progress("downloading via curl …")
    r = subprocess.run(["curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https", "--retry", "2",
                        "--max-filesize", str(max_bytes), "-A", "AdvancedCtrlF", "-o", str(dest), url],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode == 63:                                  # CURLE_FILESIZE_EXCEEDED
        raise TooLarge(f"download larger than {max_bytes // 1024 ** 2} MB")
    if r.returncode != 0:
        msg = r.stderr.strip()
        raise DownloadError(msg.splitlines()[-1] if msg else f"curl exit code {r.returncode}")


def scrub(text: str, url: str) -> str:
    """An error message without the secret parts of `url` (some errors repeat the full address)."""
    text = text.replace(url, redact(url))
    query = urllib.parse.urlsplit(url).query
    return text.replace(query, "…") if len(query) >= 4 else text


def methods() -> list:
    """Download methods to try, in order."""
    if os.name == "nt":
        return [("Python", fetch_python), ("PowerShell", fetch_powershell)]
    return [("Python", fetch_python)] + ([("curl", fetch_curl)] if shutil.which("curl") else [])


def download(url: str, dest: Path, progress=None, cancelled=None, max_bytes: int = MAX_DOWNLOAD) -> str:
    """Download with all available methods. Returns the file name to use for the download."""
    errors = []
    for name, fetch in methods():
        try:
            disposition = fetch(url, dest, progress, cancelled, max_bytes)
            log.info("Downloaded %s via %s (%d bytes)", redact(url), name, dest.stat().st_size)
            return filename_from(url, disposition if isinstance(disposition, str) else None)
        except Exception as e:
            errors.append(f"{name}: {scrub(str(e), url)}")
            log.warning("Download of %s via %s failed: %s", redact(url), name, scrub(str(e), url))
            try:
                dest.unlink()
            except OSError:
                pass
            if cancelled and cancelled():
                raise DownloadError("cancelled") from e
            if isinstance(e, TooLarge):          # every other method would get the same amount
                raise
    raise DownloadError("; ".join(errors))
