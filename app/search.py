"""Full-text search: inverted index with BM25, word forms per language, phrases, proximity,
prefix, infix, typo tolerance - combined with search by meaning.

How a search works
  1. Parse the query: words, "phrases", -exclusions, prefix*, type:pdf, file:name, folder:name
  2. Normalize every word: case, accents, umlauts; then reduce it to its stem with the
     Snowball stemmer of each language present in the index ("Verträge" -> "vertrag",
     "running" -> "run"). Scripts without spaces (Chinese, Japanese, Thai, Korean) are
     indexed as overlapping character pairs.
  3. Expansions per word: exact (1.0) > prefix (0.85/0.7) > infix (0.6) > typo (0.55/0.4)
  4. BM25 per text chunk (page/paragraph/slide/line), best expansion per query word
  5. Weighting by coverage of all query words, word proximity and phrases in query order
  6. Meaning: with a meaning model installed, every chunk also has vectors for
     overlapping windows of its text. The query vector is compared with all of them;
     chunks that are close in meaning are added even if they share no word with the
     query, and lexical hits that also match in meaning move up.
"""
from __future__ import annotations

import heapq
import math
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import lang as langmod

try:
    import snowballstemmer
except ImportError:            # without the package words are only lightly stemmed
    snowballstemmer = None
try:
    import numpy as np
except ImportError:
    np = None

TOKEN = re.compile(rf"(?:[^\W_]|[{langmod.MARKS}])+", re.UNICODE)
# Thai, kana, Han and Hangul: indexed as overlapping character pairs
_DENSE = re.compile("[฀-๿぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]+")
_STRIP = re.compile("[̀-֑ͯ-ׇً-ٰٟ]")   # accents, Hebrew/Arabic vowel marks
K1, B = 1.2, 0.75
# share of the meaning score in the combined score: low when some chunk contains all
# query words, high when none does (the searcher evidently does not know the words)
MEANING_WEIGHT_MIN = 0.3
MEANING_WEIGHT_MAX = 0.9
MEANING_FLOOR = 0.2       # chunks below this similarity are never added by meaning alone ...
MEANING_RELATIVE = 0.5    # ... nor below typical + this share of the distance to the best chunk
MEANING_CANDIDATES = 300  # chunks added per query by meaning
MEANING_MIN_TOKENS = 12   # shorter chunks (headings, single lines) count less in meaning
WORD_SIMILAR = 0.25       # meaning hits: words at least this close to the query can be highlighted,
WORD_MARK_MAX = 3         # at most this many, the closest ones

# ---------------------------------------------------------------- Normalization
_DIGRAPHS = (("ae", "a"), ("oe", "o"), ("ue", "u"))
_SUFFIXES = (
    ("ungen", "ung"), ("heiten", "heit"), ("keiten", "keit"), ("ies", "y"), ("ing", ""), ("ern", ""),
    ("ed", ""), ("en", ""), ("er", ""), ("es", ""), ("em", ""), ("e", ""), ("s", ""), ("n", ""),
)


def fold(word: str) -> str:
    """Lower-case, ß→ss, ä/ae→a, ö/oe→o, ü/ue→u, strip accents (Indic vowel signs are kept)."""
    w = word.casefold()
    for a, b in _DIGRAPHS:
        w = w.replace(a, b)
    w = _STRIP.sub("", unicodedata.normalize("NFKD", w))
    return unicodedata.normalize("NFC", w)


def stem(w: str) -> str:
    """Light suffix stripping for text of unknown language."""
    if len(w) <= 3 or w.isdigit():
        return w
    for suf, rep in _SUFFIXES:
        if w.endswith(suf):
            base = w[: -len(suf)]
            if len(base) + len(rep) >= 3 and (len(suf) > 1 or len(base) >= 4):
                return base + rep
            break
    return w


_STEMMERS: dict[str, object] = {}
_NORM: dict[tuple[str, str | None], str] = {}
_STEMMING_OFF: set[str] = set()


def set_stemming_off(isos) -> None:
    """Languages whose words are indexed unstemmed (takes effect for newly built indexes)."""
    _STEMMING_OFF.clear()
    _STEMMING_OFF.update(isos)
    _STEMMERS.clear()
    _NORM.clear()


