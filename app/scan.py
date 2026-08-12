"""Sensitive-language scan: flag lexicon terms in a transcript.

Matching is case-insensitive and whole-word (word boundaries), and supports
multi-word phrases. Terms come from project lexicons (plain lists). The scan can
run over any rendered output format (via app.exporters) and also maps hits back
to segment ids for editor highlighting.
"""

from __future__ import annotations

import re

from . import exporters


def parse_terms(raw: bytes | str) -> list[str]:
    """Parse an uploaded lexicon: one term per line, blanks/`#` comments dropped."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    out, seen = [], set()
    for line in text.splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def compile_terms(terms: list[str]):
    """One case-insensitive regex; `\\b...\\b` per term, phrases allow flexible
    internal whitespace. Longest terms first so phrases win over their words."""
    parts = []
    for t in sorted({t for t in terms if t.strip()}, key=len, reverse=True):
        esc = r"\s+".join(re.escape(w) for w in t.split())
        parts.append(rf"\b{esc}\b")
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE)


def scan_text(text: str, terms: list[str], context: int = 40) -> list[dict]:
    rx = compile_terms(terms)
    if rx is None or not text:
        return []
    hits = []
    for m in rx.finditer(text):
        a, b = m.start(), m.end()
        hits.append({
            "term": m.group(0),
            "start": a, "end": b,
            "context": text[max(0, a - context):b + context].replace("\n", " ").strip(),
        })
    return hits


def scan_segments(data: dict, terms: list[str]) -> list[dict]:
    """Per-segment hits (for editor highlighting): each hit carries its segment id."""
    rx = compile_terms(terms)
    if rx is None:
        return []
    flags = []
    for s in data.get("segments", []):
        text = s.get("text") or ""
        for m in rx.finditer(text):
            flags.append({
                "seg_id": s.get("id"), "term": m.group(0),
                "start": m.start(), "end": m.end(),
                "context": text.strip(),
            })
    return flags


def scan_data(data: dict, terms: list[str], fmt: str = "txt") -> dict:
    """Scan a rendered format's text and also collect per-segment hits.
    Returns {format, flag_count, terms_hit, flags[], segment_flags[]}."""
    rendered = exporters.render(fmt, data) if fmt in exporters.RENDERERS else exporters.to_txt(data)
    flags = scan_text(rendered, terms)
    seg_flags = scan_segments(data, terms)
    return {
        "format": fmt,
        "flag_count": len(flags),
        "terms_hit": sorted({f["term"].lower() for f in flags}),
        "flags": flags,
        "segment_flags": seg_flags,
    }


def report_text(scan: dict) -> str:
    lines = [f"Sensitive-language scan report",
             f"format checked: {scan.get('format')}",
             f"total flags: {scan.get('flag_count', 0)}",
             f"distinct terms: {', '.join(scan.get('terms_hit', [])) or '(none)'}", ""]
    for f in scan.get("flags", []):
        lines.append(f"  [{f['term']}]  …{f['context']}…")
    return "\n".join(lines) + "\n"
