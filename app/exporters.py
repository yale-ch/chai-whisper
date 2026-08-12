"""Render a transcript snapshot (data_json) to output formats: VTT, TXT, SRT, JSON.

The canonical inputs are the ``segments`` (with stable ids, start/end, speaker,
text) and the ``speakers`` label map produced by ``app.ingest._normalize``.
"""

from __future__ import annotations

import json

FORMATS = ["vtt", "txt", "srt", "json"]
EXT = {"vtt": ".vtt", "txt": ".txt", "srt": ".srt", "json": ".json"}


def _label(data: dict, key) -> str:
    if not key:
        return ""
    sp = (data.get("speakers") or {}).get(key)
    return (sp or {}).get("label", key) if isinstance(sp, dict) else (sp or key)


def _vtt_ts(sec: float) -> str:
    sec = max(0.0, sec or 0.0)
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _srt_ts(sec: float) -> str:
    return _vtt_ts(sec).replace(".", ",")


def to_vtt(data: dict) -> str:
    """WebVTT. Speaker attribution uses the `<v Label>` voice tag."""
    lines = ["WEBVTT", ""]
    for i, s in enumerate(data.get("segments", []), 1):
        lines.append(str(i))
        lines.append(f"{_vtt_ts(s.get('start'))} --> {_vtt_ts(s.get('end'))}")
        label = _label(data, s.get("speaker"))
        text = (s.get("text") or "").strip()
        lines.append(f"<v {label}>{text}" if label else text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_srt(data: dict) -> str:
    lines = []
    for i, s in enumerate(data.get("segments", []), 1):
        label = _label(data, s.get("speaker"))
        text = (s.get("text") or "").strip()
        body = f"{label}: {text}" if label else text
        lines.append(f"{i}\n{_srt_ts(s.get('start'))} --> {_srt_ts(s.get('end'))}\n{body}\n")
    return "\n".join(lines)


def to_txt(data: dict) -> str:
    """Plain reading text; a speaker header is emitted whenever the speaker changes."""
    out, last = [], object()
    for s in data.get("segments", []):
        spk = s.get("speaker")
        if spk != last and _label(data, spk):
            out.append(f"\n{_label(data, spk)}:")
            last = spk
        out.append((s.get("text") or "").strip())
    return "\n".join(out).strip() + "\n"


def to_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


RENDERERS = {"vtt": to_vtt, "txt": to_txt, "srt": to_srt, "json": to_json}


def render(fmt: str, data: dict) -> str:
    if fmt not in RENDERERS:
        raise ValueError(f"unknown format: {fmt}")
    return RENDERERS[fmt](data)
