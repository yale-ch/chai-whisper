"""Configuration for the CHAI-Whisper local app (loaded from environment / .env).

The local app connects to the CHAI-Whisper API running on the DGX. When SSH
credentials are set it opens its own SSH tunnel to the box (the API port is
firewalled to loopback on the DGX); otherwise it talks to CHAI_WHISPER_URL
directly. Mirrors the connection config of yale-scanning-qc/src/config.py.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --- DGX connection (SSH tunnel) ------------------------------------------
SPARK_IP = os.environ.get("SPARK_IP", "").strip()
SPARK_SSH_PORT = _int("SPARK_SSH_PORT", 22)
SPARK_USERNAME = os.environ.get("SPARK_USERNAME", "").strip()
# Password auth is optional — key-based auth (SSH agent / SPARK_SSH_KEY / default
# ~/.ssh keys) is used when no password is set.
SPARK_PASSWORD = os.environ.get("SPARK_PASSWORD", "").strip()
SPARK_SSH_KEY = os.environ.get("SPARK_SSH_KEY", "").strip()  # optional private-key path
# The CHAI-Whisper API's loopback port on the DGX (see server/run.sh default).
SPARK_PORT = _int("SPARK_PORT", 8060)

# Direct URL fallback, used only when no SSH tunnel credentials are configured.
CHAI_WHISPER_URL = os.environ.get("CHAI_WHISPER_URL", "http://127.0.0.1:8060").strip()

# Shared bearer secret; must equal the server's CHAI_WHISPER_TOKEN.
CHAI_WHISPER_TOKEN = os.environ.get("CHAI_WHISPER_TOKEN", "").strip()

# --- transcription request defaults ---------------------------------------
DEFAULT_MODEL = os.environ.get("CHAI_WHISPER_MODEL", "large-v3").strip()
DEFAULT_LANGUAGE = os.environ.get("CHAI_WHISPER_LANGUAGE", "").strip()  # "" = auto-detect
