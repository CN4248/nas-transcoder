/* Transcode Studio — vanilla ES2020 frontend. No build step, no dependencies. */
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const FAMILY_ICON = { audio: "music", video: "film", image: "image", archive: "archive", subtitle: "subtitle" };
const FAMILY_LABEL = { audio: "音频", video: "视频", image: "图片", archive: "压缩包", subtitle: "字幕" };
const STATUS_LABEL = {
  queued: "排队中", running: "转码中", paused: "已暂停", completed: "已完成",
  failed: "失败", canceled: "已取消", undone: "已撤销",
};
const STATUS_CLASS = {
  queued: "st-queued", running: "st-running", paused: "st-paused", completed: "st-completed",
  failed: "st-failed", canceled: "st-canceled", undone: "st-undone",
};
const PRESETS = {
  balanced: { image_quality: 88, audio_bitrate: "192k", video_crf: 23, video_preset: "veryfast", archive_level: 5 },
  high: { image_quality: 95, audio_bitrate: "320k", video_crf: 19, video_preset: "medium", archive_level: 7 },
  compact: { image_quality: 74, audio_bitrate: "128k", video_crf: 28, video_preset: "veryfast", archive_level: 9 },
  lossless: { image_quality: 100, audio_bitrate: "320k", video_crf: 16, video_preset: "slow", archive_level: 9 },
};
const TARGET_PREFERENCE = {
  audio: ["m4a", "mp3", "flac"],
  video: ["mp4", "mkv", "webm"],
  image: ["webp", "jpg", "png"],
  archive: ["zip", "7z", "tar.gz"],
  subtitle: ["srt", "vtt"],
};

const S = {
  app: null,
  caps: null,
  settings: {},
  jobs: [],
  counts: {},
  queuePaused: false,
  logs: [],
  lastLogId: 0,
  latestAction: null,
  selected: new Map(),
  cwd: "",
  browser: { path: "", entries: [], total: 0, has_more: false, offset: 0, loading: true, breadcrumbs: [], parent: null },
  queueFilter: "all",
  logLevel: "all",
  transcode: null,
  picker: null,
  theme: localStorage.getItem("nts-theme") || "dark",
  polling: false,
  offline: false,
  searchTimer: null,
};

function icon(name, cls = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  if (cls) svg.setAttribute("class", cls);
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

function elem(tag, cls = "", text = null) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== null) node.textContent = text;
  return node;
}

function button(cls, text, iconName = null, title = "") {
  const node = elem("button", cls);
  if (iconName) node.appendChild(icon(iconName));
  if (text) node.appendChild(elem("span", "", text));
  if (title) {
    node.title = title;
    node.setAttribute("aria-label", title);
  }
  node.type = "button";
  return node;
}

function fmtBytes(value) {
  if (value === null || value === undefined) return "—";
  let n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function fmtTime(ts, withDate = false) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  if (withDate) return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${hh}:${mm}`;
  return `${hh}:${mm}:${ss}`;
}

function fmtDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.ceil(seconds)} 秒`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return `${m} 分 ${s} 秒`;
  return `${Math.floor(m / 60)} 时 ${m % 60} 分`;
}

function shortId(id) {
  return (id || "").slice(0, 8);
}

function familyOf(ext) {
  return (S.caps && S.caps.extensions && S.caps.extensions[ext]) || null;
}

function extLabel(ext) {
  return (S.caps && S.caps.extension_labels && S.caps.extension_labels[ext]) || (ext || "?").toUpperCase();
}

function targetsFor(ext) {
  return (S.caps && S.caps.matrix && S.caps.matrix[ext]) || [];
}

function stemOf(name) {
  const compound = /\.(tar\.gz|tar\.bz2|tar\.xz|tgz|tbz2|txz)$/i;
  if (compound.test(name)) return name.replace(compound, "");
  const idx = name.lastIndexOf(".");
  return idx > 0 ? name.slice(0, idx) : name;
}

function recommendedTarget(entry) {
  const targets = targetsFor(entry.ext);
  if (!targets.length) return null;
  const order = TARGET_PREFERENCE[entry.family] || [];
  const familyTargets = targets.filter((t) => t.family === entry.family);
  for (const ext of order) {
    const found = familyTargets.find((t) => t.ext === ext && t.ext !== entry.ext);
    if (found) return found.ext;
  }
  const different = familyTargets.find((t) => t.ext !== entry.ext);
  return (different || familyTargets[0] || targets[0]).ext;
}

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.body !== undefined && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, opts);
  let data = null;
  try { data = await response.json(); } catch (_) { /* non-JSON */ }
  if (!response.ok || (data && data.ok === false)) {
    const message = (data && data.error && data.error.message) || `请求失败（HTTP ${response.status}）`;
    const error = new Error(message);
    error.code = data && data.error && data.error.code;
    error.status = response.status;
    throw error;
  }
  return data || {};
}

/* ------------------------------------------------------------------ toasts */
function toast(message, type = "info", action = null, timeout = 5200) {
  const host = $("#toasts");
  const node = elem("div", `toast ${type}`);
  const ic = elem("span", "toast-icon");
  ic.appendChild(icon(type === "error" ? "alert" : type === "warn" ? "alert" : "check"));
  const msg = elem("span", "toast-msg", message);
  node.append(ic, msg);
  if (action) {
    const actionBtn = button("", action.label);
    actionBtn.addEventListener("click", async () => {
      try { await action.run(); } finally { node.remove(); }
    });
    node.appendChild(actionBtn);
  }
  const close = button("toast-close", "×");
  close.addEventListener("click", () => node.remove());
  node.appendChild(close);
  host.appendChild(node);
  while (host.children.length > 4) host.firstElementChild.remove();
  if (timeout) setTimeout(() => { node.style.opacity = "0"; setTimeout(() => node.remove(), 220); }, timeout);
}

/* ------------------------------------------------------------------ theme */
function applyTheme() {
  document.documentElement.dataset.theme = S.theme;
  const btn = $("#themeBtn");
  if (btn) btn.replaceChildren(icon(S.theme === "dark" ? "sun" : "moon"));
}

function toggleTheme() {
  S.theme = S.theme === "dark" ? "light" : "dark";
  localStorage.setItem("nts-theme", S.theme);
  applyTheme();
}

/* ------------------------------------------------------------------- init */
document.addEventListener("DOMContentLoaded", init);

async function init() {
  applyTheme();
  bindGlobalEvents();
  try {
    const data = await api("/api/bootstrap");
    S.app = data.app;
    S.caps = data.capabilities;
    S.settings = data.settings || {};
    S.jobs = data.jobs || [];
    S.counts = data.counts || {};
    S.queuePaused = !!data.queue_paused;
    S.logs = data.logs || [];
    S.lastLogId = data.last_log_id || 0;
    S.latestAction = data.latest_action || null;
    S.cwd = data.app.source_root;
    $("#sourceRootLabel").textContent = `只读源 · ${data.app.source_root}`;
    $("#hiddenToggle").checked = !!S.settings.show_hidden;
    renderAll();
    await loadDir(S.cwd, { reset: true });
    startPolling();
  } catch (error) {
    setOffline(true);
    toast(`无法连接后端：${error.message}`, "error", null, 0);
  }
}

function renderAll() {
  renderHeaderStatus();
  renderBrowser();
  renderQueue();
  renderLogs();
  renderUndo();
  updateQueueControls();
}

