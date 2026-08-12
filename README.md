# CHAI-Whisper

A local **workspace** for transcribing audio/video on the Yale DGX and editing the
results — organize A/V into **projects**, transcribe + diarize on the GPU, then
review the media beside an **editable, versioned transcript** with speaker
labels, playback-synced highlighting, and edit metrics. Modeled on
[`../chai-docs`](../chai-docs) (FastAPI + a no-build vanilla-JS SPA), transcription
runs as a [chai](../chai-engine) `whisper.WhisperTranscriber` workflow on the DGX.

**Data minimization:** only *audio* ever leaves your machine — the app extracts a
16 kHz mono Opus track from whatever you point it at (even video) before sending
it to the DGX. **Media stays where it is:** the DB stores the **local path**, never
the bytes.

```
Local workspace (app/ + static/)                    DGX (server/)
  projects → materials (local paths)                  Flask API :8060
  FastAPI + vanilla SPA :5059                          bearer + SSH-key tunnel
   • add material by path/folder                       chai Workflow →
   • Transcribe → extract audio ──── audio only ──────▶  WhisperTranscriber
   • side-by-side media + transcript ◀── transcript ──   (faster-whisper +
   • edit text/speakers, versioned                        pyannote, GB10 GPU)
   • synced highlight + auto-scroll
```

## Run

Requires **ffmpeg/ffprobe** on PATH (`brew install ffmpeg`), and the DGX reachable
(SSH key + `.env`, see `.env.example`).

```bash
cp .env.example .env         # SPARK_* (host/user, key auth) + CHAI_WHISPER_TOKEN
uv run uvicorn app.main:app --port 5059   # → http://localhost:5059
```
(No build step; `static/` is plain HTML/CSS/JS. No `--reload`: restart uvicorn
after backend edits, refresh the browser for frontend edits.)

## Using it

1. **Create a project**, then **add material** by selecting audio/video file(s) or
   a folder with the file picker (or import from a **IIIF manifest**). Selected
   media is copied into the project store (`data/media/`); the DB keeps only a
   path, never the bytes. **Lexicons** for the sensitive-language scan are managed
   globally under the top-nav **Lexicons** page (create/upload/view/edit terms)
   and are shared by every project.
2. **Transcribe (DGX)** — pick a model, language, and whether to identify
   speakers; the app extracts audio, sends it over the SSH tunnel to the DGX chai
   workflow, and saves the result as **version 1**. Or **Import…** an existing
   `.json`/`.srt` as a version.
3. **Review & edit** in the viewer: play the media and the current segment
   **highlights**; toggle **auto-scroll** to keep it centered; **click a segment
   to seek** the audio/video. Edit segment text inline, reassign or rename
   speakers (with colors) in the Speakers panel.
4. **Save edits** → a new append-only version. Switch versions, **Restore** an
   older one (which appends a copy — history only grows).
5. **Your edits** panel quantifies changes: per-version and cumulative segments
   edited/added/removed, word- and character-level changes, speaker reassignments
   and renames.

## Layout

- `app/main.py` — FastAPI routes + static SPA mount. `serve()` = `chai-whisper` script.
- `app/db.py` — the only SQLite layer (projects, materials, append-only `transcription_versions`). Media = path only.
- `app/media.py` — ffprobe metadata + HTTP **Range** streaming (so `<video>` can seek).
- `app/ingest.py` — register a path/folder as materials; build snapshots from DGX results / `.json` / `.srt`.
- `app/jobs.py` — background transcription worker (extract audio → DGX → save v1).
- `app/diffing.py` — change quantification between version snapshots.
- `static/` — `index.html`, `app.js` (hash-routed SPA + synced viewer/editor), `style.css` (Yale palette).
- `src/` — reused DGX client (`remote.py`, SSH-key tunnel + bearer), audio extraction (`audio.py`), config (`config.py`).
- `server/` — the DGX API bundle (deploy separately; see [`server/README.md`](server/README.md)).

## Notes

- Media is served straight from its local path; move/delete the original and the
  material shows “media file not found” (by design — nothing is copied).
- `.env` / `server/.env` hold secrets and are gitignored; the DGX uses SSH key auth.
- Only audio is sent to the DGX, even for video — data-minimization preserved.
