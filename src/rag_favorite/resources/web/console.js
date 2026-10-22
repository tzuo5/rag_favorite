"use strict";
const $ = id => document.getElementById(id);
let csrf = "", snapshot = null, busy = false, stale = true, page = 1, active = false;
let refreshRunning = false, timer = null, generation = 0, refreshRequest = 0;
let view = "overview", selectedId = "", selectedJob = null, selectedStale = true;
let jobsStale = true, listKey = "", listItems = [], listTotal = 0, pendingList = null;
let jobsController = null, jobsRequest = 0, detailRequest = 0, jobsLoading = false;
let searchTimer = null, toastTimer = null, confirmAction = null, returnFocus = null;
const PAGE_SIZE = 40, positions = {}, rowNodes = new Map(), feedback = new Map();
const titles = {overview: "工作概览", tasks: "任务管理", runtime: "运行状态", account: "账号与设置"};
const stateLabels = {queued:"等待中",running:"运行中",paused:"已暂停",complete:"已完成",duplicate:"已合并",published:"清理中",blocked:"受阻",failed:"失败"};
function node(tag, content, className) {
  const el = document.createElement(tag);
  if (content !== undefined && content !== null) el.textContent = String(content);
  if (className) el.className = className;
  return el;
}
function text(el, value) { const next = String(value ?? "—"); if (el.textContent !== next) el.textContent = next; }
function date(value) { return value ? new Date(value).toLocaleString(undefined, {month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"}) : "—"; }
function notice(message, error = false) {
  text($("notice"), message); $("notice").classList.toggle("error", error); $("notice").hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $("notice").hidden = true; }, error ? 16000 : 7000);
}
function jobState(job) {
  if (job.display_state) return job.display_state;
  const waiting = job.state === "queued" || (job.state === "running" && ["waiting", "retry_wait"].includes(job.pipeline_status));
  return waiting ? (job.paused ? "paused" : "queued") : job.state;
}
async function api(path, body, extra = {}) {
  const requestGeneration = generation;
  const options = {credentials:"same-origin",cache:"no-store",headers:{},...extra};
  if (body !== undefined) {
    options.method = "POST"; options.headers = {"Content-Type":"application/json","X-CSRF-Token":csrf};
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch { throw new Error("服务响应异常，请稍后重试。"); }
  if (response.status === 401 && path !== "/api/login" && requestGeneration === generation) showLogin();
  if (!response.ok && !(path.startsWith("/api/snapshot") && response.status === 503)) {
    const error = new Error(typeof data.detail === "string" ? data.detail : "请求未完成，请稍后重试。");
    error.status = response.status; throw error;
  }
  return data;
}
function showLogin() {
  active = false; csrf = ""; snapshot = null; stale = true; busy=false; generation++; refreshRequest++; refreshRunning=false; jobsLoading=false;
  clearTimeout(timer); clearTimeout(searchTimer); jobsController?.abort(); jobsRequest++; detailRequest++;
  selectedId = ""; selectedJob = null; listItems = []; listKey = ""; pendingList = null; feedback.clear(); rowNodes.clear();
  if ($("confirm-dialog").open) $("confirm-dialog").close();
  $("dashboard").hidden = true; $("login-panel").hidden = false;
  for (const id of ["jobs","models","logs","model-calls","detail-fields","detail-title","exceptions","counts","pipeline","all-counts","call-metrics"]) $(id).replaceChildren();
  delete $("model-calls").dataset.signature; $("xhs-alert").hidden=true; $("pause-banner").hidden=true;
  $("notice").hidden=true;clearTimeout(toastTimer);returnFocus=null;
  setDetailLayout(); document.dispatchEvent(new CustomEvent("console-auth", {detail:false}));
}
function showDashboard() {
  active = true; $("login-panel").hidden = true; $("login-error").hidden = true; $("dashboard").hidden = false;
  applyRoute(); document.dispatchEvent(new CustomEvent("console-auth", {detail:true}));
}
function queryParams() {
  const params = new URLSearchParams({page:String(page),page_size:String(PAGE_SIZE)});
  for (const [key,id] of [["q","search"],["state","state-filter"],["reason","reason-filter"],["collection","collection-filter"]]) {
    if ($(id).value) params.set(key, $(id).value);
  }
  return params;
}
function routeHash(target = view, job = selectedId) {
  if (target !== "tasks") return "#" + target;
  const params = queryParams(); params.delete("page_size");
  if (job) params.set("job", job);
  return "#tasks?" + params.toString();
}
function setRoute(hash, replace = false) {
  if (location.hash === hash) return;
  history[replace ? "replaceState" : "pushState"]({ragConsole:true,ragDetail:hash.includes("job=") && (!replace || Boolean(history.state?.ragDetail))}, "", hash); applyRoute();
}
function navigate(target) { setRoute(routeHash(target, "")); }
function ensureOption(select, value, label) {
  if (!value || [...select.options].some(option => option.value === value)) return;
  const option = node("option", label || value); option.value = value; select.append(option);
}
function applyRoute() {
  if (!active) return;
  const [rawView, rawQuery = ""] = location.hash.slice(1).split("?");
  const next = Object.hasOwn(titles, rawView) ? rawView : "overview";
  const params = new URLSearchParams(rawQuery), oldView = view, oldKey = queryParams().toString();
  if (next !== view) positions[view] = window.scrollY;
  view = next;
  if (next === "tasks") {
    for (const [key,id] of [["q","search"],["state","state-filter"],["reason","reason-filter"],["collection","collection-filter"]]) {
      const value = params.get(key) || "";
      if (id === "reason-filter" || id === "collection-filter") ensureOption($(id), value);
      $(id).value = value;
    }
    page = Math.max(1, Math.min(1000000, parseInt(params.get("page") || "1", 10) || 1));
  }
  for (const name of Object.keys(titles)) $("view-" + name).hidden = name !== next;
  for (const button of document.querySelectorAll("[data-view]")) {
    if (button.dataset.view === next) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  }
  text($("view-title"), titles[next]); text($("view-eyebrow"), "WORKSPACE / " + next.toUpperCase());
  document.title = titles[next] + " · RAG";
  const job = next === "tasks" ? params.get("job") || "" : "";
  if (job !== selectedId) selectJob(job);
  setDetailLayout();
  if (oldView !== next) requestAnimationFrame(() => window.scrollTo(0, positions[next] || 0));
  if (next === "tasks" && (oldView !== next || oldKey !== queryParams().toString() || !listKey)) loadJobs(true);
  document.dispatchEvent(new CustomEvent("console-view", {detail:view}));
}
function filteredTasks(state, reason = "") {
  const params = new URLSearchParams({page:"1"});
  if (state) params.set("state",state); if (reason) params.set("reason",reason);
  setRoute("#tasks?" + params.toString());
}
function controls() {
  const unavailable = busy || stale || !snapshot;
  const running = snapshot?.worker?.ActiveState === "active";
  $("start").disabled = unavailable || (running && !snapshot.control.paused);
  const disabled = unavailable || !snapshot?.control_supported;
  $("stop").disabled = disabled || snapshot?.control?.paused;
  const job = selectedJob;
  $("detail-start").disabled = disabled || selectedStale || !job || job._missing || !(job.paused || ["failed","blocked"].includes(job.state));
  $("detail-stop").disabled = disabled || selectedStale || !job || job._missing || job.paused || !["queued","running"].includes(job.state);
  $("detail-start").hidden = !job || !["queued","running","failed","blocked"].includes(job.state);
  $("detail-stop").hidden = !job || !["queued","running"].includes(job.state);
}
function confirmGlobal(action) {
  confirmAction = action;
  text($("confirm-title"), action === "stop" ? "暂停整个队列？" : "恢复队列？");
  text($("confirm-message"), action === "stop" ? "将暂停领取新阶段和新模型调用，正在执行的操作保存结果后暂停。等待中的任务仍保留在队列中。" : "允许后台继续领取任务，必要时启动 worker。不会自动重试失败任务，也不会恢复单独暂停的任务。");
  text($("confirm-accept"), action === "stop" ? "确认暂停" : "确认恢复");
  $("confirm-dialog").showModal(); $("confirm-cancel").focus();
}
async function act(action, jobId) {
  if (busy || stale || (jobId && selectedStale)) return;
  const epoch = generation;
  busy = true; controls();
  if (jobId) {feedback.set(jobId, {message:"正在执行，请等待确认…",error:false}); renderDetail();}
  else notice("正在执行，请等待确认…");
  try {
    const body = {action,request_id:crypto.randomUUID()}; if (jobId) body.job_id = jobId;
    const result = await api("/api/action", body);
    if (!active || generation !== epoch) return;
    if (jobId) feedback.set(jobId, {message:result.message,error:false}); else notice(result.message);
    await refresh();
  } catch (error) {
    if (active && generation === epoch) {
      if (jobId) feedback.set(jobId, {message:error.message,error:true}); else notice(error.message,true);
    }
  } finally {if (generation===epoch) {busy = false; if (active) {renderDetail(); controls();}}}
}
function badge(el, state, label) { el.dataset.state = state; text(el,label || stateLabels[state] || state); }
function metric(key, label, value, foot, state) {
  let card = $("metric-" + key);
  if (!card) {
    card = node(state ? "button" : "div",null,"metric" + (key === "attention" ? " attention" : "")); card.id = "metric-" + key;
    card.append(node("span",label,"metric-label"),node("strong",value,"metric-value"),node("span",foot,"metric-foot"));
    if (state) card.addEventListener("click", () => filteredTasks(state));
    $("counts").append(card);
  }
  text(card.querySelector(".metric-value"),value);
}
function syncOptions(select, entries, allLabel) {
  const selected = select.value, desired = new Set(entries.map(entry => entry[0]));
  for (const option of [...select.options]) if (option.value && option.value !== selected && !desired.has(option.value)) option.remove();
  for (const [value,label] of entries) { ensureOption(select,value,label); text([...select.options].find(option => option.value === value),label); }
  text(select.options[0],allLabel); select.value = selected;
}
function render() {
  if (!snapshot) return;
  const summary = snapshot.task_summary, counts = summary?.counts || snapshot.counts || {};
  const attention = (counts.blocked || 0) + (counts.failed || 0);
  const paused = Boolean(snapshot.control?.paused), workerRunning = snapshot.worker?.ActiveState === "active";
  $("pause-banner").hidden=!paused;
  text($("queue-title"), paused ? "队列已暂停" : workerRunning ? "队列运行中" : "后台尚未运行");
  text($("worker"), paused ? "等待中的任务已保留，可随时恢复领取。" : workerRunning ? "后台允许领取任务，处理进度会自动更新。" : "可恢复队列以启动后台处理。");
  text($("freshness"), "采集于 " + date(snapshot.captured_at) + " · 共 " + (summary?.total ?? snapshot.total) + " 项任务");
  metric("running","执行中",counts.running || 0,"查看当前执行任务","running");
  metric("queued","等待中",counts.queued || 0,"查看待处理队列","queued");
  metric("searchable","可检索完成",snapshot.call_metrics?.searchable_completed ?? "—","以当前检索索引为准");
  metric("attention","需处理",attention,"失败与受阻任务","attention");
  text($("all-counts"), "已完成 " + (counts.complete || 0) + " · 已合并 " + (counts.duplicate || 0) + " · 单独暂停 " + (counts.paused || 0) + " · 清理中 " + (counts.published || 0));
  const phases = snapshot.pipeline?.stages || {};
  for (const [index,[key,label]] of Object.entries({download:"下载",prepare:"本地准备",llm:"模型处理",publish:"发布"}).entries()) {
    let card = $("stage-" + key);
    if (!card) { card=node("article",null,"stage"); card.id="stage-" + key; card.append(node("span","0" + (index+1),"stage-number"),node("strong",label),node("div",null,"stage-values")); $("pipeline").append(card); }
    const row=phases[key];
    text(card.querySelector(".stage-values"),row ? "执行 " + row.running + " · 等待 " + row.waiting + "\n重试 " + row.retry_wait + " · 受阻 " + row.blocked : "状态暂不可用");
  }
  text($("pipeline-status"),snapshot.pipeline?.available ? snapshot.pipeline.circuit_until ? "模型链路冷却至 " + date(snapshot.pipeline.circuit_until) : "各阶段独立交接 · 显示当前阶段的实际状态" : "流水线状态暂不可用");
  text($("exception-total"),attention + " 项");
  const reasons = summary?.reasons || [];
  const existing = new Map([...$("exceptions").querySelectorAll("[data-reason]")].map(el => [el.dataset.reason,el]));
  if (!reasons.length) {
    if (!$("exceptions").querySelector(".empty-state")) {const el=node("div",null,"empty-state");el.append(node("strong",summary ? "暂无待处理事项" : "正在读取异常分组"),node("span",summary ? "有需要你处理的问题时，会出现在这里。" : "请等待完整状态返回。"));$("exceptions").replaceChildren(el);}
  } else {
    $("exceptions").querySelector(".empty-state")?.remove();
    for (const row of reasons) {
      let button=existing.get(row.code);
      if (!button) {button=node("button",null,"exception");button.dataset.reason=row.code;const name=node("span");name.append(node("span",null,"reason-title"),node("small",null,"exception-code"));button.append(name,node("span",null,"exception-count"));button.addEventListener("click",()=>filteredTasks("attention",row.code));}
      text(button.querySelector(".reason-title"),row.label);text(button.querySelector(".exception-code"),row.code === "UNKNOWN" ? "具体原因尚未确认" : row.code);text(button.querySelector(".exception-count"),row.count);$("exceptions").append(button);existing.delete(row.code);
    }
    for (const el of existing.values()) el.remove();
  }
  syncOptions($("reason-filter"),reasons.map(row=>[row.code,row.label + "（" + row.count + "）"]),"全部原因");
  const collections=summary?.collections || [];
  syncOptions($("collection-filter"),collections.map(value=>[value,value]),"全部集合");
  $("collection-label").hidden=collections.length < 2 && !$("collection-filter").value;
  renderRuntime(); renderConnection(); controls();
}
function renderRuntime() {
  const models=snapshot.models || [];
  const existing=new Map([...$("models").children].map(el=>[el.dataset.model,el]));
  for (const model of models) {
    let card=existing.get(model.key);
    if (!card) {card=node("article",null,"model");card.dataset.model=model.key;const head=node("div",null,"model-head");head.append(node("h3",model.key),node("span",null,"badge"));card.append(head,node("p",null,"model-name"),node("p",null,"model-connection"),node("p",null,"model-activity"),node("p",null,"model-task"));$("models").append(card);}
    text(card.querySelector(".badge"),stale ? "信息过时" : model.state);text(card.querySelector(".model-name"),model.name);text(card.querySelector(".model-connection"),model.connection);text(card.querySelector(".model-activity"),model.activity || "尚无调用记录");text(card.querySelector(".model-task"),model.task && model.task !== "—" ? "任务：" + model.task : model.role || "");existing.delete(model.key);
  }
  for (const card of existing.values()) card.remove();
  const metrics=snapshot.call_metrics || {}, attempts=metrics.attempts || 0;
  for (const [key,label,value] of [["http","HTTP 成功",metrics.http_success],["valid","有效输出",metrics.valid_outputs],["unknown","未标注请求",metrics.unknown_attempts]]) {
    let card=$("call-" + key);
    if (!card) {card=node("div",null,"call-metric");card.id="call-"+key;card.append(node("span",label),node("strong"),node("p"));$("call-metrics").append(card);}
    text(card.querySelector("strong"),key === "unknown" ? value ?? 0 : attempts ? ((value || 0)*100/attempts).toFixed(1) + "%" : "—");
    text(card.querySelector("p"),key === "unknown" ? "保留未知状态，不计为成功" : attempts ? (value || 0) + " / " + attempts + " 次请求" : "暂无请求");
  }
  const calls=(snapshot.model_calls || []).slice(0,20), signature=JSON.stringify(calls);
  if ($("model-calls").dataset.signature !== signature) {
    $("model-calls").dataset.signature=signature;
    $("model-calls").replaceChildren(...calls.map(call=>{const card=node("article",null,"call-record");card.append(node("h3",call.model + " · " + call.operation),node("p","状态 " + call.status + " · HTTP " + (call.http_status ?? "—") + " · 流完成 " + (call.stream_complete === true ? "是" : call.stream_complete === false ? "否" : "未确认") + " · 输出 " + (call.valid_output === true ? "有效" : call.valid_output === false ? "无效" : "未确认")),node("p","任务 " + (call.job_id || "—") + " · 耗时 " + (call.duration_seconds ?? "—") + " 秒"));return card;}));
    if (!calls.length) $("model-calls").append(node("p","暂无保留的调用记录。","muted"));
  }
  const logs=$("logs"), oldTop=logs.scrollTop, pinned=logs.scrollHeight-logs.clientHeight-oldTop<24;
  text(logs,(snapshot.logs || []).join("\n") || "暂无运行日志。"); logs.scrollTop=pinned ? logs.scrollHeight : oldTop;
}
function renderConnection() {
  text($("connection-status"),stale ? "状态待更新" : "已连接");$("connection-status").classList.toggle("is-stale",stale);
  $("stale-banner").hidden=!stale;text($("stale-banner"),"连接中断或信息已过时。正在保留上次数据，恢复连接前暂停操作。");
}
function renderJobs() {
  const ids=new Set(listItems.map(item=>item.id));
  for (const [id,card] of rowNodes) if (!ids.has(id)) {card.remove();rowNodes.delete(id);}
  $("jobs").querySelector(".empty-state")?.remove();
  for (const job of listItems) {
    let card=rowNodes.get(job.id);
    if (!card) {
      card=node("article",null,"job");card.dataset.jobId=job.id;
      const button=node("button",null,"job-open"), content=node("span",null,"job-text");button.dataset.jobId=job.id;
      content.append(node("span",null,"job-title"),node("span",null,"job-meta"),node("span",null,"job-progress"));button.append(content,node("span",null,"badge"));
      button.addEventListener("click",()=>{returnFocus=button;setRoute(routeHash("tasks",job.id),Boolean(selectedId));});card.append(button);rowNodes.set(job.id,card);$("jobs").append(card);
    }
    updateRow(card,job);
  }
  if (!listItems.length) {const empty=node("div",null,"empty-state");empty.append(node("strong",jobsLoading ? "正在读取任务…" : jobsStale ? "任务暂不可用" : "没有符合条件的任务"),node("span",jobsLoading ? "正在查询完整任务库。" : jobsStale ? "请稍后刷新，当前没有可确认的结果。" : "试试其他关键词，或调整筛选条件。"));$("jobs").append(empty);}
  const pages=Math.max(1,Math.ceil(listTotal/PAGE_SIZE));
  text($("result-count"),listTotal + " 项" + (jobsStale ? " · 状态待更新" : ""));
  const pageText = page > pages && pendingList ? "第 " + page + " 页 · 列表已变化" : page + " / " + pages;
  text($("page-label"),pageText);text($("page-label-bottom"),pageText);
  $("previous").disabled=page<=1 || jobsLoading;$("next").disabled=page>=pages || jobsLoading;
  for (const b of document.querySelectorAll("[data-page]")) b.disabled=b.dataset.page === "previous" ? $("previous").disabled : $("next").disabled;
  $("list-update").hidden=!pendingList;$("jobs").setAttribute("aria-busy",String(jobsLoading));
  fitDetail();
  controls();
}
function updateRow(card,job) {
  card.classList.toggle("selected",job.id===selectedId);card.classList.toggle("open-missing",Boolean(job._missing));
  const button=card.querySelector("button");button.setAttribute("aria-pressed",String(job.id===selectedId));
  text(card.querySelector(".job-title"),job.title);text(card.querySelector(".job-meta"),job._missing ? "任务已不在当前知识库 · 刷新列表以更新" : [job.stage_label,job.model].filter(Boolean).join(" · "));
  text(card.querySelector(".job-progress"),[job.progress ? "阶段进度：" + job.progress : "",job.queue_rank ? "队列第 " + job.queue_rank + " 项" : "",(jobState(job)==="failed" || jobState(job)==="blocked") ? job.blocked_reason : ""].filter(Boolean).join(" · "));
  badge(card.querySelector(".badge"),jobState(job),job.state_label);
}
async function loadJobs(replace = false) {
  if (!active || view!=="tasks" || document.hidden || (jobsLoading && !replace)) return;
  const epoch=generation, key=queryParams().toString(), request=++jobsRequest;
  jobsController?.abort(); const controller=new AbortController(); jobsController=controller;
  const newQuery=listKey!==key;
  if (newQuery) {listItems=[];listTotal=0;rowNodes.clear();$("jobs").replaceChildren();listKey="";pendingList=null;jobsStale=true;}
  jobsLoading=true;renderJobs();
  try {
    const ids=[...new Set([...listItems.map(job=>job.id),...(selectedId ? [selectedId] : [])])];
    const [data,updates]=await Promise.all([
      api("/api/jobs?" + key,undefined,{signal:controller.signal}),
      !replace && ids.length ? api("/api/jobs/updates?ids="+encodeURIComponent(ids.join(",")),undefined,{signal:controller.signal}) : Promise.resolve(null),
    ]);
    if (!active || epoch!==generation || request!==jobsRequest || key!==queryParams().toString()) return;
    jobsStale=false;
    if (replace || newQuery) {
      listItems=data.items;listTotal=data.total;page=data.page;listKey=queryParams().toString();pendingList=null;
      history.replaceState({...history.state,ragConsole:true},"",routeHash());
      const retained=new Set(listItems.map(job=>job.id));for (const [id,el] of rowNodes) if (!retained.has(id)) {el.remove();rowNodes.delete(id);}
      renderJobs();for (const job of listItems) $("jobs").append(rowNodes.get(job.id));
      const selected=listItems.find(job=>job.id===selectedId);if (selected) {selectedJob=selected;selectedStale=false;}
    } else {
      const same=data.page===page && data.items.map(job=>job.id).join()===listItems.map(job=>job.id).join();
      pendingList=same ? null : data;listTotal=data.total;
      const current=new Map((updates?.items || data.items).map(job=>[job.id,job]));
      listItems=listItems.map(job=>current.get(job.id) || {...job,_missing:updates?.missing_ids.includes(job.id)});
      if (selectedId && current.has(selectedId)) {selectedJob=current.get(selectedId);selectedStale=false;}
      else if (selectedId && updates?.missing_ids.includes(selectedId)) {selectedJob=selectedJob ? {...selectedJob,_missing:true} : null;selectedStale=true;}
    }
    text($("jobs-status"),"全库查询 · 更新于 " + date(data.captured_at) + " · 阶段进度不代表总完成百分比。");renderDetail();
  } catch(error) {
    if (error.name!=="AbortError" && active && epoch===generation && request===jobsRequest) {jobsStale=true;selectedStale=true;text($("jobs-status"),error.message + (listItems.length ? " 已保留本次查询的上次结果。" : " 当前没有可确认的结果。"));renderDetail();}
  } finally {if (request===jobsRequest) {jobsLoading=false;if (active) renderJobs();}}
}
async function selectJob(id) {
  const previous=selectedId; selectedId=id;selectedJob=listItems.find(job=>job.id===id) || null;selectedStale=!selectedJob || jobsStale;
  const request=++detailRequest,epoch=generation;
  if (!id) {
    selectedJob=null;$("detail-pane").classList.remove("expanded");text($("detail-expand"),"展开");$("detail-expand").setAttribute("aria-label","展开任务详情");setDetailLayout();
    if (previous) requestAnimationFrame(()=>{const target=returnFocus?.isConnected ? returnFocus : rowNodes.get(previous)?.querySelector("button") || $("search");target.focus({preventScroll:true});});
    for (const job of listItems) updateRow(rowNodes.get(job.id),job);
    return;
  }
  $("detail-scroll").scrollTop=0;renderDetail();setDetailLayout();
  requestAnimationFrame(()=>$("detail-title").focus({preventScroll:true}));
  try {
    const result=await api("/api/jobs/"+encodeURIComponent(id));
    if (!active || generation!==epoch || request!==detailRequest || selectedId!==id) return;
    selectedJob=result.item;selectedStale=false;renderDetail();
  } catch(error) {if (active && generation===epoch && request===detailRequest) {selectedStale=true;selectedJob=selectedJob ? {...selectedJob,_missing:error.status===404} : null;feedback.set(id,{message:error.message,error:true});renderDetail();}}
  for (const job of listItems) updateRow(rowNodes.get(job.id),job);
}
function closeDetail() {
  if (!selectedId) return;
  if (history.state?.ragDetail) {history.back();} else {setRoute(routeHash("tasks",""),true);}
}
function setDetailLayout() {
  const opened=Boolean(active && view==="tasks" && selectedId), modal=opened && matchMedia("(max-width:1023px)").matches;
  $("detail-pane").dataset.open=String(opened);$("detail-content").hidden=!opened;$("detail-empty").hidden=opened;
  $("detail-backdrop").hidden=!modal;document.body.classList.toggle("body-locked",modal);
  $("detail-pane").setAttribute("role",modal ? "dialog" : "complementary");
  if (modal) {$("detail-pane").setAttribute("aria-modal","true");$("detail-pane").setAttribute("aria-labelledby","detail-title");}
  else {$("detail-pane").removeAttribute("aria-modal");$("detail-pane").removeAttribute("aria-labelledby");}
  for (const el of [$("navigation"),$("main-content").querySelector("header"),$("view-tasks").querySelector(".task-toolbar"),$("jobs").parentElement]) el.inert=modal;
  fitDetail();
}
let detailLayoutQueued=false;
function fitDetail() {
  if(detailLayoutQueued)return;detailLayoutQueued=true;
  requestAnimationFrame(()=>{detailLayoutQueued=false;if(!active || view!=="tasks" || !matchMedia("(min-width:1024px)").matches)return;
    const top=$("detail-pane").getBoundingClientRect().top;
    $("detail-pane").style.setProperty("--detail-height",Math.max(280,innerHeight-Math.max(24,top)-24)+"px");
  });
}
function renderDetail() {
  if (!selectedId) {controls();return;}
  const job=selectedJob;
  text($("detail-title"),job?.title || (feedback.has(selectedId) ? "任务暂不可用" : "正在读取任务…"));
  badge($("detail-state"),job ? jobState(job) : "queued",job ? job.state_label : "读取中");
  text($("detail-progress"),job ? [job.stage_label,job.progress ? "当前阶段："+job.progress : ""].filter(Boolean).join(" · ") : "");
  $("detail-error").hidden=!selectedStale && !job?._missing;
  text($("detail-error"),job?._missing ? "任务已不在当前知识库中，请刷新列表核对。" : "任务状态尚未确认，暂不能执行操作。");
  text($("detail-start"),job && ["failed","blocked"].includes(job.state) ? "重试任务" : "恢复任务");
  const fields=job ? [["任务 ID",job.id],["所属集合",job.collection],["优先级",job.priority],["当前模型",job.model],["阶段",job.pipeline_stage || job.stage_label],["阶段状态",job.pipeline_status],["等待原因",job.reason_code === "UNKNOWN" && !["failed","blocked"].includes(jobState(job)) ? "—" : job.blocked_reason],["错误码",job.reason_code && job.reason_code !== "UNKNOWN" ? job.reason_code : job.error_code],["队列顺序",job.queue_rank],["重试时间",job.pipeline_status === "retry_wait" ? date(job.retry_at) : "—"],["尝试次数",job.attempts],["执行者",job.stage_owner],["更新时间",date(job.updated_at)],["创建时间",date(job.created_at)],["来源状态",job.media_expired ? "媒体已过期，重试时重新下载" : "—"]] : [];
  if ($("detail-fields").children.length!==fields.length*2) $("detail-fields").replaceChildren(...fields.flatMap(([key,value])=>[node("dt",key),node("dd",value ?? "—")]));
  else fields.forEach(([key,value],i)=>{text($("detail-fields").children[i*2],key);text($("detail-fields").children[i*2+1],value ?? "—");});
  const result=feedback.get(selectedId);$("detail-feedback").hidden=!result;$("detail-feedback").classList.toggle("inline-error",Boolean(result?.error));if(result)text($("detail-feedback"),result.message);
  controls();
}
async function refresh() {
  if (!active || refreshRunning || document.hidden) return;
  const epoch=generation, request=++refreshRequest;refreshRunning=true;
  try {
    const data=await api("/api/snapshot?include_jobs=false");
    if (!active || generation!==epoch) return;
    if (data.snapshot) snapshot=data.snapshot;stale=data.stale || !snapshot;
    render();renderConnection();
    if (view==="tasks") await loadJobs(false);
  } catch(error) {if(active && generation===epoch){stale=true;renderConnection();controls();}}
  finally {if(request===refreshRequest){refreshRunning=false;if(active && generation===epoch && !document.hidden){clearTimeout(timer);timer=setTimeout(refresh,3000);}}}
}
function changeFilters() {
  page=1;pendingList=null;listKey="";jobsStale=true;selectedStale=true;controls();
  history.replaceState({ragConsole:true},"",routeHash("tasks",""));
  if(selectedId)selectJob("");loadJobs(true);
}
function changePage(direction) {
  page=Math.max(1,page+(direction==="next" ? 1 : -1));
  history.replaceState({ragConsole:true},"",routeHash("tasks",""));
  if(selectedId)selectJob("");loadJobs(true);$("view-tasks").scrollIntoView({block:"start"});
}
function applyTheme(value) {
  if (value==="system") delete document.documentElement.dataset.theme;else document.documentElement.dataset.theme=value;
  $("theme").value=value;try{localStorage.setItem("rag-theme",value);}catch{}
  document.querySelector('meta[name="theme-color"]').content=value==="dark" || (value==="system" && matchMedia("(prefers-color-scheme:dark)").matches) ? "#101722" : "#f5f7fb";
}
$("login-form").addEventListener("submit", async event => {
  event.preventDefault();const button=event.submitter;button.disabled=true;$("login-error").hidden=true;
  try {const data=await api("/api/login",{username:$("username").value,password:$("password").value});$("password").value="";csrf=data.csrf;showDashboard();await refresh();}
  catch(error){text($("login-error"),error.message);$("login-error").hidden=false;$("password").focus();}
  finally{button.disabled=false;}
});
$("logout").addEventListener("click",async()=>{try{await api("/api/logout",{});showLogin();notice("已退出登录。");}catch(error){notice(error.message,true);}});
$("start").addEventListener("click",()=>confirmGlobal("start"));$("stop").addEventListener("click",()=>confirmGlobal("stop"));
$("confirm-cancel").addEventListener("click",()=>$("confirm-dialog").close());
$("confirm-accept").addEventListener("click",()=>{const action=confirmAction;$("confirm-dialog").close();act(action);});
$("detail-start").addEventListener("click",()=>act("start_job",selectedId));$("detail-stop").addEventListener("click",()=>act("stop_job",selectedId));
for(const button of document.querySelectorAll("[data-view]"))button.addEventListener("click",()=>navigate(button.dataset.view));
for(const button of document.querySelectorAll("[data-go]"))button.addEventListener("click",()=>navigate(button.dataset.go));
$("xhs-alert").addEventListener("click",()=>navigate("account"));
$("refresh").addEventListener("click",()=>{if(view==="tasks")loadJobs(true);refresh();document.dispatchEvent(new CustomEvent("console-refresh"));});
$("list-update").addEventListener("click",()=>loadJobs(true));
$("search").addEventListener("input",()=>{clearTimeout(searchTimer);jobsController?.abort();jobsRequest++;jobsLoading=false;jobsStale=true;selectedStale=true;controls();searchTimer=setTimeout(changeFilters,250);});
for(const id of ["state-filter","reason-filter","collection-filter"])$(id).addEventListener("change",()=>{clearTimeout(searchTimer);changeFilters();});
$("previous").addEventListener("click",()=>changePage("previous"));$("next").addEventListener("click",()=>changePage("next"));
for(const b of document.querySelectorAll("[data-page]"))b.addEventListener("click",()=>changePage(b.dataset.page));
$("detail-close").addEventListener("click",closeDetail);$("detail-backdrop").addEventListener("click",closeDetail);
$("detail-expand").addEventListener("click",()=>{const expanded=$("detail-pane").classList.toggle("expanded");text($("detail-expand"),expanded ? "收起" : "展开");$("detail-expand").setAttribute("aria-label",expanded ? "收起任务详情" : "展开任务详情");});
$("detail-pane").addEventListener("keydown",event=>{
  if(event.key==="Escape"){event.preventDefault();closeDetail();return;}
  if(event.key!=="Tab" || !matchMedia("(max-width:1023px)").matches)return;
  const elements=[...$("detail-pane").querySelectorAll("button:not(:disabled),[tabindex='0']")].filter(el=>el.getClientRects().length);
  if(!elements.length)return;const first=elements[0],last=elements[elements.length-1];
  if(event.shiftKey && (document.activeElement===first || document.activeElement===$("detail-title"))){event.preventDefault();last.focus();}
  else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first.focus();}
});
window.addEventListener("popstate",applyRoute);window.addEventListener("hashchange",applyRoute);
window.addEventListener("scroll",fitDetail,{passive:true});window.addEventListener("resize",fitDetail);
matchMedia("(min-width:1024px)").addEventListener("change",setDetailLayout);
$("theme").addEventListener("change",()=>applyTheme($("theme").value));
matchMedia("(prefers-color-scheme:dark)").addEventListener("change",()=>{if($("theme").value==="system")applyTheme("system");});
document.addEventListener("visibilitychange",()=>{clearTimeout(timer);if(!document.hidden && active)refresh();});
document.addEventListener("xhs-state",event=>{
  const state=event.detail,entries=Object.values(state?.paths || {});
  const required=entries.some(path=>["auth_required","verification_required"].includes(path.state));
  const allValid=entries.length>0 && entries.every(path=>path.state==="valid");
  $("xhs-alert").hidden=false;$("xhs-alert").classList.toggle("is-healthy",allValid);
  text($("xhs-alert-text"),required ? "小红书需要登录或验证 · 前往处理" : allValid ? "小红书连接可用 · 查看账号状态" : "小红书连接需要关注 · 查看具体状态");
});
let preferredTheme="system";try{const value=localStorage.getItem("rag-theme");if(["system","light","dark"].includes(value))preferredTheme=value;}catch{}
applyTheme(preferredTheme);
(async()=>{try{const data=await api("/api/session");csrf=data.csrf;showDashboard();await refresh();}catch(error){showLogin();}})();

if (window.visualViewport) {
  const adjustViewport = () => {
    document.documentElement.style.setProperty("--visual-height", window.visualViewport.height + "px");
    document.documentElement.classList.toggle("keyboard-open", window.visualViewport.height < innerHeight - 120 && document.activeElement?.matches("input,textarea"));
  };
  window.visualViewport.addEventListener("resize", adjustViewport);
  document.addEventListener("focusin", adjustViewport); document.addEventListener("focusout", adjustViewport); adjustViewport();
}
