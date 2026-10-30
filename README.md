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
   a folder with the file picker (or import from a **IIIF manifest** — `Choice`
   renditions, HLS/DASH streams, and video with a separately-published audio
   track are all handled; audio the browser can't play, e.g. AC-3, is re-encoded
   to AAC). Selected media is copied into the project store (`data/media/`); the
   DB keeps only a path, never the bytes. **Lexicons** for the sensitive-language
   scan are managed globally under the top-nav **Lexicons** page.
2. **Transcribe (DGX)** — from a recording, or **select several on the project page
   and “Transcribe selected”**. Jobs run on the server and show in the
   **Background jobs** tray (bottom right, minimizable) on every page, so you can
   keep working; queued jobs can be cancelled. Language accepts ISO 639-1/-2 codes
   (`en`, `eng`, `ger`/`deu`, `en-US`) or a name. Model **auto** uses ivrit.ai's
   fine-tunes for Yiddish ([`ivrit-ai/yi-whisper-large-v3-turbo-ct2`](https://huggingface.co/ivrit-ai/yi-whisper-large-v3-turbo-ct2))
   and Hebrew ([`ivrit-ai/whisper-large-v3-turbo-ct2`](https://huggingface.co/ivrit-ai/whisper-large-v3-turbo-ct2)),
   else `large-v3`; those models always run with their language forced (map in
   `app/languages.py`). A warning flags non-Latin-script languages that come back
   in Latin script (Whisper translating instead of transcribing). Or **Import…** an existing
   `.vtt`, `.srt`, `.txt` or `.json` (any encoding — UTF-8/16, Windows-1252).
   Plain text is checked for timestamps (`[00:01:23]`, `Speaker 1  0:03`,
   `Jane (1:23): …`); without them, timings are estimated across the media length.
3. **Review & edit** in the viewer: click a line’s **▶ timestamp** to play from
   there; **✎ time** edits start/end (or set them from the playhead). Edit text
   inline, reassign or rename speakers, and leave **📝 notes** on lines (shown
   inline; resolve/delete). Leaving with unsaved edits asks first.
4. **Editors** are per recording (assign several, in bulk from the project page).
   “You are” (per browser) signs your notes and saved versions.
5. **Save edits** → a new append-only version. Switch versions, **Restore** an
   older one (which appends a copy — history only grows).
6. **Sensitive-language scan**: scanning the `vtt`/`srt` rendering reports each hit
   with its cue number, start/end time, speaker and full cue text (click to play);
   download as **CSV**. Run folders get `report.txt/json/csv`.
7. **Export** writes the chosen formats (vtt, txt, srt, json, csv) plus **TPME**
   provenance sidecars ([AI4LAM TPME 1.0](https://doi.org/10.5281/zenodo.21287085)):
   `<file>.tpme.json` per transcript file (with its sha256) and the version
   chain — either `<name>.tpme-history.json` or embedded in each sidecar as
   `version_history` (project setting). Per-recording fields (media title,
   originating file, provider, conventions, media language(s), review level)
   are set in the recording's **Recording metadata** panel; `human_agent` comes
   from its editors, and sensitive-language scans are logged in `processing`
   with the lexicons used. Field choices live in `app/tpme.py`.
8. **Your edits** panel quantifies changes per version and cumulatively.

## Layout

- `app/main.py` — FastAPI routes + static SPA mount. `serve()` = `chai-whisper` script.
- `app/db.py` — the only SQLite layer (projects, materials, append-only `transcription_versions`). Media = path only.
- `app/media.py` — ffprobe metadata + HTTP **Range** streaming (so `<video>` can seek).
- `app/ingest.py` — register uploads/IIIF as materials; parse DGX results and `.json` / `.vtt` / `.srt` / `.txt` imports.
- `app/textio.py` — encoding-tolerant decoding for every text upload.
- `app/jobs.py` — background job queue (transcribe, IIIF import), listed in the UI tray; queued jobs cancellable.
- `app/scan.py` / `app/exporters.py` / `app/exports.py` — sensitive-language scan (+ CSV), format renderers, run folders.
- `app/tpme.py` — TPME provenance records per version; `app/languages.py` — ISO 639 code mapping for Whisper.
- `app/diffing.py` — change quantification between version snapshots.
- `static/` — `index.html`, `app.js` (hash-routed SPA + synced viewer/editor), `style.css` (Yale palette).
- `src/` — reused DGX client (`remote.py`, SSH-key tunnel + bearer), audio extraction (`audio.py`), config (`config.py`).
- `server/` — the DGX API bundle (deploy separately; see [`server/README.md`](server/README.md)).

## Notes

- Media is served straight from its stored path; move/delete it and the
  material shows “media file not found”.
- The job list lives in server memory: restarting uvicorn forgets queued jobs
  (finished transcripts are already saved as versions).
- `.env` / `server/.env` hold secrets and are gitignored; the DGX uses SSH key auth.
- Only audio is sent to the DGX, even for video — data-minimization preserved.
