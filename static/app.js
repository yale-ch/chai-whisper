"use strict";

// ---------------- helpers ----------------
const $ = (id) => document.getElementById(id);
const view = () => $("view");

async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers = { ...(opts.headers || {}), "Content-Type": "application/json" };
    delete opts.json;
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}
function esc(s) { return (s ?? "").toString().replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function toast(msg, isErr = false) {
  const t = $("toast"); t.textContent = msg; t.className = "show" + (isErr ? " error" : "");
  setTimeout(() => (t.className = ""), 2800);
}
function fmtTs(sec) {
  sec = Math.max(0, sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  const p = (n) => String(n).padStart(2, "0");
  return (h ? h + ":" + p(m) : m) + ":" + p(s);
}
function fmtTsMs(sec) {
  sec = Math.max(0, sec || 0);
  const whole = Math.floor(sec), ms = Math.round((sec - whole) * 1000);
  return fmtTs(whole) + "." + String(ms).padStart(3, "0");
}
function parseTs(str) {
  // accepts "mm:ss.mmm" | "h:mm:ss" | "ss.mmm" | plain seconds
  str = (str || "").trim(); if (!str) return null;
  if (/^\d+(\.\d+)?$/.test(str)) return parseFloat(str);
  const parts = str.split(":").map(parseFloat);
  if (parts.some(isNaN)) return null;
  return parts.reduce((acc, v) => acc * 60 + v, 0);
}
function uid(prefix) { return prefix + "-" + Math.random().toString(36).slice(2, 9); }

// ---------------- DGX status pill ----------------
async function refreshStatus() {
  const dot = $("pillDot"), txt = $("pillText");
  try {
    const s = await api("/api/remote/status");
    if (!s.enabled) { dot.className = "dot off"; txt.textContent = "DGX not configured"; return; }
    if (s.reachable) { dot.className = "dot on"; txt.textContent = "DGX online" + (s.cuda ? " · GPU" : ""); }
    else { dot.className = "dot off"; txt.textContent = "DGX unreachable"; }
  } catch { dot.className = "dot off"; txt.textContent = "status error"; }
}

// ---------------- router ----------------
async function render() {
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/material\/([^/]+)/);
  const p = hash.match(/^#\/project\/([^/]+)/);
  const lexEdit = hash.match(/^#\/lexicon\/([^/]+)/);
  const nav = hash.startsWith("#/lexicon") ? "lexicons" : "projects";
  document.querySelectorAll("#topnav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
  try {
    if (m) await viewMaterial(m[1]);
    else if (p) await viewProject(p[1]);
    else if (lexEdit) await viewLexiconEditor(lexEdit[1]);
    else if (hash.startsWith("#/lexicons")) await viewLexicons();
    else await viewProjects();
  } catch (e) { view().innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", render);
window.addEventListener("load", () => { refreshStatus(); setInterval(refreshStatus, 20000); render(); });

// ================= projects =================
async function viewProjects() {
  const projects = await api("/api/projects");
  view().innerHTML = `
    <h1>Projects</h1>
    <div class="card" style="margin-bottom:18px">
      <div class="row">
        <input id="pName" type="text" class="grow" placeholder="New project name">
        <button id="pCreate">Create project</button>
      </div>
    </div>
    ${projects.length ? `<div class="project-grid">${projects.map(projectCard).join("")}</div>`
      : `<div class="empty">No projects yet. Create one above.</div>`}`;
  $("pCreate").onclick = async () => {
    const name = $("pName").value.trim();
    if (!name) return toast("Enter a project name", true);
    const p = await api("/api/projects", { method: "POST", json: { name } });
    location.hash = `#/project/${p.id}`;
  };
  view().querySelectorAll("[data-pid]").forEach((el) => {
    el.onclick = (e) => { if (!e.target.closest(".del-x")) location.hash = `#/project/${el.dataset.pid}`; };
  });
  view().querySelectorAll(".del-x").forEach((b) => {
    b.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this project and all its materials?")) return;
      await api(`/api/projects/${b.dataset.del}`, { method: "DELETE" });
      render();
    };
  });
}
function projectCard(p) {
  return `<div class="card project-card" data-pid="${p.id}">
    <button class="del-x" data-del="${p.id}" title="Delete">✕</button>
    <h3>${esc(p.name)}</h3>
    <div class="muted">${esc(p.description || "")}</div>
    <div style="margin-top:10px"><span class="chip">${p.material_count} material${p.material_count == 1 ? "" : "s"}</span></div>
  </div>`;
}

// ================= lexicons (global) =================
async function viewLexicons() {
  const lexicons = await api("/api/lexicons");
  view().innerHTML = `
    <h1>Lexicons</h1>
    <div class="muted" style="margin-bottom:14px">Sensitive-language term lists, shared across all projects. The scan flags these terms in transcripts.</div>
    <div class="card" style="margin-bottom:18px">
      <div class="row">
        <input id="lxName" type="text" class="grow" placeholder="New lexicon name">
        <button id="lxCreate">Create empty</button>
        <span class="sep"></span>
        <button class="secondary" id="lxUpload">Upload .txt…</button>
        <input id="lxFile" type="file" accept=".txt,.csv" style="display:none">
      </div>
      <div class="muted" style="margin-top:6px">Upload a plain text file with one term (or phrase) per line.</div>
    </div>
    ${lexicons.length ? `<div class="mat-list">${lexicons.map(lexRow).join("")}</div>`
      : `<div class="empty">No lexicons yet.</div>`}`;
  $("lxCreate").onclick = async () => {
    const name = $("lxName").value.trim(); if (!name) return toast("Enter a name", true);
    const l = await api("/api/lexicons", { method: "POST", json: { name, terms: [] } });
    location.hash = `#/lexicon/${l.id}`;
  };
  $("lxUpload").onclick = () => $("lxFile").click();
  $("lxFile").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try { await api("/api/lexicons/upload", { method: "POST", body: fd }); toast("Lexicon uploaded"); viewLexicons(); }
    catch (err) { toast(err.message, true); }
  };
  view().querySelectorAll("[data-lex]").forEach((el) => el.onclick = (e) => {
    if (!e.target.closest(".del-x")) location.hash = `#/lexicon/${el.dataset.lex}`;
  });
  view().querySelectorAll(".del-x").forEach((b) => b.onclick = async (e) => {
    e.stopPropagation(); if (!confirm("Delete this lexicon?")) return;
    await api(`/api/lexicons/${b.dataset.del}`, { method: "DELETE" }); viewLexicons();
  });
}
function lexRow(l) {
  return `<div class="mat-row" data-lex="${l.id}">
    <span class="mat-icon">🚩</span>
    <div><div class="mat-name">${esc(l.name)}</div><div class="mat-sub">${l.term_count} term${l.term_count == 1 ? "" : "s"}</div></div>
    <span class="spacer"></span>
    <button class="del-x" data-del="${l.id}" title="Delete">✕</button>
  </div>`;
}
async function viewLexiconEditor(lid) {
  const lex = await api(`/api/lexicons/${lid}`);
  view().innerHTML = `
    <div class="crumb"><a href="#/lexicons">Lexicons</a> › ${esc(lex.name)}</div>
    <div class="card">
      <label class="field"><span>Name</span><input id="lxEditName" type="text" value="${esc(lex.name)}"></label>
      <label class="field"><span>Terms — one per line (blank lines and # comments ignored)</span>
        <textarea id="lxTerms" rows="18" style="width:100%; font-family:var(--sans)">${esc((lex.terms || []).join("\n"))}</textarea>
      </label>
      <div class="row">
        <button id="lxSave">Save</button>
        <span id="lxCount" class="muted">${lex.term_count} terms</span>
        <span class="grow"></span>
        <button class="danger" id="lxDelete">Delete lexicon</button>
      </div>
    </div>`;
  $("lxTerms").addEventListener("input", () => {
    const n = $("lxTerms").value.split("\n").filter((t) => t.trim() && !t.trim().startsWith("#")).length;
    $("lxCount").textContent = `${n} terms`;
  });
  $("lxSave").onclick = async () => {
    await api(`/api/lexicons/${lid}`, { method: "PUT", json: { name: $("lxEditName").value.trim(), text: $("lxTerms").value } });
    toast("Saved"); viewLexiconEditor(lid);
  };
  $("lxDelete").onclick = async () => {
    if (!confirm("Delete this lexicon?")) return;
    await api(`/api/lexicons/${lid}`, { method: "DELETE" }); location.hash = "#/lexicons";
  };
}

// ================= project =================
let PROJ = null;
async function viewProject(pid) {
  const proj = await api(`/api/projects/${pid}`);
  PROJ = proj;
  const S = proj.settings || {};
  view().innerHTML = `
    <div class="crumb"><a href="#/">Projects</a> › ${esc(proj.name)}</div>
    <h1>${esc(proj.name)}</h1>

    <div class="card">
      <h3 style="margin-top:0">Add material</h3>
      <div class="tabs" id="srcTabs">
        <button class="tab active" data-src="local">Select file / folder</button>
        <button class="tab" data-src="iiif">IIIF manifest</button>
        <button class="tab" data-src="preservica" disabled title="coming soon">Preservica (soon)</button>
      </div>
      <div id="srcLocal" class="src-pane">
        <div class="muted" style="margin:8px 0">Choose audio/video file(s) or a folder. Selected media is copied into the project store (the database keeps only a path, never the bytes).</div>
        <div class="row">
          <button class="secondary" id="pickFiles">Choose file(s)…</button>
          <button class="secondary" id="pickFolder">Choose folder…</button>
          <input id="fileInput" type="file" accept="audio/*,video/*" multiple style="display:none">
          <input id="folderInput" type="file" webkitdirectory multiple style="display:none">
          <span id="uploadStatus" class="muted"></span>
        </div>
      </div>
      <div id="srcIiif" class="src-pane" style="display:none">
        <div class="muted" style="margin:8px 0">IIIF Presentation manifest URL. Sound/Video content is downloaded into the project cache.</div>
        <div class="row"><input id="iiifUrl" type="text" class="grow" placeholder="https://…/manifest.json"><button id="iiifAdd">Import</button></div>
        <div id="iiifStatus" class="job-status"></div>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <h3 style="margin-top:0">Project defaults</h3>
      <div class="muted" style="margin-bottom:8px">Lexicons are managed globally under <a href="#/lexicons">Lexicons</a> and every project can use them. The scan checks against all lexicons.</div>
      <label class="field"><span>Default output formats</span>
        <div class="row">${["vtt","txt","srt","json"].map((f)=>`<label class="toggle"><input type="checkbox" class="fmtChk" value="${f}" ${(S.output_formats||[]).includes(f)?"checked":""}> ${f}</label>`).join("")}</div>
      </label>
      <label class="toggle" style="margin-bottom:10px"><input type="checkbox" id="scanDefault" ${S.scan_enabled?"checked":""}> run sensitive-language scan by default</label>
      <div class="row">
        <label class="field grow"><span>Scan which format</span>
          <select id="scanFmt">${["txt","vtt","srt","json"].map((f)=>`<option ${S.scan_format===f?"selected":""}>${f}</option>`).join("")}</select></label>
        <label class="field grow"><span>Editor name (for notes)</span>
          <input id="editorName" type="text" value="${esc(S.editor_name||"")}" placeholder="your name"></label>
      </div>
      <button id="saveDefaults" class="small">Save defaults</button>
    </div>

    <h2>Materials <span class="muted" style="font-weight:400">(${(proj.materials||[]).length})</span></h2>
    <div class="card filter-bar" ${(proj.materials||[]).length ? "" : 'style="display:none"'}>
      <div class="row">
        <input id="matSearch" type="text" class="grow" placeholder="Search materials by name…" value="${esc(MAT_SEARCH)}">
        <div class="viewtoggle">
          <button class="vt ${MAT_VIEW==='cards'?'active':''}" data-view="cards" title="Card view">▦</button>
          <button class="vt ${MAT_VIEW==='list'?'active':''}" data-view="list" title="List view">☰</button>
        </div>
      </div>
      <div class="facets">
        <label class="facet"><span>status</span><select id="fStatus"><option value="">any</option><option value="yes">transcribed</option><option value="no">not transcribed</option></select></label>
        <label class="facet"><span>type</span><select id="fType"><option value="">any</option><option value="audio">audio</option><option value="video">video</option></select></label>
        <label class="facet"><span>sensitive language</span><select id="fFlags"><option value="">any</option><option value="flagged">has flags</option><option value="clean">no flags</option><option value="unscanned">not scanned</option></select></label>
        <label class="facet"><span>source</span><select id="fSource"><option value="">any</option><option value="file">file</option><option value="folder">folder</option><option value="iiif">iiif</option></select></label>
      </div>
    </div>
    <div id="matContainer"></div>`;

  // source tabs
  view().querySelectorAll("#srcTabs .tab").forEach((t) => t.onclick = () => {
    if (t.disabled) return;
    view().querySelectorAll("#srcTabs .tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("srcLocal").style.display = t.dataset.src === "local" ? "" : "none";
    $("srcIiif").style.display = t.dataset.src === "iiif" ? "" : "none";
  });
  const uploadFiles = async (fileList, source) => {
    const files = [...fileList].filter((f) => /\.(mp3|wav|m4a|flac|ogg|oga|opus|aac|wma|aiff|aif|alac|mp4|m4v|mov|mkv|avi|webm|flv|wmv|mpg|mpeg|ts|3gp)$/i.test(f.name));
    if (!files.length) return toast("No audio/video files in the selection", true);
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    $("uploadStatus").innerHTML = `<span class="spinner"></span> uploading ${files.length} file(s)…`;
    try {
      const res = await api(`/api/projects/${pid}/materials/upload?source=${source}`, { method: "POST", body: fd });
      toast(`Added ${res.created.length} material(s)`); viewProject(pid);
    } catch (e) { $("uploadStatus").textContent = ""; toast(e.message, true); }
  };
  $("pickFiles").onclick = () => $("fileInput").click();
  $("pickFolder").onclick = () => $("folderInput").click();
  $("fileInput").onchange = (e) => uploadFiles(e.target.files, "file");
  $("folderInput").onchange = (e) => uploadFiles(e.target.files, "folder");
  $("iiifAdd").onclick = async () => {
    const manifest_url = $("iiifUrl").value.trim(); if (!manifest_url) return toast("Enter a manifest URL", true);
    $("iiifAdd").disabled = true;
    try {
      const { job_id } = await api(`/api/projects/${pid}/materials/iiif`, { method: "POST", json: { manifest_url } });
      pollJob(job_id, (st) => { $("iiifStatus").innerHTML = `<span class="spinner"></span> ${esc(st.stage||st.status)} ${st.progress!=null?st.progress+"%":""}`; },
        () => { toast("IIIF import complete"); viewProject(pid); },
        (err) => { $("iiifStatus").innerHTML = ""; $("iiifAdd").disabled = false; toast(err, true); });
    } catch (e) { $("iiifAdd").disabled = false; toast(e.message, true); }
  };
  $("saveDefaults").onclick = async () => {
    const body = {
      output_formats: [...view().querySelectorAll(".fmtChk:checked")].map((c) => c.value),
      scan_enabled: $("scanDefault").checked,
      scan_format: $("scanFmt").value,
      editor_name: $("editorName").value.trim(),
    };
    await api(`/api/projects/${pid}/settings`, { method: "PUT", json: body });
    toast("Defaults saved");
  };
  const searchEl = $("matSearch");
  if (searchEl) {
    searchEl.oninput = () => { MAT_SEARCH = searchEl.value; renderMaterials(pid); };
    ["fStatus", "fType", "fFlags", "fSource"].forEach((id) => {
      const el = $(id); if (!el) return;
      el.value = MAT_FILTERS[id] || "";
      el.onchange = () => { MAT_FILTERS[id] = el.value; renderMaterials(pid); };
    });
    view().querySelectorAll(".vt").forEach((b) => b.onclick = () => {
      MAT_VIEW = b.dataset.view; try { localStorage.setItem("cw_matview", MAT_VIEW); } catch {}
      view().querySelectorAll(".vt").forEach((x) => x.classList.toggle("active", x === b));
      renderMaterials(pid);
    });
  }
  renderMaterials(pid);
}

// ---- materials: filtering + card/list rendering ----
let MAT_VIEW = (() => { try { return localStorage.getItem("cw_matview") || "cards"; } catch { return "cards"; } })();
let MAT_SEARCH = "";
const MAT_FILTERS = { fStatus: "", fType: "", fFlags: "", fSource: "" };

function matMatches(m) {
  if (MAT_SEARCH && !(m.name || "").toLowerCase().includes(MAT_SEARCH.toLowerCase())) return false;
  const f = MAT_FILTERS;
  if (f.fStatus === "yes" && !m.transcribed) return false;
  if (f.fStatus === "no" && m.transcribed) return false;
  if (f.fType && m.media_type !== f.fType) return false;
  if (f.fSource && (m.source || "file") !== f.fSource) return false;
  if (f.fFlags === "flagged" && !(m.flag_count > 0)) return false;
  if (f.fFlags === "clean" && !(m.flag_count === 0)) return false;
  if (f.fFlags === "unscanned" && m.flag_count !== null && m.flag_count !== undefined) return false;
  return true;
}

function renderMaterials(pid) {
  const box = $("matContainer"); if (!box) return;
  const all = (PROJ.materials || []);
  const mats = all.filter(matMatches);
  if (!all.length) { box.innerHTML = `<div class="empty">No materials yet. Add one above.</div>`; return; }
  if (!mats.length) { box.innerHTML = `<div class="empty">No materials match your search/filters.</div>`; return; }
  box.className = MAT_VIEW === "cards" ? "mat-cards" : "mat-list";
  box.innerHTML = mats.map(MAT_VIEW === "cards" ? matCard : matRow).join("");
  box.querySelectorAll("[data-mid]").forEach((el) => el.onclick = (e) => {
    if (!e.target.closest(".del-x")) location.hash = `#/material/${el.dataset.mid}`;
  });
  box.querySelectorAll(".del-x").forEach((b) => b.onclick = async (e) => {
    e.stopPropagation();
    if (!confirm("Remove this material? (its copied media file is deleted; files referenced in place are not)")) return;
    await api(`/api/materials/${b.dataset.del}`, { method: "DELETE" }); viewProject(pid);
  });
}

function _matMeta(m) {
  const chips = [];
  if (m.transcribed) chips.push(`<span class="chip green">v${m.latest_version}</span>`);
  else chips.push(`<span class="chip gray">not transcribed</span>`);
  if (m.speaker_count != null) chips.push(`<span class="chip">${m.speaker_count} speaker${m.speaker_count === 1 ? "" : "s"}</span>`);
  if (m.flag_count != null) chips.push(m.flag_count > 0
    ? `<span class="chip orange">${m.flag_count} flag${m.flag_count === 1 ? "" : "s"}</span>`
    : `<span class="chip green">no flags</span>`);
  if (m.language) chips.push(`<span class="chip">${esc(m.language)}</span>`);
  if (m.source && m.source !== "file") chips.push(`<span class="chip gray">${esc(m.source)}</span>`);
  return chips.join(" ");
}
function matRow(m) {
  const icon = m.media_type === "video" ? "🎬" : "🎧";
  const dur = m.duration ? fmtTs(m.duration) : "—";
  return `<div class="mat-row" data-mid="${m.id}">
    <span class="mat-icon">${icon}</span>
    <div class="grow"><div class="mat-name">${esc(m.name)}</div>
      <div class="mat-sub">${esc(m.media_type)} · ${dur}${m.segment_count != null ? " · " + m.segment_count + " segments" : ""}</div></div>
    <div class="mat-chips">${_matMeta(m)}</div>
    <button class="del-x" data-del="${m.id}" title="Remove">✕</button>
  </div>`;
}
function matCard(m) {
  const icon = m.media_type === "video" ? "🎬" : "🎧";
  const dur = m.duration ? fmtTs(m.duration) : "—";
  return `<div class="card mat-card" data-mid="${m.id}">
    <button class="del-x" data-del="${m.id}" title="Remove">✕</button>
    <div class="mat-card-top"><span class="mat-icon">${icon}</span>
      <div class="mat-name" title="${esc(m.name)}">${esc(m.name)}</div></div>
    <div class="mat-sub">${esc(m.media_type)} · ${dur}${m.segment_count != null ? " · " + m.segment_count + " segments" : ""}</div>
    <div class="mat-chips">${_matMeta(m)}</div>
  </div>`;
}

// ================= material viewer =================
const V = { mid: null, material: null, version: null, data: null, dirty: false,
            autoscroll: true, activeId: null, media: null, starts: [],
            flags: {}, lexicons: [], projSettings: {} };

async function viewMaterial(mid) {
  const material = await api(`/api/materials/${mid}`);
  const proj = await api(`/api/projects/${material.project_id}`);
  V.mid = mid; V.material = material; V.dirty = false; V.activeId = null;
  V.lexicons = await api("/api/lexicons"); V.projSettings = proj.settings || {};
  V.flags = {};
  const latest = material.versions[0];
  V.version = latest ? latest.version : null;
  V.data = V.version ? (await api(`/api/materials/${mid}/versions/${V.version}`)).data
                     : { kind: "transcript", speakers: {}, segments: [], notes: [], duration: material.duration };
  if (!V.data.notes) V.data.notes = [];

  view().innerHTML = `
    <div class="crumb"><a href="#/">Projects</a> › <a href="#/project/${material.project_id}">project</a> › ${esc(material.name)}</div>
    ${toolbarHTML(material)}
    <div class="viewer">
      <div class="media-pane">
        ${mediaHTML(material)}
        <div class="now-playing" id="nowPlaying"></div>
        <div class="card speakers" id="speakers"></div>
        <div class="card" id="scanCard"></div>
        <div class="card" id="notesCard"></div>
        <div class="card" id="exportCard"></div>
        <div class="card" id="changesCard" style="display:none"></div>
      </div>
      <div class="transcript" id="transcript"></div>
    </div>`;

  wireToolbar(material);
  V.media = view().querySelector("video, audio");
  if (V.media) V.media.addEventListener("timeupdate", onTimeUpdate);
  renderSpeakers(); renderTranscript(); renderScanCard(); renderNotesCard(); renderExportCard();
  await loadStats();
  await runScan(); // auto-scan with project default lexicons
}

function toolbarHTML(m) {
  const opts = m.versions.map((v) => `<option value="${v.version}">v${v.version} · ${v.source} · ${new Date(v.created_at + "Z").toLocaleString()}</option>`).join("");
  return `<div class="viewer-toolbar">
    <span class="doc-title">${esc(m.name)}</span>
    <span class="sep"></span>
    ${m.versions.length ? `<label class="muted">version <select id="verSel">${opts}</select></label>` : ""}
    <button class="small secondary" id="restoreBtn" style="display:none">Restore</button>
    <span class="sep"></span>
    <label class="toggle"><input type="checkbox" id="autoScroll" checked> auto-scroll</label>
    <span class="spacer grow"></span>
    <button class="small secondary" id="transcribeBtn">${m.versions.length ? "Re-transcribe" : "Transcribe (DGX)"}</button>
    <button class="small secondary" id="importBtn">Import…</button>
    <input type="file" id="importFile" accept=".json,.srt" style="display:none">
    <button class="small" id="saveBtn" disabled>Save edits</button>
  </div>`;
}
function mediaHTML(m) {
  if (!m.media_exists) return `<div class="media-missing">Media file not found on disk:<br><code>${esc(m.media_path)}</code></div>`;
  const src = `/api/materials/${m.id}/media`;
  if (m.media_type === "video") return `<div class="stage"><video id="media" controls preload="metadata" src="${src}"></video></div>`;
  return `<div class="audio-wrap"><audio id="media" controls preload="metadata" src="${src}"></audio></div>`;
}
function wireToolbar(m) {
  const verSel = $("verSel");
  if (verSel) {
    verSel.value = V.version;
    verSel.onchange = async () => {
      const v = parseInt(verSel.value, 10);
      V.version = v; V.dirty = false;
      V.data = (await api(`/api/materials/${m.id}/versions/${v}`)).data;
      if (!V.data.notes) V.data.notes = [];
      renderSpeakers(); renderTranscript(); renderNotesCard();
      $("saveBtn").disabled = true;
      $("restoreBtn").style.display = (v !== m.versions[0].version) ? "" : "none";
      showChanges(v); runScan();
    };
  }
  const restore = $("restoreBtn");
  if (restore) restore.onclick = async () => {
    await api(`/api/materials/${m.id}/versions/${V.version}/restore`, { method: "POST" });
    toast(`Restored v${V.version}`); viewMaterial(m.id);
  };
  $("autoScroll").onchange = (e) => { V.autoscroll = e.target.checked; };
  $("saveBtn").onclick = saveEdits;
  $("transcribeBtn").onclick = () => openTranscribe(m);
  $("importBtn").onclick = () => $("importFile").click();
  $("importFile").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try { await api(`/api/materials/${m.id}/import`, { method: "POST", body: fd }); toast("Imported"); viewMaterial(m.id); }
    catch (err) { toast(err.message, true); }
  };
}

// ---- speakers ----
function speakerColor(key) { const s = (V.data.speakers || {})[key]; return (s && s.color) || "#286DC0"; }
function speakerLabel(key) { const s = (V.data.speakers || {})[key]; return (s && s.label) || key || "—"; }

function renderSpeakers() {
  const sp = $("speakers");
  const keys = Object.keys(V.data.speakers || {});
  const counts = {};
  (V.data.segments || []).forEach((s) => { if (s.speaker) counts[s.speaker] = (counts[s.speaker] || 0) + 1; });
  sp.innerHTML = `<div class="row" style="justify-content:space-between"><h3 style="margin:0">Speakers</h3><button class="small secondary" id="spkAdd">+ add</button></div>` +
    (keys.length ? `<div style="margin-top:8px">` + keys.map((k) => `<div class="sp-row">
        <input type="color" class="sp-swatch" value="${speakerColor(k)}" data-k="${esc(k)}">
        <input type="text" class="grow" value="${esc(speakerLabel(k))}" data-label="${esc(k)}">
        <span class="chip gray">${counts[k] || 0}</span>
        <button class="del-x sp-del" data-del="${esc(k)}" title="Delete speaker">✕</button>
      </div>`).join("") + `</div>`
      : `<div class="muted" style="margin-top:8px">No speakers (run diarization to detect them).</div>`);
  $("spkAdd").onclick = () => {
    const name = prompt("New speaker name:"); if (!name) return;
    const key = uid("SPEAKER");
    V.data.speakers[key] = { label: name, color: SPK_PALETTE[keys.length % SPK_PALETTE.length] };
    markDirtyGlobal(); renderSpeakers(); renderTranscript();
  };
  sp.querySelectorAll("input[data-label]").forEach((inp) => inp.addEventListener("input", () => {
    const k = inp.dataset.label; V.data.speakers[k] = V.data.speakers[k] || {}; V.data.speakers[k].label = inp.value;
    markDirtyGlobal(); renderTranscript();
  }));
  sp.querySelectorAll("input[data-k]").forEach((inp) => inp.addEventListener("input", () => {
    const k = inp.dataset.k; V.data.speakers[k] = V.data.speakers[k] || {}; V.data.speakers[k].color = inp.value;
    markDirtyGlobal(); renderTranscript();
  }));
  sp.querySelectorAll(".sp-del").forEach((b) => b.onclick = () => deleteSpeaker(b.dataset.del));
}
const SPK_PALETTE = ["#286DC0", "#BD5319", "#5F712D", "#8B4A8B", "#2C7A7B", "#00356B", "#978D85", "#B8860B"];

function deleteSpeaker(key) {
  const others = Object.keys(V.data.speakers).filter((k) => k !== key);
  const used = (V.data.segments || []).some((s) => s.speaker === key);
  let target = null;
  if (used) {
    const choice = prompt(`Delete "${speakerLabel(key)}". Reassign its lines to which speaker?\n` +
      `Enter a name from: ${others.map(speakerLabel).join(", ") || "(none)"}\n` +
      `Leave blank to set those lines to unassigned.`, "");
    if (choice === null) return; // cancelled
    if (choice.trim()) {
      target = others.find((k) => speakerLabel(k).toLowerCase() === choice.trim().toLowerCase());
      if (!target) return toast("No speaker by that name", true);
    }
  }
  (V.data.segments || []).forEach((s) => { if (s.speaker === key) s.speaker = target; });
  delete V.data.speakers[key];
  markDirtyGlobal(); renderSpeakers(); renderTranscript();
}

// ---- transcript ----
function segFlags(segId) { return V.flags[segId] || []; }
function highlightText(text, flags) {
  if (!flags || !flags.length) return esc(text);
  // build set of [start,end) ranges (from scan, char offsets within this segment)
  const ranges = flags.map((f) => [f.start, f.end]).sort((a, b) => a[0] - b[0]);
  let out = "", cur = 0;
  for (const [a, b] of ranges) {
    if (a < cur) continue;
    out += esc(text.slice(cur, a)) + `<mark>` + esc(text.slice(a, b)) + `</mark>`;
    cur = b;
  }
  out += esc(text.slice(cur));
  return out;
}
function renderTranscript() {
  const segs = V.data.segments || [];
  V.starts = segs.map((s) => s.start || 0);
  const spkKeys = Object.keys(V.data.speakers || {});
  const noteBySeg = {};
  (V.data.notes || []).forEach((n) => { if (n.status !== "resolved") (noteBySeg[n.seg_id] = noteBySeg[n.seg_id] || []).push(n); });
  const el = $("transcript");
  if (!segs.length) { el.innerHTML = `<div class="empty">No transcript yet. Use “Transcribe (DGX)” or “Import…”.</div>`; return; }
  el.innerHTML = segs.map((s, i) => {
    const optlist = spkKeys.map((k) => `<option value="${esc(k)}" ${k === s.speaker ? "selected" : ""}>${esc(speakerLabel(k))}</option>`).join("");
    const spk = s.speaker != null
      ? `<select class="spk" data-i="${i}" style="background:${speakerColor(s.speaker)}">${optlist}<option value="__new__">＋ new…</option></select>` : "";
    const flags = segFlags(s.id);
    const notes = noteBySeg[s.id] || [];
    return `<div class="seg${flags.length ? " flagged" : ""}" data-seg-id="${esc(s.id)}" data-i="${i}">
      <div class="ts" data-i="${i}" title="click to edit start time">${fmtTs(s.start)}${notes.length ? ' <span class="note-dot" title="'+notes.length+' note(s)">●</span>' : ""}</div>
      <div class="body">
        <div class="seg-head">${spk}
          <span class="seg-tools">
            <button class="st" data-act="playhead-start" data-i="${i}" title="set start to playhead">⇤⏱</button>
            <button class="st" data-act="playhead-end" data-i="${i}" title="set end to playhead">⏱⇥</button>
            <button class="st" data-act="merge" data-i="${i}" title="merge into previous">⤒</button>
            <button class="st" data-act="split" data-i="${i}" title="split at cursor">⁄</button>
            <button class="st" data-act="note" data-i="${i}" title="add note">📝</button>
            <button class="st" data-act="del" data-i="${i}" title="delete line">✕</button>
          </span>
        </div>
        <div class="txt" contenteditable="true" data-i="${i}">${highlightText(s.text || "", flags)}</div>
      </div>
    </div>`;
  }).join("");

  el.querySelectorAll(".seg").forEach((row) => row.addEventListener("click", (e) => {
    if (e.target.closest(".txt") || e.target.closest(".spk") || e.target.closest(".seg-tools") || e.target.closest(".ts")) return;
    const i = +row.dataset.i;
    if (V.media) { V.media.currentTime = V.data.segments[i].start || 0; V.media.play().catch(() => {}); }
  }));
  el.querySelectorAll(".txt").forEach((td) => td.addEventListener("input", () => {
    const i = +td.dataset.i; V.data.segments[i].text = td.textContent; markDirty(td.closest(".seg"));
  }));
  el.querySelectorAll(".spk").forEach((sel) => {
    sel.addEventListener("change", () => {
      const i = +sel.dataset.i;
      if (sel.value === "__new__") { const name = prompt("New speaker name:"); if (name) { const key = uid("SPEAKER"); V.data.speakers[key] = { label: name, color: SPK_PALETTE[Object.keys(V.data.speakers).length % SPK_PALETTE.length] }; V.data.segments[i].speaker = key; } markDirtyGlobal(); renderSpeakers(); renderTranscript(); return; }
      V.data.segments[i].speaker = sel.value; sel.style.background = speakerColor(sel.value); markDirty(sel.closest(".seg"));
    });
    sel.addEventListener("click", (e) => e.stopPropagation());
  });
  el.querySelectorAll(".ts").forEach((ts) => ts.addEventListener("click", (e) => {
    e.stopPropagation(); const i = +ts.dataset.i; const cur = V.data.segments[i];
    const val = prompt("Start time (mm:ss.mmm):", fmtTsMs(cur.start)); if (val == null) return;
    const t = parseTs(val); if (t == null) return toast("Bad time", true);
    cur.start = t; resortSegments(); markDirtyGlobal(); renderTranscript();
  }));
  el.querySelectorAll(".st").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); segAction(b.dataset.act, +b.dataset.i); }));
}

