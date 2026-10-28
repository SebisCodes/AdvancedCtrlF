"""Shared building blocks for all extractors.

An extractor returns a list of chunks:
    {"loc": {"kind": "page", "page": 3, ...}, "text": "..."}
`loc` describes where the text was found (page, slide, line, sheet/cell, part such as "Notes").
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable

MAX_CHUNK = 900  # characters per chunk (paragraphs are merged up to this size)
MAX_XML_BYTES = 200 * 1024 * 1024


class ExtractError(Exception):
    pass


class UnsafeXML(ExtractError):
    pass


@dataclass
class Context:
    config: dict
    ocr: object | None = None                      # app.ocr.OcrEngine
    progress: Callable[[str], None] = lambda msg: None
    cancelled: Callable[[], bool] = lambda: False


@dataclass
class Result:
    chunks: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


_WS = re.compile(r"[ \t\r\f\v ]+")


def clean(text: str) -> str:
    text = text.replace("\x00", "")
    lines = [_WS.sub(" ", ln).strip() for ln in text.split("\n")]
    out, blank = [], 0
    for ln in lines:
        if ln:
            out.append(ln)
            blank = 0
        elif out and blank == 0:
            out.append("")
            blank = 1
    return "\n".join(out).strip()


def safe_xml(data: bytes) -> ET.Element:
    """Parse XML without entity definitions (protection against XXE / "billion laughs")."""
    if len(data) > MAX_XML_BYTES:
        raise ExtractError("XML part too large")
    # expat/ElementTree never resolve external entities; custom entity
    # definitions (expansion bombs) are always rejected.
    if re.search(rb"<!entity", data, re.IGNORECASE):
        raise UnsafeXML("XML with ENTITY definitions is not read for security reasons")
    return ET.fromstring(data)


class Chunker:
    """Collects text segments and merges consecutive segments with the same
    location (page/slide/part) into chunks of up to MAX_CHUNK characters."""

    def __init__(self, max_len: int = MAX_CHUNK) -> None:
        self.chunks: list[dict] = []
        self.max_len = max_len
        self._buf: list[str] = []
        self._loc: dict | None = None
        self._key = None

    def add(self, text: str, loc: dict, key=None) -> None:
        text = clean(text)
        if not text:
            return
        key = key if key is not None else tuple(sorted((k, v) for k, v in loc.items() if k not in ("line", "para")))
        size = sum(len(t) + 1 for t in self._buf)
        if self._buf and (key != self._key or size + len(text) > self.max_len):
            self.flush()
        if not self._buf:
            self._loc, self._key = dict(loc), key
        self._buf.append(text)
        if len(text) > self.max_len:
            self.flush()

    def flush(self) -> None:
        if self._buf:
            self.chunks.append({"loc": self._loc, "text": "\n".join(self._buf)})
        self._buf, self._loc, self._key = [], None, None

    def result(self) -> list[dict]:
        self.flush()
        return self.chunks


def add_lines(chunker: Chunker, text: str, base: dict, start_line: int = 1, lines_per_chunk: int = 12) -> None:
    """Split text line by line into chunks (with their start line)."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        block = lines[i:i + lines_per_chunk]
        # skip blank lines at the start of a block so that 'line' is correct
        while block and not block[0].strip():
            i += 1
            block = lines[i:i + lines_per_chunk]
        if not block:
            break
        body = "\n".join(ln.rstrip() for ln in block).strip("\n")
        if body.strip():
            loc = dict(base, kind=base.get("kind", "line"), line=start_line + i)
            chunker.chunks.append({"loc": loc, "text": body[:20000]})
        i += lines_per_chunk


def decode_bytes(data: bytes) -> str:
    """Detect the text encoding: BOM, UTF-8, UTF-16 (heuristic), otherwise cp1252."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    sample = data[:4000]
    if sample and sample.count(b"\x00") > len(sample) // 4:
        enc = "utf-16-le" if sample[1:2] == b"\x00" else "utf-16-be"
        return data.decode(enc, "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def looks_like_text(data: bytes) -> bool:
    sample = data[:8192]
    if not sample:
        return True
    if b"\x00" in sample and sample.count(b"\x00") < len(sample) // 4:
        return False
    try:
        txt = decode_bytes(sample)
    except Exception:
        return False
    bad = sum(1 for c in txt if ord(c) < 32 and c not in "\r\n\t\f")
    return bad / max(1, len(txt)) < 0.02 and txt.count("�") / max(1, len(txt)) < 0.05


_STR_ASCII = re.compile(rb"[\x20-\x7e\xa0-\xff\t]{6,}")
_STR_U16 = re.compile(rb"(?:[\x20-\x7e\xa0-\xff]\x00){6,}")


def binary_strings(data: bytes) -> str:
    """Fallback: extract readable strings from binary formats (e.g. legacy .doc/.ppt)."""
    parts = [m.decode("utf-16-le", "ignore") for m in _STR_U16.findall(data)]
    if sum(len(p) for p in parts) < 200:
        parts += [m.decode("cp1252", "ignore") for m in _STR_ASCII.findall(data)]
    return "\n".join(p.strip() for p in parts if _readable(p.strip()))


_WORDLIKE = re.compile(r"[^\W\d_]{2,25}")
_VOWELS = set("aeiouyäöüàáâèéêìíîòóôùúûAEIOUYÄÖÜ")


def _readable(s: str) -> bool:
    """Heuristic: does a string from binary data look like human-readable text?
    Rejects byte runs such as "ABCDEFGHIJ…" or "ÀÁÂÃÄÅ…" and identifiers without words."""
    if len(s) < 6 or max((len(t) for t in s.split()), default=0) > 30:
        return False
    words = _WORDLIKE.findall(s)
    if sum(len(w) for w in words) < 0.6 * len(s):
        return False
    real = [w for w in words if len(w) >= 3 and any(c in _VOWELS for c in w)
            and not any(w[i] == w[i + 1] == w[i + 2] for i in range(len(w) - 2))]
    return len(real) >= (2 if len(s) > 15 else 1)
