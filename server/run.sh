#!/usr/bin/env bash
# Launch the CHAI-Whisper DGX API server via uv.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"

# Load .env (CHAI_WHISPER_TOKEN, HF_TOKEN) if present, without clobbering the
# environment the service manager already set.
if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

if [ -z "${CHAI_WHISPER_TOKEN:-}" ]; then
    echo "ERROR: CHAI_WHISPER_TOKEN is not set (must match the local app's CHAI_WHISPER_TOKEN)." >&2
    exit 1
fi

# HF_TOKEN is required only for speaker diarization (gated pyannote model).
if [ -z "${HF_TOKEN:-}" ]; then
    echo "WARNING: HF_TOKEN not set — diarization jobs will fail until it is." >&2
fi

# uv run syncs the venv from pyproject on first launch, then runs.
exec uv run python app.py --host 0.0.0.0 --port "${CHAI_WHISPER_PORT:-8060}"