/* ------------------------------------------------------------------ header */
function renderHeaderStatus() {
  if (!S.caps) return;
  const available = (S.caps.tools || []).filter((t) => t.available).length;
  const total = (S.caps.tools || []).length;
  $("#engineText").textContent = S.offline ? "后端离线" : `引擎 ${available}/${total} 就绪`;
  $("#engineChip").classList.toggle("offline", S.offline);
  const pill = $("#queuePill");
  pill.classList.remove("busy", "paused");
  if (S.offline) {
    pill.textContent = "连接中断";
  } else if (S.queuePaused) {
    pill.classList.add("paused");
    pill.textContent = "队列已暂停";
  } else if ((S.counts.running || 0) > 0) {
    pill.classList.add("busy");
    pill.textContent = `运行中 ${S.counts.running}`;
  } else if ((S.counts.queued || 0) > 0) {
    pill.classList.add("busy");
    pill.textContent = `排队 ${S.counts.queued}`;
  } else {
    pill.textContent = "队列空闲";
  }
  const counts = S.counts || {};
  const host = $("#queueCounts");
  host.replaceChildren();
  const chips = [
    ["running", counts.running || 0, "运行"],
    ["queued", counts.queued || 0, "排队"],
    ["completed", counts.completed || 0, "完成"],
    ["failed", (counts.failed || 0) + (counts.canceled || 0), "异常"],
  ];
  for (const [cls, n, label] of chips) {
    const chip = elem("span", `count-chip ${cls}`);
    chip.append(elem("b", "", String(n)), document.createTextNode(label));
    host.appendChild(chip);
  }
  $("#queueSubtitle").textContent = S.queuePaused
    ? "队列已暂停，运行中的进程已挂起"
    : `${counts.running || 0} 个运行 · ${counts.queued || 0} 个等待 · 并发上限 ${S.settings.concurrency || 2}`;
}

function updateQueueControls() {
  $("#pauseQueueBtn").hidden = S.queuePaused;
  $("#resumeQueueBtn").hidden = !S.queuePaused;
  const done = S.counts.completed || 0;
  const failed = S.counts.failed || 0;
  const clearBtn = $("#clearCompletedBtn");
  const retryBtn = $("#retryFailedBtn");
  clearBtn.disabled = done === 0;
  retryBtn.disabled = failed === 0;
  clearBtn.title = done ? `清除 ${done} 条已完成任务记录（不删除输出文件）` : "没有已完成任务";
  retryBtn.title = failed ? `重新排队 ${failed} 个失败任务` : "没有失败任务";
  const select = $("#concurrencySelect");
  if (select && document.activeElement !== select) select.value = String(S.settings.concurrency || 2);
}

/* ----------------------------------------------------------------- browser */
async function loadDir(path, { reset = true, append = false } = {}) {
  const browser = S.browser;
  if (reset) {
    browser.path = path;
    browser.entries = [];
    browser.total = 0;
    browser.has_more = false;
    browser.offset = 0;
    browser.loading = true;
    browser.error = null;
    renderBrowser();
  } else if (browser.loading) {
    return false;
  }
  const offset = append ? browser.entries.length : 0;
  const params = new URLSearchParams({
    path,
    q: $("#searchInput").value.trim(),
    kind: browser.kind || "all",
    hidden: $("#hiddenToggle").checked ? "1" : "0",
    offset: String(offset),
    limit: "300",
  });
  try {
    const data = await api(`/api/fs/list?${params.toString()}`);
    browser.path = data.path;
    browser.real_path = data.real_path;
    browser.parent = data.parent;
    browser.breadcrumbs = data.breadcrumbs;
    browser.total = data.total;
    browser.has_more = data.has_more;
    browser.offset = data.offset + data.entries.length;
    browser.entries = append ? browser.entries.concat(data.entries) : data.entries;
    browser.kind = data.kind;
    browser.loading = false;
    S.cwd = data.path;
    renderBrowser();
    return true;
  } catch (error) {
    browser.loading = false;
    browser.error = error.message;
    renderBrowser();
    toast(error.message, "error");
    return false;
  }
}

/* Resolve a user-typed path against a mounted root.  Source paths are NAS
   absolute paths (e.g. /home/user -> /host/home/user); both the full
   mounted path and a relative path are accepted.  The server still enforces
   the real allow-list, so this is only a convenience mapping. */
function resolveRootPath(raw, root) {
  let value = String(raw || "").trim();
  if (!value) return null;
  value = value.replace(/\/{2,}/g, "/");
  if (value.length > 1) value = value.replace(/\/+$/, "");
  if (!value.startsWith("/")) value = "/" + value;
  if (value === root || value.startsWith(root + "/")) return value;
  if (value === "/") return root;
  return root + value;
}

function sourceDisplayPath(path) {
  const root = (S.app && S.app.source_root) || "/host";
  if (!path) return "/";
  if (path === root) return "/";
  if (path.startsWith(root + "/")) return path.slice(root.length);
  return path;
}

async function jumpSourcePath() {
  const input = $("#pathInput");
  if (!input) return;
  const target = resolveRootPath(input.value, (S.app && S.app.source_root) || "/host");
  if (!target) {
    toast("请输入要跳转的路径", "warn");
    input.focus();
    return;
  }
  const before = S.browser.path || S.cwd;
  input.blur();
  const ok = await loadDir(target, { reset: true });
  if (ok) {
    input.value = sourceDisplayPath(S.browser.path || target);
    input.blur();
    return;
  }
  if (before) await loadDir(before, { reset: true });
  input.value = sourceDisplayPath(S.browser.path || before);
  input.focus();
  input.select();
}

async function jumpPickerPath() {
  const input = $("#pickerPathInput");
  if (!input || !S.picker) return;
  const target = resolveRootPath(input.value, (S.app && S.app.output_root) || "/output");
  if (!target) {
    toast("请输入要跳转的路径", "warn");
    input.focus();
    return;
  }
  const before = S.picker.path;
  input.blur();
  const ok = await loadPicker(target);
  if (ok) {
    input.value = S.picker.path;
    input.blur();
    return;
  }
  input.value = before;
  input.focus();
  input.select();
}

function renderBrowser() {
  const b = S.browser;
  const list = $("#fileList");
  const hidden = $("#hiddenToggle");
  $("#sourceRootLabel").textContent = S.app ? `只读源 · ${S.app.source_root}` : "只读源";
  const pathInput = $("#pathInput");
  if (pathInput && document.activeElement !== pathInput) pathInput.value = sourceDisplayPath(b.path || S.cwd);
  $("#upBtn").disabled = !b.parent;
  renderBreadcrumbs(b.breadcrumbs || [], b.path);
  if (b.loading && !b.entries.length) {
    list.replaceChildren();
    for (let i = 0; i < 8; i++) list.appendChild(elem("div", "skeleton-row"));
    updateSelectionFooter();
    return;
  }
  if (b.error && !b.entries.length) {
    list.replaceChildren(emptyState("alert", "目录读取失败", b.error));
    updateSelectionFooter();
    return;
  }
  if (!b.entries.length) {
    list.replaceChildren(emptyState("folder", "这个目录是空的", "可以切换目录或调整筛选条件。"));
    updateSelectionFooter();
    return;
  }
  list.replaceChildren();
  list.appendChild(renderListHead());
  for (const entry of b.entries) list.appendChild(renderFileRow(entry));
  if (b.has_more) {
    const more = button("btn soft small load-more", "加载更多", "chevron-right");
    more.addEventListener("click", () => loadDir(b.path, { reset: false, append: true }));
    list.appendChild(more);
  }
  updateSelectionFooter();
}