function resortSegments() { V.data.segments.sort((a, b) => (a.start || 0) - (b.start || 0)); V.starts = V.data.segments.map((s) => s.start || 0); }

function segAction(act, i) {
  const segs = V.data.segments; const s = segs[i];
  if (act === "del") { if (!confirm("Delete this line?")) return; segs.splice(i, 1); markDirtyGlobal(); renderTranscript(); return; }
  if (act === "playhead-start") { if (V.media) { s.start = V.media.currentTime; resortSegments(); markDirtyGlobal(); renderTranscript(); } return; }
  if (act === "playhead-end") { if (V.media) { s.end = V.media.currentTime; markDirtyGlobal(); renderTranscript(); } return; }
  if (act === "merge") {
    if (i === 0) return toast("Nothing above to merge into", true);
    const prev = segs[i - 1];
    prev.text = (prev.text + " " + s.text).trim(); prev.end = s.end;
    prev.words = [...(prev.words || []), ...(s.words || [])];
    segs.splice(i, 1); markDirtyGlobal(); renderTranscript(); return;
  }
  if (act === "split") {
    const td = view().querySelector(`.txt[data-i="${i}"]`);
    let off = td ? caretOffset(td) : Math.floor((s.text || "").length / 2);
    off = Math.max(1, Math.min((s.text || "").length - 1, off));
    const t1 = s.text.slice(0, off).trim(), t2 = s.text.slice(off).trim();
    const frac = off / Math.max(1, s.text.length);
    const mid = (s.start || 0) + ((s.end || 0) - (s.start || 0)) * frac;
    const second = { id: uid("seg"), start: mid, end: s.end, speaker: s.speaker, text: t2, words: [] };
    s.text = t1; s.end = mid;
    segs.splice(i + 1, 0, second); markDirtyGlobal(); renderTranscript(); return;
  }
  if (act === "note") { addNote(s.id); return; }
}
function caretOffset(el) {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return (el.textContent || "").length;
  const r = sel.getRangeAt(0).cloneRange(); r.selectNodeContents(el); r.setEnd(sel.getRangeAt(0).endContainer, sel.getRangeAt(0).endOffset);
  return r.toString().length;
}

