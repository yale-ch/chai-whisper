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
from .textio import decode_text

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
    obj = json.loads(decode_text(raw))
    if isinstance(obj, dict) and obj.get("kind") == "transcript" and "segments" in obj:
        data = _normalize(obj["segments"], language=obj.get("language"),
                          duration=obj.get("duration"))
        # Our own export: keep edited speaker labels/colors and review notes.
        for k, v in (obj.get("speakers") or {}).items():
            if k in data["speakers"] and isinstance(v, dict):
                data["speakers"][k].update({x: v[x] for x in ("label", "color") if v.get(x)})
        if isinstance(obj.get("notes"), list):
            data["notes"] = obj["notes"]
        return data
    if isinstance(obj, dict) and "metadata" in obj:  # a DGX result
        return data_from_dgx_result(obj)
    if isinstance(obj, dict) and "segments" in obj:   # bare {segments:[...]}
        return _normalize(obj["segments"], language=obj.get("language"),
                          duration=obj.get("duration"))
    raise ValueError("unrecognized JSON transcript format")


# A cue timing like 01:02:03.456 / 02:03,456 / 1:02:03.4 (hours optional, as in WebVTT).
_CUE_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?")
_CUE_TIMING = re.compile(rf"^\s*({_CUE_TS.pattern})\s*-->\s*({_CUE_TS.pattern})")
_TAG = re.compile(r"</?[^>]+>")
_VOICE = re.compile(r"<v(?:\.[\w.-]+)?\s+([^>]+)>")
# "SPEAKER_00: text", "Interviewer: text", "Jane Doe: text" (label must start with a letter).
_SPEAKER_PREFIX = re.compile(r"^\[?([A-Za-z][\w .'-]{0,39}?)\]?:\s+(.*)$", re.S)


def _ts_seconds(ts: str) -> float:
    m = _CUE_TS.search(ts or "")
    if not m:
        return 0.0
    h, mn, s, frac = m.groups()
    return (int(h or 0) * 3600 + int(mn) * 60 + int(s)
            + (int(frac.ljust(3, "0")) / 1000.0 if frac else 0.0))


# Kept for callers/tests that used the SRT-specific name.
_srt_seconds = _ts_seconds


def _split_speaker(body: str):
    m = _SPEAKER_PREFIX.match(body)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, body


def parse_cues(text: str) -> list[dict]:
    """Parse WebVTT or SRT text into cues.

    Each cue: {index, start, end, start_ts, end_ts, speaker, text}. ``start_ts``/
    ``end_ts`` are the timings exactly as written in the file. WebVTT header,
    NOTE/STYLE/REGION blocks, cue settings, and inline markup are handled;
    speakers come from ``<v Name>`` voice tags or a ``Name:`` prefix.
    """
    import html

    blocks = re.split(r"\n[ \t]*\n", decode_text(text).strip())
    cues = []
    for b in blocks:
        lines = [ln for ln in b.split("\n") if ln.strip()]
        if not lines:
            continue
        head = lines[0].strip()
        if head.startswith("WEBVTT") or head.split(" ")[0] in ("NOTE", "STYLE", "REGION"):
            # The WEBVTT header block may run straight into the first cue.
            lines = lines[1:] if head.startswith("WEBVTT") else []
            while lines and "-->" not in lines[0] and (len(lines) < 2 or "-->" not in lines[1]):
                lines = lines[1:]
            if not lines:
                continue
        t_idx = next((i for i, ln in enumerate(lines[:2]) if "-->" in ln), None)
        if t_idx is None:
            continue
        m = _CUE_TIMING.match(lines[t_idx])
        if not m:
            continue
        start_ts, end_ts = m.group(1), m.group(6)
        raw_body = "\n".join(lines[t_idx + 1:]).strip()
        speaker = None
        v = _VOICE.search(raw_body)
        if v:
            speaker = v.group(1).strip()
        body = html.unescape(_TAG.sub("", raw_body)).replace("\n", " ").strip()
        body = re.sub(r"\s{2,}", " ", body)
        if speaker is None:
            speaker, body = _split_speaker(body)
        cues.append({"index": len(cues) + 1, "start": _ts_seconds(start_ts), "end": _ts_seconds(end_ts),
                     "start_ts": start_ts, "end_ts": end_ts, "speaker": speaker, "text": body})
    return cues


def parse_srt(raw: bytes | str) -> dict:
    """Parse an .srt file. Speaker prefixes like 'SPEAKER_00: text' are honored."""
    cues = parse_cues(decode_text(raw))
    if not cues:
        raise ValueError("no subtitle cues (\"00:00:01,000 --> 00:00:02,000\") found")
    return _normalize(cues)


