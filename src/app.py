"""CHAI-Whisper local front-end (Flask).

Runs on the user's machine. The browser uploads an A/V file to this local
server; the server extracts audio-only Opus (src/audio.py) and offloads
transcription + diarization to the CHAI-Whisper API on the DGX (src/remote.py),
which runs a chai workflow. Only the audio ever leaves this machine.

Single-file, server-rendered (render_template_string), Yale color palette.
"""

from __future__ import annotations

import threading
import traceback
import uuid

from flask import Flask, jsonify, render_template_string, request

from . import audio, remote
from .config import DEFAULT_LANGUAGE, DEFAULT_MODEL

app = Flask(__name__)

# In-process job store. Each job tracks the local pipeline stage plus the
# remote workflow stage relayed from the DGX poll.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

MAX_UPLOAD_MB = 2048
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


def _set(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _run_job(job_id: str, filename: str, raw: bytes, params: dict) -> None:
    try:
        _set(job_id, status="extracting", stage="extracting audio")
        opus = audio.extract_opus_from_bytes(raw, filename)
        _set(job_id, status="submitting", stage="uploading audio to DGX",
             audio_kb=round(len(opus) / 1024, 1))

        def on_progress(st: dict) -> None:
            _set(job_id, status="transcribing",
                 stage=st.get("stage") or st.get("status") or "transcribing",
                 progress=st.get("progress"))

        result = remote.transcribe(opus, params, filename="audio.ogg",
                                   progress_cb=on_progress)
        _set(job_id, status="done", stage="done", result=result)
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", stage="error",
             error=f"{type(e).__name__}: {e}",
             traceback=traceback.format_exc())


@app.route("/api/remote/status")
def remote_status():
    return jsonify(remote.status(refresh=request.args.get("refresh") == "1"))


