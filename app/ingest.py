"""Ingest local A/V into materials, and build/parse transcript snapshots.

Materials reference a local path (bytes are never copied). Transcript snapshots
(``data_json``) are built from a DGX whisper result, or imported from an existing
``.json`` (our result shape) or ``.srt``. Every snapshot gets stable segment ids
and a speakers map (editable label + color) so the viewer and diff engine work.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from src import audio
from . import db, media

# Speaker chip colors (Yale-ish palette), cycled by speaker order.
SPEAKER_COLORS = ["#286DC0", "#BD5319", "#5F712D", "#8B4A8B", "#2C7A7B",
                  "#00356B", "#978D85", "#B8860B"]


def _build_speakers(segments: list[dict]) -> dict:
    keys = []
    for s in segments:
        k = s.get("speaker")
        if k and k not in keys:
            keys.append(k)
    return {k: {"label": k, "color": SPEAKER_COLORS[i % len(SPEAKER_COLORS)]}
            for i, k in enumerate(keys)}


def _normalize(segments: list[dict], language=None, duration=None) -> dict:
    """Attach stable ids and assemble a transcript data_json from raw segments."""
    segs = []
    for i, s in enumerate(segments):
        segs.append({
            "id": s.get("id") or f"seg-{i}",
            "start": float(s.get("start", 0) or 0),
            "end": float(s.get("end", 0) or 0),
            "speaker": s.get("speaker"),
            "text": (s.get("text") or "").strip(),
            "words": s.get("words") or [],
        })
    return {
        "kind": "transcript",
        "language": language,
        "duration": duration,
        "speakers": _build_speakers(segs),
        "segments": segs,
    }


def data_from_dgx_result(result: dict) -> dict:
    """Map a CHAI-Whisper DGX result (chai ItemResult.to_json) into a snapshot."""
    md = result.get("metadata", {}) if isinstance(result, dict) else {}
    segs = md.get("segments", [])
    data = _normalize(segs, language=md.get("language"), duration=md.get("duration"))
    data["source_meta"] = {
        "model": md.get("model"),
        "diarize": md.get("diarize"),
        "num_speakers_detected": md.get("num_speakers_detected"),
        "language_probability": md.get("language_probability"),
    }
    return data


# ---------------------------------------------------------------- imports
def parse_json_import(raw: bytes) -> dict:
    """Accept either our stored snapshot shape or a raw DGX result JSON."""
    obj = json.loads(raw.decode("utf-8"))
    if isinstance(obj, dict) and obj.get("kind") == "transcript" and "segments" in obj:
        return _normalize(obj["segments"], language=obj.get("language"),
                          duration=obj.get("duration"))
    if isinstance(obj, dict) and "metadata" in obj:  # a DGX result
        return data_from_dgx_result(obj)
    if isinstance(obj, dict) and "segments" in obj:   # bare {segments:[...]}
        return _normalize(obj["segments"], language=obj.get("language"),
                          duration=obj.get("duration"))
    raise ValueError("unrecognized JSON transcript format")


_SRT_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _srt_seconds(ts: str) -> float:
    m = _SRT_TS.search(ts)
    if not m:
        return 0.0
    h, mn, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mn * 60 + s + ms / 1000.0


def parse_srt(raw: bytes) -> dict:
    """Parse an .srt file. Speaker prefixes like 'SPEAKER_00: text' are honored."""
    text = raw.decode("utf-8", "replace")
    blocks = re.split(r"\n\s*\n", text.strip())
    segments = []
    for b in blocks:
        lines = [ln for ln in b.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        idx = 0
        if lines[0].strip().isdigit():
            idx = 1
        if "-->" not in lines[idx]:
            continue
        start_s, end_s = lines[idx].split("-->")
        body = " ".join(lines[idx + 1:]).strip()
        speaker = None
        m = re.match(r"([A-Za-z0-9_ .-]{1,40}):\s*(.*)", body)
        if m:
            speaker, body = m.group(1).strip(), m.group(2).strip()
        segments.append({"start": _srt_seconds(start_s), "end": _srt_seconds(end_s),
                         "speaker": speaker, "text": body})
    if not segments:
        raise ValueError("no subtitle blocks found in .srt")
    return _normalize(segments)


# ---------------------------------------------------------------- registration
MEDIA_STORE = db.DATA_DIR / "media"


def register_upload(project_id: str, filename: str, raw: bytes, source: str = "file") -> dict:
    """Save an uploaded (browser-picked) A/V file into the project's media store
    and register it. Browsers can't reveal the original path, so picked media is
    copied here; the DB still stores only the path, never the bytes."""
    ext = Path(filename).suffix.lower()
    if ext not in audio.AV_EXTENSIONS:
        raise ValueError(f"not a recognized audio/video file: {filename}")
    dest_dir = MEDIA_STORE / project_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{db.new_id()}{ext}"
    dest.write_bytes(raw)
    info = media.probe(str(dest))
    return db.create_material(
        project_id=project_id, name=os.path.basename(filename), media_path=str(dest),
        media_type=info["media_type"], duration=info["duration"],
        width=info["width"], height=info["height"], source=source)


def _register_one(project_id: str, path: str, source: str = "file") -> dict:
    info = media.probe(path)
    return db.create_material(
        project_id=project_id, name=os.path.basename(path), media_path=os.path.abspath(path),
        media_type=info["media_type"], duration=info["duration"],
        width=info["width"], height=info["height"], source=source)


def register_path(project_id: str, path: str) -> list[dict]:
    """Register a single A/V file, or every A/V file directly inside a folder."""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"path does not exist: {path}")
    if p.is_dir():
        files = sorted(f for f in p.iterdir()
                       if f.is_file() and f.suffix.lower() in audio.AV_EXTENSIONS)
        if not files:
            raise ValueError(f"no audio/video files found in folder: {path}")
        return [_register_one(project_id, str(f), source="folder") for f in files]
    if p.suffix.lower() not in audio.AV_EXTENSIONS:
        raise ValueError(f"not a recognized audio/video file: {path}")
    return [_register_one(project_id, str(p), source="file")]


# ---------------------------------------------------------------- IIIF
MEDIA_CACHE = db.DATA_DIR / "media_cache"


def _iiif_av_bodies(manifest: dict):
    """Yield (label, content_url) for Sound/Video annotation bodies (v2 + v3)."""
    def label_text(label, fallback):
        if isinstance(label, str) and label.strip():
            return label.strip()
        if isinstance(label, dict):
            for vals in label.values():
                if isinstance(vals, list) and vals:
                    return str(vals[0])
        return fallback

    # v3: items → AnnotationPage.items → Annotation(body type Sound/Video)
    for i, canvas in enumerate(manifest.get("items", []) or []):
        lbl = label_text(canvas.get("label"), f"item {i + 1:03d}")
        for page in canvas.get("items", []) or []:
            for anno in page.get("items", []) or []:
                body = anno.get("body", {})
                if isinstance(body, list):
                    body = body[0] if body else {}
                if body.get("type") in ("Sound", "Video", "Audio"):
                    url = body.get("id") or body.get("@id")
                    if url:
                        yield lbl, url
    # v2 fallback: mediaSequences → elements
    for ms in manifest.get("mediaSequences", []) or []:
        for j, el in enumerate(ms.get("elements", []) or []):
            url = el.get("@id") or el.get("id")
            if url:
                yield label_text(el.get("label"), f"item {j + 1:03d}"), url


def register_iiif(project_id: str, manifest_url: str, progress=None) -> list[dict]:
    """Download Sound/Video bodies from a IIIF manifest into the project media
    cache and register them (source='iiif'). `progress(done, total, msg)` optional."""
    import mimetypes
    import requests

    resp = requests.get(manifest_url, timeout=60, headers={"Accept": "application/json"})
    resp.raise_for_status()
    manifest = resp.json()
    bodies = list(_iiif_av_bodies(manifest))
    if not bodies:
        raise ValueError("no audio/video content found in the IIIF manifest")

    cache = MEDIA_CACHE / project_id
    cache.mkdir(parents=True, exist_ok=True)
    created = []
    for i, (label, url) in enumerate(bodies):
        if progress:
            progress(i, len(bodies), f"downloading {label}")
        ext = os.path.splitext(url.split("?")[0])[1]
        if not ext:
            ext = mimetypes.guess_extension((requests.head(url, timeout=30).headers.get("Content-Type") or "").split(";")[0]) or ".mp4"
        dest = cache / f"{db.new_id()}{ext}"
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1024 * 256):
                    fh.write(chunk)
        info = media.probe(str(dest))
        created.append(db.create_material(
            project_id, name=label, media_path=str(dest), media_type=info["media_type"],
            duration=info["duration"], width=info["width"], height=info["height"],
            source="iiif", source_url=manifest_url))
    if progress:
        progress(len(bodies), len(bodies), "done")
    return created
