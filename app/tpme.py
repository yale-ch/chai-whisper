"""Transcript Provenance Metadata Elements (TPME 1.0) records.

AI4LAM Speech-to-Text WG, doi:10.5281/zenodo.21287085. TPME has no fixed
serialization; we emit JSON with the spec's element names, one record per
transcript version (as the spec intends), linked by ``parent_transcript_id``
(the spec's Example #3 pattern). Multi-step processing is a ``processing`` list
(Example #4). ``history`` returns the whole chain, newest first (Example #2);
exports write it as a separate ``.tpme-history.json`` or embed it in each
sidecar as ``version_history`` (project setting ``tpme_history``).

Field sources (Yale's local profile — still under review, so it all lives here):
- media_id: the recording's name without extension (IIIF: the canvas label)
- media_title, provider, conventions, media_language, human_review_level:
  entered per recording in the editor (``materials.tpme``); provider and
  conventions fall back to the project defaults, media_language to the
  language Whisper ran with
- originating_media_file: the original file name with extension
- human_agent: the recording's editors (plus whoever saved the version)
- processing: workflow/ASR/diarization steps, edits, and the sensitive-language
  scan (with the lexicons used) when one was run on the version
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

# Controlled vocabulary for human_review_level (editing level), chosen by the user.
REVIEW_LEVELS = ["unedited", "partially-edited", "full-edited"]


def app_version() -> str:
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


def _was_edited(material_id: str, v: dict, depth: int = 0) -> bool:
    """Whether a human edit is in this content's lineage (a restore inherits
    the lineage of the version it restored)."""
    src = v.get("source")
    if src == "edit":
        return True
    if src == "restore" and depth < 50:
        frm = (v.get("meta") or {}).get("restored_from")
        prev = db.get_version(material_id, frm) if frm else None
        return _was_edited(material_id, prev, depth + 1) if prev else False
    return False


def _is_latest(material: dict, v: dict) -> bool:
    vers = material.get("versions")
    if vers is None:
        vers = db.list_versions(material["id"])
    return not vers or v["version"] >= vers[0]["version"]


def _review_level(material: dict, v: dict) -> str:
    """The user's choice: the recording's current setting for the latest
    version, the setting saved with older versions; until someone picks one,
    ``partially-edited`` if a human edit is in the lineage, else ``unedited``."""
    chosen = (material.get("tpme") or {}).get("human_review_level") if _is_latest(material, v) else None
    chosen = chosen or (v.get("meta") or {}).get("human_review_level")
    if chosen in REVIEW_LEVELS:
        return chosen
    return "partially-edited" if _was_edited(material["id"], v) else "unedited"


def version_meta(material: dict) -> dict:
    """Snapshot of per-recording TPME choices stored with each new version, so
    older versions keep the review level they had when saved."""
    lvl = (material.get("tpme") or {}).get("human_review_level")
    return {"human_review_level": lvl} if lvl else {}


def normalize_languages(values: list[str]) -> list[str]:
    """User-entered media languages → de-duplicated codes. Whisper-known
    languages (codes or names) map to ISO 639-1; other 2–3 letter ISO 639 codes
    (e.g. ``lad`` for Ladino) are kept as typed."""
    out = []
    for raw in values:
        val = (raw or "").strip()
        if not val:
            continue
        try:
            code = languages.iso639_1(languages.normalize(val))
        except ValueError:
            if not re.fullmatch(r"[A-Za-z]{2,3}", val):
                raise ValueError(f"unrecognized language {val!r} — use an ISO 639 code (e.g. yi / yid) or a name")
            code = val.lower()
        if code and code not in out:
            out.append(code)
    return out


def _data_language(data: dict) -> str | None:
    lang = (data or {}).get("language")
    return languages.iso639_1(lang) if isinstance(lang, str) and lang else None


def _run_language(material: dict) -> str | None:
    """Language of the newest Whisper run (forced or detected)."""
    for r in db.list_versions(material["id"]):
        if r["source"] == "run":
            v = db.get_version(material["id"], r["version"])
            lang = ((v.get("meta") or {}).get("params") or {}).get("language") or _data_language(v["data"])
            if lang:
                return languages.iso639_1(lang)
    return None


def _media_languages(material: dict, data: dict) -> list[str]:
    chosen = (material.get("tpme") or {}).get("media_language")
    if chosen:
        return list(chosen)
    lang = _run_language(material) or _data_language(data)
    return [lang] if lang else []


def originating_media_file(material: dict) -> str | None:
    """The original media file name with extension."""
    mt = material.get("tpme") or {}
    if mt.get("originating_media_file"):
        return mt["originating_media_file"]
    name = material.get("name") or ""
    if Path(name).suffix:
        return name
    ext = Path(material.get("media_path") or "").suffix.replace("-play", "")
    return f"{name}{ext}" if name else None


def media_id(material: dict) -> str | None:
    return Path(material.get("name") or "").stem or material.get("name") or None


def _human_agents(material: dict, v: dict) -> list[str]:
    agents = list(material.get("editors") or [])
    if v.get("editor") and v["editor"].lower() not in (a.lower() for a in agents):
        agents.append(v["editor"])
    return agents


def last_scan(material_id: str, version: int) -> dict | None:
    """Summary of the newest sensitive-language scan exported for a version."""
    for run in db.list_runs(material_id):
        sc = run.get("scan") or {}
        if run.get("version") == version and sc.get("enabled"):
            return sc
    return None


def scan_step(scan_summary: dict) -> dict:
    """TPME processing step documenting a sensitive-language scan."""
    lex = scan_summary.get("lexicons") or [
        {"id": i} for i in scan_summary.get("lexicon_ids") or []] or [{"name": "all lexicons"}]
    n = scan_summary.get("flag_count", 0)
    step = {"application_name": "CHAI-Whisper", "application_version": app_version(),
            "application_type": "sensitive-language scan",
            "application_parameters": {
                "lexicons": [{k: x[k] for k in ("name", "term_count") if x.get(k) is not None} or x for x in lex],
                "scanned_format": scan_summary.get("format")},
            "processing_note": f"{n} flagged term occurrence(s)"
                               + (f": {', '.join(sorted(scan_summary['terms_hit']))}" if scan_summary.get("terms_hit") else "")}
    if scan_summary.get("scanned_at"):
        step["processing_date"] = scan_summary["scanned_at"]
    return step


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
    app = {"application_name": "CHAI-Whisper", "application_version": app_version()}
    if src == "run":
        params = meta.get("params") or {}
        steps = [
            {**app, "application_type": "workflow",
             "processing_note": "GPU transcription job via a chai-engine workflow"
                                + (f" (chai-engine {cv})" if (cv := meta.get("chai_version")) not in (None, "", "unknown") else "")},
            {"application_name": "ffmpeg", "application_type": "audio preprocessing",
             "application_parameters": meta.get("audio_preprocessing") or AUDIO_ARGS,
             "originating_media_file": originating_media_file(material) or meta.get("originating_media_file")},
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
        step["human_agent"] = [v["editor"]]
    if v.get("note"):
        step["processing_note"] = v["note"]
    return [step]


def build_record(material: dict, v: dict, settings: dict | None = None,
                 fmt: str | None = None, file_bytes: bytes | None = None,
                 scan_summary: dict | None = None) -> dict:
    """One TPME record for a stored version row (``db.get_version`` shape).
    Pass ``fmt``/``file_bytes`` when the record is a sidecar for a written file
    (adds ``file_format`` and a ``transcript_checksum`` of those exact bytes),
    and ``scan_summary`` when a sensitive-language scan was run on it."""
    settings = settings or {}
    mt = material.get("tpme") or {}
    data = v.get("data") or {}
    meta = v.get("meta") or {}
    parent = meta.get("parent_version")
    rec = {
        "media_id": media_id(material),
        "media_title": mt.get("media_title"),
        "originating_media_file": originating_media_file(material),
        "provider": mt.get("provider") or settings.get("provider") or None,
        "transcript_id": transcript_id(material, v["version"]),
        "parent_transcript_id": transcript_id(material, parent) if parent else None,
        "modification_date": _iso(v.get("created_at")),
        "transcript_language": _data_language(data),
        "media_language": _media_languages(material, data),
        "type": "transcript",
        "human_review_level": _review_level(material, v),
        "human_agent": _human_agents(material, v),
        "conventions": mt.get("conventions") or settings.get("conventions") or None,
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
    rec["processing"] = _processing(material, v)
    if scan_summary and scan_summary.get("enabled"):
        rec["processing"].append(scan_step(scan_summary))
    rec["metadata_standard"] = SPEC
    return {k: val for k, val in rec.items() if val not in (None, "", [])}


def history(material: dict, settings: dict | None = None, upto: int | None = None) -> list[dict]:
    """Records for every version up to ``upto`` (default: latest), newest first."""
    rows = sorted(db.list_versions(material["id"]), key=lambda r: r["version"], reverse=True)
    out = []
    for r in rows:
        if upto is not None and r["version"] > upto:
            continue
        out.append(build_record(material, db.get_version(material["id"], r["version"]), settings,
                                scan_summary=last_scan(material["id"], r["version"])))
    return out