function renderBreadcrumbs(crumbs, path) {
  const host = $("#breadcrumbs");
  host.replaceChildren();
  if (!crumbs.length) {
    host.appendChild(elem("span", "crumb", path || ""));
    return;
  }
  crumbs.forEach((crumb, index) => {
    if (index > 0) host.appendChild(elem("span", "crumb-sep", "/"));
    const btn = elem("button", "crumb", index === 0 ? (S.app && crumb.path === S.app.source_root ? "NAS 根目录" : crumb.name) : crumb.name);
    btn.title = crumb.path;
    btn.addEventListener("click", () => loadDir(crumb.path, { reset: true }));
    host.appendChild(btn);
  });
}

function renderListHead() {
  const head = elem("div", "list-head");
  const all = elem("input", "file-check");
  all.type = "checkbox";
  all.title = "全选本页可转码文件";
  const supported = S.browser.entries.filter((e) => e.is_file && e.supported);
  all.checked = supported.length > 0 && supported.every((e) => S.selected.has(e.path));
  all.indeterminate = !all.checked && supported.some((e) => S.selected.has(e.path));
  all.addEventListener("change", () => {
    for (const entry of supported) {
      if (all.checked) S.selected.set(entry.path, entry);
      else S.selected.delete(entry.path);
    }
    for (const row of $$(".file-row", $("#fileList"))) {
      const entry = S.browser.entries.find((e) => e.path === row.dataset.path);
      if (!entry) continue;
      row.classList.toggle("selected", S.selected.has(entry.path));
      const check = $(".file-check", row);
      if (check) check.checked = S.selected.has(entry.path);
    }
    updateSelectionFooter();
  });
  head.append(all, elem("span", "", ""), elem("span", "", "名称"), elem("span", "", "格式"), elem("span", "", ""));
  return head;
}

function renderFileRow(entry) {
  const row = elem("div", "file-row");
  row.dataset.path = entry.path;
  const supported = entry.is_file && entry.supported;
  if (S.selected.has(entry.path)) row.classList.add("selected");
  if (entry.is_dir) row.classList.add("dir");
  if (entry.is_file && !entry.supported) row.classList.add("unsupported");

  const check = elem("input", "file-check");
  check.type = "checkbox";
  check.checked = S.selected.has(entry.path);
  check.disabled = !supported;
  check.title = supported ? "选择此文件" : (entry.error || "该文件没有可用的目标格式");
  check.addEventListener("click", (event) => event.stopPropagation());
  check.addEventListener("change", () => toggleSelection(entry, check.checked, row));

  const iconWrap = elem("span", `file-icon ${entry.family ? `k-${entry.family}` : ""}`);
  iconWrap.appendChild(icon(entry.is_dir ? "folder" : (FAMILY_ICON[entry.family] || "file")));

  const main = elem("div", "file-main");
  main.appendChild(elem("span", "file-name", entry.name));
  const metaParts = [];
  if (entry.is_dir) metaParts.push("文件夹");
  else {
    metaParts.push(entry.label || (entry.ext ? entry.ext.toUpperCase() : "未知格式"));
    metaParts.push(fmtBytes(entry.size));
    if (entry.is_symlink) metaParts.push("符号链接");
    if (entry.error) metaParts.push(entry.error);
    else if (entry.supported) metaParts.push(`${entry.targets} 种目标格式`);
    else metaParts.push("无可用目标");
  }
  main.appendChild(elem("span", "file-meta", metaParts.join(" · ")));

  const format = elem("span", "file-format");
  const badge = elem("span", `badge ${entry.is_dir ? "gray" : entry.supported ? "supported" : "gray"}`);
  badge.textContent = entry.is_dir ? "目录" : (entry.label || "不支持");
  format.appendChild(badge);

  const action = elem("span", "file-action");
  action.appendChild(icon(entry.is_dir ? "chevron-right" : (supported ? "plus" : "file"), entry.is_dir ? "chev" : ""));

  row.append(check, iconWrap, main, format, action);
  row.addEventListener("click", () => {
    if (entry.is_dir) {
      loadDir(entry.path, { reset: true });
    } else if (supported) {
      toggleSelection(entry, !S.selected.has(entry.path), row);
      check.checked = S.selected.has(entry.path);
    } else {
      toast(entry.error || "此文件没有可用的转换目标格式", "warn");
    }
  });
  if (entry.is_dir) {
    row.addEventListener("dblclick", (event) => event.preventDefault());
  }
  return row;
}

function toggleSelection(entry, selected, row = null) {
  if (selected) S.selected.set(entry.path, entry);
  else S.selected.delete(entry.path);
  if (row) row.classList.toggle("selected", selected);
  updateSelectionFooter();
}

function updateSelectionFooter() {
  const n = S.selected.size;
  $("#selectionCount").textContent = String(n);
  $("#dirCount").textContent = `${S.browser.total || S.browser.entries.length} 项`;
  $("#clearSelectionBtn").disabled = n === 0;
  $("#openTranscodeBtn").disabled = n === 0;
  const head = $(".list-head .file-check");
  if (head) {
    const supported = S.browser.entries.filter((e) => e.is_file && e.supported);
    head.checked = supported.length > 0 && supported.every((e) => S.selected.has(e.path));
    head.indeterminate = !head.checked && supported.some((e) => S.selected.has(e.path));
  }
}

function emptyState(iconName, title, subtitle) {
  const wrap = elem("div", "empty-state");
  wrap.appendChild(icon(iconName));
  wrap.appendChild(elem("strong", "", title));
  wrap.appendChild(elem("span", "", subtitle));
  return wrap;
}

function setBrowserKind(kind) {
  $$("#typeChips .chip").forEach((chip) => chip.classList.toggle("active", chip.dataset.kind === kind));
  S.browser.kind = kind;
  loadDir(S.cwd, { reset: true });
}

/* ------------------------------------------------------------------ queue */
function sortedJobs() {
  const order = { running: 0, paused: 1, queued: 2, failed: 3, completed: 4, undone: 5, canceled: 6 };
  return [...S.jobs].sort((a, b) => {
    const d = (order[a.status] ?? 9) - (order[b.status] ?? 9);
    if (d !== 0) return d;
    return (a.created_at || 0) - (b.created_at || 0);
  });
}

function renderQueue() {
  const list = $("#queueList");
  const jobs = sortedJobs().filter((job) => {
    if (S.queueFilter === "active") return ["queued", "running", "paused"].includes(job.status);
    if (S.queueFilter === "done") return job.status === "completed" || job.status === "undone";
    if (S.queueFilter === "failed") return ["failed", "canceled"].includes(job.status);
    return true;
  });
  list.replaceChildren();
  if (!jobs.length) {
    const wrap = elem("div", "queue-empty");
    wrap.appendChild(icon("play"));
    wrap.appendChild(elem("strong", "", S.jobs.length ? "此筛选下没有任务" : "队列还是空的"));
    wrap.appendChild(elem("span", "", S.jobs.length ? "切换上方筛选查看其他状态。" : "从左侧选择文件，配置目标格式后加入队列。"));
    list.appendChild(wrap);
    return;
  }
  for (const job of jobs) list.appendChild(renderJobCard(job));
}

