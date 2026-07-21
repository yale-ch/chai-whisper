"""CHAI-Whisper DGX API server (Flask).

Runs on the NVIDIA DGX Spark. Receives audio-only uploads from the local
CHAI-Whisper app, runs the chai whisper workflow (transcription + diarization),
and returns the transcript. Every route is behind a bearer token. Async job
model: POST /jobs enqueues, GET /jobs/<id> polls, GET /jobs/<id>/result fetches.

Self-contained (imports only flask + worker + chai), deployable on its own as a
systemd user service — see README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from functools import wraps

from flask import Flask, jsonify, request

import worker

app = Flask(__name__)

TOKEN = os.environ.get("CHAI_WHISPER_TOKEN", "").strip()
MAX_UPLOAD_MB = int(os.environ.get("CHAI_WHISPER_MAX_UPLOAD_MB", "512"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def require_token(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if TOKEN:
            sent = (request.headers.get("Authorization", "").replace("Bearer ", "").strip()
                    or request.headers.get("X-Auth-Token", "").strip())
            if sent != TOKEN:
                return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapped


@app.route("/health")
@require_token
def health():
    info = {"ok": True}
    try:
        import chai
        info["chai_version"] = getattr(chai, "__version__", "unknown")
    except Exception as e:  # noqa: BLE001
        info["ok"] = False
        info["chai_error"] = str(e)
    try:
        import torch
        info["torch"] = torch.__version__
        info["cuda"] = torch.cuda.is_available()
        if torch.cuda.is_available():
            info["gpu"] = torch.cuda.get_device_name(0)
    except Exception:
        info["torch"] = None
        info["cuda"] = False
    return jsonify(info)


@app.route("/jobs", methods=["POST"])
@require_token
def create_job():
    f = request.files.get("audio")
    if f is None or not f.filename:
        return jsonify({"error": "no audio file provided (field 'audio')"}), 400

    try:
        params = json.loads(request.form.get("params", "{}") or "{}")
    except json.JSONDecodeError:
        return jsonify({"error": "params is not valid JSON"}), 400
    if not isinstance(params, dict):
        return jsonify({"error": "params must be a JSON object"}), 400

    # Save the uploaded audio into its own temp dir (cleaned up on DELETE).
    job_dir = tempfile.mkdtemp(prefix="chai-whisper-job-")
    audio_path = os.path.join(job_dir, os.path.basename(f.filename) or "audio.ogg")
    f.save(audio_path)

    job_id = worker.submit(audio_path, params)
    return jsonify({"job_id": job_id})


@app.route("/jobs/<job_id>")
@require_token
def job_status(job_id):
    st = worker.get_status(job_id)
    if st is None:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(st)


@app.route("/jobs/<job_id>/result")
@require_token
def job_result(job_id):
    st = worker.get_status(job_id)
    if st is None:
        return jsonify({"error": "unknown job"}), 404
    if st.get("status") != "done":
        return jsonify({"error": "not ready", "status": st.get("status")}), 409
    return jsonify(worker.get_result(job_id))


@app.route("/jobs/<job_id>", methods=["DELETE"])
@require_token
def job_delete(job_id):
    return jsonify({"deleted": worker.delete(job_id)})


def main() -> None:
    p = argparse.ArgumentParser(description="CHAI-Whisper DGX API server.")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("CHAI_WHISPER_PORT", "8060")))
    args = p.parse_args()
    if not TOKEN:
        print("WARNING: CHAI_WHISPER_TOKEN is not set — the API is UNAUTHENTICATED.", flush=True)
    worker.ensure_worker()
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
