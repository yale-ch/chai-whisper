"""Language codes: accept ISO 639-1 (2-letter), ISO 639-2/T and /B (3-letter),
BCP-47 tags with a region (``en-US``, ``pt_BR``), or English names, and map
them to the code Whisper expects.

Whisper uses ISO 639-1 codes, plus a few of its own: ``haw`` (Hawaiian),
``yue`` (Cantonese), and ``jw`` for Javanese (ISO 639-1 is ``jv``).
"""

from __future__ import annotations

# whisper code, English name, ISO 639-2/T, ISO 639-2/B (if different)
_TABLE = [
    ("en", "English", "eng", None), ("zh", "Chinese", "zho", "chi"), ("de", "German", "deu", "ger"),
    ("es", "Spanish", "spa", None), ("ru", "Russian", "rus", None), ("ko", "Korean", "kor", None),
    ("fr", "French", "fra", "fre"), ("ja", "Japanese", "jpn", None), ("pt", "Portuguese", "por", None),
    ("tr", "Turkish", "tur", None), ("pl", "Polish", "pol", None), ("ca", "Catalan", "cat", None),
    ("nl", "Dutch", "nld", "dut"), ("ar", "Arabic", "ara", None), ("sv", "Swedish", "swe", None),
    ("it", "Italian", "ita", None), ("id", "Indonesian", "ind", None), ("hi", "Hindi", "hin", None),
    ("fi", "Finnish", "fin", None), ("vi", "Vietnamese", "vie", None), ("he", "Hebrew", "heb", None),
    ("uk", "Ukrainian", "ukr", None), ("el", "Greek", "ell", "gre"), ("ms", "Malay", "msa", "may"),
    ("cs", "Czech", "ces", "cze"), ("ro", "Romanian", "ron", "rum"), ("da", "Danish", "dan", None),
    ("hu", "Hungarian", "hun", None), ("ta", "Tamil", "tam", None), ("no", "Norwegian", "nor", None),
    ("th", "Thai", "tha", None), ("ur", "Urdu", "urd", None), ("hr", "Croatian", "hrv", None),
    ("bg", "Bulgarian", "bul", None), ("lt", "Lithuanian", "lit", None), ("la", "Latin", "lat", None),
    ("mi", "Maori", "mri", "mao"), ("ml", "Malayalam", "mal", None), ("cy", "Welsh", "cym", "wel"),
    ("sk", "Slovak", "slk", "slo"), ("te", "Telugu", "tel", None), ("fa", "Persian", "fas", "per"),
    ("lv", "Latvian", "lav", None), ("bn", "Bengali", "ben", None), ("sr", "Serbian", "srp", None),
    ("az", "Azerbaijani", "aze", None), ("sl", "Slovenian", "slv", None), ("kn", "Kannada", "kan", None),
    ("et", "Estonian", "est", None), ("mk", "Macedonian", "mkd", "mac"), ("br", "Breton", "bre", None),
    ("eu", "Basque", "eus", "baq"), ("is", "Icelandic", "isl", "ice"), ("hy", "Armenian", "hye", "arm"),
    ("ne", "Nepali", "nep", None), ("mn", "Mongolian", "mon", None), ("bs", "Bosnian", "bos", None),
    ("kk", "Kazakh", "kaz", None), ("sq", "Albanian", "sqi", "alb"), ("sw", "Swahili", "swa", None),
    ("gl", "Galician", "glg", None), ("mr", "Marathi", "mar", None), ("pa", "Punjabi", "pan", None),
    ("si", "Sinhala", "sin", None), ("km", "Khmer", "khm", None), ("sn", "Shona", "sna", None),
    ("yo", "Yoruba", "yor", None), ("so", "Somali", "som", None), ("af", "Afrikaans", "afr", None),
    ("oc", "Occitan", "oci", None), ("ka", "Georgian", "kat", "geo"), ("be", "Belarusian", "bel", None),
    ("tg", "Tajik", "tgk", None), ("sd", "Sindhi", "snd", None), ("gu", "Gujarati", "guj", None),
    ("am", "Amharic", "amh", None), ("yi", "Yiddish", "yid", None), ("lo", "Lao", "lao", None),
    ("uz", "Uzbek", "uzb", None), ("fo", "Faroese", "fao", None), ("ht", "Haitian Creole", "hat", None),
    ("ps", "Pashto", "pus", None), ("tk", "Turkmen", "tuk", None), ("nn", "Norwegian Nynorsk", "nno", None),
    ("mt", "Maltese", "mlt", None), ("sa", "Sanskrit", "san", None), ("lb", "Luxembourgish", "ltz", None),
    ("my", "Burmese", "mya", "bur"), ("bo", "Tibetan", "bod", "tib"), ("tl", "Tagalog", "tgl", None),
    ("mg", "Malagasy", "mlg", None), ("as", "Assamese", "asm", None), ("tt", "Tatar", "tat", None),
    ("haw", "Hawaiian", "haw", None), ("ln", "Lingala", "lin", None), ("ha", "Hausa", "hau", None),
    ("ba", "Bashkir", "bak", None), ("jw", "Javanese", "jav", None), ("su", "Sundanese", "sun", None),
    ("yue", "Cantonese", "yue", None),
]

# Extra spellings that should resolve to a Whisper code.
_ALIASES = {
    "jv": "jw", "iw": "he", "in": "id", "ji": "yi", "nb": "no", "nob": "no", "fil": "tl",
    "cmn": "zh", "mandarin": "zh", "farsi": "fa", "flemish": "nl", "castilian": "es",
    "moldavian": "ro", "moldovan": "ro", "burmese": "my", "myanmar": "my", "haitian": "ht",
    "panjabi": "pa", "pushto": "ps", "sinhalese": "si", "valencian": "ca", "letzeburgesch": "lb",
    "bokmal": "no", "bokmål": "no",
}

_LOOKUP: dict[str, str] = {}
for _code, _name, _t, _b in _TABLE:
    for key in (_code, _name, _t, _b):
        if key:
            _LOOKUP[key.lower()] = _code
_LOOKUP.update(_ALIASES)

_BY_CODE = {code: (name, t, b) for code, name, t, b in _TABLE}


def normalize(value: str | None) -> str | None:
    """Map any accepted spelling to Whisper's code; ``None``/blank/"auto" → None
    (auto-detect). Raises ValueError for anything unrecognized."""
    v = (value or "").strip()
    if not v or v.lower() in ("auto", "detect", "auto-detect"):
        return None
    key = v.lower()
    if key in _LOOKUP:
        return _LOOKUP[key]
    base = key.replace("_", "-").split("-")[0]  # en-US / pt_BR / zh-Hant-TW
    if base in _LOOKUP:
        return _LOOKUP[base]
    raise ValueError(f"unrecognized language {value!r} — use a 2- or 3-letter ISO 639 code "
                     f"(e.g. en / eng) or a name (e.g. English)")


def iso639_1(code: str | None) -> str | None:
    """Whisper code → a standard tag for metadata (fixes Whisper's ``jw``)."""
    if not code:
        return None
    return "jv" if code == "jw" else code


def describe(code: str | None) -> dict | None:
    if not code or code not in _BY_CODE:
        return None
    name, t, b = _BY_CODE[code]
    return {"code": code, "name": name, "iso639_1": iso639_1(code) if len(code) == 2 or code == "jw" else None,
            "iso639_2": t, "iso639_2b": b or t}


def all_languages() -> list[dict]:
    return sorted((describe(c) for c in _BY_CODE), key=lambda d: d["name"])
