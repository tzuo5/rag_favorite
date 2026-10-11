"use strict";
const $ = id => document.getElementById(id);
let csrf = "", snapshot = null, busy = false, stale = true, page = 0, active = false;
let refreshRunning = false, timer = null, generation = 0;
const PAGE_SIZE = 40;
const opened = new Set();
function node(tag, content, className) {
  const el = document.createElement(tag);
  if (content !== undefined && content !== null) el.textContent = String(content);
  if (className) el.className = className;
  return el;
}
function notice(message, error = false) { $("notice").textContent = message; $("notice").classList.toggle("error", error); }
function date(value) { return value ? new Date(value).toLocaleString() : "—"; }
async function api(path, body) {
  const options = {credentials: "same-origin", cache: "no-store", headers: {}};
  if (body !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf};
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch { throw new Error("服务响应异常，请稍后重试。"); }
  if (response.status === 401 && path !== "/api/login") showLogin();
  if (!response.ok && !(path === "/api/snapshot" && response.status === 503)) {
    throw new Error(typeof data.detail === "string" ? data.detail : "请求未完成，请刷新后重试。");
  }
  return data;
}
function showLogin() {
  active = false; csrf = ""; snapshot = null; generation++;
  clearTimeout(timer);
  $("login-panel").hidden = false; $("dashboard").hidden = true; $("logout").hidden = true;
  $("jobs").replaceChildren(); $("models").replaceChildren(); $("logs").textContent = "";
}
function showDashboard() { active = true; $("login-panel").hidden = true; $("dashboard").hidden = false; $("logout").hidden = false; }
function controls() {
  const unavailable = busy || stale || !snapshot;
  const running = snapshot?.worker?.ActiveState === "active";
  $("start").disabled = unavailable || (running && !snapshot.control.paused);
  const disabled = unavailable || !snapshot?.control_supported;
  $("stop").disabled = disabled || snapshot?.control?.paused;
  document.querySelectorAll(".job-buttons button").forEach(button => { button.disabled = disabled; });
}
async function act(action, jobId) {
  if (busy || stale) return;
  busy = true; controls(); notice("正在执行，请等待确认…");
  try {
    const body = {action, request_id: crypto.randomUUID()};
    if (jobId) body.job_id = jobId;
    const result = await api("/api/action", body);
    notice(result.message);
    await refresh(true);
  } catch (error) { notice(error.message, true); }
  finally { busy = false; controls(); }
}
function renderJobs() {
  if (!snapshot) return;
  const search = $("search").value.toLowerCase(), state = $("state-filter").value, collection = $("collection-filter").value;
  const jobs = snapshot.jobs.filter(job => {
    const statusMatch = !state || (state === "paused" ? job.paused && job.state === "queued" : job.state === state && !(state === "queued" && job.paused));
    return statusMatch && (!collection || job.collection === collection) && [job.title, job.id, job.error_code].join(" ").toLowerCase().includes(search);
  });
  const pages = Math.max(1, Math.ceil(jobs.length / PAGE_SIZE));
  page = Math.min(page, pages - 1);
  $("result-count").textContent = `${jobs.length} 项${snapshot.truncated ? " · 列表仅包含后台返回的前 2000 项" : ""}`;
  $("page-label").textContent = `${page + 1} / ${pages}`;
  $("previous").disabled = page === 0; $("next").disabled = page >= pages - 1;
  const fragment = document.createDocumentFragment();
  for (const job of jobs.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)) {
    const card = node("article", null, "job"); card.dataset.jobId = job.id;
    const heading = node("div", null, "job-heading");
    heading.append(node("h3", job.title), node("span", job.state_label, "badge")); card.append(heading);
    const meta = node("div", null, "job-meta");
    [job.collection, job.stage_label, job.model, `实际进度：${job.progress}`, job.queue_rank ? `队列顺序 ${job.queue_rank}` : ""].filter(Boolean).forEach(text => meta.append(node("span", text)));
    card.append(meta);
    const details = node("details"); details.open = opened.has(job.id);
    details.append(node("summary", "任务详情"));
    const dl = node("dl");
    for (const [key, value] of [["任务 ID", job.id], ["错误码", job.error_code || "—"], ["尝试次数", job.attempts], ["更新时间", date(job.updated_at)], ["创建时间", date(job.created_at)], ["来源状态", job.media_expired ? "媒体已过期，需重新提交来源" : "—"]]) dl.append(node("dt", key), node("dd", value));
    details.append(dl); details.addEventListener("toggle", () => { if (details.open) opened.add(job.id); else opened.delete(job.id); }); card.append(details);
    if (["queued", "running", "failed", "blocked"].includes(job.state)) {
      const buttons = node("div", null, "job-buttons");
      const start = node("button", ["failed", "blocked"].includes(job.state) ? "Start · 重试任务" : "Start · 恢复任务");
      start.addEventListener("click", () => act("start_job", job.id));
      const stop = node("button", "Stop · 暂停任务"); stop.addEventListener("click", () => act("stop_job", job.id));
      buttons.append(start, stop); card.append(buttons);
    }
    fragment.append(card);
  }
  if (!jobs.length) fragment.append(node("p", "没有符合条件的任务。", "muted"));
  $("jobs").replaceChildren(fragment); controls();
}
function render() {
  if (!snapshot) return;
  const counts = snapshot.counts, paused = snapshot.paused_pending;
  const phases = snapshot.pipeline?.stages || {};
  const actualRunning = snapshot.pipeline?.available ? Object.values(phases).reduce((sum, row) => sum + row.running, 0) : (counts.running || 0);
  const stageWaiting = Object.values(phases).reduce((sum, row) => sum + row.waiting + row.retry_wait, 0);
  $("counts").replaceChildren(...[["执行阶段中", actualRunning], ["等待中", Math.max(0, (counts.queued || 0) - paused) + stageWaiting], ["已暂停", paused], ["已完成", counts.complete || 0], ["可检索完成", snapshot.call_metrics?.searchable_completed ?? "—"], ["已合并", counts.duplicate || 0], ["受阻", counts.blocked || 0], ["失败", counts.failed || 0]].map(([label, value]) => {
    const card = node("div", null, "stat"); card.append(node("strong", value), node("span", label)); return card;
  }));
  $("pipeline").replaceChildren(...Object.entries(phases).map(([stage, row]) => {
    const card = node("article", null, "stat");
    card.append(node("strong", {download:"下载",prepare:"本地准备",llm:"LLM",publish:"发布"}[stage]), node("span", `执行 ${row.running} · 等待 ${row.waiting} · 重试 ${row.retry_wait} · 受阻 ${row.blocked}`));
    return card;
  }));
  $("pipeline-status").textContent = snapshot.pipeline?.available ? (snapshot.pipeline.circuit_until ? `链路冷却至 ${date(snapshot.pipeline.circuit_until)}` : "各阶段独立交接 · LLM 并发 1") : "流水线状态暂不可用";
  const metrics = snapshot.call_metrics || {}, attempts = metrics.attempts || 0;
  const rate = value => attempts ? `${value}/${attempts} (${(100 * value / attempts).toFixed(1)}%)` : "暂无请求";
  $("call-metrics").textContent = `HTTP 成功 ${rate(metrics.http_success || 0)} · 完整 JSON ${rate(metrics.valid_outputs || 0)} · 未标注请求 ${metrics.unknown_attempts || 0}`;
  $("model-calls").replaceChildren(...(snapshot.model_calls || []).slice(0, 20).map(call => {
    const card = node("article", null, "job");
    card.append(node("h3", `${call.model} · ${call.operation}`), node("p", `状态 ${call.status} · HTTP ${call.http_status ?? "—"} · 流完成 ${call.stream_complete === true ? "是" : "否"} · JSON ${call.valid_output === true ? "有效" : "未确认"}`), node("p", `任务 ${call.job_id || "—"} · 耗时 ${call.duration_seconds ?? "—"} 秒 · ${call.error_category || ""}`));
    return card;
  }));
  $("worker").textContent = `后台：${snapshot.worker.ActiveState} · 队列${snapshot.control.paused ? "已暂停领取" : "允许领取"}${snapshot.control_supported ? "" : " · 控制能力暂不可用"}`;
  $("freshness").textContent = `${stale ? "信息过时 · " : "每 3 秒刷新 · "}采集时间：${date(snapshot.captured_at)} · 共 ${snapshot.total} 项`;
  $("models").replaceChildren(...snapshot.models.map(model => {
    const card = node("article", null, "model");
    card.append(node("h3", model.key), node("span", stale ? "信息过时" : model.state, "badge"));
    [model.role, model.name, model.connection, model.activity, model.task !== "—" ? `任务：${model.task}` : ""].filter(Boolean).forEach(text => card.append(node("p", text)));
    return card;
  }));
  const selected = $("collection-filter").value;
  const all = node("option", "全部分类"); all.value = "";
  $("collection-filter").replaceChildren(all, ...[...new Set(snapshot.jobs.map(job => job.collection))].sort().map(value => {const option = node("option", value); option.value = value; return option;}));
  $("collection-filter").value = selected;
  $("logs").textContent = snapshot.logs.join("\n"); renderJobs();
}
async function refresh(keepNotice = false) {
  if (!active || refreshRunning) return;
  const current = generation;
  refreshRunning = true;
  try {
    const data = await api("/api/snapshot");
    if (!active || current !== generation) return;
    snapshot = data.snapshot; stale = data.stale;
    if (stale) notice(data.error, true);
    else if (!busy && !keepNotice) notice("连接正常 · 正在显示真实任务状态");
    render(); controls();
  } catch (error) {
    if (active && current === generation) {stale = true; notice("连接中断，显示的数据可能过时。" + error.message, true); render(); controls();}
  } finally {
    refreshRunning = false;
    if (active && current === generation) {clearTimeout(timer); timer = setTimeout(refresh, 3000);}
  }
}
$("login-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const data = await api("/api/login", {username: $("username").value, password: $("password").value});
    $("password").value = ""; csrf = data.csrf; showDashboard(); await refresh();
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", async () => {try {await api("/api/logout", {}); showLogin(); notice("已退出登录。");} catch (error) {notice(error.message, true);}});
$("start").addEventListener("click", () => act("start")); $("stop").addEventListener("click", () => act("stop"));
for (const id of ["search", "state-filter", "collection-filter"]) $(id).addEventListener(id === "search" ? "input" : "change", () => {page = 0; renderJobs();});
$("previous").addEventListener("click", () => {page--; renderJobs();}); $("next").addEventListener("click", () => {page++; renderJobs();});
(async () => {try {const data = await api("/api/session"); csrf = data.csrf; showDashboard(); await refresh();} catch (error) {showLogin(); notice(error.message);}})();
