"""SQLite persistence for CHAI-Whisper (mirrors chai-docs/app/db.py conventions).

One local database holds projects, materials (A/V references), app settings, and
an append-only ``transcription_versions`` table — every save (DGX run, human
edit, import, restore) becomes a new numbered version, so no change is ever lost.

Media files are NOT stored here: ``materials.media_path`` holds the original
absolute local path only. This is the single SQLite access layer (stdlib
sqlite3, no ORM); never update version rows in place.
"""

import json
import os
import sqlite3
import uuid
from pathlib import Path

DATA_DIR = Path(os.environ.get("CHAI_WHISPER_DATA", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "chai_whisper.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    settings_json TEXT,                -- per-project defaults (formats, scan, editor name)
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    media_path TEXT NOT NULL,          -- absolute local path (or cache path for IIIF); bytes not copied for file/folder
    media_type TEXT DEFAULT 'audio',   -- 'audio' | 'video'
    source TEXT DEFAULT 'file',        -- file | folder | iiif
    source_url TEXT,                   -- provenance (e.g. IIIF manifest URL)
    duration REAL,
    width INTEGER,
    height INTEGER,
    position INTEGER DEFAULT 0,
    editors_json TEXT,                 -- JSON list of editor names assigned to this recording
    has_audio INTEGER,                 -- 1/0 from ffprobe; NULL = unknown (older rows)
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS lexicons (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    terms_json TEXT NOT NULL,          -- JSON list of terms (one per line as uploaded)
    term_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    version INTEGER,
    folder_path TEXT,
    formats_json TEXT,
    scan_json TEXT,                    -- scan summary {enabled, lexicon_ids, format, flag_count}
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS transcription_versions (
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    source TEXT DEFAULT 'edit',        -- run | edit | import | restore
    note TEXT DEFAULT '',
    model TEXT,
    editor TEXT,                       -- who saved this version (human edits)
    meta_json TEXT,                    -- provenance for TPME: params, app versions, parent, import info
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(material_id, version)
);
CREATE INDEX IF NOT EXISTS idx_materials_project ON materials(project_id);
CREATE INDEX IF NOT EXISTS idx_versions_material ON transcription_versions(material_id);
CREATE INDEX IF NOT EXISTS idx_runs_material ON runs(material_id);
"""

DEFAULT_SETTINGS = {
    "model": "large-v3",
    "diarize": "true",
}

# Per-project defaults, merged at read time (stored in projects.settings_json).
DEFAULT_PROJECT_SETTINGS = {
    "output_formats": ["vtt", "txt"],
    "scan_enabled": True,
    "default_lexicon_ids": [],
    "scan_format": "txt",
    "editor_name": "",          # legacy (editors are now per recording); kept for old rows
    "tpme_enabled": True,       # write TPME provenance sidecars with each export
    "provider": "",             # TPME `provider` (e.g. "Yale University Library")
}


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Background transcription threads write versions concurrently; WAL + a busy
    # timeout keep them from failing with "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    conn = connect()
    try:
        # Migrate an older project-scoped lexicons table to the global shape,
        # preserving rows, before the schema's CREATE-IF-NOT-EXISTS runs.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(lexicons)").fetchall()]
        if cols and "project_id" in cols:
            conn.execute("ALTER TABLE lexicons RENAME TO lexicons_old")
            conn.execute("""CREATE TABLE lexicons (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, terms_json TEXT NOT NULL,
                term_count INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')))""")
            conn.execute("""INSERT INTO lexicons (id, name, terms_json, term_count, created_at)
                SELECT id, name, terms_json, term_count, created_at FROM lexicons_old""")
            conn.execute("DROP TABLE lexicons_old")

        conn.executescript(SCHEMA)
        # Upgrade pre-existing databases in place; each fails harmlessly once present.
        for ddl in (
            "ALTER TABLE projects ADD COLUMN settings_json TEXT",
            "ALTER TABLE materials ADD COLUMN source TEXT DEFAULT 'file'",
            "ALTER TABLE materials ADD COLUMN source_url TEXT",
            "ALTER TABLE materials ADD COLUMN editors_json TEXT",
            "ALTER TABLE materials ADD COLUMN has_audio INTEGER",
            "ALTER TABLE transcription_versions ADD COLUMN editor TEXT",
            "ALTER TABLE transcription_versions ADD COLUMN meta_json TEXT",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- helpers
def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _row(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- settings
def get_settings() -> dict:
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()
    stored = {r["key"]: r["value"] for r in rows}
    return {**DEFAULT_SETTINGS, **stored}


def save_settings(values: dict):
    conn = connect()
    try:
        for key in DEFAULT_SETTINGS:
            if key in values and values[key] is not None:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(values[key])),
                )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- projects
def list_projects() -> list:
    conn = connect()
    try:
        return _rows(conn, """
            SELECT p.*, COUNT(m.id) AS material_count
            FROM projects p LEFT JOIN materials m ON m.project_id = p.id
            GROUP BY p.id ORDER BY p.created_at DESC""")
    finally:
        conn.close()


def create_project(name: str, description: str = "") -> dict:
    pid = new_id()
    conn = connect()
    try:
        conn.execute("INSERT INTO projects (id, name, description) VALUES (?, ?, ?)",
                     (pid, name, description))
        conn.commit()
        return _row(conn, "SELECT * FROM projects WHERE id = ?", (pid,))
    finally:
        conn.close()


def _summarize_material(conn, m: dict) -> dict:
    """Attach display metadata for the project cards/list: transcribed?, speaker
    count, language, segment count, and the sensitive-language flag count from
    the material's most recent run (None if it was never scanned)."""
    m["transcribed"] = m.get("latest_version") is not None
    m["speaker_count"] = None
    m["language"] = None
    m["segment_count"] = None
    m["flag_count"] = None
    if m["transcribed"]:
        row = conn.execute("""SELECT data_json FROM transcription_versions
            WHERE material_id = ? ORDER BY version DESC LIMIT 1""", (m["id"],)).fetchone()
        if row:
            try:
                data = json.loads(row["data_json"])
                m["speaker_count"] = len(data.get("speakers") or {})
                m["segment_count"] = len(data.get("segments") or [])
                lang = data.get("language")
                m["language"] = lang if isinstance(lang, str) else (lang or {}).get("code") if isinstance(lang, dict) else None
            except (ValueError, TypeError):
                pass
    run = conn.execute("""SELECT scan_json FROM runs WHERE material_id = ?
        ORDER BY created_at DESC LIMIT 1""", (m["id"],)).fetchone()
    if run:
        try:
            scan = json.loads(run["scan_json"] or "{}")
            if scan.get("enabled"):
                m["flag_count"] = scan.get("flag_count", 0)
        except (ValueError, TypeError):
            pass
    return m


def get_project(pid: str):
    conn = connect()
    try:
        project = _row(conn, "SELECT * FROM projects WHERE id = ?", (pid,))
        if project:
            project["settings"] = {**DEFAULT_PROJECT_SETTINGS,
                                   **(json.loads(project.get("settings_json") or "{}"))}
            mats = _rows(conn, """
                SELECT m.*,
                    (SELECT MAX(version) FROM transcription_versions v WHERE v.material_id = m.id) AS latest_version,
                    (SELECT COUNT(*) FROM transcription_versions v WHERE v.material_id = m.id) AS version_count
                FROM materials m WHERE m.project_id = ?
                ORDER BY m.position, m.name""", (pid,))
            project["materials"] = [_summarize_material(conn, _material_out(m)) for m in mats]
        return project
    finally:
        conn.close()


def delete_project(pid: str):
    conn = connect()
    try:
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- materials
def _material_out(m: dict) -> dict:
    m["editors"] = json.loads(m.pop("editors_json", None) or "[]")
    if m.get("has_audio") is not None:
        m["has_audio"] = bool(m["has_audio"])
    return m


def create_material(project_id: str, name: str, media_path: str, media_type: str,
                    duration=None, width=None, height=None, source="file", source_url=None,
                    has_audio=None) -> dict:
    mid = new_id()
    conn = connect()
    try:
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM materials WHERE project_id = ?",
                           (project_id,)).fetchone()[0]
        conn.execute("""INSERT INTO materials
            (id, project_id, name, media_path, media_type, source, source_url, duration, width, height,
             position, has_audio)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, project_id, name, media_path, media_type, source, source_url, duration, width, height,
             pos, None if has_audio is None else int(bool(has_audio))))
        conn.commit()
        return _material_out(_row(conn, "SELECT * FROM materials WHERE id = ?", (mid,)))
    finally:
        conn.close()


def get_material(mid: str):
    conn = connect()
    try:
        material = _row(conn, "SELECT * FROM materials WHERE id = ?", (mid,))
        if material:
            _material_out(material)
            material["media_exists"] = bool(material["media_path"]) and os.path.exists(material["media_path"])
            material["versions"] = _rows(conn, """
                SELECT id, version, source, note, model, editor, created_at
                FROM transcription_versions WHERE material_id = ?
                ORDER BY version DESC""", (mid,))
        return material
    finally:
        conn.close()


def set_material_editors(mid: str, editors: list[str]) -> list[str]:
    """Replace a recording's editor list (trimmed, de-duplicated, order kept)."""
    seen, out = set(), []
    for e in editors:
        e = (e or "").strip()
        if e and e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    conn = connect()
    try:
        conn.execute("UPDATE materials SET editors_json = ? WHERE id = ?", (json.dumps(out), mid))
        conn.commit()
    finally:
        conn.close()
    return out


def is_owned_media(media_path: str) -> bool:
    """True for files we copied into our data dir (uploads / IIIF cache)."""
    try:
        return bool(media_path) and str(Path(media_path).resolve()).startswith(str(DATA_DIR.resolve()))
    except OSError:
        return False


def update_material_media(mid: str, media_path: str, info: dict):
    conn = connect()
    try:
        conn.execute("""UPDATE materials SET media_path = ?, media_type = ?, duration = ?, width = ?,
            height = ?, has_audio = ? WHERE id = ?""",
            (media_path, info["media_type"], info["duration"], info["width"], info["height"],
             None if info.get("has_audio") is None else int(bool(info["has_audio"])), mid))
        conn.commit()
    finally:
        conn.close()


def _remove_owned_media(media_path: str):
    """Delete a media file only if it lives inside our data dir (uploads / IIIF
    cache). Files referenced in place from elsewhere on disk are never touched."""
    if not media_path:
        return
    try:
        p = Path(media_path).resolve()
        if str(p).startswith(str(DATA_DIR.resolve())) and p.is_file():
            p.unlink()
    except OSError:
        pass


def delete_material(mid: str):
    conn = connect()
    try:
        row = _row(conn, "SELECT media_path FROM materials WHERE id = ?", (mid,))
        conn.execute("DELETE FROM materials WHERE id = ?", (mid,))
        conn.commit()
    finally:
        conn.close()
    if row:
        _remove_owned_media(row["media_path"])


# ---------------------------------------------------------------- versions
def save_version(material_id: str, data: dict, source: str = "edit",
                 note: str = "", model: str = None, editor: str = None,
                 meta: dict = None) -> dict:
    """Append a new numbered version snapshot. The ONLY writer of versions —
    history only ever grows; nothing is updated in place. ``meta`` records
    provenance (parent version, params, app versions) for TPME sidecars."""
    conn = connect()
    try:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM transcription_versions WHERE material_id = ?",
            (material_id,)).fetchone()[0]
        meta = {"parent_version": (nxt - 1) or None, **(meta or {})}
        vid = new_id()
        conn.execute("""INSERT INTO transcription_versions
            (id, material_id, version, data_json, source, note, model, editor, meta_json)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (vid, material_id, nxt, json.dumps(data), source, note, model, editor or None,
             json.dumps(meta)))
        conn.commit()
        return _row(conn, "SELECT id, version, source, note, model, editor, created_at "
                          "FROM transcription_versions WHERE id = ?", (vid,))
    finally:
        conn.close()


def get_version(material_id: str, version: int = None):
    conn = connect()
    try:
        if version is None:
            row = _row(conn, """SELECT * FROM transcription_versions
                WHERE material_id = ? ORDER BY version DESC LIMIT 1""", (material_id,))
        else:
            row = _row(conn, """SELECT * FROM transcription_versions
                WHERE material_id = ? AND version = ?""", (material_id, version))
        if row:
            row["data"] = json.loads(row.pop("data_json"))
            row["meta"] = json.loads(row.pop("meta_json", None) or "{}")
        return row
    finally:
        conn.close()


def list_versions(material_id: str) -> list:
    conn = connect()
    try:
        return _rows(conn, """SELECT id, version, source, note, model, editor, created_at
            FROM transcription_versions WHERE material_id = ?
            ORDER BY version DESC""", (material_id,))
    finally:
        conn.close()


# ---------------------------------------------------------------- project settings
def get_project_settings(pid: str) -> dict:
    conn = connect()
    try:
        row = _row(conn, "SELECT settings_json FROM projects WHERE id = ?", (pid,))
    finally:
        conn.close()
    stored = json.loads((row or {}).get("settings_json") or "{}") if row else {}
    return {**DEFAULT_PROJECT_SETTINGS, **stored}


def save_project_settings(pid: str, values: dict) -> dict:
    current = get_project_settings(pid)
    merged = {**current, **{k: v for k, v in values.items() if k in DEFAULT_PROJECT_SETTINGS and v is not None}}
    conn = connect()
    try:
        conn.execute("UPDATE projects SET settings_json = ? WHERE id = ?", (json.dumps(merged), pid))
        conn.commit()
    finally:
        conn.close()
    return merged


# ---------------------------------------------------------------- lexicons (global)
def create_lexicon(name: str, terms: list) -> dict:
    lid = new_id()
    terms = [t for t in terms if t]
    conn = connect()
    try:
        conn.execute("INSERT INTO lexicons (id, name, terms_json, term_count) VALUES (?,?,?,?)",
                     (lid, name, json.dumps(terms), len(terms)))
        conn.commit()
        return get_lexicon(lid)
    finally:
        conn.close()


def list_lexicons() -> list:
    conn = connect()
    try:
        return _rows(conn, "SELECT id, name, term_count, created_at FROM lexicons ORDER BY name")
    finally:
        conn.close()


def get_lexicon(lexicon_id: str):
    conn = connect()
    try:
        row = _row(conn, "SELECT * FROM lexicons WHERE id = ?", (lexicon_id,))
    finally:
        conn.close()
    if row:
        row["terms"] = json.loads(row.pop("terms_json"))
    return row


def update_lexicon(lexicon_id: str, name: str = None, terms: list = None) -> dict:
    conn = connect()
    try:
        if name is not None:
            conn.execute("UPDATE lexicons SET name = ? WHERE id = ?", (name, lexicon_id))
        if terms is not None:
            terms = [t for t in terms if t]
            conn.execute("UPDATE lexicons SET terms_json = ?, term_count = ? WHERE id = ?",
                         (json.dumps(terms), len(terms), lexicon_id))
        conn.commit()
    finally:
        conn.close()
    return get_lexicon(lexicon_id)


def get_lexicon_terms(lexicon_ids: list) -> list:
    """Merged, de-duplicated terms across the given lexicon ids (all if empty)."""
    conn = connect()
    try:
        if lexicon_ids:
            qs = ",".join("?" * len(lexicon_ids))
            rows = conn.execute(f"SELECT terms_json FROM lexicons WHERE id IN ({qs})", lexicon_ids).fetchall()
        else:
            rows = conn.execute("SELECT terms_json FROM lexicons").fetchall()
    finally:
        conn.close()
    seen, out = set(), []
    for r in rows:
        for t in json.loads(r["terms_json"]):
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return out


def delete_lexicon(lexicon_id: str):
    conn = connect()
    try:
        conn.execute("DELETE FROM lexicons WHERE id = ?", (lexicon_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- runs
def create_run(material_id: str, version: int, folder_path: str,
               formats: list, scan_summary: dict) -> dict:
    rid = new_id()
    conn = connect()
    try:
        conn.execute("""INSERT INTO runs (id, material_id, version, folder_path, formats_json, scan_json)
            VALUES (?,?,?,?,?,?)""",
            (rid, material_id, version, folder_path, json.dumps(formats), json.dumps(scan_summary or {})))
        conn.commit()
        return _row(conn, "SELECT * FROM runs WHERE id = ?", (rid,))
    finally:
        conn.close()


def list_runs(material_id: str) -> list:
    conn = connect()
    try:
        rows = _rows(conn, "SELECT * FROM runs WHERE material_id = ? ORDER BY created_at DESC", (material_id,))
    finally:
        conn.close()
    for r in rows:
        r["formats"] = json.loads(r.pop("formats_json") or "[]")
        r["scan"] = json.loads(r.pop("scan_json") or "{}")
    return rows


def get_run(run_id: str):
    conn = connect()
    try:
        return _row(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
    finally:
        conn.close()
