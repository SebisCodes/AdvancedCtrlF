"""Full-text search: inverted index with BM25 plus phrases, proximity, prefix, infix and typo tolerance.

How a search works
  1. Parse the query: words, "phrases", -exclusions, prefix*, type:pdf, file:name
  2. Normalize every word (case, umlauts, accents) and apply light stemming (DE/EN)
  3. Expansions per word: exact (1.0) > prefix (0.85/0.7) > infix (0.6) > typo (0.55/0.4)
  4. BM25 per text chunk (page/paragraph/slide/line), best expansion per query word
  5. Weighting by coverage of all query words, word proximity and phrases in query order
"""
from __future__ import annotations

import heapq
import math
import re
import unicodedata
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field

TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
K1, B = 1.2, 0.75

# ---------------------------------------------------------------- Normalization
_DIGRAPHS = (("ae", "a"), ("oe", "o"), ("ue", "u"))
_SUFFIXES = (
    ("ungen", "ung"), ("heiten", "heit"), ("keiten", "keit"), ("ies", "y"), ("ing", ""), ("ern", ""),
    ("ed", ""), ("en", ""), ("er", ""), ("es", ""), ("em", ""), ("e", ""), ("s", ""), ("n", ""),
)


def fold(word: str) -> str:
    """Lower-case, ß→ss, ä/ae→a, ö/oe→o, ü/ue→u, strip accents."""
    w = word.casefold()
    for a, b in _DIGRAPHS:
        w = w.replace(a, b)
    w = unicodedata.normalize("NFKD", w)
    return "".join(c for c in w if not unicodedata.combining(c))


def stem(w: str) -> str:
    if len(w) <= 3 or w.isdigit():
        return w
    for suf, rep in _SUFFIXES:
        if w.endswith(suf):
            base = w[: -len(suf)]
            if len(base) + len(rep) >= 3 and (len(suf) > 1 or len(base) >= 4):
                return base + rep
            break
    return w


def norm(word: str) -> str:
    return stem(fold(word))


def tokenize(text: str) -> list[str]:
    return [norm(m.group()) for m in TOKEN.finditer(text)]


def token_spans(text: str):
    for m in TOKEN.finditer(text):
        yield m.start(), m.end(), norm(m.group())


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


# ---------------------------------------------------------------- Query
@dataclass
class Query:
    terms: list[tuple[str, bool]] = field(default_factory=list)   # (stem, is prefix?)
    phrases: list[list[str]] = field(default_factory=list)
    exclude: list[list[str]] = field(default_factory=list)
    kinds: set[str] = field(default_factory=set)
    files: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def empty(self) -> bool:
        return not self.terms and not self.phrases


_QTOK = re.compile(r'(-?)(?:(\w+):)?(?:"([^"]*)"?|(\S+))')


def parse_query(text: str) -> Query:
    q = Query(raw=text)
    for neg, key, phrase, word in _QTOK.findall(text):
        key = key.lower()
        val = phrase if phrase else word
        if key in ("typ", "type", "ext") and val:
            q.kinds.update(v.strip(".").lower() for v in val.split(","))
            continue
        if key in ("datei", "file", "name") and val:
            q.files.append(fold(val))
            continue
        if key:  # unknown key -> treat as plain text
            val = f"{key}:{val}"
        toks = tokenize(val)
        if not toks:
            continue
        if neg:
            q.exclude.append(toks)
        elif phrase and len(toks) > 1:
            q.phrases.append(toks)
            q.terms += [(t, False) for t in toks]
        else:
            prefix = val.endswith("*")
            q.terms += [(t, prefix and i == len(toks) - 1) for i, t in enumerate(toks)]
    # drop duplicate terms, keep order
    seen, terms = set(), []
    for t in q.terms:
        if t[0] not in seen:
            seen.add(t[0])
            terms.append(t)
    q.terms = terms
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


