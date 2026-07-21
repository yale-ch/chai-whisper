# CHAI-Whisper

Audio transcription **and speaker diarization** for the Yale DGX, with a local
Yale-branded web app that offloads GPU inference to the DGX. Modeled on
[`yale-scanning-qc`](../yale-scanning-qc): a local app calls a small Flask API on
the DGX, which runs the model. Here the model work is a **[chai](../chai-engine)
workflow** (`whisper.WhisperTranscriber` = faster-whisper + pyannote), and the
inference core is ported from [`yale-ch/dgx-whisper`](https://github.com/yale-ch/dgx-whisper).

**Data minimization:** the local app strips the audio track from whatever A/V
file you pick and re-encodes it to 16 kHz mono Opus before upload. Only that
audio ever leaves your machine — never the video, never the original file.

```
┌─────────────────────────────┐         SSH tunnel          ┌────────────────────────────┐
│  Local machine (src/)        │  (bearer-token HTTP over    │  DGX Spark (server/)        │
│                              │   127.0.0.1:<fwd> → :8060)  │                             │
│  browser ── upload A/V ──▶   │                             │  Flask API                  │
│  Flask app                   │                             │   POST /jobs                │
│   • ffmpeg → 16k mono Opus ──┼──── audio only ────────────▶│   → worker → chai Workflow  │
│   • submit job, poll         │◀─── transcript JSON ────────┼──   WhisperTranscriber      │
│   • render transcript        │                             │      (faster-whisper +      │
│                              │                             │       pyannote, on GPU)     │
└─────────────────────────────┘                             └────────────────────────────┘
```

This repo is **both** halves:
- **`src/`** — the local front-end (Flask, Yale palette). Run on your machine.
- **`server/`** — the DGX API server bundle. Deploy on the DGX. See [`server/README.md`](server/README.md).

## Local app

Requires **ffmpeg** on PATH (`brew install ffmpeg` / `winget install ffmpeg`).

```bash
cp .env.example .env         # set SPARK_* + CHAI_WHISPER_TOKEN
uv run chai-whisper serve    # http://127.0.0.1:5058
```

Open the page, pick an audio or video file, choose the model / language and
whether to identify speakers, and hit **Transcribe**. Results render with
speaker labels and timestamps and download as `.txt` / `.srt` / `.json`.

Config (`.env`, see `.env.example`): `SPARK_IP`, `SPARK_USERNAME`,
`SPARK_PASSWORD`, `SPARK_PORT` (SSH-tunnel to the DGX), or `CHAI_WHISPER_URL`
(direct), plus the shared `CHAI_WHISPER_TOKEN`.

## The chai workflow

The DGX runs this workflow (assembled in `server/worker.py`):

```
provider.FileListProvider  →  iterator.Iterator  →  whisper.WhisperTranscriber
```

`whisper.WhisperTranscriber` was added to `chai-engine` (`chai/whisper.py`, beta
branch). It decodes to 16 kHz WAV, transcribes with faster-whisper (word
timestamps + VAD), and — when `diarize` is on — labels speakers with pyannote.
Its output `ItemResult` carries the transcript text plus structured
`segments`, `srt`, and `txt` in `metadata`. Install the extra with
`pip install "chai-engine[whisper]"`.

## DGX server

Deploy `server/` on the DGX as a systemd user service (uv + `setup_ct2_gpu.sh`
for GPU CTranslate2 on aarch64/CUDA 13). Full instructions and the API contract
are in [`server/README.md`](server/README.md).

## Security notes

- `.env` (local) and `server/.env` (DGX) hold live secrets and are gitignored —
  never commit them. Use a long random `CHAI_WHISPER_TOKEN`.
- The DGX API port stays on loopback; reach it only over the SSH tunnel.
- Diarization uses the gated pyannote model, so the DGX needs an `HF_TOKEN`.
