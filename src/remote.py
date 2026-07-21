"""Client for the CHAI-Whisper API on the DGX (see server/app.py).

Opens and maintains an SSH tunnel to the box when SSH credentials are set
(the API port is firewalled to loopback on the DGX), otherwise talks to
CHAI_WHISPER_URL directly. Every request carries a bearer token. Adapted from
yale-scanning-qc/src/remote.py, but the payload is a single audio blob and the
job lifecycle is submit -> poll -> fetch result.
"""

import threading
import time

import requests

from .config import (
    CHAI_WHISPER_TOKEN,
    CHAI_WHISPER_URL,
    SPARK_IP,
    SPARK_PASSWORD,
    SPARK_PORT,
    SPARK_SSH_KEY,
    SPARK_SSH_PORT,
    SPARK_USERNAME,
)

POLL_INTERVAL = 3.0
POLL_MAX_SECONDS = 6 * 60 * 60  # ceiling for a single transcription job

# When an IP + username are configured the app opens (and maintains) its own SSH
# tunnel to the box, so it works on any machine with no manual `ssh -L`. Auth is
# by password if set, otherwise by SSH key (SPARK_SSH_KEY or the agent / default
# ~/.ssh keys).
_use_tunnel = bool(SPARK_IP and SPARK_USERNAME)

_tunnel = None
_tunnel_lock = threading.Lock()


def _ensure_tunnel() -> int:
    """Open (or reuse) the SSH tunnel; return the local forwarded port."""
    global _tunnel
    with _tunnel_lock:
        if _tunnel is not None:
            try:
                if _tunnel.is_active:
                    return _tunnel.local_bind_port
            except Exception:
                pass
            try:
                _tunnel.stop()
            except Exception:
                pass
            _tunnel = None

        # sshtunnel 0.4.0 references paramiko.DSSKey, removed in paramiko >= 4.
        # DSA keys are long obsolete; shim the attribute so the import works.
        import paramiko
        if not hasattr(paramiko, "DSSKey"):
            paramiko.DSSKey = paramiko.RSAKey
        import sshtunnel

        kwargs = dict(
            ssh_username=SPARK_USERNAME,
            remote_bind_address=("127.0.0.1", int(SPARK_PORT)),
            local_bind_address=("127.0.0.1", 0),
            set_keepalive=30.0,
        )
        # Prefer explicit password, else key auth (agent / default keys, or an
        # explicit private-key path). allow_agent stays on for key discovery.
        if SPARK_PASSWORD:
            kwargs["ssh_password"] = SPARK_PASSWORD
        if SPARK_SSH_KEY:
            kwargs["ssh_pkey"] = SPARK_SSH_KEY

        _tunnel = sshtunnel.SSHTunnelForwarder((SPARK_IP, SPARK_SSH_PORT), **kwargs)
        _tunnel.start()
        return _tunnel.local_bind_port


def _base_url() -> str:
    if _use_tunnel:
        return f"http://127.0.0.1:{_ensure_tunnel()}"
    return CHAI_WHISPER_URL


def close_tunnel() -> None:
    global _tunnel
    with _tunnel_lock:
        if _tunnel is not None:
            try:
                _tunnel.stop()
            except Exception:
                pass
            _tunnel = None


def enabled() -> bool:
    """Config check only — does not open the tunnel."""
    return _use_tunnel or bool(CHAI_WHISPER_URL)


def _headers() -> dict:
    return {"Authorization": f"Bearer {CHAI_WHISPER_TOKEN}"} if CHAI_WHISPER_TOKEN else {}


def health(timeout: float = 10) -> dict:
    r = requests.get(f"{_base_url()}/health", headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def reachable(timeout: float = 6) -> bool:
    if not enabled():
        return False
    try:
        health(timeout=timeout)
        return True
    except Exception:
        return False


_status_cache = {"t": -1e9, "val": None}
_STATUS_TTL = 20.0


def status(refresh: bool = False) -> dict:
    """Cached remote-box status for the UI health pill."""
    if not enabled():
        return {"enabled": False, "reachable": False}
    now = time.monotonic()
    if not refresh and _status_cache["val"] and now - _status_cache["t"] < _STATUS_TTL:
        return _status_cache["val"]
    val = {"enabled": True, "use_tunnel": _use_tunnel, "host": SPARK_IP or "direct"}
    try:
        h = health(timeout=5)
        val.update(reachable=True, gpu=h.get("gpu"), cuda=h.get("cuda"),
                   chai_version=h.get("chai_version"))
    except Exception as e:
        val.update(reachable=False, error=str(e)[:140])
    _status_cache.update(t=now, val=val)
    return val


def submit_job(audio_bytes: bytes, params: dict, filename: str = "audio.ogg") -> str:
    """Upload the extracted audio and enqueue a transcription job. Returns job_id."""
    import json

    files = {"audio": (filename, audio_bytes, "audio/ogg")}
    r = requests.post(f"{_base_url()}/jobs", headers=_headers(),
                      files=files, data={"params": json.dumps(params)}, timeout=300)
    r.raise_for_status()
    return r.json()["job_id"]


def poll_job(job_id: str, timeout: float = 30) -> dict:
    """One status poll -> {status, stage, progress, error}."""
    r = requests.get(f"{_base_url()}/jobs/{job_id}", headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_result(job_id: str, timeout: float = 120) -> dict:
    """Fetch the finished transcript (chai result JSON)."""
    r = requests.get(f"{_base_url()}/jobs/{job_id}/result", headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def delete_job(job_id: str) -> None:
    try:
        requests.delete(f"{_base_url()}/jobs/{job_id}", headers=_headers(), timeout=30)
    except Exception:
        pass


def transcribe(audio_bytes: bytes, params: dict, filename: str = "audio.ogg",
               progress_cb=None) -> dict:
    """Full round-trip: submit -> poll to completion -> fetch result -> cleanup.

    Returns the transcript payload. Raises on remote error / timeout.
    """
    job_id = submit_job(audio_bytes, params, filename=filename)
    deadline = time.monotonic() + POLL_MAX_SECONDS
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        st = poll_job(job_id)
        if progress_cb:
            progress_cb(st)
        if st.get("status") == "done":
            result = fetch_result(job_id)
            delete_job(job_id)
            return result
        if st.get("status") == "error":
            delete_job(job_id)
            raise RuntimeError(f"remote transcription failed: {st.get('error')}")
    delete_job(job_id)
    raise TimeoutError("remote transcription exceeded time limit")