// ---- notes ----
function addNote(segId) {
  const text = prompt("Note (e.g. 'have a German editor review here'):"); if (!text) return;
  const author = V.projSettings.editor_name || "editor";
  V.data.notes.push({ id: uid("note"), seg_id: segId, text, author, status: "open", created_at: new Date().toISOString() });
  markDirtyGlobal(); renderTranscript(); renderNotesCard();
}
function renderNotesCard() {
  const c = $("notesCard"); const notes = V.data.notes || [];
  c.innerHTML = `<h3 style="margin-top:0">Notes ${notes.length ? `<span class="chip gray">${notes.filter((n)=>n.status!=="resolved").length} open</span>` : ""}</h3>` +
    (notes.length ? notes.map((n) => `<div class="note-row ${n.status}">
      <div><b>${esc(n.author)}</b> · <span class="muted">${n.status}</span><div>${esc(n.text)}</div></div>
      <div class="row"><button class="small secondary note-jump" data-seg="${esc(n.seg_id)}">jump</button>
      <button class="small note-toggle" data-id="${esc(n.id)}">${n.status === "resolved" ? "reopen" : "resolve"}</button></div>
    </div>`).join("") : `<div class="muted">No notes. Use 📝 on a line to leave a review comment.</div>`);
  c.querySelectorAll(".note-jump").forEach((b) => b.onclick = () => {
    const row = view().querySelector(`.seg[data-seg-id="${CSS.escape(b.dataset.seg)}"]`);
    if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
  });
  c.querySelectorAll(".note-toggle").forEach((b) => b.onclick = () => {
    const n = V.data.notes.find((x) => x.id === b.dataset.id); if (!n) return;
    n.status = n.status === "resolved" ? "open" : "resolved"; markDirtyGlobal(); renderNotesCard(); renderTranscript();
  });
}

