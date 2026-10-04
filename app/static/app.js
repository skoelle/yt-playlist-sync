// Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
// Licensed under the MIT License. See LICENSE file in project root for details.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const TABS = ["status", "sync", "oneshot"];
const state = {
  tab: "status",
  status: null,
  busy: false,
  log: { jobId: null, offset: 0, open: false, finished: false },
  sort: { key: "date", dir: "desc" },
  syncSort: { key: "title", dir: "asc" },
  oneshots: [],
  syncs: [],
  detail: null,
  detailId: null,
  detailFrom: "status",
  player: null,
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtBytes(n) {
  if (n == null) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + u[i];
}
function fmtDur(s) {
  if (s == null) return "-";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}
function fmtAbs(iso) { return iso ? new Date(iso).toLocaleString("de-DE") : ""; }
function fmtClock(s) {
  s = Math.round(Number(s) || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}
function fmtUploadDate(s) {
  const m = /^(\d{4})(\d{2})(\d{2})$/.exec(String(s || ""));
  return m ? `${m[3]}.${m[2]}.${m[1]}` : "";
}
function fmtViews(n) { return Number(n).toLocaleString("de-DE") + " Aufrufe"; }
function fmtRel(iso) {
  if (!iso) return "-";
  const diff = (new Date(iso).getTime() - Date.now()) / 1000;
  const abs = Math.abs(diff);
  let txt;
  if (abs < 60) txt = "wenigen Sekunden";
  else if (abs < 3600) txt = Math.round(abs / 60) + " Min.";
  else if (abs < 86400) txt = Math.round(abs / 3600) + " Std.";
  else txt = Math.round(abs / 86400) + " Tagen";
  return diff < 0 ? `vor ${txt}` : `in ${txt}`;
}
const when = (iso) => iso ? `<span title="${esc(fmtAbs(iso))}">${esc(fmtRel(iso))}</span>` : "-";
const badge = (st) => `<span class="badge s-${esc(st)}">${esc(st)}</span>`;

async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* ignore */ }
    throw new Error(msg);
  }
  return res.json();
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el && el.textContent !== text) el.textContent = text;
}

function syncRows(tbody, items, keyFn, htmlFn) {
  const existing = new Map([...tbody.children].map((tr) => [tr.dataset.key, tr]));
  let prev = null;
  for (const item of items) {
    const key = String(keyFn(item));
    const html = htmlFn(item);
    let tr = existing.get(key);
    if (!tr) { tr = document.createElement("tr"); tr.dataset.key = key; } else { existing.delete(key); }
    if (tr._html !== html) { tr.innerHTML = html; tr._html = html; }
    const ref = prev ? prev.nextSibling : tbody.firstChild;
    if (tr !== ref) tbody.insertBefore(tr, ref);
    prev = tr;
  }
  existing.forEach((tr) => tr.remove());
}

function renderBanners(st) {
  const items = [];
  if (st.dry_run) items.push(["warn", "DRY RUN: es werden keine Dateien heruntergeladen."]);
  if (!st.data_writable) items.push(["bad", "Zielordner /data ist nicht beschreibbar. Downloads pausieren."]);
  if (st.failed_oneshots) items.push(["bad", `${st.failed_oneshots} Oneshot-Playlist(s) mit Fehler. Siehe Tab Oneshot-Playlists.`]);
  if (st.paused_until) items.push(["warn", `Warteschlange pausiert bis ${fmtAbs(st.paused_until)} (Rate-Limit).`]);
  const html = items.map(([c, t]) => `<div class="banner ${c}">${esc(t)}</div>`).join("");
  const box = $("#banners");
  if (box._html !== html) { box.innerHTML = html; box._html = html; }
}

function runText(run) {
  if (!run) return "-";
  const when_ = run.finished_at || run.started_at;
  return `${run.status} (${fmtRel(when_)})` + (run.message ? `: ${run.message}` : "");
}

