"""Sensitive-language scan: flag lexicon terms in a transcript.

Matching is case-insensitive and whole-word (word boundaries), and supports
multi-word phrases. Terms come from project lexicons (plain lists). The scan can
run over any rendered output format (via app.exporters) and also maps hits back
to segment ids for editor highlighting.
"""

from __future__ import annotations

import re

from . import exporters
from .textio import decode_text


def parse_terms(raw: bytes | str) -> list[str]:
    """Parse an uploaded lexicon: one term per line, blanks/`#` comments dropped."""
    text = decode_text(raw)
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
                "seg_start": s.get("start"), "seg_end": s.get("end"),
                "context": text.strip(),
            })
    return flags


CUE_FORMATS = ("vtt", "srt")


def scan_cues(rendered: str, terms: list[str], data: dict) -> list[dict]:
    """Scan a rendered WebVTT/SRT file cue by cue, reporting each hit with the
    cue's number, start/end timing (as written in the file), speaker, and the
    cue's entire text block. Cues map 1:1 onto segments, so each hit also
    carries ``seg_id`` for click-to-seek in the editor."""
    from .ingest import parse_cues  # local: ingest pulls in media/audio helpers

    rx = compile_terms(terms)
    if rx is None:
        return []
    segs = data.get("segments", [])
    flags = []
    for cue in parse_cues(rendered):
        # Scan what a reader of the file sees, speaker prefix included (as the
        # srt/webvtt libraries expose a cue's content).
        body = f"{cue['speaker']}: {cue['text']}" if cue["speaker"] else cue["text"]
        seg = segs[cue["index"] - 1] if cue["index"] - 1 < len(segs) else {}
        for m in rx.finditer(body):
            flags.append({
                "term": m.group(0), "cue": cue["index"],
                "start_time": cue["start"], "end_time": cue["end"],
                "start_ts": cue["start_ts"], "end_ts": cue["end_ts"],
                "speaker": cue["speaker"], "text": cue["text"],
                "context": body, "seg_id": seg.get("id"),
            })
    return flags


def scan_data(data: dict, terms: list[str], fmt: str = "txt") -> dict:
    """Scan a rendered format's text and also collect per-segment hits.
    WebVTT/SRT are scanned per cue (with timings); other formats as running text.
    Returns {format, flag_count, terms_hit, flags[], segment_flags[]}."""
    fmt = fmt if fmt in exporters.RENDERERS else "txt"
    rendered = exporters.render(fmt, data)
    flags = scan_cues(rendered, terms, data) if fmt in CUE_FORMATS else scan_text(rendered, terms)
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
        if "cue" in f:
            lines.append(f"  #{f['cue']}  {f['start_ts']} --> {f['end_ts']}  [{f['term']}]  {f['context']}")
        else:
            lines.append(f"  [{f['term']}]  …{f['context']}…")
    return "\n".join(lines) + "\n"


CSV_COLUMNS = ["format", "term", "cue", "start", "end", "speaker", "text"]


def report_csv(scan: dict) -> str:
    """One row per flag. start/end/cue/speaker are filled for WebVTT/SRT scans;
    for txt/json scans ``text`` holds the surrounding context. Written with a
    UTF-8 BOM so Excel opens accented text correctly."""
    import csv
    import io

    buf = io.StringIO()
    buf.write("\ufeff")
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for f in scan.get("flags", []):
        if "cue" in f:
            w.writerow([scan.get("format"), f["term"], f["cue"], f["start_ts"], f["end_ts"],
                        f.get("speaker") or "", f["text"]])
        else:
            w.writerow([scan.get("format"), f["term"], "", "", "", "", f["context"]])
    return buf.getvalue()
