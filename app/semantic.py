"""Search by meaning with static multilingual embeddings.

Every word piece of a multilingual vocabulary (about 500 000 pieces, 100+ languages)
has a fixed vector. A text becomes the normalized mean of its piece vectors, so texts
about the same thing end up close to each other even if they share no word
("Kündigungsfrist" ~ "Vertrag mit drei Monaten Vorlauf beenden"). No neural network
runs at search time: encoding is a vocabulary lookup plus an average, fast enough for
every page of every document on an ordinary laptop.

The model pack is downloaded once (tokenizer.json + model.safetensors in the Model2Vec
format) and converted: the vectors are stored as int8 with one scale per row (a
quarter of the size), the word pieces as a plain JSON list. The original files are
deleted afterwards.

Tokenization is a Unigram (SentencePiece) segmentation: each word gets the split
into vocabulary pieces with the highest total log probability (Viterbi search).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import struct
import unicodedata
from pathlib import Path

from . import net

log = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:            # without numpy the program runs, only without search by meaning
    np = None

MODEL_FILES = ("tokenizer.json", "model.safetensors")
DEFAULT_URL = "https://huggingface.co/minishlab/potion-multilingual-128M/resolve/main/{file}"
DOWNLOAD_MB = 531
DISK_MB = 140
WINDOW = 48          # word pieces per window of a long text
STRIDE = 32          # windows overlap, so a sentence is never cut in half only
MAX_PIECE = 24       # longest piece considered during segmentation
UNKNOWN_COST = 20.0  # log-probability penalty for a character outside the vocabulary
_PUNCT = re.compile(r"([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")
_SPACE = "▁"


def available() -> bool:
    return np is not None


class Model:
    """Loaded model pack: segmentation + vector table."""

    def __init__(self, folder: Path) -> None:
        pieces = json.loads((folder / "pieces.json").read_text(encoding="utf-8"))
        self.ids = {p: i for i, (p, _) in enumerate(pieces)}
        self.score = [s for _, s in pieces]
        self.unk = int(json.loads((folder / "meta.json").read_text(encoding="utf-8"))["unk_id"])
        self.vectors = np.load(folder / "vectors.npy", mmap_mode="r")
        self.scales = np.load(folder / "scales.npy")
        self.dim = self.vectors.shape[1]
        self._cache: dict[str, list[int]] = {}

    # ------------------------------------------------ Segmentation
    @staticmethod
    def normalize(text: str) -> str:
        text = unicodedata.normalize("NFKC", text)
        return " ".join(_PUNCT.sub(r" \1 ", text).split())

    def _word(self, w: str) -> list[int]:
        hit = self._cache.get(w)
        if hit is not None:
            return hit
        n = len(w)
        best = [0.0] + [float("-inf")] * n
        back = [0] * (n + 1)
        piece = [0] * (n + 1)
        ids, score = self.ids, self.score
        for i in range(n):
            if best[i] == float("-inf"):
                continue
            found = False
            for j in range(i + 1, min(n, i + MAX_PIECE) + 1):
                pid = ids.get(w[i:j])
                if pid is not None:
                    found = True
                    s = best[i] + score[pid]
                    if s > best[j]:
                        best[j], back[j], piece[j] = s, i, pid
            if not found or best[i + 1] == float("-inf"):   # character outside the vocabulary
                s = best[i] - UNKNOWN_COST
                if s > best[i + 1]:
                    best[i + 1], back[i + 1], piece[i + 1] = s, i, self.unk
        out, j = [], n
        while j > 0:
            out.append(piece[j])
            j = back[j]
        out.reverse()
        if len(self._cache) < 400_000:
            self._cache[w] = out
        return out

    def pieces(self, text: str) -> list[int]:
        out: list[int] = []
        for w in self.normalize(text).split(" "):
            if w:
                out += self._word(_SPACE + w)
        return [i for i in out if i != self.unk and i > 1]

    # ------------------------------------------------ Vectors
    def _mean(self, ids) -> "np.ndarray | None":
        if not len(ids):
            return None
        ids = np.asarray(ids)
        v = (self.vectors[ids].astype(np.float32) * self.scales[ids, None]).mean(0)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else None

    def embed(self, text: str) -> "np.ndarray | None":
        """Unit vector of a short text (query, word), None if it has no known piece."""
        return self._mean(self.pieces(text))

    def embed_windows(self, text: str) -> list:
        """Unit vectors of overlapping windows of a longer text."""
        ids = self.pieces(text)
        if len(ids) <= WINDOW:
            v = self._mean(ids)
            return [v] if v is not None else []
        out = []
        for start in range(0, len(ids) - STRIDE, STRIDE):
            v = self._mean(ids[start:start + WINDOW])
            if v is not None:
                out.append(v)
        return out


# ---------------------------------------------------------------- Pack management
def installed(folder: Path) -> bool:
    return all((folder / f).is_file() for f in ("pieces.json", "vectors.npy", "scales.npy", "meta.json"))


def load(folder: Path) -> Model | None:
    if not available() or not installed(folder):
        return None
    try:
        return Model(folder)
    except Exception as e:
        log.warning("Meaning model in %s could not be loaded: %s", folder, e)
        return None


def remove(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)


def install(folder: Path, url_template: str = DEFAULT_URL, progress=lambda msg: None, cancelled=None) -> None:
    """Download and convert the model pack into `folder`."""
    if not available():
        raise RuntimeError("the Python package numpy is missing")
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / "download"
    raw.mkdir(exist_ok=True)
    for n, name in enumerate(MODEL_FILES, 1):
        dest = raw / name
        if dest.is_file() and dest.stat().st_size > 1000:
            continue
        progress(f"Downloading meaning model ({n}/{len(MODEL_FILES)}: {name}) …")
        part = raw / (name + ".part")
        net.download(url_template.format(file=name), part,
                     lambda m, name=name: progress(f"Downloading meaning model ({name}): {m}"), cancelled)
        part.replace(dest)
    progress("Converting meaning model …")
    convert(raw / "tokenizer.json", raw / "model.safetensors", folder)
    shutil.rmtree(raw, ignore_errors=True)
    progress("Meaning model ready.")


def _safetensors_matrix(path: Path):
    with open(path, "rb") as f:
        header_len = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(header_len))
    name, info = next((k, v) for k, v in header.items() if k != "__metadata__")
    if info["dtype"] not in ("F32", "F16"):
        raise RuntimeError(f"unsupported tensor type {info['dtype']}")
    dtype = np.float32 if info["dtype"] == "F32" else np.float16
    start, _ = info["data_offsets"]
    return np.memmap(path, dtype=dtype, mode="r", offset=8 + header_len + start, shape=tuple(info["shape"]))


def convert(tokenizer_json: Path, safetensors: Path, folder: Path) -> None:
    tok = json.loads(tokenizer_json.read_text(encoding="utf-8"))
    model = tok.get("model", {})
    if model.get("type") != "Unigram":
        raise RuntimeError("only Unigram tokenizers are supported")
    pieces = model["vocab"]
    matrix = _safetensors_matrix(safetensors)
    if matrix.shape[0] != len(pieces):
        raise RuntimeError("vocabulary and vector table do not match")
    rows, dim = matrix.shape
    q = np.lib.format.open_memmap(folder / "vectors.npy.part", mode="w+", dtype=np.int8, shape=(rows, dim))
    scales = np.empty(rows, dtype=np.float32)
    for a in range(0, rows, 50_000):
        block = np.asarray(matrix[a:a + 50_000], dtype=np.float32)
        s = np.abs(block).max(1) / 127.0
        s[s == 0] = 1.0
        q[a:a + len(block)] = np.round(block / s[:, None]).astype(np.int8)
        scales[a:a + len(block)] = s
    q.flush()
    del q
    (folder / "vectors.npy.part").replace(folder / "vectors.npy")
    np.save(folder / "scales.npy", scales)
    (folder / "pieces.json").write_text(json.dumps(pieces, ensure_ascii=False), encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({"unk_id": model.get("unk_id", 1), "rows": rows, "dim": dim}),
                                      encoding="utf-8")