function markDirty(segEl) { V.dirty = true; if (segEl) segEl.classList.add("dirty"); $("saveBtn").disabled = false; }
function markDirtyGlobal() { V.dirty = true; $("saveBtn").disabled = false; }

async function saveEdits() {
  $("saveBtn").disabled = true;
  try { const ver = await api(`/api/materials/${V.mid}/transcription`, { method: "PUT", json: { data: V.data, note: "manual edit" } });
    toast(`Saved v${ver.version}`); viewMaterial(V.mid);
  } catch (e) { toast(e.message, true); $("saveBtn").disabled = false; }
}

// ---- media <-> transcript sync ----
function onTimeUpdate() {
  const t = V.media.currentTime; const segs = V.data.segments || [];
  let lo = 0, hi = V.starts.length - 1, idx = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (V.starts[mid] <= t) { idx = mid; lo = mid + 1; } else hi = mid - 1; }
  let activeId = null;
  if (idx >= 0) { const s = segs[idx]; if (t < (s.end || Infinity) + 0.05) activeId = s.id; }
  if (activeId === V.activeId) return;
  V.activeId = activeId;
  const tc = $("transcript");
  tc.querySelectorAll(".seg.active").forEach((e) => e.classList.remove("active"));
  if (activeId != null) {
    const row = tc.querySelector(`.seg[data-seg-id="${CSS.escape(activeId)}"]`);
    if (row) { row.classList.add("active"); if (V.autoscroll) row.scrollIntoView({ block: "center", behavior: "smooth" });
      const s = segs[idx]; $("nowPlaying").textContent = `${fmtTs(s.start)} · ${speakerLabel(s.speaker)}`; }
  }
}

