"""Language catalog and fast language detection.

Detection works in three steps:
  1. writing system: every letter is assigned to its Unicode script (Latin, Cyrillic,
     Greek, Arabic, Hebrew, Devanagari, Bengali, Gurmukhi, Gujarati, Tamil, Telugu,
     Kannada, Malayalam, Thai, Kana, Hangul, Han, ...). A non-Latin script wins as soon
     as it makes up a substantial share, because Latin brand names and loanwords are
     common in every language;
  2. scripts used by a single language in the catalog decide directly (Greek -> el);
  3. scripts shared by several languages are decided by weighted function words and
     letters: a word or letter that belongs to few languages counts more than one that
     many languages share ("och" -> Swedish only, "de" -> many).

The catalog also maps languages to the Tesseract OCR language packs (tessdata_fast) and
to the Snowball stemmer used by the search index.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Language:
    iso: str            # ISO 639-1 code used in the index ("de")
    tess: str           # Tesseract language pack ("deu")
    name: str           # English name shown in the UI
    native: str         # name in the language itself
    size_mb: float      # size of the tessdata_fast pack
    script: str         # main writing system (see _SCRIPTS)
    stemmer: str = ""   # Snowball algorithm ("" = no stemming)
    stopwords: frozenset = field(default_factory=frozenset)
    letters: str = ""   # letters typical for the language (beyond a-z)


def _sw(words: str) -> frozenset:
    return frozenset(words.split())


LANGUAGES: list[Language] = [
    # ---------------------------------------------------------------- Latin script
    Language("en", "eng", "English", "English", 4.1, "latin", "english", _sw(
        "the and is are was were of to in that it for on with as be by this which or from at have has had "
        "but they you he she we his her their there been would will can could should into than these those "
        "what who not an also its about after more said when over")),
    Language("de", "deu", "German", "Deutsch", 1.5, "latin", "german", _sw(
        "der die das und ist nicht ein eine einer eines einem einen zu den von mit sich des auf für im dem "
        "auch es an als wie werden wird bei aus nach sind noch oder aber vor zur bis unter über dass sie wir "
        "ich um hat haben wurde nur durch zum seine ihre kann gegen sei mehr"), "äöüß"),
    Language("fr", "fra", "French", "Français", 1.1, "latin", "french", _sw(
        "le la les des et est un une du dans que qui pour pas sur au avec ce il elle sont nous vous ne se par "
        "plus mais ou leur cette aux été être fait comme tout ses son sa ont cela lors après selon aussi"),
        "àâçéèêëîïôœùû"),
    Language("es", "spa", "Spanish", "Español", 2.3, "latin", "spanish", _sw(
        "el la los las de que y en un una es por con para del se no al lo como más pero sus le ya fue este "
        "ha son entre cuando muy sin sobre también hay desde está porque esta según tras ser"), "áéíñóú¿¡"),
    Language("it", "ita", "Italian", "Italiano", 2.7, "latin", "italian", _sw(
        "il lo la gli le di che e è un una per non con del della sono anche come più nel alla dei delle ma si "
        "questo ha ci da al nella essere degli stato dopo questa sua suo loro"), "àèéìòù"),
    Language("pt", "por", "Portuguese", "Português", 2.0, "latin", "portuguese", _sw(
        "o a os as de que e em um uma do da para com não por mais se como dos das ao é mas foi também pelo "
        "pela isso está são ser seu sua já quando após entre sobre até"), "áâãàçéêíóôõú"),
    Language("ca", "cat", "Catalan", "Català", 1.1, "latin", "catalan", _sw(
        "el la els les de que i en un una és per amb del al no es com més però seu seva va ha ser aquest "
        "aquesta són també hi ho molt entre quan des fins sobre pel dels han"), "àçèéíïòóúüŀ"),
    Language("nl", "nld", "Dutch", "Nederlands", 6.1, "latin", "dutch", _sw(
        "de het een en van is dat op te in voor niet met zijn er maar ook als bij aan om door naar dan wordt "
        "worden deze nog hij ze die heeft hebben werd uit over"), "ëï"),
    Language("af", "afr", "Afrikaans", "Afrikaans", 2.7, "latin", "", _sw(
        "die en van is in het nie te dat op vir met wat sy hy word ook was deur nog hulle"), "êëôŉ"),
    Language("pl", "pol", "Polish", "Polski", 4.8, "latin", "polish", _sw(
        "i w na z że się nie do jest to jak o co ale po tak przez od dla są lub oraz jego może być tylko "
        "który która które już także jednak"), "ąćęłńóśźż"),
    Language("cs", "ces", "Czech", "Čeština", 3.8, "latin", "czech", _sw(
        "a v na je se že to s z do o jako ale by pro jsem tak jsou podle také nebo jeho které který bylo "
        "jen však byl když"), "áčďéěíňóřšťúůýž"),
    Language("sk", "slk", "Slovak", "Slovenčina", 4.4, "latin", "", _sw(
        "a v na je sa že to s z do o ako ale by pre som tak sú podľa aj alebo jeho ktoré ktorý bolo len "
        "však bol keď"), "áäčďéíĺľňóôŕšťúýž"),
    Language("sl", "slv", "Slovenian", "Slovenščina", 3.0, "latin", "", _sw(
        "in je v na se da za so z ki pa ne bo tudi kot to ali od po bi pri"), "čšž"),
    Language("hr", "hrv", "Croatian", "Hrvatski", 4.1, "latin", "serbian", _sw(
        "i u je na da se za od su ne kao s to ali biti ili koji koja koje što će bio"), "čćđšž"),
    Language("sv", "swe", "Swedish", "Svenska", 4.2, "latin", "swedish", _sw(
        "och i att det som en på är av för med den till inte har de ett om var men så jag kan också efter "
        "vid från sig hade eller blir nu"), "åäö"),
    Language("da", "dan", "Danish", "Dansk", 2.6, "latin", "danish", _sw(
        "og i at det er en til på som de med for af ikke den har et var men så jeg kan også efter blev fra "
        "sig havde eller bliver nu hun nogle mig kun"), "æøå"),
    Language("no", "nor", "Norwegian", "Norsk", 3.6, "latin", "norwegian", _sw(
        "og i det som er en på til at av for med ikke har de den et var men så jeg kan også etter ble fra "
        "seg hadde eller blir nå hun skal noen meg bare"), "æøå"),
    Language("fi", "fin", "Finnish", "Suomi", 7.9, "latin", "finnish", _sw(
        "ja on ei se että oli hän mutta kun niin myös tai ovat kuin mitä tämä joka sen olla vain jo nyt "
        "hänen"), "äö"),
    Language("et", "est", "Estonian", "Eesti", 4.5, "latin", "estonian", _sw(
        "ja on ei see et oli ta aga kui nii ka või nad kes mis selle oma ning siis veel"), "õäöü"),
    Language("lv", "lav", "Latvian", "Latviešu", 2.7, "latin", "", _sw(
        "un ir ar par no uz kas lai ka bet arī tā to vai kā tika"), "āčēģīķļņšūž"),
    Language("lt", "lit", "Lithuanian", "Lietuvių", 3.2, "latin", "lithuanian", _sw(
        "ir yra kad su į iš už bet tai kaip arba jo buvo ar o"), "ąčęėįšųūž"),
    Language("hu", "hun", "Hungarian", "Magyar", 5.3, "latin", "hungarian", _sw(
        "a az és hogy nem is egy ez meg van volt de már csak mint ki el"), "áéíóöőúüű"),
    Language("ro", "ron", "Romanian", "Română", 2.4, "latin", "romanian", _sw(
        "și în de la cu este pe un o care nu din pentru sunt mai fost ca sau dar acest că să se au fi"),
        "ăâîșşțţ"),
    Language("tr", "tur", "Turkish", "Türkçe", 4.6, "latin", "turkish", _sw(
        "ve bir bu da de için ile çok olarak daha gibi ama ne değil olan en kadar sonra her ancak ise şu "
        "olduğu ya veya göre yeni"), "çğışöüİ"),
    Language("id", "ind", "Indonesian", "Bahasa Indonesia", 1.1, "latin", "indonesian", _sw(
        "yang dan di ini itu dengan untuk dari tidak dalam akan ada pada juga oleh atau ke karena sudah bisa "
        "saat telah")),
    Language("vi", "vie", "Vietnamese", "Tiếng Việt", 0.5, "latin", "", _sw(
        "và của là có không được trong những người một các cho này với đã để"),
        "ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ"),
    # ---------------------------------------------------------------- other scripts
    Language("ru", "rus", "Russian", "Русский", 3.9, "cyrillic", "russian", _sw(
        "и в не на что с по он как это к из у за от но то о так все же для был она бы его"), "ыэё"),
    Language("uk", "ukr", "Ukrainian", "Українська", 3.8, "cyrillic", "", _sw(
        "і в не на що з по він як це до від у за але та й щоб бути його був"), "іїєґ"),
    Language("bg", "bul", "Bulgarian", "Български", 1.7, "cyrillic", "", _sw(
        "и в не на че с по той как това да за от но се са е ще бъде"), "ъ"),
    Language("el", "ell", "Greek", "Ελληνικά", 1.4, "greek", "greek"),
    Language("ar", "ara", "Arabic", "العربية", 1.4, "arabic", "arabic", _sw(
        "في من على أن إلى عن مع هذا هذه التي الذي كان ما لا قد بين"), "ةي"),
    Language("fa", "fas", "Persian", "فارسی", 0.4, "arabic", "persian", _sw(
        "و در به از که این را با است برای آن یک می شود"), "پچژگی"),
    Language("ur", "urd", "Urdu", "اردو", 1.4, "arabic", "", _sw(
        "کے کی میں ہے اور سے کو نے پر یہ ہیں کہ"), "ٹڈڑںھہےۓ"),
    Language("he", "heb", "Hebrew", "עברית", 1.0, "hebrew"),
    Language("hi", "hin", "Hindi", "हिन्दी", 1.1, "devanagari", "hindi", _sw(
        "के है में की और को से का एक यह पर भी नहीं कि लिए था हैं ने")),
    Language("mr", "mar", "Marathi", "मराठी", 2.1, "devanagari", "", _sw(
        "आहे आणि या व हे की ते त्या ने च्या मध्ये करण्यात आले होते असे आहेत"), "ळ"),
    Language("ne", "nep", "Nepali", "नेपाली", 1.0, "devanagari", "nepali", _sw(
        "र छ को मा पनि गरेको भएको हो छन् गर्न थियो")),
    Language("bn", "ben", "Bengali", "বাংলা", 0.9, "bengali"),
    Language("pa", "pan", "Punjabi", "ਪੰਜਾਬੀ", 0.5, "gurmukhi"),
    Language("gu", "guj", "Gujarati", "ગુજરાતી", 1.4, "gujarati"),
    Language("or", "ori", "Odia", "ଓଡ଼ିଆ", 1.5, "oriya"),
    Language("ta", "tam", "Tamil", "தமிழ்", 3.2, "tamil", "tamil"),
    Language("te", "tel", "Telugu", "తెలుగు", 2.8, "telugu"),
    Language("kn", "kan", "Kannada", "ಕನ್ನಡ", 3.6, "kannada"),
    Language("ml", "mal", "Malayalam", "മലയാളം", 5.3, "malayalam"),
    Language("si", "sin", "Sinhala", "සිංහල", 1.7, "sinhala"),
    Language("th", "tha", "Thai", "ไทย", 1.1, "thai"),
    Language("ja", "jpn", "Japanese", "日本語", 2.5, "kana"),
    Language("ko", "kor", "Korean", "한국어", 1.7, "hangul"),
    Language("zh", "chi_sim", "Chinese (simplified)", "简体中文", 2.5, "han"),
    Language("zh-Hant", "chi_tra", "Chinese (traditional)", "繁體中文", 2.4, "han"),
]
BY_ISO = {l.iso: l for l in LANGUAGES}
BY_TESS = {l.tess: l for l in LANGUAGES}
DEFAULT_PACKS = ("eng", "deu")

# letters plus combining marks: Indic vowel signs are not word characters for \w
MARKS = "\u0300-\u036F\u0483-\u0489\u0591-\u05C7\u064B-\u065F\u0670\u06D6-\u06ED\u0900-\u0DFF\u0E31-\u0E3A\u0E47-\u0E4E"
_WORD = re.compile(rf"(?:[^\W\d_]|[{MARKS}])+", re.UNICODE)
_SCRIPTS = [  # (name, ranges) - Latin last: it is the fallback for mixed texts
    ("cyrillic", ((0x0400, 0x04FF),)), ("greek", ((0x0370, 0x03FF), (0x1F00, 0x1FFF))),
    ("arabic", ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
    ("hebrew", ((0x0590, 0x05FF),)), ("devanagari", ((0x0900, 0x097F),)), ("bengali", ((0x0980, 0x09FF),)),
    ("gurmukhi", ((0x0A00, 0x0A7F),)), ("gujarati", ((0x0A80, 0x0AFF),)), ("oriya", ((0x0B00, 0x0B7F),)),
    ("tamil", ((0x0B80, 0x0BFF),)), ("telugu", ((0x0C00, 0x0C7F),)), ("kannada", ((0x0C80, 0x0CFF),)),
    ("malayalam", ((0x0D00, 0x0D7F),)), ("sinhala", ((0x0D80, 0x0DFF),)), ("thai", ((0x0E00, 0x0E7F),)),
    ("kana", ((0x3040, 0x30FF),)), ("hangul", ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F))),
    ("han", ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))),
    ("latin", ((0x41, 0x5A), (0x61, 0x7A), (0xC0, 0x24F), (0x1E00, 0x1EFF))),
]
_ZH_PAIRS = (  # (simplified, traditional) of frequent characters that differ
    "们們 这這 说說 为為 国國 会會 对對 时時 来來 个個 发發 经經 过過 还還 样樣 问問 长長 门門 见見 现現 关關 点點 "
    "开開 动動 电電 车車 东東 应應 头頭 机機 产產 认認 实實 书書 进進 无無 业業 务務 让讓 记記 话話 语語 读讀 给給 "
    "将將 帮幫 网網 与與 两兩 区區 级級 报報 华華 际際 种種 战戰 军軍 总總 统統 选選 举舉 价價 买買 卖賣 钱錢 银銀 "
    "资資 计計 划劃 达達 标標 济濟 观觀 场場 线線 题題 气氣 热熱 体體 数數 据據 处處 该該 变變 难難 历歷 农農 万萬 "
    "亿億 准準 亚亞 欧歐 员員 从從 么麼 们們 导導 领領 级級 车車 购購 产產 鸡雞 鱼魚 马馬 鸟鳥 龙龍 门門 页頁 视視 "
    "频頻 显顯 传傳 讯訊 设設 证證 议議 论論 调調 质質 务務 节節 画畫 广廣 厂廠 县縣 乡鄉 镇鎮 艺藝 术術 医醫 药藥")
_SIMPLIFIED = {p[0] for p in _ZH_PAIRS.split()}
_TRADITIONAL = {p[1] for p in _ZH_PAIRS.split()}
LETTER_WEIGHT = 0.3   # one typical letter counts as 0.3 function words


def _weights(attr: str) -> dict[str, dict[str, float]]:
    """item -> {iso: weight}; an item shared by k languages of a script weighs 1/k."""
    owners: dict[tuple[str, str], list[str]] = defaultdict(list)
    for l in LANGUAGES:
        for item in getattr(l, attr):
            owners[(l.script, item)].append(l.iso)
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for (_, item), isos in owners.items():
        for iso in isos:
            out[item][iso] = 1.0 / len(isos)
    return out


_WORD_W = _weights("stopwords")
_LETTER_W = _weights("letters")
_BY_SCRIPT: dict[str, list[Language]] = defaultdict(list)
for _l in LANGUAGES:
    _BY_SCRIPT[_l.script].append(_l)


def _script_of(o: int) -> str | None:
    for name, ranges in _SCRIPTS:
        for a, b in ranges:
            if a <= o <= b:
                return name
    return None


def _script_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for ch in text:
        o = ord(ch)
        if o >= 0x41 and (ch.isalpha() or 0x0900 <= o <= 0x0DFF):   # incl. Indic vowel signs
            s = _script_of(o)
            if s:
                counts[s] += 1
    return counts


def _main_script(scripts: dict[str, int]) -> str:
    letters = sum(scripts.values())
    if scripts.get("kana", 0) > 0.05 * letters:          # Japanese mixes kana with Han characters
        return "kana"
    others = {s: c for s, c in scripts.items() if s != "latin"}
    if others:
        s, c = max(others.items(), key=lambda kv: kv[1])
        if c >= 0.25 * letters:                           # Latin names inside another script
            return s
    return max(scripts.items(), key=lambda kv: kv[1])[0]


def _score_words(text: str, script: str) -> dict[str, float]:
    cands = {l.iso for l in _BY_SCRIPT[script]}
    scores = dict.fromkeys(cands, 0.0)
    for w in _WORD.findall(text.lower()):
        for iso, wt in _WORD_W.get(w, {}).items():
            if iso in cands:
                scores[iso] += wt
    for ch in text.lower():
        for iso, wt in _LETTER_W.get(ch, {}).items():
            if iso in cands:
                scores[iso] += LETTER_WEIGHT * wt
    return scores


def detect(text: str) -> tuple[str, float]:
    """Return (ISO code or "und", confidence 0..1)."""
    sample = text[:20000]
    scripts = _script_counts(sample)
    letters = sum(scripts.values())
    if letters < 10:
        return "und", 0.0
    script = _main_script(scripts)
    share = (scripts[script] + (scripts.get("han", 0) if script == "kana" else 0)) / letters
    if script == "han":
        trad = sum(1 for c in sample if c in _TRADITIONAL)
        simp = sum(1 for c in sample if c in _SIMPLIFIED)
        return ("zh-Hant" if trad > simp else "zh"), share
    cands = _BY_SCRIPT.get(script, [])
    if len(cands) == 1:
        return cands[0].iso, share
    scores = _score_words(sample, script)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), second = ranked[0], (ranked[1][1] if len(ranked) > 1 else 0.0)
    if top < 1.0 or (top < 1.5 and top < 2 * second):   # short texts need a clear lead
        return "und", 0.0
    return best, share * top / (top + second)


def tesseract_code(iso: str) -> str | None:
    lang = BY_ISO.get(iso)
    return lang.tess if lang else None


def name_of(code: str) -> str:
    lang = BY_ISO.get(code) or BY_TESS.get(code)
    return lang.name if lang else code