function renderJobCard(job) {
  const card = elem("div", `job-card ${job.status}`);
  const top = elem("div", "job-top");
  const type = elem("span", `job-type ${job.family ? `k-${job.family}` : ""}`);
  type.appendChild(icon(FAMILY_ICON[job.family] || "file"));
  const main = elem("div", "job-main");
  main.appendChild(elem("div", "job-name", job.source_name || "未命名"));
  const sub = elem("div", "job-sub");
  sub.append(
    document.createTextNode(`${extLabel(job.source_ext)} `),
    elem("span", "arrow", "→"),
    document.createTextNode(` ${extLabel(job.target)}`),
    document.createTextNode(` · ${fmtBytes(job.source_size)}`),
  );
  main.appendChild(sub);
  const state = elem("span", `job-state ${STATUS_CLASS[job.status] || ""}`, STATUS_LABEL[job.status] || job.status);
  top.append(type, main, state);

  const progressRow = elem("div", "job-progress-row");
  const track = elem("div", "progress-track");
  const fill = elem("div", `progress-fill ${job.status === "failed" ? "failed" : job.status === "completed" ? "completed" : ""}`);
  fill.style.width = `${Math.max(0, Math.min(100, job.progress || 0))}%`;
  track.appendChild(fill);
  let metaText = `${Math.round(job.progress || 0)}%`;
  if (job.status === "running" && job.speed) metaText += ` · ${job.speed}`;
  if (job.status === "running" && job.eta) metaText += ` · 剩余 ${fmtDuration(job.eta)}`;
  if (job.status === "paused") metaText = `${Math.round(job.progress || 0)}% · 已暂停`;
  if (job.status === "completed" && job.output_size) metaText = fmtBytes(job.output_size);
  progressRow.append(track, elem("span", "progress-meta", metaText));

  const bottom = elem("div", "job-bottom");
  const path = elem("span", "job-path", job.status === "completed" ? (job.output_path || "") : (job.message || `#${shortId(job.batch_id)}`));
  path.title = job.output_path || "";
  const actions = elem("div", "job-actions");
  const addAction = (iconName, title, fn, danger = false) => {
    const btn = button(`icon-btn ${danger ? "danger" : ""}`, "", iconName, title);
    btn.addEventListener("click", (event) => { event.stopPropagation(); fn(); });
    actions.appendChild(btn);
  };
  if (["running", "queued"].includes(job.status)) addAction("pause", "暂停任务", () => jobAction(job.id, "pause"));
  if (job.status === "paused") addAction("play", "继续任务", () => jobAction(job.id, "resume"));
  if (["running", "queued", "paused"].includes(job.status)) addAction("stop", "取消任务", () => jobAction(job.id, "cancel"), true);
  if (job.status === "completed") {
    addAction("download", "下载输出", () => {
      if (job.output_path) window.open(`/api/download?path=${encodeURIComponent(job.output_path)}`, "_blank");
    });
    addAction("undo", "撤销输出（移入回收站）", () => jobAction(job.id, "undo-output"), true);
  }
  if (job.status === "undone") addAction("rotate", "恢复输出", () => jobAction(job.id, "restore"));
  if (["failed", "canceled", "undone"].includes(job.status)) addAction("rotate", "重试任务", () => jobAction(job.id, "retry"));
  if (!["running", "paused"].includes(job.status)) addAction("trash", "删除任务记录（不删除输出）", () => deleteJob(job), true);
  bottom.append(path, actions);
  card.append(top, progressRow, bottom);
  if (job.status === "failed" && job.error) card.appendChild(elem("div", "job-error", job.error));
  return card;
}