def has_stemmer(lang: str) -> bool:
    l = langmod.BY_ISO.get(lang)
    return bool(l and l.stemmer and snowballstemmer)


def _stemmer(lang: str | None):
    if lang not in _STEMMERS:
        algo = langmod.BY_ISO[lang].stemmer if lang in langmod.BY_ISO and lang not in _STEMMING_OFF else ""
        _STEMMERS[lang] = snowballstemmer.stemmer(algo) if algo and snowballstemmer else None
    return _STEMMERS[lang]


def norm(word: str, lang: str | None = None) -> str:
    """Index form of a word in language `lang` (None: unknown language)."""
    key = (word, lang)
    hit = _NORM.get(key)
    if hit is not None:
        return hit
    w = word.casefold()
    st = _stemmer(lang) if lang else None
    if w.isdigit() or len(w) <= 2 or _DENSE.match(w):
        out = fold(w)
    elif st is not None:
        out = fold(st.stemWord(w))
    elif lang in langmod.BY_ISO:          # known language without stemmer: no guessing
        out = fold(w)
    else:
        out = stem(fold(w))
    if len(_NORM) > 1_000_000:
        _NORM.clear()
    _NORM[key] = out
    return out


def _pieces(text: str):
    """(start, end) of every token: words, and character pairs inside dense scripts."""
    for m in TOKEN.finditer(text):
        s, word = m.start(), m.group()
        pos = 0
        for d in _DENSE.finditer(word):
            if d.start() > pos:
                yield s + pos, s + d.start()
            a, b = d.start(), d.end()
            if b - a == 1:
                yield s + a, s + b
            for i in range(a, b - 1):
                yield s + i, s + i + 2
            pos = b
        if pos < len(word):
            yield s + pos, s + len(word)


def tokenize(text: str, lang: str | None = None) -> list[str]:
    return [norm(text[a:b], lang) for a, b in _pieces(text)]


def token_spans(text: str, lang: str | None = None):
    for a, b in _pieces(text):
        yield a, b, norm(text[a:b], lang)


def levenshtein(a: str, b: str, maxd: int) -> int:
    if abs(len(a) - len(b)) > maxd:
        return maxd + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:  # transposed letters
                v = min(v, prev2[j - 2] + 1)
            cur.append(v)
            best = min(best, v)
        if best > maxd:
            return maxd + 1
        prev2, prev = prev, cur
    return prev[-1]


# ---------------------------------------------------------------- Folders
_FOLDER_SEP = re.compile(r"/| › ")


def folder_of(doc: dict) -> tuple[str, ...]:
    """Folder of a document as shown in the program, as path parts:
    "Contracts/2024/rent.pdf" -> ("Contracts", "2024"), "acme/shop › docs/a.md" -> ("acme", "shop", "docs")."""
    parts = [p for p in _FOLDER_SEP.split(doc.get("display_name") or doc.get("name", "")) if p]
    return tuple(parts[:-1])


def in_folder(doc: dict, folder: tuple[str, ...]) -> bool:
    return folder_of(doc)[:len(folder)] == tuple(folder)


# ---------------------------------------------------------------- Query
@dataclass(frozen=True)
class Term:
    base: str                 # folded word without stemming (prefix, infix, typos)
    forms: tuple[str, ...]    # index forms in every candidate language
    prefix: bool = False


@dataclass
class Query:
    terms: list[Term] = field(default_factory=list)
    phrases: list[list[Term]] = field(default_factory=list)
    exclude: list[list[Term]] = field(default_factory=list)
    kinds: set[str] = field(default_factory=set)
    files: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    text: str = ""            # free text of the query, for the meaning comparison
    raw: str = ""

    @property
    def empty(self) -> bool:
        return not self.terms and not self.phrases


_QTOK = re.compile(r'(-?)(?:(\w+):)?(?:"([^"]*)"?|(\S+))')


def _terms(val: str, langs) -> list[Term]:
    out = []
    for a, b in _pieces(val):
        w = val[a:b]
        forms = tuple(dict.fromkeys([norm(w, None)] + [norm(w, l) for l in langs]))
        out.append(Term(fold(w), forms))
    return out