function renderStatus(st) {
  const cur = st.current;
  $("#current-empty").hidden = !!cur;
  $("#current-box").hidden = !cur;
  if (cur) {
    setText("cur-title", cur.playlist_title || "");
    setText("cur-item", cur.current_item || "");
    const pct = cur.percent == null ? 0 : cur.percent;
    $("#cur-bar").style.width = pct + "%";
    const idx = cur.item_index ? `Video ${cur.item_index}/${cur.item_total} | ` : "";
    setText("cur-meta", `${idx}${cur.percent != null ? cur.percent.toFixed(1) + "% | " : ""}${cur.speed || ""} ${cur.eta ? "| ETA " + cur.eta : ""}`);
  }
  setText("sch-d-last", runText(st.schedules.discovery.last));
  setText("sch-d-next", st.schedules.discovery.next ? `${fmtAbs(st.schedules.discovery.next)} (${fmtRel(st.schedules.discovery.next)})` : "-");
  setText("sch-s-last", runText(st.schedules.sync.last));
  setText("sch-s-next", st.schedules.sync.next ? `${fmtAbs(st.schedules.sync.next)} (${fmtRel(st.schedules.sync.next)})` : "-");
  setText("sys-channel", st.channel);
  setText("sys-ytdlp", st.ytdlp_version);
  setText("sys-free", st.free_bytes == null ? "-" : `${fmtBytes(st.free_bytes)} von ${fmtBytes(st.total_bytes)}`);
  setText("sys-queue", st.queue.length ? st.queue.map((q) => q.playlist_title).join(", ") : "leer");
}

function jobRow(j, showTitle = true) {
  const titleCol = showTitle
    ? `<td><a href="#playlist-${j.playlist_pk}">${esc(j.playlist_title)}</a></td>` : "";
  return `<td>${j.id}</td>${titleCol}<td>${esc(j.trigger)}</td>
    <td>${badge(j.status)}${j.error_summary ? `<span class="sub">${esc(j.error_summary)}</span>` : ""}</td>
    <td>${esc(fmtDur(j.duration_s))}</td><td>${j.items_new}</td><td>${j.items_skipped}</td><td>${j.items_failed}</td>
    <td><button data-action="show-log" data-job="${j.id}">Log</button>
    ${["queued", "running"].includes(j.status) ? `<button data-action="cancel" data-job="${j.id}" class="danger">Abbrechen</button>` : ""}</td>`;
}

function syncRow(p) {
  const st = state.status;
  const nextSync = st && st.schedules.sync.next;
  const videos = `${p.downloaded_count} / ${p.remote_item_count ?? "?"}` + (p.skipped_count ? `<span class="sub">${p.skipped_count} nicht verfügbar</span>` : "");
  const last = p.last_sync_at ? when(p.last_sync_at) : "-";
  const title = `<a href="#playlist-${p.id}">${esc(p.title)}</a>` +
    ` <a class="ext" href="${esc(p.url)}" target="_blank" rel="noopener" title="YouTube öffnen">↗</a>` +
    (p.remote_status === "removed" ? ` <span class="badge s-failed">removed</span>` : "") +
    (p.ignored ? ` <span class="badge">ignoriert</span>` : "");
  const err = p.state === "failed" && p.last_job && p.last_job.error_summary ? `<span class="sub">${esc(p.last_job.error_summary)}</span>` : "";
  return `<td>${title}</td><td>${videos}</td><td>${fmtBytes(p.size_bytes)}</td><td>${last}</td>
    <td>${p.ignored || p.remote_status === "removed" ? "-" : nextSync ? when(nextSync) : "-"}</td>
    <td>${badge(p.state)}${err}</td>
    <td><button data-action="run" data-id="${p.id}">Jetzt syncen</button>
    <button data-action="ignore" data-id="${p.id}" data-ignored="${p.ignored ? 0 : 1}">${p.ignored ? "Reaktivieren" : "Ignorieren"}</button>
    <button data-action="to-oneshot" data-id="${p.id}">Als Oneshot markieren</button></td>`;
}

function oneshotSortValue(p, key) {
  switch (key) {
    case "title": return p.title.toLowerCase();
    case "date": return p.completed_at || p.first_downloaded_at || "";
    case "songs": return p.downloaded_count;
    case "size": return p.size_bytes;
    case "duration": return p.last_job ? (p.last_job.duration_s || 0) : 0;
    case "state": return p.state;
    default: return "";
  }
}