// ---- scan (sensitive language) ----
function renderScanCard() {
  const c = $("scanCard");
  c.innerHTML = `<div class="row" style="justify-content:space-between"><h3 style="margin:0">Sensitive-language flags <span id="flagCount"></span></h3></div>
    <div class="row" style="margin-top:6px">
      <select id="scanLexSel" multiple size="${Math.min(3, Math.max(1, V.lexicons.length))}" style="min-width:150px">
        ${V.lexicons.map((l) => `<option value="${l.id}" selected>${esc(l.name)}</option>`).join("")}
      </select>
      <select id="scanFmtSel">${["txt","vtt","srt","json"].map((f) => `<option ${V.projSettings.scan_format===f?"selected":""}>${f}</option>`).join("")}</select>
      <button class="small" id="scanBtn">Scan</button>
    </div>
    <div id="flagList" style="margin-top:8px"></div>`;
  $("scanBtn").onclick = runScan;
}
async function runScan() {
  if (!V.version || !V.lexicons.length) { const fl = $("flagList"); if (fl) fl.innerHTML = `<div class="muted">${V.lexicons.length ? "Choose lexicon(s) and Scan." : 'No lexicons yet — add one under <a href="#/lexicons">Lexicons</a>.'}</div>`; return; }
  const sel = $("scanLexSel"); const ids = sel ? [...sel.selectedOptions].map((o) => o.value) : [];  // [] = all
  const fmt = $("scanFmtSel") ? $("scanFmtSel").value : "txt";
  try {
    const res = await api(`/api/materials/${V.mid}/scan?version=${V.version}&lexicon_ids=${ids.join(",")}&fmt=${fmt}`);
    V.flags = {};
    (res.segment_flags || []).forEach((f) => { (V.flags[f.seg_id] = V.flags[f.seg_id] || []).push(f); });
    $("flagCount").innerHTML = res.flag_count ? `<span class="chip orange">${res.flag_count}</span>` : `<span class="chip green">0</span>`;
    $("flagList").innerHTML = res.flag_count
      ? (res.flags || []).slice(0, 100).map((f) => `<div class="flag-row"><span class="chip orange">${esc(f.term)}</span> <span class="muted">…${esc(f.context)}…</span></div>`).join("")
      : `<div class="muted">No flagged terms.</div>`;
    renderTranscript();
  } catch (e) { $("flagList").innerHTML = `<div class="muted">scan error: ${esc(e.message)}</div>`; }
}

