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
from . import db, diffing, exports, ingest, jobs, media, scan

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="CHAI-Whisper")


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


class SaveTranscriptionIn(BaseModel):
    data: dict
    note: str = ""


class SettingsIn(BaseModel):
    model: str | None = None
    diarize: str | None = None


class ProjectSettingsIn(BaseModel):
    output_formats: list[str] | None = None
    scan_enabled: bool | None = None
    default_lexicon_ids: list[str] | None = None
    scan_format: str | None = None
    editor_name: str | None = None


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


# ---------------------------------------------------------------- settings/status
@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.post("/api/settings")
def post_settings(body: SettingsIn):
    db.save_settings({k: v for k, v in body.model_dump().items() if v is not None})
    return db.get_settings()


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
    return m


@app.delete("/api/materials/{mid}")
def api_delete_material(mid: str):
    db.delete_material(mid)
    return {"deleted": True}


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
@app.post("/api/materials/{mid}/transcribe")
def api_transcribe(mid: str, body: TranscribeIn):
    m = db.get_material(mid)
    if not m:
        raise HTTPException(404, "material not found")
    settings = db.get_settings()
    params = {
        "model": body.model or settings.get("model", "large-v3"),
        "language": (body.language or None),
        "diarize": body.diarize,
    }
    for k in ("num_speakers", "min_speakers", "max_speakers"):
        v = getattr(body, k)
        if v is not None:
            params[k] = v
    job_id = jobs.submit(mid, params)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    return {k: j.get(k) for k in ("status", "stage", "error", "audio_kb", "version",
                                  "progress", "count", "run_folder", "export_error", "kind")}


@app.post("/api/materials/{mid}/import")
async def api_import(mid: str, file: UploadFile = File(...)):
    m = db.get_material(mid)
    if not m:
        raise HTTPException(404, "material not found")
    raw = await file.read()
    name = (file.filename or "").lower()
    try:
        if name.endswith(".srt"):
            data = ingest.parse_srt(raw)
        else:
            data = ingest.parse_json_import(raw)
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(400, f"could not parse transcript: {e}")
    ver = db.save_version(mid, data, source="import", note=f"imported {file.filename}")
    return ver


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
    if not db.get_material(mid):
        raise HTTPException(404, "material not found")
    return db.save_version(mid, body.data, source="edit", note=body.note)


@app.post("/api/materials/{mid}/versions/{version}/restore")
def api_restore(mid: str, version: int):
    v = db.get_version(mid, version)
    if not v:
        raise HTTPException(404, "version not found")
    return db.save_version(mid, v["data"], source="restore",
                           note=f"Restored from v{version}")


# ---------------------------------------------------------------- scan / export / runs
@app.get("/api/materials/{mid}/scan")
def api_scan(mid: str, version: int | None = None, lexicon_ids: str = "", fmt: str = "txt"):
    v = db.get_version(mid, version)
    if not v:
        raise HTTPException(404, "no transcript to scan")
    ids = [x for x in lexicon_ids.split(",") if x]
    terms = db.get_lexicon_terms(ids)
    return scan.scan_data(v["data"], terms, fmt)


@app.post("/api/materials/{mid}/export")
def api_export(mid: str, body: ExportIn):
    m = db.get_material(mid)
    if not m:
        raise HTTPException(404, "material not found")
    v = db.get_version(mid, body.version)
    if not v:
        raise HTTPException(404, "no version to export")
    scan_cfg = body.scan.model_dump() if body.scan else None
    run = exports.write_run(m, v["version"], v["data"], body.formats, scan_cfg)
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