function oneshotRow(p) {
  const date = p.completed_at ? when(p.completed_at)
    : p.first_downloaded_at ? `${when(p.first_downloaded_at)}<span class="sub">${p.state === "running" ? "läuft" : "nicht abgeschlossen"}</span>` : "-";
  const extra = [];
  if (p.skipped_count) extra.push(`${p.skipped_count} nicht verfügbar`);
  if (p.failed_count) extra.push(`${p.failed_count} fehlgeschlagen`);
  const songs = `${p.downloaded_count} / ${p.remote_item_count ?? "?"}` + (extra.length ? `<span class="sub">${extra.join(", ")}</span>` : "");
  const err = p.state === "failed" && p.last_job && p.last_job.error_summary ? `<span class="sub">${esc(p.last_job.error_summary)}</span>` : "";
  const acts = [];
  if (p.state === "failed") acts.push(`<button data-action="retry" data-id="${p.id}">Erneut versuchen</button>`);
  if (p.state === "new") acts.push(`<button data-action="run" data-id="${p.id}">Jetzt laden</button>`);
  if (p.state === "done") acts.push(`<button data-action="rerun" data-id="${p.id}">Full Re-Run</button>`);
  if (p.last_job) acts.push(`<button data-action="show-log" data-job="${p.last_job.id}">Log</button>`);
  acts.push(`<button data-action="to-sync" data-id="${p.id}">Als Sync markieren</button>`);
  return `<td><a href="#playlist-${p.id}">${esc(p.title)}</a>
    <a class="ext" href="${esc(p.url)}" target="_blank" rel="noopener" title="YouTube öffnen">↗</a></td><td>${date}</td><td>${songs}</td>
    <td>${fmtBytes(p.size_bytes)}</td><td>${p.last_job ? esc(fmtDur(p.last_job.duration_s)) : "-"}</td>
    <td>${badge(p.state)}${err}</td><td>${acts.join(" ")}</td>`;
}

function renderOneshots() {
  const { key, dir } = state.sort;
  const list = [...state.oneshots].sort((a, b) => {
    const va = oneshotSortValue(a, key), vb = oneshotSortValue(b, key);
    const c = va < vb ? -1 : va > vb ? 1 : 0;
    return dir === "asc" ? c : -c;
  });
  syncRows($("#oneshot-table tbody"), list, (p) => p.id, oneshotRow);
  const songs = state.oneshots.reduce((a, p) => a + p.downloaded_count, 0);
  const size = state.oneshots.reduce((a, p) => a + p.size_bytes, 0);
  setText("oneshot-summary", `${state.oneshots.length} Setlists | ${songs} Songs | ${fmtBytes(size)}`);
}

function syncSortValue(p, key) {
  switch (key) {
    case "title": return p.title.toLowerCase();
    case "videos": return p.downloaded_count;
    case "size": return p.size_bytes;
    case "last": return p.last_sync_at || "";
    case "state": return p.state;
    default: return "";
  }
}

function renderSyncs() {
  const { key, dir } = state.syncSort;
  const list = [...state.syncs].sort((a, b) => {
    const va = syncSortValue(a, key), vb = syncSortValue(b, key);
    const c = va < vb ? -1 : va > vb ? 1 : 0;
    return dir === "asc" ? c : -c;
  });
  syncRows($("#sync-table tbody"), list, (p) => p.id, syncRow);
  const videos = state.syncs.reduce((a, p) => a + p.downloaded_count, 0);
  const size = state.syncs.reduce((a, p) => a + p.size_bytes, 0);
  setText("sync-summary", `${state.syncs.length} Sync-Playlists | ${videos} Videos | ${fmtBytes(size)}`);
}

function videoRow(v) {
  const thumb = v.thumb
    ? `<img class="vid-thumb" loading="lazy" alt="" src="/api/playlists/${state.detailId}/thumb?file=${encodeURIComponent(v.thumb)}">`
    : `<div class="vid-thumb vid-thumb-empty"></div>`;
  const dur = v.duration_s != null ? `<span class="vid-dur">${fmtClock(v.duration_s)}</span>` : "";
  const meta = [];
  if (v.channel) meta.push(v.channel);
  const up = fmtUploadDate(v.upload_date);
  if (up) meta.push(up);
  if (v.view_count != null) meta.push(fmtViews(v.view_count));
  const chips = (v.sidecars || []).map((s) => `<code>${esc(s)}</code>`).join(" ");
  const play = `data-action="play" data-file="${esc(v.file)}"`;
  const tech = [];
  if (v.resolution) tech.push(v.resolution);
  if (v.size_bytes) tech.push(fmtBytes(v.size_bytes));
  if (v.vcodec) tech.push(v.vbr ? `${v.vcodec} · ${Math.round(v.vbr)} kbps` : v.vcodec);
  if (v.acodec) tech.push(v.abr ? `${v.acodec} · ${Math.round(v.abr)} kbps` : v.acodec);
  const techHtml = tech.length
    ? `<div class="vid-tech">${tech.map(esc).join(" · ")}</div>` : "";
  return `<td class="vid-td-thumb"><a class="vid-play" ${play}>${thumb}${dur}</a></td>
    <td><div class="vid-title"><a class="vid-play" ${play}>${esc(v.title)}</a></div>
    <div class="vid-meta">${meta.map(esc).join(" · ")}</div>
    ${techHtml}
    <div class="vid-chips">${chips}</div></td>`;
}

