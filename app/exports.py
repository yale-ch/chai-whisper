"""Per-run output folders.

Each transcription/export run writes its selected format files, an optional
sensitive-language scan report (txt/json/csv), TPME provenance sidecars (one
``<file>.tpme.json`` per transcript file, plus the version chain in
``<name>.tpme-history.json``), and a manifest into its own timestamped folder
under the exports root (CHAI_WHISPER_EXPORTS or data/exports), and records a
``runs`` row. Mirrors chai-docs' export-folder pattern.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import db, exporters, scan, tpme

EXPORTS_ROOT = Path(os.environ.get("CHAI_WHISPER_EXPORTS", db.DATA_DIR / "exports"))


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", (name or "").strip()) or "item"


def write_run(material: dict, version: int, data: dict, formats: list[str],
              scan_cfg: dict | None, run_stamp: str = None, tpme_enabled: bool | None = None) -> dict:
    """Write a run folder and record a runs row.

    scan_cfg: {enabled, lexicon_ids, format} — when enabled, a report is written.
    tpme_enabled: write TPME sidecars (None = use the project default).
    Returns the created run row (with folder_path).
    """
    run_stamp = run_stamp or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    project = db.get_project(material["project_id"])
    proj_name = _safe(project["name"] if project else material["project_id"])
    mat_name = _safe(Path(material["name"]).stem)
    folder = EXPORTS_ROOT / proj_name / mat_name / f"run-{run_stamp}"
    folder.mkdir(parents=True, exist_ok=True)

    settings = db.get_project_settings(material["project_id"])
    if tpme_enabled is None:
        tpme_enabled = bool(settings.get("tpme_enabled", True))
    vrow = db.get_version(material["id"], version) if tpme_enabled else None

    written = []
    for fmt in formats:
        if fmt not in exporters.RENDERERS:
            continue
        path = folder / f"{mat_name}{exporters.EXT[fmt]}"
        body = exporters.render(fmt, data).encode("utf-8")
        path.write_bytes(body)
        written.append(fmt)
        if vrow:
            rec = tpme.build_record(material, {**vrow, "data": data}, settings, fmt=fmt, file_bytes=body)
            (folder / f"{path.name}.tpme.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    if vrow:
        (folder / f"{mat_name}.tpme-history.json").write_text(
            json.dumps(tpme.history(material, settings, upto=version), ensure_ascii=False, indent=2),
            encoding="utf-8")

    scan_summary = {"enabled": False}
    if scan_cfg and scan_cfg.get("enabled"):
        terms = db.get_lexicon_terms(scan_cfg.get("lexicon_ids") or [])
        result = scan.scan_data(data, terms, scan_cfg.get("format", "txt"))
        (folder / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "report.txt").write_text(scan.report_text(result), encoding="utf-8")
        (folder / "report.csv").write_text(scan.report_csv(result), encoding="utf-8", newline="")
        scan_summary = {"enabled": True, "lexicon_ids": scan_cfg.get("lexicon_ids") or [],
                        "format": result["format"], "flag_count": result["flag_count"],
                        "terms_hit": result["terms_hit"]}

    manifest = {
        "material": material["name"], "material_id": material["id"],
        "project": project["name"] if project else None,
        "version": version, "run": run_stamp, "formats": written, "scan": scan_summary,
        "tpme": bool(vrow),
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return db.create_run(material["id"], version, str(folder), written, scan_summary)


def zip_run(folder_path: str) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    root = Path(folder_path)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in root.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(root.parent).as_posix())
    return buf.getvalue()