async function jobAction(jobId, action) {
  try {
    await api(`/api/jobs/${jobId}/${action}`, { method: "POST", body: {} });
    await refreshState();
    const messages = {
      pause: "任务已暂停", resume: "任务已继续", cancel: "任务正在取消",
      retry: "任务已重新排队", restore: "输出已恢复", "undo-output": "输出已移入回收站，可恢复",
    };
    toast(messages[action] || "操作已提交", action === "cancel" || action === "undo-output" ? "warn" : "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteJob(job) {
  const ok = await confirmDialog(
    "删除任务记录",
    `删除「${job.source_name}」的任务记录？已生成的输出文件会保留，不会被删除。`,
    "删除记录", true,
  );
  if (!ok) return;
  try {
    await api(`/api/jobs/${job.id}`, { method: "DELETE" });
    await refreshState();
    toast("任务记录已删除（输出文件保留）", "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

/* -------------------------------------------------------------------- logs */
function renderLogs() {
  const body = $("#logBody");
  body.replaceChildren();
  const level = $("#logLevel").value;
  const logs = S.logs.filter((log) => level === "all" || log.level === level);
  if (!logs.length) {
    body.appendChild(elem("div", "log-empty", "暂无日志。开始一个转码任务后，这里会实时滚动。"));
    return;
  }
  const frag = document.createDocumentFragment();
  for (const log of logs) frag.appendChild(renderLogLine(log));
  body.appendChild(frag);
  if ($("#autoScroll").checked) body.scrollTop = body.scrollHeight;
}

function renderLogLine(log) {
  const line = elem("div", "log-line");
  line.dataset.level = log.level;
  line.appendChild(elem("span", "log-time", fmtTime(log.ts)));
  const level = elem("span", `log-level ${log.level}`, String(log.level || "info").toUpperCase().padEnd(5, " "));
  line.appendChild(level);
  if (log.job_id) line.appendChild(elem("span", "log-job", `#${shortId(log.job_id)}`));
  line.appendChild(elem("span", "log-msg", log.message || ""));
  return line;
}

function appendLogs(logs) {
  if (!logs || !logs.length) return;
  const body = $("#logBody");
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 32;
  const empty = $(".log-empty", body);
  if (empty) empty.remove();
  for (const log of logs) {
    S.logs.push(log);
    if ($("#logLevel").value === "all" || log.level === $("#logLevel").value) {
      body.appendChild(renderLogLine(log));
    }
  }
  if (S.logs.length > 1500) S.logs = S.logs.slice(-1500);
  while (body.children.length > 1200) body.firstElementChild.remove();
  if ($("#autoScroll").checked && (atBottom || body.scrollTop > 0)) body.scrollTop = body.scrollHeight;
}

/* ------------------------------------------------------------------- undo */
function renderUndo() {
  const btn = $("#undoBtn");
  const action = S.latestAction;
  btn.disabled = !action || !action.can_undo;
  btn.title = action
    ? (action.can_undo ? `撤销：${action.summary}（Ctrl+Z）` : `最近操作不可撤销：${action.reason || action.summary}`)
    : "没有可撤销的操作";
  btn.querySelector("span").textContent = action && action.can_undo ? "撤销" : "撤销";
}

async function undoLatest() {
  try {
    const data = await api("/api/undo", { method: "POST", body: {} });
    toast(`已撤销：${data.message || "最近操作"}`, "success");
    await refreshState();
    if (!$("#actionsModal").hidden) await openActions();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ------------------------------------------------------------------ polling */
function startPolling() {
  setInterval(pollState, 1100);
}

async function pollState() {
  if (S.polling || !S.app) return;
  S.polling = true;
  try {
    const data = await api(`/api/state?after_log=${S.lastLogId}`);
    S.jobs = data.jobs || [];
    S.counts = data.counts || {};
    S.queuePaused = !!data.queue_paused;
    S.latestAction = data.latest_action || null;
    if (data.settings && $("#settingsModal").hidden) S.settings = data.settings;
    if (data.logs && data.logs.length) {
      S.lastLogId = data.last_log_id || S.lastLogId;
      appendLogs(data.logs);
    } else if (data.last_log_id) {
      S.lastLogId = Math.max(S.lastLogId, data.last_log_id);
    }
    if (S.offline) {
      S.offline = false;
      toast("后端连接已恢复", "success");
    }
    renderHeaderStatus();
    renderQueue();
    renderUndo();
    updateQueueControls();
  } catch (error) {
    if (!S.offline) {
      S.offline = true;
      renderHeaderStatus();
      toast("与后端连接中断，正在重试…", "error", null, 4000);
    }
  } finally {
    S.polling = false;
  }
}

async function refreshState() {
  try {
    const data = await api("/api/state?after_log=" + S.lastLogId);
    S.jobs = data.jobs || [];
    S.counts = data.counts || {};
    S.queuePaused = !!data.queue_paused;
    S.latestAction = data.latest_action || null;
    if (data.settings) S.settings = data.settings;
    if (data.logs && data.logs.length) {
      S.lastLogId = data.last_log_id || S.lastLogId;
      appendLogs(data.logs);
    }
    renderHeaderStatus();
    renderQueue();
    renderUndo();
    updateQueueControls();
    renderSettingsValues();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ---------------------------------------------------------------- transcode */
function openTranscode() {
  if (!S.selected.size) return;
  const entries = [...S.selected.values()];
  const defaultOut = S.settings.default_output_dir || (S.app && S.app.output_root);
  S.transcode = {
    items: entries.map((entry) => ({
      entry,
      target: recommendedTarget(entry),
      name: stemOf(entry.name),
    })),
    outputDir: defaultOut,
    pattern: S.settings.name_pattern || "{name}",
    preset: S.settings.quality_preset || "balanced",
  };
  $("#outputDirInput").value = S.transcode.outputDir;
  $("#namePatternInput").value = S.transcode.pattern;
  $("#transcodeCount").textContent = String(entries.length);
  setTranscodePreset(S.transcode.preset, false);
  $("#optImageQuality").value = S.settings.image_quality ?? 88;
  $("#optAudioBitrate").value = S.settings.audio_bitrate || "192k";
  $("#optVideoCrf").value = S.settings.video_crf ?? 23;
  $("#optVideoPreset").value = S.settings.video_preset || "veryfast";
  $("#optArchiveLevel").value = S.settings.archive_level ?? 5;
  $("#optPreserveMeta").checked = S.settings.preserve_metadata !== false;
  buildBatchTargetOptions();
  renderTranscodeRows();
  $("#transcodeError").hidden = true;
  openModal("transcodeModal");
}

function uniqueTargets(entries) {
  const map = new Map();
  for (const entry of entries) {
    for (const target of targetsFor(entry.ext)) {
      if (!map.has(target.ext)) map.set(target.ext, target);
    }
  }
  return [...map.values()];
}

function buildBatchTargetOptions() {
  const select = $("#batchTargetSelect");
  select.replaceChildren();
  const opt = elem("option", "", "按文件类型推荐");
  opt.value = "";
  select.appendChild(opt);
  for (const target of uniqueTargets(S.transcode.items.map((item) => item.entry))) {
    const node = elem("option", "", `${target.label} · ${target.codec_label || target.codec || target.format || ""}`);
    node.value = target.ext;
    select.appendChild(node);
  }
}

function targetsSelect(entry, current) {
  const select = elem("select", "sel-target");
  const seen = new Set();
  for (const target of targetsFor(entry.ext)) {
    if (seen.has(target.ext)) continue;
    seen.add(target.ext);
    const option = elem("option", "", `${target.label} · ${target.codec}${target.ext === entry.ext ? " · 重编码" : ""}`);
    option.value = target.ext;
    if (target.ext === current) option.selected = true;
    select.appendChild(option);
  }
  return select;
}

function renderTranscodeRows() {
  const body = $("#transcodeRows");
  body.replaceChildren();
  if (!S.transcode) return;
  S.transcode.items.forEach((item) => {
    const tr = elem("tr");
    const fileTd = elem("td");
    const fileBox = elem("div", "sel-file");
    const iconWrap = elem("span", `file-icon k-${item.entry.family}`);
    iconWrap.appendChild(icon(FAMILY_ICON[item.entry.family] || "file"));
    const names = elem("div");
    names.appendChild(elem("div", "sel-name", item.entry.name));
    names.appendChild(elem("div", "sel-path", item.entry.path));
    fileBox.append(iconWrap, names);
    fileTd.appendChild(fileBox);

    const targetTd = elem("td");
    const select = targetsSelect(item.entry, item.target);
    select.addEventListener("change", () => { item.target = select.value; });
    targetTd.appendChild(select);

    const nameTd = elem("td");
    const wrap = elem("div", "sel-name-wrap");
    const input = elem("input", "");
    input.value = item.name;
    input.maxLength = 160;
    input.spellcheck = false;
    input.addEventListener("input", () => { item.name = input.value; });
    const ext = elem("span", "sel-ext", "." + item.target);
    select.addEventListener("change", () => { ext.textContent = "." + item.target; });
    wrap.append(input, ext);
    nameTd.appendChild(wrap);

    tr.append(fileTd, targetTd, nameTd);
    body.appendChild(tr);
  });
}

function setTranscodePreset(preset, updateAdvanced = true) {
  S.transcode.preset = preset;
  $$("#presetRow .preset").forEach((btn) => btn.classList.toggle("active", btn.dataset.preset === preset));
  if (updateAdvanced && PRESETS[preset]) {
    const p = PRESETS[preset];
    $("#optImageQuality").value = p.image_quality;
    $("#optAudioBitrate").value = p.audio_bitrate;
    $("#optVideoCrf").value = p.video_crf;
    $("#optVideoPreset").value = p.video_preset;
    $("#optArchiveLevel").value = p.archive_level;
  }
}

function applyPattern() {
  if (!S.transcode) return;
  let pattern = $("#namePatternInput").value.trim() || "{name}";
  S.transcode.pattern = pattern;
  const date = new Date();
  const dateStr = `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, "0")}${String(date.getDate()).padStart(2, "0")}`;
  S.transcode.items.forEach((item, index) => {
    let name = pattern
      .replaceAll("{name}", stemOf(item.entry.name))
      .replaceAll("{target}", item.target)
      .replaceAll("{index}", String(index + 1))
      .replaceAll("{date}", dateStr);
    name = name.replace(/[\\/:*?"<>|\x00-\x1f]/g, "_").trim();
    item.name = name || stemOf(item.entry.name);
  });
  renderTranscodeRows();
  toast("命名模板已应用", "success");
}

function applyBatchTarget() {
  const targetExt = $("#batchTargetSelect").value;
  if (!targetExt || !S.transcode) return;
  let applied = 0;
  let skipped = 0;
  for (const item of S.transcode.items) {
    const found = targetsFor(item.entry.ext).find((t) => t.ext === targetExt);
    if (found) { item.target = targetExt; applied++; } else skipped++;
  }
  renderTranscodeRows();
  toast(`已应用 ${applied} 个文件${skipped ? `，${skipped} 个不兼容已跳过` : ""}`, skipped ? "warn" : "success");
}

async function submitTranscode() {
  if (!S.transcode) return;
  const errorBox = $("#transcodeError");
  errorBox.hidden = true;
  const items = S.transcode.items.map((item) => ({
    source_path: item.entry.path,
    target: item.target,
    output_name: item.name.trim() || undefined,
  }));
  if (!items.length) return;
  const options = {
    quality_preset: S.transcode.preset,
    image_quality: Number($("#optImageQuality").value) || 88,
    audio_bitrate: $("#optAudioBitrate").value,
    video_crf: Number($("#optVideoCrf").value) || 23,
    video_preset: $("#optVideoPreset").value,
    archive_level: Number($("#optArchiveLevel").value),
    preserve_metadata: $("#optPreserveMeta").checked,
  };
  const btn = $("#createJobsBtn");
  btn.disabled = true;
  btn.replaceChildren(icon("refresh"), elem("span", "", "提交中…"));
  try {
    const data = await api("/api/jobs", {
      method: "POST",
      body: { items, options, name_pattern: S.transcode.pattern },
    });
    closeModal("transcodeModal");
    const count = data.jobs ? data.jobs.length : items.length;
    S.selected.clear();
    renderBrowser();
    toast(`已加入队列：${count} 个任务`, "success", {
      label: "撤销",
      run: async () => {
        try {
          const res = await api("/api/undo", { method: "POST", body: {} });
          toast(`已撤销：${res.message}`, "success");
          await refreshState();
        } catch (e) { toast(e.message, "error"); }
      },
    });
    await refreshState();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    btn.disabled = false;
    btn.replaceChildren(icon("play"), elem("span", "", "加入队列"));
  }
}

/* ------------------------------------------------------------------ picker */
async function openPicker(kind) {
  if (!S.transcode && kind === "transcode") return;
  if (kind === "settings" && $("#settingsModal").hidden) return;
  const current = kind === "settings" ? $("#setOutputDir").value : S.transcode.outputDir;
  S.picker = { kind, path: current || (S.app && S.app.output_root) };
  openModal("pickerModal");
  await loadPicker(S.picker.path);
}

async function loadPicker(path) {
  if (!S.picker) return false;
  try {
    const data = await api(`/api/output/list?path=${encodeURIComponent(path)}`);
    S.picker.path = data.path;
    S.picker.parent = data.parent;
    S.picker.breadcrumbs = data.breadcrumbs;
    S.picker.dirs = data.dirs;
    S.picker.free = data.free;
    $("#pickerCurrent").textContent = data.path;
    renderPicker();
    return true;
  } catch (error) {
    toast(error.message, "error");
    return false;
  }
}

function renderPicker() {
  const pathInput = $("#pickerPathInput");
  if (pathInput && document.activeElement !== pathInput) pathInput.value = S.picker.path;
  const host = $("#pickerBreadcrumbs");
  host.replaceChildren();
  for (let i = 0; i < S.picker.breadcrumbs.length; i++) {
    const crumb = S.picker.breadcrumbs[i];
    if (i > 0) host.appendChild(elem("span", "crumb-sep", "/"));
    const btn = elem("button", "crumb", i === 0 ? "导出根目录" : crumb.name);
    btn.addEventListener("click", () => loadPicker(crumb.path));
    host.appendChild(btn);
  }
  const list = $("#pickerList");
  list.replaceChildren();
  if (S.picker.parent) {
    const up = elem("div", "picker-row");
    up.append(icon("chevron-left"), elem("span", "", "上一级"));
    up.addEventListener("click", () => loadPicker(S.picker.parent));
    list.appendChild(up);
  }
  if (!S.picker.dirs.length) {
    list.appendChild(elem("div", "picker-empty", S.picker.parent ? "此目录没有子文件夹" : "还没有文件夹，可在下方新建"));
  }
  for (const dir of S.picker.dirs) {
    const row = elem("div", "picker-row");
    row.append(icon("folder"), elem("span", "", dir.name));
    row.addEventListener("click", () => loadPicker(dir.path));
    list.appendChild(row);
  }
  const free = S.picker.free ? `剩余空间 ${fmtBytes(S.picker.free)}` : "";
  $("#pickerCurrent").textContent = S.picker.path;
  $("#pickerCurrent").title = free;
}

async function createFolder() {
  const input = $("#newFolderName");
  const name = input.value.trim();
  if (!name) { toast("请输入文件夹名称", "warn"); input.focus(); return; }
  try {
    const data = await api("/api/output/mkdir", { method: "POST", body: { parent: S.picker.path, name } });
    input.value = "";
    toast("文件夹已创建", "success");
    await loadPicker(data.path);
  } catch (error) {
    toast(error.message, "error");
  }
}

function choosePicker() {
  if (!S.picker || !S.picker.path) return;
  if (S.picker.kind === "settings") $("#setOutputDir").value = S.picker.path;
  else {
    S.transcode.outputDir = S.picker.path;
    $("#outputDirInput").value = S.picker.path;
  }
  closeModal("pickerModal");
}

/* ----------------------------------------------------------------- settings */
function fillSettings() {
  const s = S.settings;
  $("#setConcurrency").value = String(s.concurrency || 2);
  $("#setOutputDir").value = s.default_output_dir || (S.app && S.app.output_root) || "/output";
  $("#setNamePattern").value = s.name_pattern || "{name}";
  $("#setQualityPreset").value = s.quality_preset || "balanced";
  $("#setVideoCrf").value = s.video_crf ?? 23;
  $("#setImageQuality").value = s.image_quality ?? 88;
  $("#setAudioBitrate").value = s.audio_bitrate || "192k";
  $("#setArchiveLevel").value = s.archive_level ?? 5;
  $("#setShowHidden").checked = !!s.show_hidden;
  $("#setPreserveMeta").checked = s.preserve_metadata !== false;
  $("#concurrencySelect").value = String(s.concurrency || 2);
}

function renderSettingsValues() {
  if (!$("#settingsModal").hidden) return;
  fillSettings();
  renderEngineInfo();
}

function renderEngineInfo() {
  const host = $("#engineInfo");
  if (!host || !S.caps) return;
  host.replaceChildren();
  for (const tool of S.caps.tools || []) {
    const pill = elem("span", `tool-pill ${tool.available ? "" : "off"}`);
    pill.append(elem("span", "dot"), document.createTextNode(`${tool.name} ${tool.version || (tool.available ? "" : "不可用")}`));
    if (tool.error) pill.title = tool.error;
    host.appendChild(pill);
  }
  for (const warning of S.caps.warnings || []) {
    const pill = elem("span", "tool-pill off");
    pill.append(elem("span", "dot"), document.createTextNode(warning));
    host.appendChild(pill);
  }
}

function openSettings() {
  fillSettings();
  renderEngineInfo();
  openModal("settingsModal");
}

async function reprobeCapabilities() {
  const btn = $("#reprobeBtn");
  if (!btn) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "探测中…";
  try {
    const data = await api("/api/capabilities/refresh", { method: "POST" });
    S.caps = data.capabilities;
    renderHeaderStatus();
    renderBrowser();
    renderEngineInfo();
    if (!$("#formatsModal").hidden) renderFormats();
    const tools = S.caps.tools || [];
    const ready = tools.filter((t) => t.available).length;
    toast(`重新探测完成：引擎 ${ready}/${tools.length} 就绪`, ready === tools.length ? "success" : "error");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function saveSettings() {
  const payload = {
    concurrency: Number($("#setConcurrency").value),
    default_output_dir: $("#setOutputDir").value,
    name_pattern: $("#setNamePattern").value.trim() || "{name}",
    quality_preset: $("#setQualityPreset").value,
    video_crf: Number($("#setVideoCrf").value),
    image_quality: Number($("#setImageQuality").value),
    audio_bitrate: $("#setAudioBitrate").value,
    archive_level: Number($("#setArchiveLevel").value),
    show_hidden: $("#setShowHidden").checked,
    preserve_metadata: $("#setPreserveMeta").checked,
  };
  try {
    const data = await api("/api/settings", { method: "PATCH", body: payload });
    S.settings = data.settings;
    $("#hiddenToggle").checked = !!S.settings.show_hidden;
    closeModal("settingsModal");
    toast("设置已保存", "success");
    updateQueueControls();
    await loadDir(S.cwd, { reset: true });
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ----------------------------------------------------------------- formats */
function openFormats() {
  if (!S.caps) return;
  renderFormats();
  openModal("formatsModal");
}

function renderFormats() {
  if (!S.formatsFamily) S.formatsFamily = "audio";
  const toolStrip = $("#toolStrip");
  toolStrip.replaceChildren();
  for (const tool of S.caps.tools || []) {
    const pill = elem("span", `tool-pill ${tool.available ? "" : "off"}`);
    pill.append(elem("span", "dot"), document.createTextNode(`${tool.name} ${tool.version || (tool.available ? "" : "不可用")}`));
    if (tool.error) pill.title = tool.error;
    toolStrip.appendChild(pill);
  }
  const tabs = $("#formatTabs");
  tabs.replaceChildren();
  const families = S.caps.families || {};
  const order = ["audio", "video", "image", "archive", "subtitle"];
  for (const key of order) {
    if (!families[key]) continue;
    const btn = elem("button", S.formatsFamily === key ? "active" : "", families[key].label || key);
    btn.addEventListener("click", () => { S.formatsFamily = key; renderFormats(); });
    tabs.appendChild(btn);
  }
  if (!S.formatsFamily || !families[S.formatsFamily]) S.formatsFamily = order.find((k) => families[k]) || "audio";
  const body = $("#formatBody");
  body.replaceChildren();
  if (S.caps.warnings && S.caps.warnings.length) {
    body.appendChild(elem("div", "warning-box", S.caps.warnings.join("；")));
  }
  const family = families[S.formatsFamily];
  if (!family) {
    body.appendChild(elem("div", "empty-mini", "当前镜像没有探测到可用格式。"));
    return;
  }
  if (family.note) body.appendChild(elem("div", "warning-box", family.note));
  const extensions = family.available_extensions || [];
  if (!extensions.length) {
    body.appendChild(elem("div", "empty-mini", "该类别没有可用的源格式。"));
  }
  for (const ext of extensions) {
    const targets = targetsFor(ext);
    const row = elem("div", "format-source");
    const head = elem("div", "format-source-head");
    head.append(
      elem("strong", "", extLabel(ext)),
      elem("span", "badge", family.label || S.formatsFamily),
      elem("span", "muted", `${targets.length} 种目标`),
    );
    const targetWrap = elem("div", "format-targets");
    for (const target of targets) {
      const chip = elem("span", "format-target");
      chip.append(elem("b", "", target.label), document.createTextNode(target.codec_label || target.codec || target.format || ""));
      targetWrap.appendChild(chip);
    }
    row.append(head, targetWrap);
    body.appendChild(row);
  }
  const totalSources = Object.keys(S.caps.matrix || {}).length;
  const probed = new Date((S.caps.probed_at || 0) * 1000);
  const stamp = `${probed.getFullYear()}-${String(probed.getMonth() + 1).padStart(2, "0")}-${String(probed.getDate()).padStart(2, "0")} ${String(probed.getHours()).padStart(2, "0")}:${String(probed.getMinutes()).padStart(2, "0")}`;
  $("#formatFoot").textContent = `${totalSources} 个源扩展名 · 探测于 ${stamp}`;
}

/* ------------------------------------------------------------------ actions */
async function openActions() {
  try {
    const data = await api("/api/actions?limit=50");
    const host = $("#actionList");
    host.replaceChildren();
    if (!data.actions.length) {
      host.appendChild(elem("div", "empty-mini", "还没有操作记录。"));
    }
    for (const action of data.actions) {
      const row = elem("div", "action-row");
      const ic = elem("span", "action-icon");
      ic.appendChild(icon(action.kind === "settings" ? "settings" : action.kind === "mkdir" ? "folder-plus" : action.kind === "complete" ? "check" : (action.kind === "delete_job" || action.kind === "clear_completed") ? "trash" : "undo"));
      const main = elem("div", "action-main");
      main.appendChild(elem("div", "action-title", action.summary));
      main.appendChild(elem("div", "action-time", `${fmtTime(action.ts, true)}${action.undone_at ? " · 已撤销" : ""}`));
      row.append(ic, main);
      if (!action.undone_at && action.can_undo) {
        const btn = button("btn soft small", "撤销");
        btn.addEventListener("click", async () => {
          try {
            const res = await api(`/api/actions/${action.id}/undo`, { method: "POST", body: {} });
            toast(`已撤销：${res.message}`, "success");
            await refreshState();
            await openActions();
          } catch (error) { toast(error.message, "error"); }
        });
        row.appendChild(btn);
      } else if (action.can_restore) {
        const btn = button("btn soft small", "恢复输出");
        btn.addEventListener("click", async () => {
          try {
            await api(`/api/jobs/${action.payload.job_id}/restore`, { method: "POST", body: {} });
            toast("输出已恢复", "success");
            await refreshState();
            await openActions();
          } catch (error) { toast(error.message, "error"); }
        });
        row.appendChild(btn);
      } else {
        row.appendChild(elem("span", "action-state", action.undone_at ? "已撤销" : (action.reason || "不可撤销")));
      }
      host.appendChild(row);
    }
    openModal("actionsModal");
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ------------------------------------------------------------------ modals */
function openModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.hidden = false;
  setTimeout(() => {
    const focusable = el.querySelector("button:not([disabled]), input:not([disabled]), select:not([disabled])");
    if (focusable) focusable.focus({ preventScroll: true });
  }, 40);
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.hidden = true;
}

function closeTopModal() {
  const open = $$(".modal-backdrop").filter((el) => !el.hidden);
  if (open.length) open[open.length - 1].hidden = true;
}

function confirmDialog(title, text, okLabel = "确认", danger = false) {
  return new Promise((resolve) => {
    $("#confirmTitle").textContent = title;
    $("#confirmText").textContent = text;
    const ok = $("#confirmOk");
    ok.textContent = okLabel;
    ok.className = `btn ${danger ? "danger" : "primary"}`;
    const cleanup = () => {
      closeModal("confirmModal");
      ok.removeEventListener("click", onOk);
      $("#confirmCancel").removeEventListener("click", onCancel);
      ok.removeEventListener("click", onCancel);
    };
    const onOk = () => { cleanup(); resolve(true); };
    const onCancel = () => { cleanup(); resolve(false); };
    ok.addEventListener("click", onOk);
    $("#confirmCancel").addEventListener("click", onCancel);
    openModal("confirmModal");
  });
}

/* ------------------------------------------------------------------- events */
function bindGlobalEvents() {
  $("#themeBtn").addEventListener("click", toggleTheme);
  $("#refreshBtn").addEventListener("click", () => loadDir(S.cwd, { reset: true }));
  $("#upBtn").addEventListener("click", () => { if (S.browser.parent) loadDir(S.browser.parent, { reset: true }); });
  $("#pathGoBtn").addEventListener("click", jumpSourcePath);
  $("#pathInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); jumpSourcePath(); }
    if (event.key === "Escape") {
      event.preventDefault();
      $("#pathInput").value = sourceDisplayPath(S.browser.path || S.cwd);
      $("#pathInput").blur();
    }
  });
  $("#hiddenToggle").addEventListener("change", () => loadDir(S.cwd, { reset: true }));
  $("#searchInput").addEventListener("input", () => {
    clearTimeout(S.searchTimer);
    S.searchTimer = setTimeout(() => loadDir(S.browser.path || S.cwd, { reset: true }), 320);
  });
  $("#searchInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { clearTimeout(S.searchTimer); loadDir(S.browser.path || S.cwd, { reset: true }); }
  });
  $("#typeChips").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (chip) setBrowserKind(chip.dataset.kind);
  });
  $("#clearSelectionBtn").addEventListener("click", () => {
    S.selected.clear();
    renderBrowser();
  });
  $("#openTranscodeBtn").addEventListener("click", openTranscode);
  $("#pauseQueueBtn").addEventListener("click", async () => {
    try { await api("/api/queue/pause", { method: "POST", body: {} }); await refreshState(); toast("队列已暂停", "warn"); }
    catch (error) { toast(error.message, "error"); }
  });
  $("#resumeQueueBtn").addEventListener("click", async () => {
    try { await api("/api/queue/resume", { method: "POST", body: {} }); await refreshState(); toast("队列已继续", "success"); }
    catch (error) { toast(error.message, "error"); }
  });
  $("#stopQueueBtn").addEventListener("click", async () => {
    const ok = await confirmDialog(
      "终止整个队列",
      "将取消所有排队任务并终止正在运行的转码进程。已完成输出不会被删除，源文件绝不会改动。",
      "终止任务", true,
    );
    if (!ok) return;
    try { await api("/api/queue/stop", { method: "POST", body: {} }); await refreshState(); toast("队列已终止，可在需要时继续", "warn"); }
    catch (error) { toast(error.message, "error"); }
  });
  $("#retryFailedBtn").addEventListener("click", async () => {
    const count = S.counts.failed || 0;
    if (!count) return;
    try {
      const data = await api("/api/queue/retry-failed", { method: "POST", body: {} });
      await refreshState();
      toast(data.retried ? `已重新排队 ${data.retried} 个失败任务` : "没有可重试的失败任务", "success");
    } catch (error) { toast(error.message, "error"); }
  });
  $("#clearCompletedBtn").addEventListener("click", async () => {
    const count = S.counts.completed || 0;
    if (!count) return;
    const ok = await confirmDialog(
      "清除已完成任务记录",
      `将删除 ${count} 条已完成任务记录，但不会删除任何输出文件，也不会触碰源文件。此操作可撤销。`,
      "清除记录", true,
    );
    if (!ok) return;
    try {
      const data = await api("/api/queue/clear-completed", { method: "POST", body: {} });
      await refreshState();
      toast(`已清除 ${data.cleared} 条已完成记录（输出保留）`, "success", {
        label: "撤销",
        run: async () => {
          try {
            const res = await api("/api/undo", { method: "POST", body: {} });
            toast(`已撤销：${res.message}`, "success");
            await refreshState();
          } catch (e) { toast(e.message, "error"); }
        },
      });
    } catch (error) { toast(error.message, "error"); }
  });
  $("#concurrencySelect").addEventListener("change", async (event) => {
    const value = Number(event.target.value);
    try {
      const data = await api("/api/settings", { method: "PATCH", body: { concurrency: value } });
      S.settings = data.settings;
      toast(`并发数已设为 ${value}`, "success");
      updateQueueControls();
    } catch (error) { toast(error.message, "error"); }
  });
  $("#queueTabs").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-filter]");
    if (!btn) return;
    S.queueFilter = btn.dataset.filter;
    $$("#queueTabs button").forEach((b) => b.classList.toggle("active", b === btn));
    renderQueue();
  });
  $("#undoBtn").addEventListener("click", undoLatest);
  $("#historyBtn").addEventListener("click", openActions);
  $("#formatsBtn").addEventListener("click", openFormats);
  $("#settingsBtn").addEventListener("click", openSettings);
  $("#logLevel").addEventListener("change", () => { S.logLevel = $("#logLevel").value; renderLogs(); });
  $("#clearLogBtn").addEventListener("click", () => {
    S.logs = [];
    $("#logBody").replaceChildren(elem("div", "log-empty", "视图已清空；服务端历史日志仍保留。"));
  });
  $("#autoScroll").addEventListener("change", () => { if ($("#autoScroll").checked) $("#logBody").scrollTop = $("#logBody").scrollHeight; });

  $("#transcodeModal").addEventListener("click", (event) => { if (event.target === $("#transcodeModal")) closeModal("transcodeModal"); });
  $("#pickerModal").addEventListener("click", (event) => { if (event.target === $("#pickerModal")) closeModal("pickerModal"); });
  $("#settingsModal").addEventListener("click", (event) => { if (event.target === $("#settingsModal")) closeModal("settingsModal"); });
  $("#formatsModal").addEventListener("click", (event) => { if (event.target === $("#formatsModal")) closeModal("formatsModal"); });
  $("#actionsModal").addEventListener("click", (event) => { if (event.target === $("#actionsModal")) closeModal("actionsModal"); });
  $$("[data-close]").forEach((btn) => btn.addEventListener("click", () => closeModal(btn.dataset.close)));

  $("#batchTargetSelect").addEventListener("change", applyBatchTarget);
  $("#applyPatternBtn").addEventListener("click", applyPattern);
  $("#pickOutputBtn").addEventListener("click", () => openPicker("transcode"));
  $("#presetRow").addEventListener("click", (event) => {
    const btn = event.target.closest(".preset");
    if (btn) setTranscodePreset(btn.dataset.preset, true);
  });
  $("#createJobsBtn").addEventListener("click", submitTranscode);

  $("#chooseFolderBtn").addEventListener("click", choosePicker);
  $("#createFolderBtn").addEventListener("click", createFolder);
  $("#pickerPathGoBtn").addEventListener("click", jumpPickerPath);
  $("#pickerPathInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); jumpPickerPath(); }
    if (event.key === "Escape") {
      event.preventDefault();
      $("#pickerPathInput").value = S.picker ? S.picker.path : "";
      $("#pickerPathInput").blur();
    }
  });
  $("#newFolderName").addEventListener("keydown", (event) => { if (event.key === "Enter") createFolder(); });

  $("#pickDefaultOutputBtn").addEventListener("click", () => openPicker("settings"));
  $("#saveSettingsBtn").addEventListener("click", saveSettings);
  $("#reprobeBtn").addEventListener("click", reprobeCapabilities);

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { closeTopModal(); return; }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
      const tag = (event.target.tagName || "").toLowerCase();
      if (["input", "textarea", "select"].includes(tag)) return;
      event.preventDefault();
      undoLatest();
    }
  });
}

/* ------------------------------------------------------------------ offline */
function setOffline(value) {
  S.offline = value;
  renderHeaderStatus();
}

/* --------------------------------------------------------------------- start */
// expose a tiny debug surface for browser-based smoke tests (no user data)
window.__NTS__ = { S, api, refreshState };