function renderDetail(p, f, vids) {
  $("#detail-back").href = "#" + state.detailFrom;
  setText("detail-title", p.title);
  const yt = $("#detail-yt");
  yt.href = p.url;
  yt.hidden = false;
  const cover = $("#detail-cover");
  const cv = vids.cover || "";
  if (cover.dataset.cv !== cv) {
    cover.dataset.cv = cv;
    if (cv) {
      cover.src = `/api/playlists/${p.id}/thumb?file=${encodeURIComponent(cv)}`;
      cover.hidden = false;
    } else {
      cover.removeAttribute("src");
      cover.hidden = true;
    }
  }
  const stats = [`${vids.video_count} Videos`];
  if (vids.total_duration_s > 0) stats.push(fmtClock(vids.total_duration_s));
  stats.push(fmtBytes(f.exists ? f.total_bytes : p.size_bytes));
  setText("detail-stats", stats.join(" · "));
  const badges = [badge(p.type), badge(p.state)];
  if (p.ignored) badges.push('<span class="badge">ignoriert</span>');
  if (p.remote_status === "removed") badges.push('<span class="badge s-failed">removed</span>');
  const bhtml = badges.join(" ");
  const bbox = $("#detail-badges");
  if (bbox._html !== bhtml) { bbox.innerHTML = bhtml; bbox._html = bhtml; }

  const acts = [];
  if (p.state === "failed") acts.push(`<button data-action="retry" data-id="${p.id}">Erneut versuchen</button>`);
  if (p.state === "new" || p.state === "idle") {
    acts.push(`<button data-action="run" data-id="${p.id}">${p.type === "oneshot" ? "Jetzt laden" : "Jetzt synchronisieren"}</button>`);
  }
  if (p.state === "done" && p.type === "oneshot") {
    acts.push(`<button data-action="rerun" data-id="${p.id}">Full Re-Run</button>`);
  }
  if (p.last_job) acts.push(`<button data-action="show-log" data-job="${p.last_job.id}">Log</button>`);
  acts.push(`<button data-action="ignore" data-id="${p.id}" data-ignored="${p.ignored ? 0 : 1}">${p.ignored ? "Reaktivieren" : "Ignorieren"}</button>`);
  if (p.type === "sync") acts.push(`<button data-action="to-oneshot" data-id="${p.id}">Als Oneshot markieren</button>`);
  else acts.push(`<button data-action="to-sync" data-id="${p.id}">Als Sync markieren</button>`);
  const ahtml = acts.join(" ");
  const abox = $("#detail-actions");
  if (abox._html !== ahtml) { abox.innerHTML = ahtml; abox._html = ahtml; }

  const kv = [
    ["Playlist-ID", `<code>${esc(p.playlist_id)}</code>`],
    ["Typ", esc(p.type)],
    ["Status", badge(p.state)],
    ["Ordner", `<code>${esc(p.folder_name || "-")}</code>`],
    ["Videos (YouTube)", p.remote_item_count ?? "-"],
    ["Heruntergeladen", p.downloaded_count],
    ["Nicht verfügbar", p.skipped_count],
    ["Fehlgeschlagen", p.failed_count],
    ["Größe (DB)", fmtBytes(p.size_bytes)],
    ["Remote-Status", esc(p.remote_status || "-")],
    ["Ignoriert", p.ignored ? "ja" : "nein"],
    ["Erstmals gesehen", when(p.first_seen_at)],
    ["Zuletzt gesehen", when(p.last_seen_at)],
    ["Erster Download", when(p.first_downloaded_at)],
    ["Abgeschlossen", when(p.completed_at)],
    ["Letzter Sync", when(p.last_sync_at)],
    ["Letzter Job", p.last_job
      ? `#${p.last_job.id} · ${esc(p.last_job.trigger)} · ${badge(p.last_job.status)} · ${when(p.last_job.finished_at || p.last_job.started_at)}`
      : "-"],
  ];
  const kvhtml = kv.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`).join("");
  const kvbox = $("#detail-kv");
  if (kvbox._html !== kvhtml) { kvbox.innerHTML = kvhtml; kvbox._html = kvhtml; }

  setText("detail-video-summary", `${vids.video_count} / ${p.remote_item_count ?? vids.video_count}`);
  const vnone = $("#detail-videos-none");
  const vwrap = $("#detail-videos-wrap");
  if (!vids.exists) {
    vnone.textContent = "Ordner existiert (noch) nicht – Videos erscheinen hier, sobald der erste Download lief.";
    vnone.hidden = false;
    vwrap.hidden = true;
  } else if (!vids.video_count) {
    vnone.textContent = "Keine Videodateien gefunden.";
    vnone.hidden = false;
    vwrap.hidden = true;
  } else {
    vnone.hidden = true;
    vwrap.hidden = false;
    syncRows($("#detail-videos-table tbody"), vids.videos, (x) => x.file, videoRow);
  }

  if (!f.exists) {
    $("#detail-files-none").hidden = false;
    $("#detail-files-wrap").hidden = true;
    setText("detail-files-summary", "");
  } else {
    $("#detail-files-none").hidden = true;
    $("#detail-files-wrap").hidden = false;
    const chips = Object.entries(f.by_ext).sort((a, b) => a[0] < b[0] ? -1 : 1)
      .map(([e, n]) => `${n}× .${e || "?"}`).join(", ");
    setText("detail-files-summary", `— ${f.total_files} Dateien, ${fmtBytes(f.total_bytes)}${chips ? ` (${chips})` : ""}`);
    syncRows($("#detail-files-table tbody"), f.files, (x) => x.name, (x) =>
      `<td>${esc(x.name)}</td><td>${fmtBytes(x.size_bytes)}</td><td>${when(x.modified_at)}</td>`);
  }
  syncRows($("#detail-jobs-table tbody"), p.jobs, (j) => j.id, (j) => jobRow(j, false));
}

function closePlayer() {
  const video = $("#player-video");
  if (video) { video.pause(); video.removeAttribute("src"); video.load(); }
  state.player = null;
  $("#player").hidden = true;
}

function renderPlayer() {
  const P = state.player;
  const vids = state.detail && state.detail.vids ? state.detail.vids.videos : [];
  const i = P ? vids.findIndex((x) => x.file === P.file) : -1;
  if (i < 0) { closePlayer(); return; }
  const v = vids[i];
  $("#player").hidden = false;
  setText("player-title", v.title);
  setText("player-count", `${i + 1} / ${vids.length}`);
  const yt = $("#player-yt");
  if (v.video_id) {
    yt.href = `https://www.youtube.com/watch?v=${v.video_id}`;
    yt.hidden = false;
  } else {
    yt.hidden = true;
  }
  const video = $("#player-video");
  const src = `/api/playlists/${state.detailId}/video?file=${encodeURIComponent(v.file)}`;
  if (video.dataset.src !== src) {
    video.dataset.src = src;
    video.src = src;
    video.onended = () => playerMove(1);
  }
  $("#player-prev").disabled = i === 0;
  $("#player-next").disabled = i >= vids.length - 1;
}