def parse_vtt(raw: bytes | str) -> dict:
    """Parse a WebVTT file (voice tags ``<v Name>`` become speakers)."""
    text = decode_text(raw)
    cues = parse_cues(text)
    if not cues:
        raise ValueError("no WebVTT cues (\"00:00:01.000 --> 00:00:02.000\") found")
    return _normalize(cues)


# ---- plain text: home-grown timestamp detection
# A bare time token: 1:23, 01:23, 1:02:03, 01:02:03.5, 00:01:02,345
_TXT_TS = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"
# [00:01:23] text | (1:23) Speaker: text | 00:01:23 - 00:01:30 text | 0:12 --> 0:15 text
_TXT_LEAD = re.compile(
    rf"^\s*[\[(<{{]?\s*({_TXT_TS})\s*(?:(?:-->|–|—|-|to)\s*({_TXT_TS}))?\s*[\])>}}]?\s*[:\-–—|]?\s*(.*)$")
# Speaker header with the time at line end (Otter/Zoom style): "Speaker 1  0:03"
_TXT_SPK_HEADER = re.compile(rf"^\s*([A-Za-z][\w.'-]*(?: [\w.'-]+){{0,3}})\s+[\[(]?({_TXT_TS})[\])]?\s*:?\s*$")
# Speaker with a bracketed time, text inline or on following lines: "Jane Doe (00:01:23): …"
_TXT_SPK_INLINE = re.compile(rf"^\s*([A-Za-z][\w .'-]{{0,49}}?)\s*[\[(]({_TXT_TS})[\])]\s*:?\s*(.*)$")


def _txt_ts_seconds(tok: str) -> float:
    tok = tok.replace(",", ".")
    parts = tok.split(":")
    secs = float(parts[-1])
    mins = int(parts[-2])
    hrs = int(parts[-3]) if len(parts) == 3 else 0
    return hrs * 3600 + mins * 60 + secs


def parse_txt(raw: bytes | str, duration: float | None = None) -> tuple[dict, bool]:
    """Parse a plain-text transcript. Returns ``(data, timed)``.

    Lines that start with a timestamp (``[00:01:23] …``, ``(1:23) Name: …``,
    ``00:01:23 - 00:01:30 …``) or a speaker header carrying one (``Speaker 1  0:03``,
    ``Jane (00:01:23): …``) begin a new segment; untimed lines continue it.
    With no timestamps at all, paragraphs become segments whose timings are
    *estimated* by spreading them across ``duration`` by length (``timed`` is
    False) — fix them afterwards with the timing editor.
    """
    text = decode_text(raw).strip()
    if not text:
        raise ValueError("the text file is empty")
    segs: list[dict] = []
    preamble: list[str] = []
    n_timed = 0
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        spk = start = end = rest = None
        m = _TXT_LEAD.match(s)
        if m:
            start, end, rest = m.group(1), m.group(2), m.group(3)
        elif (m := _TXT_SPK_HEADER.match(s)):
            spk, start, rest = m.group(1).strip(), m.group(2), ""
        elif (m := _TXT_SPK_INLINE.match(s)):
            spk, start, rest = m.group(1).strip(), m.group(2), m.group(3)
        if start is not None:
            n_timed += 1
            rest = (rest or "").strip()
            if spk is None and rest:
                spk, rest = _split_speaker(rest)
            segs.append({"start": _txt_ts_seconds(start),
                         "end": _txt_ts_seconds(end) if end else None,
                         "speaker": spk, "text": rest})
        elif segs:
            segs[-1]["text"] = (segs[-1]["text"] + " " + s).strip()
        else:
            preamble.append(s)

    # One stray time ("we met at 10:30") doesn't make a timed transcript.
    if n_timed >= 2:
        if preamble:
            segs.insert(0, {"start": 0.0, "end": None, "speaker": None, "text": " ".join(preamble)})
        segs.sort(key=lambda x: x["start"])
        for i, sg in enumerate(segs):
            if sg["end"] is None or sg["end"] < sg["start"]:
                nxt = segs[i + 1]["start"] if i + 1 < len(segs) else None
                guess = sg["start"] + max(1.0, len(sg["text"].split()) / 2.5)
                sg["end"] = nxt if nxt is not None else (min(guess, duration) if duration and duration > sg["start"] else guess)
        return _normalize(segs, duration=duration), True

    # Untimed: paragraphs (or lines, if there are no blank-line breaks).
    segs = []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) == 1:
        paras = [ln.strip() for ln in text.split("\n") if ln.strip()]
    chunks = []
    for p in paras:
        spk, body = _split_speaker(" ".join(p.split()))
        chunks.append((spk, body))
    total_words = sum(max(1, len(b.split())) for _, b in chunks)
    span = duration or total_words / 2.5  # ~150 wpm when the media length is unknown
    t = 0.0
    for spk, body in chunks:
        d = span * max(1, len(body.split())) / total_words
        segs.append({"start": round(t, 3), "end": round(t + d, 3), "speaker": spk, "text": body})
        t += d
    return _normalize(segs, duration=duration), False


