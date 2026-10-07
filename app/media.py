"""Media helpers: ffprobe metadata + HTTP Range streaming from a local path.

Media files stay at their original on-disk location; we stream bytes straight
from ``media_path`` with Range support so the browser's <video>/<audio> can seek
(essential for the click-a-segment-to-seek and video mapping features).
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

_CHUNK = 1024 * 1024  # 1 MiB


def probe(path: str) -> dict:
    """Return {media_type, duration, width, height, has_audio, audio_codec,
    video_codec} via ffprobe (best-effort; has_audio is None if unknown)."""
    info = {"media_type": "audio", "duration": None, "width": None, "height": None,
            "has_audio": None, "audio_codec": None, "video_codec": None}
    if shutil.which("ffprobe") is None:
        return info
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height:stream_disposition=attached_pic",
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
        elif s.get("codec_type") == "audio" and not info["has_audio"]:
            info["has_audio"] = True
            info["audio_codec"] = s.get("codec_name")
    return info


# Audio codecs Chromium/Safari/Firefox decode inside mp4/mov/webm. Others (PCM in
# a .mov, AC-3, ALAC, WMA…) give *silent* playback even though ffmpeg — and so
# transcription — handles them fine.
BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}


def ensure_browser_audio(path: str, info: dict | None = None) -> tuple[str, dict]:
    """If an owned media file's audio can't be played in the browser, re-encode
    just the audio to AAC (video stream copied) into an .mp4/.m4a beside it and
    delete the original. Returns (path, probe-info) of the file to use."""
    info = info or probe(path)
    codec = info.get("audio_codec")
    if not info.get("has_audio") or codec in BROWSER_AUDIO or shutil.which("ffmpeg") is None:
        return path, info
    if (codec or "").startswith("pcm_") and Path(path).suffix.lower() == ".wav":
        return path, info  # PCM WAV plays natively
    src = Path(path)
    dest = src.with_name(src.stem + "-play" + (".mp4" if info["media_type"] == "video" else ".m4a"))
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0?", "-map", "0:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dest)]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except Exception:
        dest.unlink(missing_ok=True)
        return path, info
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
