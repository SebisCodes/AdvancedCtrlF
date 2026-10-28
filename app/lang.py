"""Simple, fast German/English language detection based on stop words."""
from __future__ import annotations

import re

_DE = set("""der die das und ist nicht ein eine einer eines einem einen zu den von mit sich des auf für im dem
auch es an als wie werden wird bei aus nach sind noch oder aber vor zur bis unter über dass daß sie wir ich
um man nur kann können hat haben wurde wurden sein seine ihre durch diese dieser dieses wenn zum vom sowie
mehr schon gegen ohne hier wo weil dann doch so sehr""".split())
_EN = set("""the and is not a an of to in that it for on with as was were be are by this which or from at
have has had but they you he she we his her their there been would will can could should into than then
these those what when where who how all any some such only also more other its our about after over""".split())

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect(text: str) -> tuple[str, float]:
    """Return ("de"|"en"|"und", confidence 0..1); "und" = undetermined."""
    de = en = 0
    umlauts = sum(text.count(c) for c in "äöüÄÖÜß")
    for w in _WORD.findall(text[:20000].lower()):
        if w in _DE:
            de += 1
        if w in _EN:
            en += 1
    de += umlauts * 0.5
    total = de + en
    if total < 3:
        return "und", 0.0
    if de > en:
        return "de", de / total
    if en > de:
        return "en", en / total
    return "und", 0.5


def tesseract_code(lang: str) -> str:
    return {"de": "deu", "en": "eng"}.get(lang, "deu+eng")
