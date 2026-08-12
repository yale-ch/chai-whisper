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
    """Return {media_type, duration, width, height} via ffprobe (best-effort)."""
    info = {"media_type": "audio", "duration": None, "width": None, "height": None}
    if shutil.which("ffprobe") is None:
        return info
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height",
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
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            info["media_type"] = "video"
            info["width"] = s.get("width")
            info["height"] = s.get("height")
            break
    return info


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