// ---- export / runs ----
function renderExportCard() {
  const S = V.projSettings;
  $("exportCard").innerHTML = `<h3 style="margin-top:0">Export / Runs</h3>
    <div class="row">${["vtt","txt","srt","json"].map((f) => `<label class="toggle"><input type="checkbox" class="expFmt" value="${f}" ${(S.output_formats||["vtt","txt"]).includes(f)?"checked":""}> ${f}</label>`).join("")}</div>
    <label class="toggle" style="margin:8px 0"><input type="checkbox" id="expScan" ${S.scan_enabled?"checked":""}> include scan report</label>
    <button class="small" id="expBtn">Export this version</button>
    <div id="runsList" style="margin-top:10px"></div>`;
  $("expBtn").onclick = doExport;
  loadRuns();
}
async function doExport() {
  const formats = [...view().querySelectorAll(".expFmt:checked")].map((c) => c.value);
  if (!formats.length) return toast("Pick at least one format", true);
  const sel = $("scanLexSel"); const ids = sel ? [...sel.selectedOptions].map((o) => o.value) : (V.projSettings.default_lexicon_ids || []);
  const body = { version: V.version, formats,
    scan: { enabled: $("expScan").checked, lexicon_ids: ids, format: ($("scanFmtSel") && $("scanFmtSel").value) || "txt" } };
  try { const run = await api(`/api/materials/${V.mid}/export`, { method: "POST", json: body });
    toast("Exported to " + run.folder_path); loadRuns();
  } catch (e) { toast(e.message, true); }
}
async function loadRuns() {
  const runs = await api(`/api/materials/${V.mid}/runs`);
  const el = $("runsList"); if (!el) return;
  el.innerHTML = runs.length ? runs.map((r) => `<div class="run-row">
      <div><span class="chip">v${r.version}</span> ${(r.formats||[]).join(", ")} ${r.scan && r.scan.enabled ? `<span class="chip orange">${r.scan.flag_count||0} flags</span>` : ""}
      <div class="muted" style="font-size:11px">${esc(r.folder_path)}</div></div>
      <a class="btn small secondary" href="/api/runs/${r.id}/download">zip</a>
    </div>`).join("") : `<div class="muted">No runs yet.</div>`;
}