def parse_query(text: str, langs=()) -> Query:
    q = Query(raw=text)
    free = []
    for neg, key, phrase, word in _QTOK.findall(text):
        key = key.lower()
        val = phrase if phrase else word
        if key in ("typ", "type", "ext") and val:
            q.kinds.update(v.strip(".").lower() for v in val.split(","))
            continue
        if key in ("datei", "file", "name") and val:
            q.files.append(fold(val))
            continue
        if key in ("folder", "ordner", "in", "dir") and val:
            q.folders.append(fold(val))
            continue
        if key:  # unknown key -> treat as plain text
            val = f"{key}:{val}"
        toks = _terms(val, langs)
        if not toks:
            continue
        if neg:
            q.exclude.append(toks)
            continue
        free.append(val.rstrip("*"))
        if phrase and len(toks) > 1:
            q.phrases.append(toks)
            q.terms += toks
        else:
            prefix = val.endswith("*")
            q.terms += [Term(t.base, t.forms, prefix and i == len(toks) - 1) for i, t in enumerate(toks)]
    # drop duplicate terms, keep order
    seen, terms = set(), []
    for t in q.terms:
        if t.forms not in seen:
            seen.add(t.forms)
            terms.append(t)
    q.terms = terms
    q.text = " ".join(free)
    return q


# ---------------------------------------------------------------- Result
@dataclass
class Hit:
    doc_id: str
    chunk: int
    score: float
    relevance: int = 0           # 0..100 relative to the best hit
    coverage: float = 0.0
    match_terms: frozenset = frozenset()
    meaning: float = 0.0         # similarity in meaning to the query (0 = not compared)
    lang: str | None = None      # language of the chunk