function playerMove(delta) {
  const P = state.player;
  const vids = state.detail && state.detail.vids ? state.detail.vids.videos : [];
  const i = P ? vids.findIndex((x) => x.file === P.file) : -1;
  const target = vids[i + delta];
  if (!target) return;
  state.player = { file: target.file };
  renderPlayer();
}

async function pollLog() {
  const L = state.log;
  if (!L.open || L.jobId == null) return;
  try {
    const r = await api(`/jobs/${L.jobId}/log?offset=${L.offset}`);
    if (r.text) {
      const pre = $("#log-pre");
      const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
      pre.appendChild(document.createTextNode(r.text));
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }
    L.offset = r.offset;
    L.finished = r.finished;
  } catch (_) { /* ignore transient errors */ }
}

function openLog(jobId) {
  state.log = { jobId, offset: 0, open: true, finished: false };
  $("#log-pre").textContent = "";
  setText("log-title", `Job #${jobId}`);
  $("#log-panel").hidden = false;
  pollLog();
}

async function tick() {
  if (state.busy) return;
  state.busy = true;
  try {
    const st = await api("/status");
    state.status = st;
    renderBanners(st);
    if (state.tab === "status") {
      renderStatus(st);
      const jobs = await api("/jobs?limit=15");
      syncRows($("#jobs-table tbody"), jobs, (j) => j.id, jobRow);
    } else if (state.tab === "sync") {
      state.syncs = await api("/playlists?type=sync");
      renderSyncs();
    } else if (state.tab === "oneshot") {
      state.oneshots = await api("/playlists?type=oneshot");
      renderOneshots();
    } else if (state.tab === "playlist") {
      const id = state.detailId;
      const [pl, files, vids] = await Promise.all([
        api(`/playlists/${id}`), api(`/playlists/${id}/files`), api(`/playlists/${id}/videos`),
      ]);
      if (state.detailId !== id) return;
      state.detail = { pl, files, vids };
      renderDetail(pl, files, vids);
    }
    if (state.log.open) await pollLog();
  } catch (err) {
    $("#banners").innerHTML = `<div class="banner bad">Backend nicht erreichbar: ${esc(err.message)}</div>`;
    $("#banners")._html = null;
  } finally {
    state.busy = false;
  }
}

