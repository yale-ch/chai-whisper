"""Media helpers: ffprobe metadata + HTTP Range streaming from a local path.

Media files stay at their original on-disk location; we stream bytes straight
from ``media_path`` with Range support so the browser's <video>/<audio> can seek
(essential for the click-a-segment-to-seek and video mapping features).
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

_CHUNK = 1024 * 1024  # 1 MiB


def probe(path: str) -> dict:
    """Return {media_type, duration, width, height, has_audio, audio_codec,
    video_codec, audio_streams} via ffprobe (best-effort; has_audio is None if
    unknown). ``audio_streams`` lists every audio track: {index, codec,
    profile, channels, sample_rate}."""
    info = {"media_type": "audio", "duration": None, "width": None, "height": None,
            "has_audio": None, "audio_codec": None, "video_codec": None, "audio_streams": []}
    if shutil.which("ffprobe") is None:
        return info
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=index,codec_type,codec_name,profile,channels,sample_rate,width,height"
             ":stream_disposition=attached_pic",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True).stdout
        data = json.loads(out)
    except Exception:
        return info
    dur = (data.get("format") or {}).get("duration")
    if dur:
        try:
            info["duration"] = float(dur)
        except ValueError:
            pass
    info["has_audio"] = False
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            if info["video_codec"] is None:
                info["media_type"] = "video"
                info["width"] = s.get("width")
                info["height"] = s.get("height")
                info["video_codec"] = s.get("codec_name")
        elif s.get("codec_type") == "audio":
            info["audio_streams"].append({
                "index": s.get("index"), "codec": s.get("codec_name"), "profile": s.get("profile"),
                "channels": s.get("channels"), "sample_rate": s.get("sample_rate")})
            if not info["has_audio"]:
                info["has_audio"] = True
                info["audio_codec"] = s.get("codec_name")
    return info


# Audio codecs Chromium/Safari/Firefox decode inside mp4/mov/webm. Others (PCM in
# a .mov, AC-3, ALAC, WMA…) give *silent* playback even though ffmpeg — and so
# transcription — handles them fine.
BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
# Bump when playback_issue() learns new cases, so stored recordings are re-checked once.
AUDIO_REV = 2


# Containers browsers play directly; others (.avi, .wmv, .mpg, .ts, .flv, …)
# are remuxed into .mp4/.m4a.
BROWSER_CONTAINERS = {".mp4", ".m4v", ".mov", ".webm", ".m4a", ".mp3", ".ogg", ".oga", ".opus",
                      ".wav", ".flac", ".aac"}
# AAC profiles every browser decodes (Main/LTP/SSR play silently in Chromium).
BROWSER_AAC_PROFILES = {None, "", "LC", "HE-AAC", "HE-AACv2"}


def playback_issue(path: str, info: dict) -> str | None:
    """Why the browser would play this file's video but not its sound (or not
    at all), or None if it should play fine. Transcription is unaffected either
    way — ffmpeg decodes everything and picks the richest audio track."""
    if not info.get("has_audio"):
        return None
    streams = info.get("audio_streams") or []
    first = streams[0] if streams else {"codec": info.get("audio_codec")}
    codec, ext = first.get("codec") or "", Path(path).suffix.lower()
    if codec.startswith("pcm_") and ext == ".wav":
        return None  # PCM WAV plays natively
    if codec not in BROWSER_AUDIO:
        return f"{codec or 'unknown'} audio is not playable in browsers"
    if codec == "aac" and first.get("profile") not in BROWSER_AAC_PROFILES:
        return f"AAC {first.get('profile')} audio is not playable in browsers"
    if len(streams) > 1:
        return f"{len(streams)} audio tracks (browsers play only the first, which may be silent)"
    if (first.get("channels") or 0) > 2:
        return f"{first.get('channels')}-channel audio (browsers may play only silent front/side channels)"
    if ext not in BROWSER_CONTAINERS:
        return f"{ext} files are not playable in browsers"
    return None


def _loudest_audio_stream(path: str, streams: list[dict]) -> int:
    """Position (0-based among audio tracks) of the loudest track, sampled over
    the first 5 minutes — archival video often carries a silent first track."""
    if len(streams) < 2:
        return 0
    best, best_vol = 0, float("-inf")
    for k in range(len(streams)):
        try:
            err = subprocess.run(["ffmpeg", "-v", "info", "-t", "300", "-i", str(path), "-map", f"0:a:{k}",
                                  "-af", "volumedetect", "-f", "null", "-"],
                                 capture_output=True, text=True, timeout=600).stderr
            m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", err)
            vol = float(m.group(1)) if m and m.group(1) != "-inf" else float("-inf")
        except Exception:
            continue
        if vol > best_vol:
            best, best_vol = k, vol
    return best


def ensure_browser_audio(path: str, info: dict | None = None, force: bool = False,
                         dest_dir: str | None = None, keep_source: bool = False) -> tuple[str, dict]:
    """If a media file's sound won't play in the browser (see ``playback_issue``),
    write a copy with one stereo AAC track (the loudest original track; video
    stream copied) as ``<name>-play.mp4``/``.m4a`` — beside it, or in
    ``dest_dir`` — and delete the original unless ``keep_source``. ``force``
    re-encodes even when no issue is detected. Returns (path, probe-info) of
    the file to use; on any ffmpeg failure the original is kept."""
    info = info or probe(path)
    if not info.get("has_audio") or shutil.which("ffmpeg") is None:
        return path, info
    if not force and playback_issue(path, info) is None:
        return path, info
    src = Path(path)
    stem = src.stem if src.stem.endswith("-play") else src.stem + "-play"
    dest = Path(dest_dir or src.parent) / (stem + (".mp4" if info["media_type"] == "video" else ".m4a"))
    if dest == src:
        dest = dest.with_name(stem + "2" + dest.suffix)
    track = _loudest_audio_stream(path, info.get("audio_streams") or [])
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0?", "-map", f"0:a:{track}",
           "-c:v", "copy", "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "192k",
           "-movflags", "+faststart", str(dest)]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except Exception:
        if info["media_type"] != "video":
            dest.unlink(missing_ok=True)
            return path, info
        # Video codec that can't be copied into .mp4 (e.g. WMV, FLV): re-encode it too.
        try:
            subprocess.run(cmd[:cmd.index("-c:v")] + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                                                      "-pix_fmt", "yuv420p"] + cmd[cmd.index("-c:v") + 2:],
                           capture_output=True, check=True)
        except Exception:
            dest.unlink(missing_ok=True)
            return path, info
    if not keep_source:
        src.unlink(missing_ok=True)
    return str(dest), probe(str(dest))


def guess_type(path: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    return mt or "application/octet-stream"


def stream(path: str, request: Request) -> Response:
    """Serve a file with HTTP Range support (206 partial content when requested)."""
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="media file not found on disk")
    file_size = os.path.getsize(path)
    content_type = guess_type(path)
    range_header = request.headers.get("range") or request.headers.get("Range")

    if range_header is None:
        def full():
            with open(path, "rb") as f:
                while chunk := f.read(_CHUNK):
                    yield chunk
        return StreamingResponse(full(), media_type=content_type,
                                 headers={"Accept-Ranges": "bytes",
                                          "Content-Length": str(file_size)})

    # e.g. "bytes=0-1023" (open-ended end allowed)
    try:
        units, rng = range_header.split("=", 1)
        assert units.strip() == "bytes"
        start_s, end_s = rng.split("-", 1)
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else file_size - 1
    except Exception:
        raise HTTPException(status_code=400, detail="invalid Range header")
    if start >= file_size or start < 0:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
    end = min(end, file_size - 1)
    length = end - start + 1

    def partial():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0 and (chunk := f.read(min(_CHUNK, remaining))):
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        partial(), status_code=206, media_type=content_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
        })


def poster_frame(path: str) -> bytes | None:
    """Grab a single JPEG frame ~1s into a video for a thumbnail (best-effort)."""
    if shutil.which("ffmpeg") is None:
        return None
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", "1", "-i", str(path),
             "-frames:v", "1", "-vf", "scale=480:-1", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
            capture_output=True, check=True).stdout
        return out or None
    except Exception:
        return None