@app.route("/api/transcribe", methods=["POST"])
def transcribe():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "no file provided"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"error": "empty file"}), 400

    diarize = request.form.get("diarize", "true").lower() in ("1", "true", "on", "yes")
    params = {
        "model": request.form.get("model") or DEFAULT_MODEL,
        "language": (request.form.get("language") or DEFAULT_LANGUAGE) or None,
        "diarize": diarize,
    }
    for key in ("num_speakers", "min_speakers", "max_speakers"):
        val = request.form.get(key, "").strip()
        if val:
            try:
                params[key] = int(val)
            except ValueError:
                pass

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "stage": "queued", "filename": f.filename}
    threading.Thread(target=_run_job, args=(job_id, f.filename, raw, params),
                     daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/jobs/<job_id>")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        # Do not ship the full transcript on every poll; expose it separately.
        return jsonify({k: v for k, v in job.items()
                        if k not in ("result", "traceback")})


@app.route("/api/jobs/<job_id>/result")
def job_result(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        if job.get("status") != "done":
            return jsonify({"error": "not ready"}), 409
        return jsonify(job["result"])


@app.route("/")
def index():
    return render_template_string(INDEX_HTML, default_model=DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# UI (Yale palette). Yale Blue #00356B is the primary brand color.
# ---------------------------------------------------------------------------
INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CHAI-Whisper</title>
<style>
  :root {
    --yale-blue: #00356b;
    --yale-blue-dk: #002548;
    --yale-blue-md: #286dc0;
    --yale-blue-lt: #63aaff;
    --accent: #ffd55a;
    --ink: #1e1e1e;
    --muted: #5b6b7b;
    --line: #d9e0e8;
    --bg: #f4f6f9;
    --card: #ffffff;
    --good: #1a7f4b;
    --bad: #b3261e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  header {
    background: var(--yale-blue); color: #fff; padding: 18px 28px;
    display: flex; align-items: center; gap: 14px;
    border-bottom: 4px solid var(--accent);
  }
  header .mark {
    width: 40px; height: 40px; border-radius: 8px; background: #fff;
    color: var(--yale-blue); font-weight: 800; font-size: 20px;
    display: grid; place-items: center; letter-spacing: -1px;
  }
  header h1 { font-size: 20px; margin: 0; font-weight: 700; letter-spacing: .3px; }
  header .sub { color: #cfe0f5; font-size: 12.5px; margin-top: 2px; }
  .pill {
    margin-left: auto; font-size: 12.5px; padding: 6px 12px; border-radius: 999px;
    background: rgba(255,255,255,.14); color: #fff; display: flex; align-items: center; gap: 7px;
  }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #9aa7b4; }
  .dot.on { background: #4ade80; } .dot.off { background: #f87171; }
  main { max-width: 900px; margin: 26px auto; padding: 0 20px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 22px; margin-bottom: 20px; box-shadow: 0 1px 2px rgba(0,0,0,.03);
  }
  h2 { font-size: 15px; margin: 0 0 14px; color: var(--yale-blue); }
  label { display: block; font-size: 13px; color: var(--muted); margin: 12px 0 5px; font-weight: 600; }
  input[type=text], input[type=number], select {
    width: 100%; padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px;
    font-size: 14px; background: #fff; color: var(--ink);
  }
  .drop {
    border: 2px dashed var(--yale-blue-lt); border-radius: 12px; padding: 30px;
    text-align: center; color: var(--muted); cursor: pointer; transition: .15s; background: #fbfcfe;
  }
  .drop:hover, .drop.over { border-color: var(--yale-blue-md); background: #eef4fc; color: var(--yale-blue); }
  .drop b { color: var(--yale-blue); }
  .row { display: flex; gap: 16px; flex-wrap: wrap; }
  .row > div { flex: 1; min-width: 150px; }
  .opts { margin-top: 16px; }
  .btn {
    background: var(--yale-blue); color: #fff; border: 0; border-radius: 8px;
    padding: 11px 20px; font-size: 14.5px; font-weight: 600; cursor: pointer; margin-top: 18px;
  }
  .btn:hover { background: var(--yale-blue-dk); }
  .btn:disabled { background: #9fb2c6; cursor: not-allowed; }
  .toggle { display: flex; align-items: center; gap: 9px; margin-top: 16px; }
  .toggle input { width: 18px; height: 18px; accent-color: var(--yale-blue); }
  .toggle label { margin: 0; font-weight: 600; color: var(--ink); }
  .status { display: none; align-items: center; gap: 12px; margin-top: 6px; }
  .status.show { display: flex; }
  .spinner {
    width: 18px; height: 18px; border: 3px solid var(--line); border-top-color: var(--yale-blue);
    border-radius: 50%; animation: spin .8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .stage { font-size: 13.5px; color: var(--muted); }
  .err { color: var(--bad); font-size: 13.5px; white-space: pre-wrap; }
  .tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); margin-bottom: 14px; }
  .tab {
    padding: 8px 14px; font-size: 13px; cursor: pointer; border: 0; background: none;
    color: var(--muted); border-bottom: 2px solid transparent; font-weight: 600;
  }
  .tab.active { color: var(--yale-blue); border-bottom-color: var(--yale-blue); }
  .meta { font-size: 12.5px; color: var(--muted); margin-bottom: 12px; }
  .seg { padding: 8px 0; border-bottom: 1px solid #eef2f6; }
  .seg .ts { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 12px; margin-right: 8px; }
  .spk {
    display: inline-block; font-size: 11.5px; font-weight: 700; padding: 1px 8px; border-radius: 999px;
    background: var(--yale-blue); color: #fff; margin-right: 8px;
  }
  pre { white-space: pre-wrap; word-break: break-word; font-size: 13px; background: #f7f9fb;
        border: 1px solid var(--line); border-radius: 8px; padding: 14px; max-height: 460px; overflow: auto; }
  .dl { margin-top: 12px; display: flex; gap: 10px; }
  .dl a { font-size: 13px; color: var(--yale-blue-md); text-decoration: none; font-weight: 600;
          border: 1px solid var(--line); padding: 6px 12px; border-radius: 8px; }
  .dl a:hover { background: #eef4fc; }
  .hidden { display: none; }
</style>
</head>
<body>
<header>
  <div class="mark">CW</div>
  <div>
    <h1>CHAI-Whisper</h1>
    <div class="sub">Audio transcription &amp; speaker diarization on the Yale DGX</div>
  </div>
  <div class="pill"><span id="pillDot" class="dot"></span><span id="pillText">checking DGX…</span></div>
</header>

<main>
  <div class="card">
    <h2>Transcribe an audio or video file</h2>
    <div id="drop" class="drop">
      <b>Choose a file</b> or drag it here
      <div style="font-size:12.5px;margin-top:6px">Only 16&nbsp;kHz mono audio is sent to the DGX — never the video.</div>
      <input id="file" type="file" accept="audio/*,video/*" class="hidden">
    </div>
    <div id="fileName" class="meta" style="margin-top:10px"></div>

    <div class="opts">
      <div class="row">
        <div>
          <label>Whisper model</label>
          <select id="model">
            <option value="large-v3">large-v3 (best)</option>
            <option value="medium">medium</option>
            <option value="small">small</option>
            <option value="base">base</option>
            <option value="tiny">tiny (fastest)</option>
          </select>
        </div>
        <div>
          <label>Language (blank = auto-detect)</label>
          <input id="language" type="text" placeholder="e.g. en">
        </div>
      </div>
      <div class="toggle">
        <input id="diarize" type="checkbox" checked>
        <label for="diarize">Identify speakers (diarization)</label>
      </div>
      <div id="spkRow" class="row" style="margin-top:12px">
        <div>
          <label>Exact # speakers (optional)</label>
          <input id="num_speakers" type="number" min="1" placeholder="e.g. 2">
        </div>
        <div>
          <label>Min speakers</label>
          <input id="min_speakers" type="number" min="1" placeholder="optional">
        </div>
        <div>
          <label>Max speakers</label>
          <input id="max_speakers" type="number" min="1" placeholder="optional">
        </div>
      </div>
    </div>

    <button id="go" class="btn" disabled>Transcribe</button>
    <div id="status" class="status">
      <div class="spinner"></div><div class="stage" id="stage">…</div>
    </div>
    <div id="error" class="err"></div>
  </div>

  <div id="results" class="card hidden">
    <div class="tabs">
      <button class="tab active" data-tab="segments">Transcript</button>
      <button class="tab" data-tab="text">Plain text</button>
      <button class="tab" data-tab="srt">SRT</button>
    </div>
    <div id="resultMeta" class="meta"></div>
    <div id="tab-segments"></div>
    <pre id="tab-text" class="hidden"></pre>
    <pre id="tab-srt" class="hidden"></pre>
    <div class="dl">
      <a href="#" id="dlTxt">Download .txt</a>
      <a href="#" id="dlSrt">Download .srt</a>
      <a href="#" id="dlJson">Download .json</a>
    </div>
  </div>
</main>

<script>
const $ = id => document.getElementById(id);
let currentResult = null;

// --- DGX health pill ---
async function refreshStatus() {
  try {
    const s = await (await fetch("/api/remote/status")).json();
    const dot = $("pillDot"), txt = $("pillText");
    if (!s.enabled) { dot.className = "dot off"; txt.textContent = "DGX not configured"; return; }
    if (s.reachable) {
      dot.className = "dot on";
      txt.textContent = "DGX online" + (s.cuda ? " · GPU" : "") + (s.gpu ? " · " + s.gpu : "");
    } else { dot.className = "dot off"; txt.textContent = "DGX unreachable"; }
  } catch (e) { $("pillDot").className = "dot off"; $("pillText").textContent = "status error"; }
}
refreshStatus(); setInterval(refreshStatus, 20000);

// --- file picking ---
const drop = $("drop"), fileInput = $("file");
let picked = null;
drop.onclick = () => fileInput.click();
fileInput.onchange = () => setFile(fileInput.files[0]);
["dragover","dragenter"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => { if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });
function setFile(f) {
  if (!f) return;
  picked = f;
  $("fileName").textContent = "Selected: " + f.name + " (" + (f.size/1048576).toFixed(1) + " MB)";
  $("go").disabled = false;
}
$("diarize").onchange = () => { $("spkRow").style.opacity = $("diarize").checked ? "1" : ".4"; };

// --- run ---
$("go").onclick = async () => {
  if (!picked) return;
  $("go").disabled = true;
  $("error").textContent = "";
  $("results").classList.add("hidden");
  $("status").classList.add("show");
  $("stage").textContent = "uploading to local app…";

  const fd = new FormData();
  fd.append("file", picked);
  fd.append("model", $("model").value);
  fd.append("language", $("language").value.trim());
  fd.append("diarize", $("diarize").checked ? "true" : "false");
  ["num_speakers","min_speakers","max_speakers"].forEach(k => {
    if ($(k).value.trim()) fd.append(k, $(k).value.trim());
  });

  try {
    const r = await fetch("/api/transcribe", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "submit failed");
    poll(j.job_id);
  } catch (e) { fail(e.message); }
};

function fail(msg) {
  $("status").classList.remove("show");
  $("error").textContent = msg;
  $("go").disabled = false;
}

async function poll(jobId) {
  try {
    const st = await (await fetch("/api/jobs/" + jobId)).json();
    $("stage").textContent = (st.stage || st.status || "working") +
      (st.audio_kb ? "  ·  audio " + st.audio_kb + " KB" : "");
    if (st.status === "done") { showResult(jobId); return; }
    if (st.status === "error") { fail(st.error || "transcription failed"); return; }
    setTimeout(() => poll(jobId), 1500);
  } catch (e) { fail(e.message); }
}

async function showResult(jobId) {
  const res = await (await fetch("/api/jobs/" + jobId + "/result")).json();
  currentResult = res;
  $("status").classList.remove("show");
  $("go").disabled = false;

  const md = res.metadata || {};
  const segs = md.segments || [];
  $("resultMeta").textContent =
    "language: " + (md.language || "?") +
    "  ·  duration: " + fmtDur(md.duration) +
    "  ·  " + (md.diarize ? (md.num_speakers_detected + " speaker(s)") : "no diarization") +
    "  ·  model: " + (md.model || "?");

  $("tab-segments").innerHTML = segs.length
    ? segs.map(s => `<div class="seg">
        <span class="ts">${fmtTs(s.start)}</span>
        ${s.speaker ? `<span class="spk">${esc(s.speaker)}</span>` : ""}
        ${esc(s.text)}</div>`).join("")
    : `<div class="meta">No speech detected.</div>`;
  $("tab-text").textContent = (md.txt || res.value || "").trim();
  $("tab-srt").textContent = md.srt || "";

  const base = ($("fileName").textContent.match(/Selected: (.+?) \(/) || [,"transcript"])[1]
                 .replace(/\.[^.]+$/, "");
  wireDownload("dlTxt", base + ".txt", md.txt || res.value || "", "text/plain");
  wireDownload("dlSrt", base + ".srt", md.srt || "", "text/plain");
  wireDownload("dlJson", base + ".json", JSON.stringify(res, null, 2), "application/json");

  $("results").classList.remove("hidden");
}

function wireDownload(id, name, content, mime) {
  const a = $(id);
  const blob = new Blob([content], { type: mime });
  a.href = URL.createObjectURL(blob);
  a.download = name;
}

// tabs
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  ["segments","text","srt"].forEach(n =>
    $("tab-" + n).classList.toggle("hidden", n !== t.dataset.tab));
});

function fmtTs(s) {
  s = s || 0; const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = Math.floor(s%60);
  const p = n => String(n).padStart(2,"0");
  return (h ? p(h)+":" : "") + p(m) + ":" + p(sec);
}
function fmtDur(s) { if (!s) return "?"; return Math.round(s) + "s"; }
function esc(s) { return (s||"").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
</script>
</body>
</html>
"""


def main() -> None:
    """Console-script entry point kept minimal; real arg parsing lives in cli.py."""
    from .cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
