"""CHAI-Whisper workspace — FastAPI app.

A local project workspace over the DGX CHAI-Whisper API: organize A/V into
projects (referenced by local path), transcribe/diarize on the DGX, review the
media beside an editable, versioned transcript, and quantify edits. Static
vanilla-JS SPA in static/ is mounted last so /api/* wins.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fastapi.responses import Response as _Resp
from src import remote
from . import db, diffing, exports, ingest, jobs, languages, media, scan, tpme

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="CHAI-Whisper")


def _git_build() -> str:
    """Short commit + date of the running code, shown in the UI so testers can
    tell at a glance whether they're on the latest version."""
    import subprocess
    try:
        return subprocess.run(["git", "log", "-1", "--format=%h · %cs"], cwd=STATIC_DIR.parent,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


_BUILD = _git_build()


@app.middleware("http")
async def _no_stale_ui(request: Request, call_next):
    """Make browsers revalidate the SPA (index.html/app.js/style.css) on every
    load. Without this, a browser may keep running a cached copy of an older
    UI after the app is updated."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.on_event("startup")
def _startup():
    db.init_db()


# ---------------------------------------------------------------- schemas
class ProjectIn(BaseModel):
    name: str
    description: str = ""


class MaterialIn(BaseModel):
    path: str


class TranscribeIn(BaseModel):
    model: str | None = None
    language: str | None = None
    diarize: bool = True
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    requested_by: str | None = None


class BatchTranscribeIn(TranscribeIn):
    material_ids: list[str]


class SaveTranscriptionIn(BaseModel):
    data: dict
    note: str = ""
    editor: str | None = None


class EditorsIn(BaseModel):
    editors: list[str]


class MaterialTpmeIn(BaseModel):
    """User-entered, per-recording TPME fields (blank clears → project default)."""
    media_title: str | None = None
    provider: str | None = None
    conventions: str | None = None
    originating_media_file: str | None = None
    media_language: list[str] | None = None
    human_review_level: str | None = None


class SettingsIn(BaseModel):
    model: str | None = None
    diarize: str | None = None


class ProjectSettingsIn(BaseModel):
    output_formats: list[str] | None = None
    scan_enabled: bool | None = None
    default_lexicon_ids: list[str] | None = None
    scan_format: str | None = None
    editor_name: str | None = None
    tpme_enabled: bool | None = None
    provider: str | None = None
    conventions: str | None = None
    tpme_history: str | None = None


class IIIFIn(BaseModel):
    manifest_url: str


class LexiconIn(BaseModel):
    name: str | None = None
    terms: list[str] | None = None
    text: str | None = None  # newline-separated, parsed server-side


class ScanCfg(BaseModel):
    enabled: bool = True
    lexicon_ids: list[str] = []
    format: str = "txt"


class ExportIn(BaseModel):
    version: int | None = None
    formats: list[str] = ["vtt", "txt"]
    scan: ScanCfg | None = None
    tpme: bool | None = None


# ---------------------------------------------------------------- settings/status
@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.post("/api/settings")
def post_settings(body: SettingsIn):
    db.save_settings({k: v for k, v in body.model_dump().items() if v is not None})
    return db.get_settings()


@app.get("/api/version")
def api_version_info():
    return {"version": tpme.app_version(), "build": _BUILD}


@app.get("/api/languages")
def api_languages():
    return languages.all_languages()


@app.get("/api/models")
def api_models():
    return languages.MODELS


@app.get("/api/remote/status")
def remote_status(refresh: bool = False):
    return remote.status(refresh=refresh)


# ---------------------------------------------------------------- projects
@app.get("/api/projects")
def api_list_projects():
    return db.list_projects()


@app.post("/api/projects")
def api_create_project(body: ProjectIn):
    if not body.name.strip():
        raise HTTPException(400, "project name required")
    return db.create_project(body.name.strip(), body.description.strip())


@app.get("/api/projects/{pid}")
def api_get_project(pid: str):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "project not found")
    return p


@app.delete("/api/projects/{pid}")
def api_delete_project(pid: str):
    db.delete_project(pid)
    return {"deleted": True}


# ---------------------------------------------------------------- project settings
@app.get("/api/projects/{pid}/settings")
def api_get_project_settings(pid: str):
    if not db.get_project(pid):
        raise HTTPException(404, "project not found")
    return db.get_project_settings(pid)


@app.put("/api/projects/{pid}/settings")
def api_save_project_settings(pid: str, body: ProjectSettingsIn):
    if not db.get_project(pid):
        raise HTTPException(404, "project not found")
    return db.save_project_settings(pid, {k: v for k, v in body.model_dump().items() if v is not None})


# ---------------------------------------------------------------- lexicons (global)
@app.get("/api/lexicons")
def api_list_lexicons():
    return db.list_lexicons()


@app.get("/api/lexicons/{lid}")
def api_get_lexicon(lid: str):
    lex = db.get_lexicon(lid)
    if not lex:
        raise HTTPException(404, "lexicon not found")
    return lex


@app.post("/api/lexicons")
def api_create_lexicon(body: LexiconIn):
    name = (body.name or "").strip() or "lexicon"
    terms = body.terms if body.terms is not None else scan.parse_terms(body.text or "")
    return db.create_lexicon(name, terms)


@app.post("/api/lexicons/upload")
async def api_upload_lexicon(file: UploadFile = File(...)):
    raw = await file.read()
    terms = scan.parse_terms(raw)
    if not terms:
        raise HTTPException(400, "no terms found in the uploaded file")
    return db.create_lexicon(file.filename or "lexicon", terms)


@app.put("/api/lexicons/{lid}")
def api_update_lexicon(lid: str, body: LexiconIn):
    if not db.get_lexicon(lid):
        raise HTTPException(404, "lexicon not found")
    terms = body.terms if body.terms is not None else (scan.parse_terms(body.text) if body.text is not None else None)
    return db.update_lexicon(lid, name=(body.name.strip() if body.name else None), terms=terms)


@app.delete("/api/lexicons/{lid}")
def api_delete_lexicon(lid: str):
    db.delete_lexicon(lid)
    return {"deleted": True}


# ---------------------------------------------------------------- materials
@app.post("/api/projects/{pid}/materials")
def api_add_material(pid: str, body: MaterialIn):
    if not db.get_project(pid):
        raise HTTPException(404, "project not found")
    try:
        created = ingest.register_path(pid, body.path)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"created": created}


@app.post("/api/projects/{pid}/materials/upload")
async def api_upload_material(pid: str, files: list[UploadFile] = File(...), source: str = "file"):
    if not db.get_project(pid):
        raise HTTPException(404, "project not found")
    created, skipped = [], []
    for f in files:
        try:
            created.append(ingest.register_upload(pid, f.filename or "media", await f.read(), source=source))
        except ValueError:
            skipped.append(f.filename)  # non-A/V files in a picked folder
    if not created:
        raise HTTPException(400, "no audio/video files among the selection")
    return {"created": created, "skipped": skipped}


@app.post("/api/projects/{pid}/materials/iiif")
def api_add_iiif(pid: str, body: IIIFIn):
    if not db.get_project(pid):
        raise HTTPException(404, "project not found")
    job_id = jobs.submit_iiif(pid, body.manifest_url.strip())
    return {"job_id": job_id}


@app.get("/api/materials/{mid}")
def api_get_material(mid: str):
    m = db.get_material(mid)
    if not m:
        raise HTTPException(404, "material not found")
    if m.get("media_exists") and (m.get("has_audio") is None or (m.get("audio_rev") or 0) < media.AUDIO_REV):
        # One-time (per media.AUDIO_REV) check that the browser can play this
        # file's sound. Files we own are fixed in the background; for files
        # referenced in place the viewer offers a "fix playback audio" button.
        info = media.probe(m["media_path"])
        if info.get("has_audio") is not None:
            db.update_material_media(mid, m["media_path"], info, audio_rev=media.AUDIO_REV)
            if db.is_owned_media(m["media_path"]) and media.playback_issue(m["media_path"], info):
                jobs.submit_media_fix(mid)
            m = db.get_material(mid)
    if m.get("media_exists"):
        m["playback_issue"] = media.playback_issue(m["media_path"], media.probe(m["media_path"]))
    m["media_fix_running"] = any(j["kind"] == "media" and j["material_id"] == mid and j["status"] in ("queued", "running")
                                 for j in jobs.list_jobs())
    return m


@app.post("/api/materials/{mid}/fix-audio")
def api_fix_audio(mid: str):
    m = db.get_material(mid)
    if not m:
        raise HTTPException(404, "material not found")
    if not m.get("media_exists"):
        raise HTTPException(400, "media file not found on disk")
    if m.get("has_audio") is False:
        raise HTTPException(400, "this file has no audio track")
    return {"job_id": jobs.submit_media_fix(mid)}


@app.put("/api/materials/{mid}/tpme")
def api_set_material_tpme(mid: str, body: MaterialTpmeIn):
    if not db.get_material(mid):
        raise HTTPException(404, "material not found")
    vals = body.model_dump(exclude_unset=True)
    if "human_review_level" in vals and vals["human_review_level"] not in (None, "", *tpme.REVIEW_LEVELS):
        raise HTTPException(400, f"human_review_level must be one of {', '.join(tpme.REVIEW_LEVELS)}")
    if "media_language" in vals:
        try:
            vals["media_language"] = tpme.normalize_languages(vals["media_language"] or [])
        except ValueError as e:
            raise HTTPException(400, str(e))
    for k, v in vals.items():
        if isinstance(v, str):
            vals[k] = v.strip()
    return {"tpme": db.set_material_tpme(mid, vals)}


@app.delete("/api/materials/{mid}")
def api_delete_material(mid: str):
    db.delete_material(mid)
    return {"deleted": True}


@app.put("/api/materials/{mid}/editors")
def api_set_editors(mid: str, body: EditorsIn):
    if not db.get_material(mid):
        raise HTTPException(404, "material not found")
    return {"editors": db.set_material_editors(mid, body.editors)}


@app.get("/api/materials/{mid}/media")
def api_media(mid: str, request: Request):
    m = db.get_material(mid)
    if not m:
        raise HTTPException(404, "material not found")
    return media.stream(m["media_path"], request)


@app.get("/api/materials/{mid}/poster")
def api_poster(mid: str):
    m = db.get_material(mid)
    if not m or m.get("media_type") != "video":
        raise HTTPException(404, "no poster")
    img = media.poster_frame(m["media_path"])
    if not img:
        raise HTTPException(404, "no poster")
    return Response(content=img, media_type="image/jpeg")


# ---------------------------------------------------------------- transcribe / import
def _transcribe_params(body: TranscribeIn) -> dict:
    settings = db.get_settings()
    try:
        language = languages.normalize(body.language)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        model, language = languages.resolve_model(body.model, language, settings.get("model", "large-v3"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    params = {
        "model": model,
        "language": language,
        "diarize": body.diarize,
    }
    for k in ("num_speakers", "min_speakers", "max_speakers"):
        v = getattr(body, k)
        if v is not None:
            params[k] = v
    return params


@app.post("/api/materials/{mid}/transcribe")
def api_transcribe(mid: str, body: TranscribeIn):
    m = db.get_material(mid)
    if not m:
        raise HTTPException(404, "material not found")
    job_id = jobs.submit(mid, _transcribe_params(body), requested_by=body.requested_by)
    return {"job_id": job_id}


@app.post("/api/projects/{pid}/transcribe")
def api_transcribe_batch(pid: str, body: BatchTranscribeIn):
    """Queue one DGX job per selected material (they run one after another)."""
    if not db.get_project(pid):
        raise HTTPException(404, "project not found")
    params = _transcribe_params(body)
    queued, skipped = [], []
    for mid in body.material_ids:
        m = db.get_material(mid)
        if not m or m["project_id"] != pid:
            skipped.append({"id": mid, "reason": "not in this project"})
        elif not m.get("media_exists"):
            skipped.append({"id": mid, "name": m["name"], "reason": "media file missing"})
        elif m.get("has_audio") is False:
            skipped.append({"id": mid, "name": m["name"], "reason": "no audio track"})
        else:
            queued.append({"id": mid, "job_id": jobs.submit(mid, params, requested_by=body.requested_by)})
    return {"queued": queued, "skipped": skipped}


@app.get("/api/jobs")
def api_jobs():
    return jobs.list_jobs()


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    return jobs.public(j)


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    if not jobs.cancel(job_id):
        raise HTTPException(409, "only queued jobs can be cancelled")
    return {"cancelled": True}


@app.delete("/api/jobs/{job_id}")
def api_job_dismiss(job_id: str):
    return {"dismissed": jobs.dismiss(job_id)}


@app.post("/api/materials/{mid}/import")
async def api_import(mid: str, file: UploadFile = File(...)):
    m = db.get_material(mid)
    if not m:
        raise HTTPException(404, "material not found")
    raw = await file.read()
    try:
        data, fmt, timed = ingest.parse_transcript(raw, file.filename or "", duration=m.get("duration"))
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(400, f"could not parse transcript: {e}")
    note = f"imported {file.filename}" + ("" if timed else " (no timestamps found; timings estimated)")
    ver = db.save_version(mid, data, source="import", note=note,
                          meta={"import_format": fmt, "import_filename": file.filename, "timed": timed})
    return {**ver, "format": fmt, "timed": timed, "segments": len(data.get("segments", []))}


# ---------------------------------------------------------------- versions
@app.get("/api/materials/{mid}/versions")
def api_versions(mid: str):
    return db.list_versions(mid)


@app.get("/api/materials/{mid}/versions/{version}")
def api_version(mid: str, version: int):
    v = db.get_version(mid, version)
    if not v:
        raise HTTPException(404, "version not found")
    return v


@app.put("/api/materials/{mid}/transcription")
def api_save_transcription(mid: str, body: SaveTranscriptionIn):
    m = db.get_material(mid)
    if not m:
        raise HTTPException(404, "material not found")
    return db.save_version(mid, body.data, source="edit", note=body.note, editor=body.editor,
                           meta=tpme.version_meta(m))


@app.post("/api/materials/{mid}/versions/{version}/restore")
def api_restore(mid: str, version: int, editor: str | None = None):
    v = db.get_version(mid, version)
    m = db.get_material(mid)
    if not v or not m:
        raise HTTPException(404, "version not found")
    return db.save_version(mid, v["data"], source="restore", editor=editor,
                           note=f"Restored from v{version}", meta={"restored_from": version, **tpme.version_meta(m)})


@app.get("/api/materials/{mid}/versions/{version}/tpme")
def api_tpme(mid: str, version: int, history: bool = False):
    m = db.get_material(mid)
    v = db.get_version(mid, version)
    if not m or not v:
        raise HTTPException(404, "version not found")
    settings = db.get_project_settings(m["project_id"])
    if history:
        return tpme.history(m, settings, upto=version)
    return tpme.build_record(m, v, settings, scan_summary=tpme.last_scan(mid, version))


# ---------------------------------------------------------------- scan / export / runs
@app.get("/api/materials/{mid}/scan")
def api_scan(mid: str, version: int | None = None, lexicon_ids: str = "", fmt: str = "txt"):
    v = db.get_version(mid, version)
    if not v:
        raise HTTPException(404, "no transcript to scan")
    ids = [x for x in lexicon_ids.split(",") if x]
    terms = db.get_lexicon_terms(ids)
    return scan.scan_data(v["data"], terms, fmt)


@app.get("/api/materials/{mid}/scan.csv")
def api_scan_csv(mid: str, version: int | None = None, lexicon_ids: str = "", fmt: str = "txt"):
    m = db.get_material(mid)
    v = db.get_version(mid, version)
    if not m or not v:
        raise HTTPException(404, "no transcript to scan")
    terms = db.get_lexicon_terms([x for x in lexicon_ids.split(",") if x])
    result = scan.scan_data(v["data"], terms, fmt)
    stem = exports._safe(Path(m["name"]).stem).encode("ascii", "ignore").decode() or "transcript"
    fname = f"{stem}_v{v['version']}_scan_{result['format']}.csv"
    return _Resp(content=scan.report_csv(result).encode("utf-8"), media_type="text/csv; charset=utf-8",
                 headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/materials/{mid}/export")
def api_export(mid: str, body: ExportIn):
    m = db.get_material(mid)
    if not m:
        raise HTTPException(404, "material not found")
    v = db.get_version(mid, body.version)
    if not v:
        raise HTTPException(404, "no version to export")
    scan_cfg = body.scan.model_dump() if body.scan else None
    run = exports.write_run(m, v["version"], v["data"], body.formats, scan_cfg, tpme_enabled=body.tpme)
    return run


@app.get("/api/materials/{mid}/runs")
def api_runs(mid: str):
    return db.list_runs(mid)


@app.get("/api/runs/{rid}/download")
def api_run_download(rid: str):
    run = db.get_run(rid)
    if not run or not run.get("folder_path"):
        raise HTTPException(404, "run not found")
    import os
    if not os.path.isdir(run["folder_path"]):
        raise HTTPException(410, "run folder no longer on disk")
    data = exports.zip_run(run["folder_path"])
    fname = os.path.basename(run["folder_path"]) + ".zip"
    return _Resp(content=data, media_type="application/zip",
                 headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/materials/{mid}/versions/{version}/changes")
def api_changes(mid: str, version: int, base: int | None = None):
    curr = db.get_version(mid, version)
    if not curr:
        raise HTTPException(404, "version not found")
    base_v = base if base is not None else version - 1
    prev = db.get_version(mid, base_v)
    prev_data = prev["data"] if prev else {"segments": [], "speakers": {}}
    metrics = diffing.diff(prev_data, curr["data"])
    metrics["total_edits"] = diffing.total_edits(metrics)
    metrics["base"] = base_v if prev else None
    metrics["version"] = version
    return metrics


@app.get("/api/materials/{mid}/stats")
def api_stats(mid: str):
    versions = db.list_versions(mid)  # newest first
    ordered = sorted(versions, key=lambda v: v["version"])
    snapshots = [db.get_version(mid, v["version"])["data"] for v in ordered]
    agg = diffing.cumulative(snapshots)
    agg["versions"] = len(snapshots)
    return agg


# ---------------------------------------------------------------- static SPA (mounted last)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def serve():
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=5059, reload=False)


if __name__ == "__main__":
    serve()
