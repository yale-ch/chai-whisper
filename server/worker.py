"""CHAI-Whisper DGX worker: runs the chai whisper workflow for each job.

A single background worker thread consumes jobs sequentially (natural GPU
serialization). Each job builds a chai ``Workflow``
(FileListProvider -> Iterator -> whisper.WhisperTranscriber), runs it, and
captures the transcriber's result via a workflow event listener so we don't have
to walk the nested result tree. Progress (stage) is relayed from the same events.
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

from chai.workflow import Workflow

# job_id -> {status, stage, progress, error, result, audio_path, created}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_work_q: "queue.Queue[str]" = queue.Queue()
_worker_started = threading.Lock()


def _set(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _build_workflow_config(audio_path: str, params: dict) -> dict:
    """Assemble the workflow JSON. `params` come straight from the API request.

    Device/compute-type default to the DGX GPU, but can be overridden per host
    via CHAI_WHISPER_DEVICE / CHAI_WHISPER_COMPUTE_TYPE (e.g. cpu / int8 for a
    local CPU test) without changing what the UI sends.
    """
    default_device = os.environ.get("CHAI_WHISPER_DEVICE", "cuda")
    default_compute = os.environ.get("CHAI_WHISPER_COMPUTE_TYPE", "float16")
    settings = {
        "model": params.get("model", "large-v3"),
        "compute_type": params.get("compute_type", default_compute),
        "device": params.get("device", default_device),
        "diarize": bool(params.get("diarize", True)),
        "diarization_device": params.get("diarization_device", "auto"),
    }
    if params.get("language"):
        settings["language"] = params["language"]
    for k in ("num_speakers", "min_speakers", "max_speakers", "beam_size"):
        if params.get(k) is not None:
            settings[k] = params[k]

    return {
        "id": "chai_whisper", "type": "Workflow", "name": "CHAI-Whisper transcription",
        "steps": [{
            "id": "files", "type": "provider.FileListProvider",
            "steps": [{
                "id": "iter", "type": "iterator.Iterator",
                "settings": {"workers": 1, "continue_on_error": False},
                "steps": [{
                    "id": "whisper", "type": "whisper.WhisperTranscriber",
                    "settings": settings,
                }],
            }],
        }],
    }


def _run_job(job_id: str) -> None:
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))
    audio_path = job.get("audio_path")
    params = job.get("params", {})
    if not audio_path or not os.path.exists(audio_path):
        _set(job_id, status="error", stage="error", error="audio file missing")
        return

    _set(job_id, status="running", stage="starting")
    captured: dict = {}

    def listener(payload: dict) -> None:
        ev = payload.get("event")
        cls = payload.get("component_class")
        if ev == "component_start" and cls == "WhisperTranscriber":
            _set(job_id, stage="transcribing")
        elif ev == "component_log" and payload.get("message"):
            _set(job_id, stage=payload["message"])
        elif ev == "component_end" and cls == "WhisperTranscriber":
            res = payload.get("result")
            if res is not None:
                captured["result"] = res

    try:
        js = _build_workflow_config(audio_path, params)
        wf = Workflow(js)
        wf.add_listener(listener)
        wf.run([audio_path])

        res = captured.get("result")
        if res is None:
            raise RuntimeError("workflow produced no transcription result")
        # to_json carries value (transcript text) + metadata (segments/srt/txt).
        _set(job_id, status="done", stage="done",
             result=res.to_json(recurse=False), finished=time.time())
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", stage="error", error=f"{type(e).__name__}: {e}")


def _worker_loop() -> None:
    while True:
        job_id = _work_q.get()
        try:
            _run_job(job_id)
        finally:
            _work_q.task_done()


def ensure_worker() -> None:
    """Start the single worker thread once."""
    if _worker_started.acquire(blocking=False):
        threading.Thread(target=_worker_loop, daemon=True).start()


def submit(audio_path: str, params: dict) -> str:
    ensure_worker()
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "queued", "stage": "queued", "progress": None,
            "audio_path": audio_path, "params": params, "created": time.time(),
        }
    _work_q.put(job_id)
    return job_id


def get_status(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        return {k: job.get(k) for k in ("status", "stage", "progress", "error")}


def get_result(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return job.get("result") if job else None


def delete(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.pop(job_id, None)
    if not job:
        return False
    audio_path = job.get("audio_path")
    if audio_path:
        # Remove the per-job temp dir the uploaded audio lived in.
        try:
            shutil.rmtree(Path(audio_path).parent, ignore_errors=True)
        except Exception:
            pass
    return True