# ---------------------------------------------------------------- Index
class SearchIndex:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.refs: list[tuple[str, int] | None] = []     # cid -> (doc_id, chunk index)
        self.lens: list[int] = []
        self.post: dict[str, dict[int, list[int]]] = defaultdict(dict)
        self.doc_cids: dict[str, list[int]] = {}
        self.total_len = 0
        self.alive = 0
        self._vocab: list[str] | None = None
        self._tri: dict[str, list[str]] | None = None

    # ------------------------------------------------ Maintenance
    def add(self, doc: dict) -> None:
        if doc.get("id") in self.docs:
            self.remove(doc["id"])
        did = doc["id"]
        self.docs[did] = doc
        cids = []
        name_tokens = tokenize(doc.get("display_name") or doc.get("name", ""))
        for ci, ch in enumerate(doc.get("chunks") or []):
            cid = len(self.refs)
            self.refs.append((did, ci))
            toks = tokenize(ch.get("text", ""))
            title = ch.get("loc", {}).get("title")
            for pos, t in enumerate(toks):
                self.post[t].setdefault(cid, []).append(pos)
            # slide title and file name count weakly (far-away position -> never part of a phrase)
            for extra in (tokenize(title) if title else []) + (name_tokens if ci == 0 else []):
                self.post[extra].setdefault(cid, []).append(10 ** 6 + len(toks))
            self.lens.append(len(toks))
            self.total_len += len(toks)
            cids.append(cid)
        self.doc_cids[did] = cids
        self.alive += len(cids)
        self._vocab = self._tri = None

    def remove(self, doc_id: str) -> None:
        self.docs.pop(doc_id, None)
        for cid in self.doc_cids.pop(doc_id, []):
            self.refs[cid] = None
            self.total_len -= self.lens[cid]
            self.alive -= 1
        dead = len(self.refs) - self.alive
        if dead > 5000 and dead > self.alive:
            self.rebuild()

    def rebuild(self) -> None:
        docs = list(self.docs.values())
        self.__init__()
        for d in docs:
            self.add(d)

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

    def df(self, term: str) -> int:
        return len(self.post.get(term, ()))

    def expand(self, term: str, prefix: bool, is_last: bool) -> dict[str, float]:
        exp: dict[str, float] = {}
        if term in self.post:
            exp[term] = 1.0
        if term.isdigit():
            return exp
        vocab = self.vocab
        # prefix: explicit with * or automatic for the last word (search as you type)
        if (prefix or is_last) and len(term) >= 3:
            w = 0.85 if prefix else 0.7
            i = bisect_left(vocab, term)
            cands = []
            while i < len(vocab) and vocab[i].startswith(term) and len(cands) < 2000:
                if vocab[i] != term:
                    cands.append(vocab[i])
                i += 1
            for t in sorted(cands, key=self.df, reverse=True)[:150]:
                exp.setdefault(t, w)
        # infix (German compounds: "vertrag" in "arbeitsvertrag")
        if len(term) >= 4:
            cands = [t for t in vocab if term in t and not t.startswith(term)]
            for t in sorted(cands, key=self.df, reverse=True)[:150]:
                exp.setdefault(t, 0.6)
        # typos, if the word is rare or missing
        if len(term) >= 4 and self.df(term) < 3:
            maxd = 1 if len(term) <= 6 else 2
            p = f"${term}$"
            grams = {p[i:i + 3] for i in range(len(p) - 2)}
            counts: dict[str, int] = defaultdict(int)
            tri = self._trigrams()
            for g in grams:
                for t in tri.get(g, ()):
                    counts[t] += 1
            need = max(1, len(grams) - 3 * maxd)
            fuzzy = []
            for t, c in counts.items():
                if c >= need and t != term and abs(len(t) - len(term)) <= maxd:
                    d = levenshtein(term, t, maxd)
                    if d <= maxd:
                        fuzzy.append((d, -self.df(t), t))
            for d, _, t in sorted(fuzzy)[:30]:
                exp.setdefault(t, 0.55 if d == 1 else 0.4)
        return exp

    # ------------------------------------------------ Search
    def search(self, text: str, kinds: set[str] | None = None, limit: int = 300,
               one_per_doc: bool = False) -> tuple[list[Hit], Query]:
        q = parse_query(text)
        if kinds:
            q.kinds |= kinds
        if q.empty or not self.alive:
            return [], q
        n = max(1, self.alive)
        avg = max(1.0, self.total_len / n)
        term_scores: list[dict[int, float]] = []
        term_pos: list[dict[int, list[int]]] = []
        all_match: set[str] = set()
        for i, (term, prefix) in enumerate(q.terms):
            exp = self.expand(term, prefix, i == len(q.terms) - 1 and not q.raw.rstrip().endswith((" ", '"')))
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
        # exclusions
        for ex in q.exclude:
            if len(ex) == 1:
                cands -= set(self.post.get(ex[0], {}))
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
            if q.kinds and doc.get("kind", "") not in q.kinds:
                continue
            if q.files and not all(f in fold(doc.get("display_name") or doc.get("name", "")) for f in q.files):
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
            results.append(Hit(did, ci, score, coverage=cov))
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
        return top, q

    def _has_phrase(self, cid: int, phrase: list[str]) -> bool:
        lists = [self.post.get(t, {}).get(cid) for t in phrase]
        if any(l is None for l in lists):
            return False
        rest = [set(l) for l in lists[1:]]
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

    def highlights(self, text: str, match_terms) -> list[tuple[int, int]]:
        return [(s, e) for s, e, t in token_spans(text) if t in match_terms]

    def snippet(self, hit: Hit, width: int = 260) -> tuple[str, list[tuple[int, int]], int]:
        """(snippet, highlight spans relative to the snippet, offset of the best hit in the chunk)."""
        text = self.chunk_of(hit)["text"]
        spans = self.highlights(text, hit.match_terms)
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
    elif "line" in loc and loc.get("kind") in ("line", "page", "chapter"):
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
        s = f"{t.get('sheet')}!{t.get('cell', '')}" if t.get("cell") else f"{t.get('sheet')}, row {t.get('row')}"
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
    else:
        s = ""
    if t.get("part"):
        s = f"{s} · {t['part']}" if s else t["part"]
    return s