# ---------------------------------------------------------------- Index
class SearchIndex:
    def __init__(self, model=None) -> None:
        self.docs: dict[str, dict] = {}
        self.refs: list[tuple[str, int] | None] = []     # cid -> (doc_id, chunk index)
        self.lens: list[int] = []
        self.clang: list[str | None] = []                # cid -> language of the chunk
        self.post: dict[str, dict[int, list[int]]] = defaultdict(dict)
        self.doc_cids: dict[str, list[int]] = {}
        self.langs: Counter = Counter()                  # language -> number of documents
        self.total_len = 0
        self.alive = 0
        self._vocab: list[str] | None = None
        self._joined: tuple[str, list[int]] | None = None
        self._tri: dict[str, list[str]] | None = None
        self.model = model
        self._vec: dict[int, object] = {}                # cid -> window vectors (float16)
        self._matrix = None                              # (stacked vectors, owner cid per row)

    # ------------------------------------------------ Maintenance
    @staticmethod
    def doc_language(doc: dict) -> str:
        """Language of a document, detected once and stored in the document."""
        if not doc.get("lang"):
            sample, n = [], 0
            for ch in doc.get("chunks") or []:
                sample.append(ch.get("text", "")[:2000])
                n += len(sample[-1])
                if n > 6000:
                    break
            doc["lang"] = langmod.detect("\n".join(sample))[0]
        return doc["lang"]

    def add(self, doc: dict) -> None:
        if doc.get("id") in self.docs:
            self.remove(doc["id"])
        did = doc["id"]
        self.docs[did] = doc
        dlang = self.doc_language(doc)
        dlang = None if dlang == "und" else dlang
        self.langs[dlang] += 1
        cids = []
        name_tokens = tokenize(doc.get("display_name") or doc.get("name", ""), dlang)
        for ci, ch in enumerate(doc.get("chunks") or []):
            cid = len(self.refs)
            self.refs.append((did, ci))
            text = ch.get("text", "")
            clang = dlang
            if len(text) >= 400:                     # long passages may be in another language
                code, conf = langmod.detect(text[:3000])
                if code != "und" and conf >= 0.5:
                    clang = code
            self.clang.append(clang)
            toks = tokenize(text, clang)
            title = ch.get("loc", {}).get("title")
            for pos, t in enumerate(toks):
                self.post[t].setdefault(cid, []).append(pos)
            # slide title and file name count weakly (far-away position -> never part of a phrase)
            for extra in (tokenize(title, clang) if title else []) + (name_tokens if ci == 0 else []):
                self.post[extra].setdefault(cid, []).append(10 ** 6 + len(toks))
            self.lens.append(len(toks))
            self.total_len += len(toks)
            cids.append(cid)
            if self.model is not None:
                self._embed(cid, text, title)
        self.doc_cids[did] = cids
        self.alive += len(cids)
        self._vocab = self._joined = self._tri = None

    def remove(self, doc_id: str) -> None:
        doc = self.docs.pop(doc_id, None)
        if doc is not None:
            lang = None if doc.get("lang") in (None, "", "und") else doc["lang"]
            self.langs[lang] -= 1
            if self.langs[lang] <= 0:
                del self.langs[lang]
        for cid in self.doc_cids.pop(doc_id, []):
            self.refs[cid] = None
            self.total_len -= self.lens[cid]
            self.alive -= 1
            if self._vec.pop(cid, None) is not None:
                self._matrix = None
        dead = len(self.refs) - self.alive
        if dead > 5000 and dead > self.alive:
            self.rebuild()

    def rebuild(self) -> None:
        docs = list(self.docs.values())
        self.__init__(self.model)
        for d in docs:
            self.add(d)

    # ------------------------------------------------ Meaning
    def _embed(self, cid: int, text: str, title: str | None) -> None:
        vecs = self.model.embed_windows(f"{title}\n{text}" if title else text)
        if vecs:
            self._vec[cid] = np.stack(vecs).astype(np.float16)
            self._matrix = None

    def compute_vectors(self, model, progress=None, cancelled=None) -> dict:
        """Window vectors of all current chunks. Only reads the index, so it can run in a
        background thread; the result is attached with attach_model()."""
        out = {}
        live = [(cid, ref) for cid, ref in enumerate(list(self.refs)) if ref is not None]
        for k, (cid, (did, ci)) in enumerate(live):
            if cancelled and cancelled():
                break
            doc = self.docs.get(did)
            if doc is None:
                continue
            ch = doc["chunks"][ci]
            title = ch.get("loc", {}).get("title")
            vecs = model.embed_windows(f"{title}\n{ch.get('text', '')}" if title else ch.get("text", ""))
            if vecs:
                out[cid] = np.stack(vecs).astype(np.float16)
            if progress and k % 200 == 0:
                progress(k, len(live))
        return out

    def attach_model(self, model, vectors: dict | None = None) -> None:
        """Use a meaning model (None switches search by meaning off). Chunks added while
        `vectors` was computed get their vectors now."""
        self.model = model
        self._vec, self._matrix = {}, None
        if model is None:
            return
        self._vec = {c: v for c, v in (vectors or {}).items() if c < len(self.refs) and self.refs[c] is not None}
        for cid, ref in enumerate(self.refs):
            if ref is not None and cid not in self._vec:
                ch = self.docs[ref[0]]["chunks"][ref[1]]
                self._embed(cid, ch.get("text", ""), ch.get("loc", {}).get("title"))

    @property
    def meaning_ready(self) -> bool:
        return self.model is not None and bool(self._vec)

    def _vectors(self):
        """(window vectors, chunk ids, first row per chunk, length factor per chunk) -
        rebuilt after changes."""
        if self._matrix is None and self._vec:
            cids = sorted(self._vec)
            mats = [self._vec[c] for c in cids]
            starts = np.cumsum([0] + [len(m) for m in mats[:-1]])
            lens = np.asarray([self.lens[c] for c in cids], dtype=np.float32)
            factor = np.sqrt(np.minimum(1.0, lens / MEANING_MIN_TOKENS))
            self._matrix = (np.concatenate(mats), np.asarray(cids, dtype=np.int64), starts, factor)
        return self._matrix

    def meaning_scores(self, text: str):
        """Similarity in meaning per chunk (best window) for the query text.
        Returns (chunk ids, similarities, query vector) - empty arrays without a model."""
        empty = (np.zeros(0, np.int64), np.zeros(0, np.float32), None) if np is not None else ((), (), None)
        if self.model is None or not text.strip():
            return empty
        qv = self.model.embed(text)
        m = self._vectors()
        if qv is None or m is None:
            return empty
        mat, cids, starts, factor = m
        best = np.maximum.reduceat(mat @ qv.astype(np.float16), starts).astype(np.float32) * factor
        return cids, best, qv

    # ------------------------------------------------ Helpers
    @property
    def vocab(self) -> list[str]:
        if self._vocab is None:
            self._vocab = sorted(self.post)
        return self._vocab

    def _trigrams(self) -> dict[str, list[str]]:
        if self._tri is None:
            tri = defaultdict(list)
            for t in self.vocab:
                if len(t) >= 3 and not t.isdigit():
                    p = f"${t}$"
                    for g in {p[i:i + 3] for i in range(len(p) - 2)}:
                        tri[g].append(t)
            self._tri = tri
        return self._tri

    def _containing(self, part: str, limit: int = 3000) -> list[str]:
        """Vocabulary words that contain `part` but do not start with it (substring search
        on the joined vocabulary, much faster than testing every word)."""
        if self._joined is None:
            offsets, pos = [], 0
            for t in self.vocab:
                offsets.append(pos)
                pos += len(t) + 1
            self._joined = ("\n".join(self.vocab), offsets)
        joined, offsets = self._joined
        vocab, out, i = self.vocab, [], 0
        while len(out) < limit:
            j = joined.find(part, i)
            if j < 0:
                break
            k = bisect_right(offsets, j) - 1
            if j > offsets[k] and j + len(part) <= offsets[k] + len(vocab[k]):
                out.append(vocab[k])
            i = offsets[k + 1] if k + 1 < len(offsets) else len(joined)
        return out

    def df(self, term: str) -> int:
        return len(self.post.get(term, ()))

    def folders(self) -> dict[tuple[str, ...], int]:
        """Every folder (and parent folder) of the documents with its number of documents."""
        out: dict[tuple[str, ...], int] = defaultdict(int)
        for doc in self.docs.values():
            parts = folder_of(doc)
            for k in range(1, len(parts) + 1):
                out[parts[:k]] += 1
        return dict(out)

    def query_langs(self) -> list[str]:
        return [l for l in self.langs if l]

    def expand(self, term: Term, is_last: bool) -> dict[str, float]:
        exp: dict[str, float] = {}
        for f in term.forms:
            if f in self.post:
                exp[f] = 1.0
        base = term.base
        if base.isdigit():
            return exp
        if base in self.post:
            exp.setdefault(base, 0.95)
        if _DENSE.match(base):                       # character pairs need no further expansion
            return exp
        vocab = self.vocab
        # prefix: explicit with * or automatic for the last word (search as you type)
        if (term.prefix or is_last) and len(base) >= 3:
            w = 0.85 if term.prefix else 0.7
            i = bisect_left(vocab, base)
            cands = []
            while i < len(vocab) and vocab[i].startswith(base) and len(cands) < 2000:
                if vocab[i] not in exp:
                    cands.append(vocab[i])
                i += 1
            for t in sorted(cands, key=self.df, reverse=True)[:150]:
                exp.setdefault(t, w)
        # infix (compounds: "vertrag" in "arbeitsvertrag")
        stems = {f for f in term.forms if len(f) >= 4} | ({base} if len(base) >= 4 else set())
        cands = set()
        for st in stems:
            cands.update(self._containing(st))
        for t in sorted(cands, key=self.df, reverse=True)[:150]:
            exp.setdefault(t, 0.6)
        # typos, if the word is rare or missing
        if len(base) >= 4 and max(self.df(f) for f in term.forms + (base,)) < 3:
            maxd = 1 if len(base) <= 6 else 2
            p = f"${base}$"
            grams = {p[i:i + 3] for i in range(len(p) - 2)}
            counts: dict[str, int] = defaultdict(int)
            tri = self._trigrams()
            for g in grams:
                for t in tri.get(g, ()):
                    counts[t] += 1
            need = max(1, len(grams) - 3 * maxd)
            fuzzy = []
            for t, c in counts.items():
                if c >= need and t != base and abs(len(t) - len(base)) <= maxd:
                    d = levenshtein(base, t, maxd)
                    if d <= maxd:
                        fuzzy.append((d, -self.df(t), t))
            for d, _, t in sorted(fuzzy)[:30]:
                exp.setdefault(t, 0.55 if d == 1 else 0.4)
        return exp

    # ------------------------------------------------ Search
    def search(self, text: str, kinds: set[str] | None = None, limit: int = 300,
               one_per_doc: bool = False, meaning: bool = True,
               folder: tuple[str, ...] | None = None) -> tuple[list[Hit], Query]:
        """`folder`: only documents in this folder (path parts, see folder_of) and its subfolders."""
        q = parse_query(text, self.query_langs())
        if kinds:
            q.kinds |= kinds
        if q.empty or not self.alive:
            return [], q
        n = max(1, self.alive)
        avg = max(1.0, self.total_len / n)
        term_scores: list[dict[int, float]] = []
        term_pos: list[dict[int, list[int]]] = []
        all_match: set[str] = set()
        open_end = not q.raw.rstrip().endswith((" ", '"')) and not q.raw.endswith(" ")
        for i, term in enumerate(q.terms):
            exp = self.expand(term, i == len(q.terms) - 1 and open_end)
            all_match.update(exp)
            scores: dict[int, float] = {}
            positions: dict[int, list[int]] = {}
            for t, w in exp.items():
                plist = self.post.get(t, {})
                df = len(plist)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                for cid, pos in plist.items():
                    if self.refs[cid] is None:
                        continue
                    tf = len(pos)
                    s = w * idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * self.lens[cid] / avg))
                    if s > scores.get(cid, 0.0):
                        scores[cid] = s
                        positions[cid] = pos
            term_scores.append(scores)
            term_pos.append(positions)

        cands: set[int] = set()
        for s in term_scores:
            cands.update(s)
        sem: dict[int, float] = {}
        typical_top = None
        if meaning:
            sem_cids, sims, qv = self.meaning_scores(q.text)
        else:
            sem_cids, sims, qv = (), (), None
        if len(sims):
            typical, best_sim = float(np.median(sims)), float(sims.max())
            floor = max(MEANING_FLOOR, typical + MEANING_RELATIVE * (best_sim - typical))
            pick = sims
            if folder or q.folders or q.kinds or q.files:    # candidates only from the documents in scope
                allowed = [c for did, doc in self.docs.items() if self._in_scope(doc, q, folder)
                           for c in self.doc_cids.get(did, ())]
                pick = np.where(np.isin(sem_cids, allowed), sims, -np.inf)
            k = min(MEANING_CANDIDATES, len(sims))
            top_rows = np.argpartition(-pick, k - 1)[:k]
            for r in top_rows.tolist():
                c = int(sem_cids[r])
                if pick[r] >= floor and self.refs[c] is not None:
                    cands.add(c)
            lookup = [c for c in cands]
            if lookup:
                rows = np.searchsorted(sem_cids, lookup)
                rows = np.minimum(rows, len(sem_cids) - 1)
                for c, r in zip(lookup, rows.tolist()):
                    if int(sem_cids[r]) == c:
                        sem[c] = float(sims[r])
            typical_top = (typical, best_sim)
        # exclusions
        for ex in q.exclude:
            if len(ex) == 1:
                for f in ex[0].forms:
                    cands -= set(self.post.get(f, {}))
            else:
                cands = {c for c in cands if not self._has_phrase(c, ex)}
        # required phrases
        for ph in q.phrases:
            cands = {c for c in cands if self._has_phrase(c, ph)}
        nterms = len(q.terms)
        results: list[Hit] = []
        for cid in cands:
            did, ci = self.refs[cid]
            doc = self.docs[did]
            if not self._in_scope(doc, q, folder):
                continue
            matched = [i for i in range(nterms) if cid in term_scores[i]]
            score = sum(term_scores[i][cid] for i in matched)
            cov = len(matched) / nterms
            score *= 0.25 + 0.75 * cov ** 2
            if len(matched) > 1:
                window, ordered = self._proximity([term_pos[i][cid] for i in matched])
                score *= 1 + 0.8 * (len(matched) - 1) / max(1, window - len(matched) + 1)
                if ordered:
                    score *= 1.4
            if q.phrases:
                score *= 1.3
            results.append(Hit(did, ci, score, coverage=cov, meaning=sem.get(cid, 0.0), lang=self.clang[cid]))
        self._combine(results, typical_top)
        if one_per_doc:
            best: dict[str, Hit] = {}
            for h in results:
                if h.doc_id not in best or h.score > best[h.doc_id].score:
                    best[h.doc_id] = h
            results = list(best.values())
        top = heapq.nlargest(limit, results, key=lambda h: (h.score, -h.chunk))
        if top:
            mx = top[0].score or 1.0
            mt = frozenset(all_match)
            for h in top:
                h.relevance = max(1, round(100 * h.score / mx))
                h.match_terms = mt
        self._qv = qv
        return top, q

    @staticmethod
    def _in_scope(doc: dict, q: Query, folder: tuple[str, ...] | None) -> bool:
        if q.kinds and doc.get("kind", "") not in q.kinds:
            return False
        if q.files and not all(f in fold(doc.get("display_name") or doc.get("name", "")) for f in q.files):
            return False
        if folder and not in_folder(doc, folder):
            return False
        return not q.folders or all(f in fold("/".join(folder_of(doc))) for f in q.folders)

    @staticmethod
    def _combine(results: list[Hit], typical_top: tuple[float, float] | None) -> None:
        """Combined score: lexical score relative to the best lexical hit plus the
        similarity in meaning relative to the best and a typical (median) similarity.
        The weight of meaning grows the fewer query words the best-covered chunk contains."""
        lex_max = max((h.score for h in results), default=0.0) or 1.0
        if typical_top is None:
            for h in results:
                h.score /= lex_max
            return
        typical, top = typical_top
        span = max(1e-6, top - typical)
        cover = max((h.coverage for h in results), default=0.0)
        w = MEANING_WEIGHT_MIN + (MEANING_WEIGHT_MAX - MEANING_WEIGHT_MIN) * (1 - cover)
        for h in results:
            m = min(1.0, max(0.0, (h.meaning - typical) / span)) if h.meaning else 0.0
            h.score = (1 - w) * h.score / lex_max + w * m

    def _has_phrase(self, cid: int, phrase: list[Term]) -> bool:
        lists = []
        for t in phrase:
            pos = set()
            for f in t.forms + (t.base,):
                pos.update(self.post.get(f, {}).get(cid, ()))
            if not pos:
                return False
            lists.append(pos)
        rest = lists[1:]
        return any(all(p + k + 1 in s for k, s in enumerate(rest)) for p in lists[0])

    @staticmethod
    def _proximity(pos_lists: list[list[int]]) -> tuple[int, bool]:
        """Smallest window containing all words; ordered = adjacent and in query order."""
        events = sorted((p, i) for i, pl in enumerate(pos_lists) for p in pl[:200] if p < 10 ** 6)
        k = len(pos_lists)
        if not events:
            return 10 ** 6, False
        count: dict[int, int] = defaultdict(int)
        have = 0
        best = 10 ** 6
        best_ordered = False
        left = 0
        for right, (p, i) in enumerate(events):
            if count[i] == 0:
                have += 1
            count[i] += 1
            while have == k:
                lp, li = events[left]
                width = p - lp + 1
                if width < best:
                    best = width
                    seq = [ti for _, ti in events[left:right + 1]]
                    best_ordered = width == k and seq == sorted(seq)
                count[li] -= 1
                if count[li] == 0:
                    have -= 1
                left += 1
        return best, best_ordered

    # ------------------------------------------------ Presentation
    def chunk_of(self, hit: Hit) -> dict:
        return self.docs[hit.doc_id]["chunks"][hit.chunk]

    def highlights(self, text: str, match_terms, lang: str | None = None) -> list[tuple[int, int]]:
        return [(s, e) for s, e, t in token_spans(text, lang) if t in match_terms]

    _SENTENCE = re.compile(r"[^.!?。！？\n]+[.!?。！？]?")

    def meaning_spans(self, hit: Hit, text: str) -> list[tuple[int, int]]:
        """For a hit found by meaning: the words of its closest sentence that are close
        to the query in meaning. Their index forms are added to the hit's match terms,
        so preview and document view highlight them too."""
        qv = getattr(self, "_qv", None)
        if self.model is None or qv is None or not hit.meaning:
            return []
        best, best_sim = None, -1.0
        for m in self._SENTENCE.finditer(text[:20000]):
            if len(m.group().strip()) < 12:
                continue
            v = self.model.embed(m.group())
            if v is not None and float(v @ qv) > best_sim:
                best, best_sim = m, float(v @ qv)
        if best is None:
            return []
        scored = []
        for a, b, t in token_spans(best.group(), hit.lang):
            v = self.model.embed(best.group()[a:b]) if b - a > 2 else None
            if v is not None:
                scored.append((float(v @ qv), a, b, t))
        scored.sort(reverse=True)
        top = scored[0][0] if scored else 0.0
        keep = [x for x in scored[:WORD_MARK_MAX] if x[0] >= max(WORD_SIMILAR, 0.8 * top)]
        if not keep:                                    # no single word stands out: mark the whole sentence
            return [(best.start(), best.end())]
        hit.match_terms = frozenset(hit.match_terms | {t for _, _, _, t in keep})
        return sorted((best.start() + a, best.start() + b) for _, a, b, _ in keep)

    def snippet(self, hit: Hit, width: int = 260) -> tuple[str, list[tuple[int, int]], int]:
        """(snippet, highlight spans relative to the snippet, offset of the best hit in the chunk)."""
        text = self.chunk_of(hit)["text"]
        spans = self.highlights(text, hit.match_terms, hit.lang) or self.meaning_spans(hit, text)
        if not spans:
            return text[:width], [], 0
        # find the window with the most hits
        best_i, best_n, j = 0, 0, 0
        for i in range(len(spans)):
            while j < len(spans) and spans[j][1] - spans[i][0] <= width * 0.8:
                j += 1
            if j - i > best_n:
                best_i, best_n = i, j - i
        center = spans[best_i][0]
        start = max(0, center - width // 4)
        if start:
            ws = text.rfind(" ", 0, start)
            start = ws + 1 if ws >= 0 and start - ws < 30 else start
        end = min(len(text), start + width)
        if end < len(text):
            we = text.find(" ", end)
            end = we if 0 <= we - end < 30 else end
        snip = text[start:end]
        rel = [(s - start, e - start) for s, e in spans if s >= start and e <= end]
        prefix = "… " if start else ""
        rel = [(s + len(prefix), e + len(prefix)) for s, e in rel]
        return prefix + snip + (" …" if end < len(text) else ""), rel, spans[best_i][0]


# ---------------------------------------------------------------- Location as text
def target_of(loc: dict, text: str, offset: int) -> dict:
    """Concrete location (page/line/cell) for a hit at `offset` in the chunk text."""
    t = dict(loc)
    nl = text.count("\n", 0, offset)
    if loc.get("lines"):  # explicit offset -> source line map (HTML/XML)
        pos, line = max((m for m in loc["lines"] if m[0] <= offset), default=loc["lines"][0])
        t["line"] = line + text.count("\n", pos, offset)
    elif "line" in loc and loc.get("kind") in ("line", "page", "chapter", "nbcell"):
        t["line"] = loc["line"] + nl
    t.pop("lines", None)
    for pos, ref in reversed(loc.get("cells") or []):
        if pos <= offset:
            t["cell"] = ref
            break
    else:
        if loc.get("cells"):
            t["cell"] = loc["cells"][0][1]
    t.pop("cells", None)
    return t


def format_location(t: dict) -> str:
    kind = t.get("kind")
    approx = "~" if t.get("approx") else ""
    if kind == "page":
        s = f"p. {approx}{t.get('page')}"
        if t.get("line") and not t.get("approx"):
            s += f", l. {t['line']}"
    elif kind == "slide":
        s = f"Slide {t.get('slide')}"
    elif kind == "cell":
        s = f"{t.get('sheet')}!{t.get('cell', '')}" if t.get("cell") else (f"{t.get('sheet')}, row {t.get('row')}" if t.get("row") else f"{t.get('sheet')}")
    elif kind == "row":
        s = f"Line {t.get('line')}" + (f", column {t['cell']}" if t.get("cell") else "")
    elif kind == "line":
        s = f"Line {t.get('line')}"
    elif kind == "paragraph":
        s = f"Paragraph {t.get('para')}"
    elif kind == "chapter":
        s = f"Chapter {t.get('chapter')}"
    elif kind == "image":
        s = "Image"
    elif kind == "nbcell":
        s = f"Cell {t.get('cell')}" + (f", l. {t['line']}" if t.get("line") and t.get("line") > 1 else "")
    elif kind == "file":
        s = "File name"
    else:
        s = ""
    if t.get("part"):
        s = f"{s} · {t['part']}" if s else t["part"]
    return s
