"""Transcript Provenance Metadata Elements (TPME 1.0) records.

AI4LAM Speech-to-Text WG, doi:10.5281/zenodo.21287085. TPME has no fixed
serialization; we emit JSON with the spec's element names, one record per
transcript version (as the spec intends), linked by ``parent_transcript_id``
(the spec's Example #3 pattern). Multi-step processing is a ``processing`` list
(Example #4). ``history`` returns the whole chain, newest first (Example #2).

Everything here is derived from what the workspace already stores — version
source/note/editor/meta, the transcript data, the material, and project
settings (``provider``). Adjust field choices in this one module.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from . import db, languages

SPEC = "TPME 1.0 (doi:10.5281/zenodo.21287085)"
FILE_FORMATS = {"vtt": "WebVTT", "srt": "SRT", "txt": "text/plain", "json": "application/json",
                "csv": "text/csv"}

# How src/audio.extract_opus prepares audio (only this leaves the machine).
AUDIO_ARGS = "-vn -ac 1 -c:a libopus -b:a 16k -f ogg"

# Controlled vocabulary for human_review_level (local choice, per the spec).
REVIEW_LEVELS = {"run": "machine-generated", "edit": "human-corrected", "import": "unknown-source"}


def _app_version() -> str:
    try:
        from importlib.metadata import version
        return version("chai-whisper")
    except Exception:
        return "0.1.0"


def _iso(created_at: str | None) -> str | None:
    """SQLite ``datetime('now')`` (UTC, no zone) → ISO 8601 with ``Z``."""
    if not created_at:
        return None
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except ValueError:
        return created_at


def transcript_id(material: dict, version: int) -> str:
    return f"{material['id']}-v{version}"


def _review_level(material_id: str, v: dict, depth: int = 0) -> str:
    """The content's review level follows its lineage: any human edit makes it
    corrected; a restore inherits the level of the version it restored."""
    src = v.get("source")
    if src == "restore" and depth < 50:
        frm = (v.get("meta") or {}).get("restored_from")
        prev = db.get_version(material_id, frm) if frm else None
        return _review_level(material_id, prev, depth + 1) if prev else "unknown"
    if src == "edit":
        return REVIEW_LEVELS["edit"]
    return REVIEW_LEVELS.get(src, "unknown")


def _features(data: dict, meta: dict, source: str) -> dict:
    segs = data.get("segments") or []
    speakers = data.get("speakers") or {}
    # Real names (not generic diarization labels like SPEAKER_00 / Speaker 1).
    generic = re.compile(r"^(speaker|spk|spkr|s)[\s_-]*\d+$", re.I)
    named = any(not generic.match(str((sp or {}).get("label") or key).strip())
                for key, sp in speakers.items() if isinstance(sp, dict))
    return {
        "time_alignment": bool(segs) and meta.get("timed", True),
        "word_level_timestamps": any(s.get("words") for s in segs),
        "diarization": bool(speakers),
        "speaker_identification": named,
        "segment_count": len(segs),
    }


def _processing(material: dict, v: dict) -> list[dict]:
    meta = v.get("meta") or {}
    src = v.get("source")
    app = {"application_name": "CHAI-Whisper", "application_version": _app_version()}
    if src == "run":
        params = meta.get("params") or {}
        steps = [
            {**app, "application_type": "workflow",
             "processing_note": "GPU transcription job via a chai-engine workflow"
                                + (f" (chai-engine {cv})" if (cv := meta.get("chai_version")) not in (None, "", "unknown") else "")},
            {"application_name": "ffmpeg", "application_type": "audio preprocessing",
             "application_parameters": meta.get("audio_preprocessing") or AUDIO_ARGS,
             "originating_media_file": meta.get("originating_media_file") or material.get("name")},
            {"application_name": "faster-whisper", "application_type": "ASR",
             "application_provider": "SYSTRAN", "application_repo": "https://github.com/SYSTRAN/faster-whisper",
             "inference_model": params.get("model") or v.get("model"),
             "application_parameters": {k: val for k, val in params.items() if val is not None}},
        ]
        if params.get("diarize"):
            steps.append({"application_name": "pyannote.audio", "application_type": "diarization",
                          "application_repo": "https://github.com/pyannote/pyannote-audio"})
        return steps
    step = {**app, "application_type": {"edit": "transcript editor", "import": "transcript import",
                                        "restore": "version restore"}.get(src, src)}
    if v.get("editor"):
        step["human_agent"] = v["editor"]
    if v.get("note"):
        step["processing_note"] = v["note"]
    return [step]


def build_record(material: dict, v: dict, settings: dict | None = None,
                 fmt: str | None = None, file_bytes: bytes | None = None) -> dict:
    """One TPME record for a stored version row (``db.get_version`` shape).
    Pass ``fmt``/``file_bytes`` when the record is a sidecar for a written file
    (adds ``file_format`` and a ``transcript_checksum`` of those exact bytes)."""
    settings = settings or {}
    data = v.get("data") or {}
    meta = v.get("meta") or {}
    lang = languages.iso639_1(data.get("language") if isinstance(data.get("language"), str) else None)
    parent = meta.get("parent_version")
    rec = {
        "media_id": material.get("source_url") if material.get("source") == "iiif" else material.get("name"),
        "media_title": Path(material.get("name") or "").stem or None,
        "transcript_id": transcript_id(material, v["version"]),
        "parent_transcript_id": transcript_id(material, parent) if parent else None,
        "modification_date": _iso(v.get("created_at")),
        "provider": settings.get("provider") or None,
        "transcript_language": lang,
        "media_language": lang if v.get("source") == "run" else None,
        "type": "transcript",
        "human_review_level": _review_level(material["id"], v),
        "features": _features(data, meta, v.get("source")),
    }
    if fmt:
        rec["file_format"] = FILE_FORMATS.get(fmt, fmt)
    if file_bytes is not None:
        rec["transcript_checksum"] = "sha256:" + hashlib.sha256(file_bytes).hexdigest()
    open_notes = sum(1 for n in data.get("notes") or [] if n.get("status") != "resolved")
    if open_notes:
        rec["characteristics_note"] = f"{open_notes} open review note(s)"
    if v.get("source") == "import":
        rec["origin_history"] = (f"Imported from {meta.get('import_filename')}"
                                 if meta.get("import_filename") else v.get("note") or "Imported")
    if v.get("editor"):
        rec["human_agent"] = v["editor"]
    rec["processing"] = _processing(material, v)
    rec["metadata_standard"] = SPEC
    return {k: val for k, val in rec.items() if val not in (None, "", [])}


def history(material: dict, settings: dict | None = None, upto: int | None = None) -> list[dict]:
    """Records for every version up to ``upto`` (default: latest), newest first."""
    rows = sorted(db.list_versions(material["id"]), key=lambda r: r["version"], reverse=True)
    out = []
    for r in rows:
        if upto is not None and r["version"] > upto:
            continue
        out.append(build_record(material, db.get_version(material["id"], r["version"]), settings))
    return out
