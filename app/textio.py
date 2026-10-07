"""Decode uploaded text files without assuming UTF-8.

Transcripts and lexicons come from many tools: Windows editors save UTF-16
("Unicode text") or cp1252, macOS/web tools save UTF-8 with or without a BOM.
A strict ``raw.decode("utf-8")`` rejects most of those, so every text upload
goes through ``decode_text``.
"""

from __future__ import annotations

import codecs

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),  # before UTF-16 LE: same 2-byte prefix
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _looks_utf16(raw: bytes) -> str | None:
    """BOM-less UTF-16: ASCII-heavy text has a NUL in every other byte."""
    sample = raw[:4000]
    if len(sample) < 4:
        return None
    even_nuls = sample[0::2].count(0) / max(1, len(sample[0::2]))
    odd_nuls = sample[1::2].count(0) / max(1, len(sample[1::2]))
    if odd_nuls > 0.3 and even_nuls < 0.05:
        return "utf-16-le"
    if even_nuls > 0.3 and odd_nuls < 0.05:
        return "utf-16-be"
    return None


def decode_text(raw: bytes | str) -> str:
    """Best-effort decode: BOM → UTF-16 sniff → UTF-8 → cp1252 → latin-1.
    Line endings are normalized to ``\\n``."""
    if isinstance(raw, str):
        text = raw
    else:
        text = None
        for bom, enc in _BOMS:
            if raw.startswith(bom):
                text = raw.decode(enc, "replace")
                break
        if text is None:
            enc16 = _looks_utf16(raw)
            if enc16:
                text = raw.decode(enc16, "replace")
        if text is None:
            for enc in ("utf-8", "cp1252"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
        if text is None:
            text = raw.decode("latin-1")
    return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