def parse_transcript(raw: bytes, filename: str = "", duration: float | None = None) -> tuple[dict, str, bool]:
    """Detect the format (extension first, then content) and parse.
    Returns ``(data, format_label, timed)``."""
    ext = Path(filename or "").suffix.lower()
    text = decode_text(raw)
    head = text.lstrip()[:200]
    if ext == ".json" or (ext not in (".vtt", ".srt", ".txt") and head[:1] in "{["):
        return parse_json_import(text.encode("utf-8")), "json", True
    if ext == ".vtt" or head.startswith("WEBVTT"):
        return parse_vtt(text), "vtt", True
    has_cues = re.search(r"^\s*\S+\s*-->\s*\S+", text, re.M) is not None
    if ext == ".srt" or has_cues:
        return parse_srt(text), "srt", True
    data, timed = parse_txt(text, duration=duration)
    return data, "txt", timed


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
    path, info = media.ensure_browser_audio(str(dest))
    return db.create_material(
        project_id=project_id, name=os.path.basename(filename), media_path=path,
        media_type=info["media_type"], duration=info["duration"],
        width=info["width"], height=info["height"], source=source, has_audio=info["has_audio"])


def _register_one(project_id: str, path: str, source: str = "file") -> dict:
    info = media.probe(path)
    return db.create_material(
        project_id=project_id, name=os.path.basename(path), media_path=os.path.abspath(path),
        media_type=info["media_type"], duration=info["duration"],
        width=info["width"], height=info["height"], source=source, has_audio=info["has_audio"])


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


def _label_text(label, fallback):
    if isinstance(label, str) and label.strip():
        return label.strip()
    if isinstance(label, dict):
        for vals in label.values():
            if isinstance(vals, list) and vals:
                return str(vals[0])
    return fallback


def _flatten_bodies(body) -> list[dict]:
    """A painting body may be a resource, a list, or a ``Choice`` of alternatives
    (e.g. several renditions of the same video). Flatten to plain resources."""
    if isinstance(body, list):
        return [r for b in body for r in _flatten_bodies(b)]
    if not isinstance(body, dict):
        return []
    if body.get("type") == "Choice":
        return [r for b in body.get("items", []) or [] for r in _flatten_bodies(b)]
    url = body.get("id") or body.get("@id")
    kind = body.get("type") or body.get("@type") or ""
    fmt = (body.get("format") or "").lower()
    kind = {"dctypes:Sound": "Sound", "dctypes:MovingImage": "Video"}.get(kind, kind)
    if not kind and fmt:
        kind = "Video" if fmt.startswith("video/") else "Sound" if fmt.startswith("audio/") else ""
    if url and kind in ("Sound", "Audio", "Video"):
        return [{"url": url, "type": "Video" if kind == "Video" else "Sound", "format": fmt}]
    return []


def _iiif_av_canvases(manifest: dict):
    """Yield (label, videos[], sounds[]) per canvas (v3), or per v2 media element.
    Each list holds {url, type, format} alternatives in manifest order."""
    for i, canvas in enumerate(manifest.get("items", []) or []):
        lbl = _label_text(canvas.get("label"), f"item {i + 1:03d}")
        res = []
        for page in canvas.get("items", []) or []:
            for anno in page.get("items", []) or []:
                res += _flatten_bodies(anno.get("body"))
        if res:
            yield (lbl, [r for r in res if r["type"] == "Video"], [r for r in res if r["type"] == "Sound"])
    for ms in manifest.get("mediaSequences", []) or []:
        for j, el in enumerate(ms.get("elements", []) or []):
            res = _flatten_bodies({**el, "type": el.get("@type") or el.get("type")}) or (
                [{"url": el.get("@id") or el.get("id"), "type": "Video", "format": ""}]
                if (el.get("@id") or el.get("id")) else [])
            if res:
                yield (_label_text(el.get("label"), f"item {j + 1:03d}"),
                       [r for r in res if r["type"] == "Video"], [r for r in res if r["type"] == "Sound"])


def _iiif_av_bodies(manifest: dict):
    """Yield (label, content_url) for every Sound/Video body (kept for callers/tests)."""
    for lbl, videos, sounds in _iiif_av_canvases(manifest):
        for r in videos + sounds:
            yield lbl, r["url"]


_STREAM_FORMATS = ("application/x-mpegurl", "application/vnd.apple.mpegurl", "application/dash+xml")


