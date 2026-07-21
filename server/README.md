# CHAI-Whisper DGX API server

Runs on the **NVIDIA DGX Spark** (GB10 / sm_121 / aarch64 / CUDA 13). Receives
audio-only uploads from the local CHAI-Whisper app and runs the **chai** whisper
workflow (faster-whisper transcription + pyannote diarization) on the GPU.

This bundle is self-contained: `app.py` (Flask API) + `worker.py` (chai workflow
runner). It installs `chai-engine[whisper]` from GitHub plus the DGX-specific
CUDA 13 torch wheels.

## API

All routes require `Authorization: Bearer $CHAI_WHISPER_TOKEN`.

| Method / path | Body | Response |
|---|---|---|
| `GET /health` | — | `{ok, chai_version, torch, cuda, gpu}` |
| `POST /jobs` | multipart: `audio` file + form `params` (JSON) | `{job_id}` |
| `GET /jobs/<id>` | — | `{status, stage, progress, error}` (queued/running/done/error) |
| `GET /jobs/<id>/result` | — | chai result JSON: `value` (transcript) + `metadata.segments/srt/txt` |
| `DELETE /jobs/<id>` | — | `{deleted}` (also removes the uploaded audio) |

`params` fields (all optional): `model` (default `large-v3`), `language`,
`diarize` (default true), `num_speakers`/`min_speakers`/`max_speakers`,
`compute_type`, `beam_size`.

## One-time setup on the DGX

```bash
# as the service user (e.g. qc_user), with this bundle copied to ~/apps/chai-whisper
cd ~/apps/chai-whisper/server

uv sync                     # creates the venv, installs chai-engine[whisper] + torch cu130
bash setup_ct2_gpu.sh       # enable GPU CTranslate2 for faster-whisper (aarch64 / CUDA 13)

# secrets
cat > .env <<'EOF'
CHAI_WHISPER_TOKEN=<same value the local app uses>
HF_TOKEN=<HuggingFace token; accept https://hf.co/pyannote/speaker-diarization-3.1>
CHAI_WHISPER_PORT=8060
EOF
chmod 600 .env

# smoke test
./run.sh &
curl -s -H "Authorization: Bearer $CHAI_WHISPER_TOKEN" localhost:8060/health
```

`HF_TOKEN` is only needed for diarization (gated pyannote model). If it is
unset, transcription-only jobs (`diarize:false`) still work.

## Run as a systemd user service

Mirrors the yale-scanning-qc trainer deployment. The API port stays on loopback;
the local app reaches it over an SSH tunnel.

```ini
# ~/.config/systemd/user/chai-whisper.service
[Unit]
Description=CHAI-Whisper DGX API server
After=network-online.target

[Service]
WorkingDirectory=%h/apps/chai-whisper/server
ExecStart=%h/apps/chai-whisper/server/run.sh
Restart=on-failure
# CHAI_WHISPER_TOKEN / HF_TOKEN are read from server/.env by run.sh

[Install]
WantedBy=default.target
```

```bash
loginctl enable-linger "$USER"            # keep the service alive across logout/reboot
systemctl --user daemon-reload
systemctl --user enable --now chai-whisper
systemctl --user status chai-whisper
journalctl --user -u chai-whisper -f      # logs
```

## GPU smoke test (on the DGX)

```bash
# short audio, diarized
curl -s -H "Authorization: Bearer $CHAI_WHISPER_TOKEN" \
  -F 'audio=@sample.ogg' \
  -F 'params={"model":"large-v3","diarize":true,"num_speakers":2}' \
  localhost:8060/jobs
# -> {"job_id":"..."} ; then poll GET /jobs/<id> until status=done, GET /jobs/<id>/result
```

See `../README.md` for the full architecture and the local app.
