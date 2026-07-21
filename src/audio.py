"""Local audio extraction — the data-minimization boundary.

Any audio/video file the user picks is decoded locally with ffmpeg and
re-encoded to a small mono Opus stream (~16 kbps). Only this audio ever leaves
the machine: no video, no original container, no metadata. The DGX re-decodes it
to 16 kHz WAV for whisper/pyannote.

Note: Opus always stores samples at 48 kHz internally, so the container reports
48 kHz regardless of the requested rate — the size win comes from mono + the low
bitrate, and the DGX downsamples to whisper's 16 kHz on arrival.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

# Audio/video containers ffmpeg can decode (matches dgx-whisper's list).
AV_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".opus", ".aac", ".wma",
    ".aiff", ".aif", ".alac",
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".mpg",
    ".mpeg", ".ts", ".3gp",
}


class FFmpegNotFound(RuntimeError):
    pass


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise FFmpegNotFound(
            "ffmpeg is required to extract audio. Install it (e.g. `brew install ffmpeg` "
            "on macOS, `winget install ffmpeg` on Windows) and ensure it is on PATH."
        )


def is_av_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in AV_EXTENSIONS


def extract_opus(src_path: str | Path, bitrate: str = "16k") -> bytes:
    """Extract the audio track of any A/V file as 16 kHz mono Opus (OGG) bytes.

    -vn drops any video stream, so nothing but audio is produced. Raises
    FFmpegNotFound if ffmpeg is missing, or CalledProcessError on decode failure.
    """
    ensure_ffmpeg()
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src_path),
        "-vn",                      # strip video — audio only leaves the machine
        "-ac", "1",                 # mono (the real size win)
        "-c:a", "libopus", "-b:a", bitrate,
        "-f", "ogg", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
        )
    return proc.stdout


def extract_opus_from_bytes(raw: bytes, filename: str, bitrate: str = "16k") -> bytes:
    """Extract audio-only Opus from uploaded bytes.

    The bytes are written to a temp file (keeping the original extension so
    ffmpeg detects the container) and then decoded via extract_opus. This is how
    the local Flask app processes a browser upload without the video ever leaving
    the machine.
    """
    suffix = Path(filename).suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tf:
        tf.write(raw)
        tf.flush()
        return extract_opus(tf.name, bitrate=bitrate)