// ---- transcribe modal ----
async function openTranscribe(m) {
  const settings = await api("/api/settings");
  const panel = document.createElement("div");
  panel.className = "card modal";
  panel.innerHTML = `
    <h3 style="margin-top:0">Transcribe on the DGX</h3>
    <label class="field"><span>Model</span>
      <select id="tModel">${["large-v3","medium","small","base","tiny"].map((x) => `<option ${x === settings.model ? "selected" : ""}>${x}</option>`).join("")}</select></label>
    <label class="field"><span>Language (blank = auto)</span><input id="tLang" type="text" placeholder="en"></label>
    <label class="toggle" style="margin-bottom:10px"><input type="checkbox" id="tDia" ${settings.diarize === "true" ? "checked" : ""}> identify speakers (diarization)</label>
    <label class="field"><span># speakers (optional)</span><input id="tNum" type="number" min="1" placeholder="e.g. 2"></label>
    <div class="muted" style="margin-bottom:8px">Outputs (${(V.projSettings.output_formats||["vtt","txt"]).join(", ")}) + scan run automatically per the project defaults.</div>
    <div class="row"><button id="tGo">Start</button><button class="secondary" id="tCancel">Cancel</button><span class="grow"></span><span id="tStage" class="muted"></span></div>`;
  document.body.appendChild(panel);
  const close = () => panel.remove();
  $("tCancel").onclick = close;
  $("tGo").onclick = async () => {
    $("tGo").disabled = true;
    const body = { model: $("tModel").value, language: $("tLang").value.trim() || null, diarize: $("tDia").checked };
    const n = parseInt($("tNum").value, 10); if (!isNaN(n)) body.num_speakers = n;
    try { const { job_id } = await api(`/api/materials/${m.id}/transcribe`, { method: "POST", json: body });
      pollJob(job_id, (st) => { $("tStage").innerHTML = `<span class="spinner"></span> ${esc(st.stage || st.status)}`; },
        () => { close(); toast("Transcription complete"); viewMaterial(m.id); },
        (err) => { $("tGo").disabled = false; $("tStage").textContent = "error: " + err; });
    } catch (e) { $("tGo").disabled = false; toast(e.message, true); }
  };
}
async function pollJob(jobId, onTick, onDone, onErr) {
  try {
    const st = await api(`/api/jobs/${jobId}`); onTick(st);
    if (st.status === "done") return onDone(st);
    if (st.status === "error") return onErr(st.error || "failed");
    setTimeout(() => pollJob(jobId, onTick, onDone, onErr), 1500);
  } catch (e) { onErr(e.message); }
}

