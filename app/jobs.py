"""Background transcription jobs (in-memory registry, single worker thread).

A material's Transcribe action enqueues a job: extract audio-only Opus from its
local media_path (only audio ever leaves the machine, even for video), send it to
the DGX CHAI-Whisper API, map the result to a snapshot, and save it as a new
version. Progress is polled over HTTP. Mirrors chai-docs' import-job pattern.
"""

from __future__ import annotations

import queue
import threading
import uuid

from src import audio, remote
from . import db, exports, ingest

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_q: "queue.Queue[str]" = queue.Queue()
_started = threading.Lock()


def _set(job_id: str, **fields):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def get(job_id: str):
    with _lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def _run(job_id: str):
    with _lock:
        job = dict(_jobs.get(job_id, {}))
    mid = job.get("material_id")
    params = job.get("params", {})
    material = db.get_material(mid)
    if not material:
        _set(job_id, status="error", stage="error", error="material not found")
        return
    if not material.get("media_exists"):
        _set(job_id, status="error", stage="error",
             error=f"media file missing on disk: {material['media_path']}")
        return
    try:
        _set(job_id, status="running", stage="extracting audio")
        opus = audio.extract_opus(material["media_path"])

        def on_progress(st: dict):
            _set(job_id, stage=st.get("stage") or st.get("status") or "transcribing")

        _set(job_id, status="running", stage="uploading to DGX",
             audio_kb=round(len(opus) / 1024, 1))
        result = remote.transcribe(opus, params, filename="audio.ogg", progress_cb=on_progress)

        data = ingest.data_from_dgx_result(result)
        note = f"{params.get('model', 'whisper')}" + (" + diarization" if params.get("diarize") else "")
        ver = db.save_version(mid, data, source="run", note=note, model=params.get("model"))

        # Auto-write a per-run output folder using the project's defaults, so
        # every run leaves .vtt/.txt (+ optional scan report) on disk.
        try:
            _set(job_id, stage="writing outputs")
            ps = db.get_project_settings(material["project_id"])
            scan_cfg = {"enabled": bool(ps.get("scan_enabled")),
                        "lexicon_ids": [],  # [] = all global lexicons (auto-available)
                        "format": ps.get("scan_format", "txt")}
            run = exports.write_run(material, ver["version"], data,
                                    ps.get("output_formats") or ["vtt", "txt"], scan_cfg)
            _set(job_id, run_folder=run["folder_path"])
        except Exception as ex:  # export failure shouldn't lose the transcript
            _set(job_id, export_error=f"{type(ex).__name__}: {ex}")

        _set(job_id, status="done", stage="done", version=ver["version"])
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", stage="error", error=f"{type(e).__name__}: {e}")


def _loop():
    while True:
        jid = _q.get()
        try:
            with _lock:
                kind = (_jobs.get(jid) or {}).get("kind", "transcribe")
            if kind == "iiif":
                _run_iiif(jid)
            else:
                _run(jid)
        finally:
            _q.task_done()


def ensure_worker():
    if _started.acquire(blocking=False):
        threading.Thread(target=_loop, daemon=True).start()


def submit(material_id: str, params: dict) -> str:
    ensure_worker()
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {"status": "queued", "stage": "queued", "kind": "transcribe",
                         "material_id": material_id, "params": params}
    _q.put(job_id)
    return job_id


def _run_iiif(job_id: str):
    with _lock:
        job = dict(_jobs.get(job_id, {}))
    try:
        def prog(done, total, msg):
            _set(job_id, status="running", stage=msg, progress=round(100 * done / max(total, 1)))
        created = ingest.register_iiif(job["project_id"], job["manifest_url"], progress=prog)
        _set(job_id, status="done", stage="done", count=len(created))
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", stage="error", error=f"{type(e).__name__}: {e}")


def submit_iiif(project_id: str, manifest_url: str) -> str:
    ensure_worker()
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {"status": "queued", "stage": "queued", "kind": "iiif",
                         "project_id": project_id, "manifest_url": manifest_url}
    _q.put(job_id)
    return job_id
