"""Language catalog and fast language detection.

Detection works in two steps:
  1. writing system: Cyrillic, Greek, Arabic, Hebrew, Devanagari, Thai, Japanese,
     Korean and Chinese are recognised by their Unicode ranges (plus a few letters
     that tell e.g. Russian/Ukrainian/Bulgarian or simplified/traditional apart);
  2. Latin script: frequency of very common words ("stop words") per language.

The catalog also maps languages to the Tesseract OCR language packs (tessdata_fast).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Language:
    iso: str            # ISO 639-1 code used in the index ("de")
    tess: str           # Tesseract language pack ("deu")
    name: str           # English name shown in the UI
    native: str         # name in the language itself
    size_mb: float      # size of the tessdata_fast pack
    stopwords: frozenset = field(default_factory=frozenset)


def _sw(words: str) -> frozenset:
    return frozenset(words.split())


LANGUAGES: list[Language] = [
    Language("en", "eng", "English", "English", 4.1, _sw(
        "the and is not of to in that it for on with as was were be are by this which or from at have has had "
        "but they you he she we his her their there been would will can could should into than these those what")),
    Language("de", "deu", "German", "Deutsch", 1.5, _sw(
        "der die das und ist nicht ein eine einer eines einem einen zu den von mit sich des auf für im dem auch "
        "es an als wie werden wird bei aus nach sind noch oder aber vor zur bis unter über dass sie wir ich um")),
    Language("fr", "fra", "French", "Français", 1.1, _sw(
        "le la les des et est un une du dans que qui pour pas sur au avec ce il elle sont nous vous ne se par "
        "plus mais ou leur cette aux été être fait comme tout")),
    Language("es", "spa", "Spanish", "Español", 2.3, _sw(
        "el la los las de que y en un una es por con para del se no al lo como más pero sus le ya o fue este "
        "ha son entre cuando muy sin sobre también")),
    Language("it", "ita", "Italian", "Italiano", 2.7, _sw(
        "il lo la gli le di che e è un una per non con del della sono anche come più nel alla dei delle ma si "
        "questo ha ci da al nella essere")),
    Language("pt", "por", "Portuguese", "Português", 2.0, _sw(
        "o a os as de que e em um uma do da para com não por mais se como dos das ao é mas foi também pelo "
        "pela isso está são")),
    Language("nl", "nld", "Dutch", "Nederlands", 6.1, _sw(
        "de het een en van is dat op te in voor niet met zijn er maar ook als bij aan om door naar dan wordt "
        "worden deze nog hij ze")),
    Language("pl", "pol", "Polish", "Polski", 4.8, _sw(
        "i w na z że się nie do jest to jak o co ale po tak przez od dla są lub oraz jego może być tylko")),
    Language("cs", "ces", "Czech", "Čeština", 3.8, _sw(
        "a v na je se že to s z do o jako ale by pro jsem tak jsou podle také nebo jeho které který bylo jen")),
    Language("sk", "slk", "Slovak", "Slovenčina", 4.4, _sw(
        "a v na je sa že to s z do o ako ale by pre som tak sú podľa aj alebo jeho ktoré ktorý bolo len")),
    Language("sv", "swe", "Swedish", "Svenska", 4.2, _sw(
        "och i att det som en på är av för med den till inte har de ett om var men så jag kan")),
    Language("da", "dan", "Danish", "Dansk", 2.6, _sw(
        "og i at det er en til på som de med for af ikke den har et var men så jeg kan også efter")),
    Language("no", "nor", "Norwegian", "Norsk", 3.6, _sw(
        "og i det som er en på til at av for med ikke har de den et var men så jeg kan også etter")),
    Language("fi", "fin", "Finnish", "Suomi", 7.9, _sw(
        "ja on ei se että oli hän mutta kun niin myös tai ovat kuin mitä tämä joka sen olla vain")),
    Language("tr", "tur", "Turkish", "Türkçe", 4.6, _sw(
        "ve bir bu da de için ile çok olarak daha gibi ama ne değil olan en kadar sonra her ancak")),
    Language("ro", "ron", "Romanian", "Română", 2.4, _sw(
        "și în de la cu este pe un o care nu din pentru sunt mai fost ca sau dar acest")),
    Language("hu", "hun", "Hungarian", "Magyar", 5.3, _sw(
        "a az és hogy nem is egy ez meg van volt de már csak mint ki el")),
    Language("hr", "hrv", "Croatian", "Hrvatski", 4.1, _sw(
        "i u je na da se za od su ne kao s to ali biti ili koji koja")),
    Language("id", "ind", "Indonesian", "Bahasa Indonesia", 1.1, _sw(
        "yang dan di ini itu dengan untuk dari tidak dalam akan ada pada juga oleh atau")),
    Language("vi", "vie", "Vietnamese", "Tiếng Việt", 0.5, _sw(
        "và của là có không được trong những người một các cho này với đã để")),
    Language("ru", "rus", "Russian", "Русский", 3.9),
    Language("uk", "ukr", "Ukrainian", "Українська", 3.8),
    Language("bg", "bul", "Bulgarian", "Български", 1.7),
    Language("el", "ell", "Greek", "Ελληνικά", 1.4),
    Language("ar", "ara", "Arabic", "العربية", 1.4),
    Language("fa", "fas", "Persian", "فارسی", 0.4),
    Language("he", "heb", "Hebrew", "עברית", 1.0),
    Language("hi", "hin", "Hindi", "हिन्दी", 1.1),
    Language("th", "tha", "Thai", "ไทย", 1.1),
    Language("ja", "jpn", "Japanese", "日本語", 2.5),
    Language("ko", "kor", "Korean", "한국어", 1.7),
    Language("zh", "chi_sim", "Chinese (simplified)", "简体中文", 2.5),
    Language("zh-Hant", "chi_tra", "Chinese (traditional)", "繁體中文", 2.4),
]
BY_ISO = {l.iso: l for l in LANGUAGES}
BY_TESS = {l.tess: l for l in LANGUAGES}
DEFAULT_PACKS = ("eng", "deu")

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_SCRIPTS = [  # (name, ranges)
    ("cyrillic", ((0x0400, 0x04FF),)), ("greek", ((0x0370, 0x03FF),)),
    ("arabic", ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
    ("hebrew", ((0x0590, 0x05FF),)), ("devanagari", ((0x0900, 0x097F),)), ("thai", ((0x0E00, 0x0E7F),)),
    ("kana", ((0x3040, 0x30FF),)), ("hangul", ((0xAC00, 0xD7AF), (0x1100, 0x11FF))),
    ("han", ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))), ("latin", ((0x41, 0x5A), (0x61, 0x7A), (0xC0, 0x24F))),
]
_SIMPLIFIED = set("们这说为国会对时来个发经过还样问长门见现关点开动电车东应头机产认实书")
_TRADITIONAL = set("們這說為國會對時來個發經過還樣問長門見現關點開動電車東應頭機產認實書")


def _script_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ch in text:
        o = ord(ch)
        if o < 0x41:
            continue
        for name, ranges in _SCRIPTS:
            if any(a <= o <= b for a, b in ranges):
                counts[name] = counts.get(name, 0) + 1
                break
    return counts


def _non_latin(script: str, text: str) -> str:
    if script == "cyrillic":
        if any(c in text for c in "іїєґІЇЄҐ"):
            return "uk"
        bul = text.count("ъ") + text.count("Ъ")
        rus = sum(text.count(c) for c in "ыэёЫЭЁ")
        return "bg" if bul > rus else "ru"
    if script == "arabic":
        return "fa" if sum(text.count(c) for c in "پچژگی") > text.count("ي") else "ar"
    if script == "kana":
        return "ja"
    if script == "han":
        trad = sum(1 for c in text if c in _TRADITIONAL)
        simp = sum(1 for c in text if c in _SIMPLIFIED)
        return "zh-Hant" if trad > simp else "zh"
    return {"greek": "el", "hebrew": "he", "devanagari": "hi", "thai": "th", "hangul": "ko"}[script]


def detect(text: str) -> tuple[str, float]:
    """Return (ISO code or "und", confidence 0..1)."""
    sample = text[:20000]
    scripts = _script_counts(sample)
    letters = sum(scripts.values())
    if letters < 10:
        return "und", 0.0
    script, count = max(scripts.items(), key=lambda kv: kv[1])
    if scripts.get("kana", 0) > 0.05 * letters:          # Japanese mixes kana with Han characters
        script, count = "kana", scripts["kana"] + scripts.get("han", 0)
    if script != "latin":
        return _non_latin(script, sample), count / letters
    words = _WORD.findall(sample.lower())
    scores = {l.iso: 0.0 for l in LANGUAGES if l.stopwords}
    for w in words:
        for l in LANGUAGES:
            if w in l.stopwords:
                scores[l.iso] += 1
    scores["de"] += sum(sample.count(c) for c in "äöüßÄÖÜ") * 0.5   # strong hint for German
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top < 3:
        return "und", 0.0
    return best, top / (top + second)


def tesseract_code(iso: str) -> str | None:
    lang = BY_ISO.get(iso)
    return lang.tess if lang else None


def name_of(code: str) -> str:
    lang = BY_ISO.get(code) or BY_TESS.get(code)
    return lang.name if lang else code