def _is_stream(r: dict) -> bool:
    path = r["url"].split("?")[0].lower()
    return path.endswith((".m3u8", ".mpd")) or r.get("format") in _STREAM_FORMATS


def _download(r: dict, cache: Path) -> str:
    """Fetch one IIIF media resource into the cache. Adaptive streams (HLS/DASH)
    keep audio and video as separate renditions, so they go through ffmpeg,
    which muxes the best of both into a single .mp4."""
    import mimetypes
    import subprocess
    import requests

    if _is_stream(r):
        audio.ensure_ffmpeg()
        dest = cache / f"{db.new_id()}.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", r["url"], "-map", "0:v:0?", "-map", "0:a:0?",
                        "-c", "copy", "-bsf:a", "aac_adtstoasc", str(dest)],
                       capture_output=True, check=True, timeout=3600)
        return str(dest)
    ext = os.path.splitext(r["url"].split("?")[0])[1]
    if not ext or len(ext) > 6:
        ctype = r.get("format") or (requests.head(r["url"], timeout=30, allow_redirects=True)
                                    .headers.get("Content-Type") or "").split(";")[0]
        ext = mimetypes.guess_extension(ctype) or (".mp4" if r["type"] == "Video" else ".mp3")
    dest = cache / f"{db.new_id()}{ext}"
    with requests.get(r["url"], stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(1024 * 256):
                fh.write(chunk)
    return str(dest)


def _mux(video_path: str, audio_path: str, cache: Path) -> str:
    """Combine a silent video with a separately-published audio track."""
    import subprocess

    dest = cache / f"{db.new_id()}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video_path, "-i", audio_path,
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                    "-shortest", "-movflags", "+faststart", str(dest)], capture_output=True, check=True)
    for pth in (video_path, audio_path):
        Path(pth).unlink(missing_ok=True)
    return str(dest)


def _fetch_canvas(videos: list[dict], sounds: list[dict], cache: Path, note) -> list[tuple[str, dict]]:
    """Download a canvas's media → [(path, probe-info)], making sure a video
    has sound when the manifest offers it anywhere on the canvas: try each video
    alternative (``Choice``) until one has audio, else mux in a Sound body."""
    out: list[tuple[str, dict]] = []
    if videos:
        path, info = None, None
        # Non-streaming renditions first — they're single files and fast to fetch.
        for r in sorted(videos, key=_is_stream):
            note(f"downloading {r['url'].rsplit('/', 1)[-1]}")
            cand = _download(r, cache)
            cinfo = media.probe(cand)
            if path is None or (cinfo["has_audio"] and not info["has_audio"]):
                if path:
                    Path(path).unlink(missing_ok=True)
                path, info = cand, cinfo
            else:
                Path(cand).unlink(missing_ok=True)
            if info["has_audio"]:
                break
        used_sound = False
        if info["has_audio"] is False and sounds:
            note("video has no audio track — adding the canvas's separate audio")
            snd = _download(sounds[0], cache)
            path = _mux(path, snd, cache)
            info = media.probe(path)
            used_sound = True
        out.append(media.ensure_browser_audio(path, info))
        if used_sound:
            return out
    # Audio-only canvas, or extra Sound bodies beside a video that has its own sound.
    for r in sounds:
        note(f"downloading {r['url'].rsplit('/', 1)[-1]}")
        out.append(media.ensure_browser_audio(_download(r, cache)))
    return out


def register_iiif(project_id: str, manifest_url: str, progress=None) -> list[dict]:
    """Download Sound/Video bodies from a IIIF manifest into the project media
    cache and register them (source='iiif'). `progress(done, total, msg)` optional."""
    import requests

    resp = requests.get(manifest_url, timeout=60, headers={"Accept": "application/json"})
    resp.raise_for_status()
    manifest = json.loads(decode_text(resp.content))
    canvases = list(_iiif_av_canvases(manifest))
    if not canvases:
        raise ValueError("no audio/video content found in the IIIF manifest")

    cache = MEDIA_CACHE / project_id
    cache.mkdir(parents=True, exist_ok=True)
    created = []
    for i, (label, videos, sounds) in enumerate(canvases):
        def note(msg, i=i, label=label):
            if progress:
                progress(i, len(canvases), f"{label}: {msg}")
        note("starting")
        files = _fetch_canvas(videos, sounds, cache, note)
        for k, (path, info) in enumerate(files):
            created.append(db.create_material(
                project_id, name=label if k == 0 else f"{label} (audio {k})", media_path=path,
                media_type=info["media_type"], duration=info["duration"], width=info["width"],
                height=info["height"], source="iiif", source_url=manifest_url,
                has_audio=info["has_audio"]))
    if progress:
        progress(len(canvases), len(canvases), "done")
    return created