function showTab() {
  if (state.player) closePlayer();
  const h = (location.hash || "#status").slice(1);
  const m = /^playlist-(\d+)$/.exec(h);
  if (m) {
    state.tab = "playlist";
    state.detailId = Number(m[1]);
    state.detail = null;
  } else {
    state.tab = TABS.includes(h) ? h : "status";
    state.detailId = null;
    state.detail = null;
  }
  for (const sec of document.querySelectorAll("main > section")) sec.hidden = sec.dataset.tab !== state.tab;
  const navTab = state.tab === "playlist" ? state.detailFrom : state.tab;
  for (const a of document.querySelectorAll("nav a")) a.classList.toggle("active", a.dataset.tab === navTab);
  tick();
}

async function act(action, el) {
  const id = el.dataset.id, job = el.dataset.job;
  try {
    switch (action) {
      case "run": await api(`/playlists/${id}/run`, { method: "POST" }); break;
      case "retry": await api(`/playlists/${id}/retry`, { method: "POST" }); break;
      case "rerun":
        if (!confirm("Playlist erneut prüfen und fehlende Videos laden? Vorhandene Dateien bleiben erhalten.")) return;
        await api(`/playlists/${id}/rerun`, { method: "POST" }); break;
      case "ignore": await api(`/playlists/${id}/ignore`, { method: "POST", body: { ignored: el.dataset.ignored === "1" } }); break;
      case "to-oneshot":
        if (!confirm("Diese Playlist künftig als Oneshot behandeln?")) return;
        await api(`/playlists/${id}/type`, { method: "POST", body: { type: "oneshot" } }); break;
      case "to-sync":
        if (!confirm("Diese Playlist künftig als Sync-Playlist behandeln?")) return;
        await api(`/playlists/${id}/type`, { method: "POST", body: { type: "sync" } }); break;
      case "cancel": await api(`/jobs/${job}/cancel`, { method: "POST" }); break;
      case "cancel-current":
        if (state.status && state.status.current) await api(`/jobs/${state.status.current.job_id}/cancel`, { method: "POST" });
        break;
      case "show-log": openLog(Number(job)); return;
      case "show-current-log":
        if (state.status && state.status.current) openLog(state.status.current.job_id);
        return;
      case "close-log": state.log.open = false; $("#log-panel").hidden = true; return;
      case "play": state.player = { file: el.dataset.file }; renderPlayer(); return;
      case "player-close": closePlayer(); return;
      case "player-prev": playerMove(-1); return;
      case "player-next": playerMove(1); return;
      case "discovery-now": await api("/discovery/run", { method: "POST" }); break;
      case "sync-now": await api("/sync/run", { method: "POST" }); break;
      default: return;
    }
  } catch (err) {
    alert(err.message);
  }
  tick();
}

document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-action]");
  if (btn) { act(btn.dataset.action, btn); return; }
  const plLink = ev.target.closest('a[href^="#playlist-"]');
  if (plLink && state.tab !== "playlist") state.detailFrom = state.tab;
  const th = ev.target.closest("th[data-sort]");
  if (th) {
    const key = th.dataset.sort;
    const table = th.closest("table").id;
    const st = table === "sync-table" ? state.syncSort : state.sort;
    st.dir = st.key === key && st.dir === "desc" ? "asc" : "desc";
    st.key = key;
    if (table === "sync-table") renderSyncs();
    else renderOneshots();
  }
});
window.addEventListener("hashchange", showTab);
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && state.player) closePlayer();
});
$("#player").addEventListener("click", (ev) => {
  if (ev.target === ev.currentTarget && state.player) closePlayer();
});
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") tick(); });
setInterval(() => { if (document.visibilityState === "visible") tick(); }, 5000);
showTab();