// ---- change quantification ----
async function loadStats() {
  try {
    const s = await api(`/api/materials/${V.mid}/stats`);
    if ((s.total_edits || 0) > 0 || s.versions > 1) {
      const card = $("changesCard"); card.style.display = "";
      card.innerHTML = `<h3 style="margin-top:0">Your edits (cumulative)</h3>
        <div class="headline">You've made <b>${s.total_edits}</b> change(s) across ${s.versions} version(s).</div>
        ${metricsGrid(s)}<div id="perVer"></div>`;
      if (V.version) showChanges(V.version);
    }
  } catch {}
}
async function showChanges(v) {
  const holder = $("perVer"); if (!holder) return;
  if (!v || v <= 1) { holder.innerHTML = ""; return; }
  try { const c = await api(`/api/materials/${V.mid}/versions/${v}/changes`);
    holder.innerHTML = `<div class="headline" style="margin-top:12px">This version (v${v} vs v${c.base}): <b>${c.total_edits}</b> change(s)</div>${metricsGrid(c)}`;
  } catch {}
}
function metricsGrid(s) {
  const items = [
    ["segments_edited", "segments edited"], ["segments_added", "added"], ["segments_removed", "removed"],
    ["words_changed", "words changed"], ["words_added", "words added"], ["words_removed", "words removed"],
    ["speaker_reassignments", "speaker changes"], ["speakers_renamed", "speakers renamed"],
    ["notes_added", "notes added"], ["notes_resolved", "notes resolved"],
  ];
  return `<div class="changes">${items.map(([k, label]) => `<div class="metric"><div class="n">${s[k] || 0}</div><div class="k">${label}</div></div>`).join("")}</div>`;
}
