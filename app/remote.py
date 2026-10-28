"""Add documents from the web: GitHub repositories (whole repo, branch, sub folder or a
single file) and direct HTTPS links to files. Only public content - no credentials."""
from __future__ import annotations

import re
import tempfile
import urllib.parse
from pathlib import Path

from . import net
from .extractors import Context, ExtractError

_GITHUB = re.compile(
    r"^https://(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?"
    r"(?:/(?P<mode>tree|blob)/(?P<ref>[^/?#]+)(?:/(?P<path>[^?#]*))?)?/?(?:[?#].*)?$")


def parse(url: str) -> dict:
    """Classify a URL: {"kind": "github", owner, repo, ref, path, mode} or {"kind": "file", url}."""
    url = url.strip()
    if url.lower().startswith("http://"):
        raise ExtractError("Only HTTPS links are supported")
    if not url.lower().startswith("https://"):
        raise ExtractError("Not a web link (https://…)")
    m = _GITHUB.match(url)
    if m and m["repo"] not in ("orgs", "settings", "topics", "search", "marketplace", "features"):
        return {"kind": "github", "owner": m["owner"], "repo": m["repo"], "ref": m["ref"] or "",
                "path": urllib.parse.unquote(m["path"] or "").strip("/"), "mode": m["mode"] or "", "url": url}
    return {"kind": "file", "url": url}


def fetch(url: str, store, ctx: Context) -> list[dict]:
    """Download `url` and add its content to `store`. Returns the new documents."""
    spec = parse(url)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        progress = lambda m: ctx.progress(f"Downloading: {m}")   # noqa: E731
        if spec["kind"] == "github":
            owner, repo, ref, path = spec["owner"], spec["repo"], spec["ref"], spec["path"]
            label = f"{owner}/{repo}" + (f"@{ref}" if ref else "")
            extra = {"source": url}
            if spec["mode"] == "blob":   # a single file inside a repository
                raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{urllib.parse.quote(path)}"
                dest = tmp / Path(path).name
                net.download(raw, dest, progress, ctx.cancelled)
                folder = str(Path(path).parent).replace("\\", "/")
                return store.add_path(dest, ctx, f"{label} › " + ("" if folder == "." else f"{folder}/"), extra=extra)
            archive = tmp / "repo.zip"
            ctx.progress(f"Downloading {label} …")
            net.download(f"https://codeload.github.com/{owner}/{repo}/zip/{ref or 'HEAD'}", archive,
                         progress, ctx.cancelled)
            return store.add_repository_archive(archive, label, ctx, subpath=path, extra=extra)
        # any other HTTPS link: a single file (archives are unpacked as usual)
        host = urllib.parse.urlsplit(url).hostname or "web"
        part = tmp / "download.part"
        name = net.download(url, part, progress, ctx.cancelled)
        from .store import safe_filename
        dest = tmp / safe_filename(name)
        part.rename(dest)
        return store.add_path(dest, ctx, f"{host} › ", extra={"source": url})
