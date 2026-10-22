"use strict";
(() => {
  let state = null, frame = null, working = false, polling = false, frameLoading = false, drawing = false, points = [], urls = [], fresh = false;
  const labels = {valid:"可用",auth_required:"需要登录",verification_required:"需要手机验证",rate_limited:"请求限流",blocked:"访问受限",unknown:"尚未确认原因",network_error:"网络异常",account_mismatch:"账号不符"};
  const paths = {owner:"账号",favorites:"收藏",following:"关注",author:"作者笔记",note:"笔记读取",media:"媒体下载"};
  const messages = {XHS_DEFERRED:"正在等待小红书会话操作，请稍后再检查。",XHS_SESSION_BUSY:"小红书正在处理上一项操作。",XHS_SESSION_UNAVAILABLE:"会话服务暂不可用，原会话仍保留。",XHS_INTERACTION_EXPIRED:"画面已更新，请刷新后再操作。",XHS_ACCOUNT_MISMATCH:"当前登录账号与原账号不符，采集保持暂停。",XHS_SESSION_UNKNOWN:"尚未确认原因，会话已保留。",XHS_VERIFICATION_REQUIRED:"请在手机验证画面中完成平台验证。",XHS_PLATFORM_BLOCKED:"平台限制访问，请等待冷却。"};
  function clearImages(clearCode = true) {
    urls.forEach(url=>URL.revokeObjectURL(url));urls=[];frame=null;
    ["xhs-frame","xhs-qr"].forEach(id=>$(id).removeAttribute("src"));
    $("xhs-qr-link").removeAttribute("href");if(clearCode)$("xhs-otp").value="";
  }
  function picture(encoded,mime) {
    const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));
    const url=URL.createObjectURL(new Blob([bytes],{type:mime}));urls.push(url);return url;
  }
  function report(error) {text($("xhs-error"),messages[error.message] || error.message);$("xhs-error").hidden=false;}
  function render() {
    const login=state?.login,interactive=login && ["waiting","verifying"].includes(login.state);
    $("xhs-interaction").hidden=!interactive;$("xhs-cancel").hidden=!interactive;
    text($("xhs-status"),!fresh ? "会话状态尚未确认，正在重新连接。" : login?.state==="waiting" ? "等待手机操作；完成后会核对账号与读取能力。" : login?.state==="verifying" ? "手机操作已收到，正在核对账号及笔记读取。" : login?.state==="complete" ? "登录与读取验证已完成。" : login?.state==="expired" ? "本次交互已超时，浏览器会话仍保留。" : login?.error_code ? messages[login.error_code] || login.error_code : "会话服务已连接，读取能力见下方状态。");
    const known=new Map([...$("xhs-paths").children].map(el=>[el.dataset.path,el]));
    for(const [key,value] of Object.entries(state?.paths || {})) {
      let row=known.get(key);
      if(!row){row=node("div",null,"source-path");row.dataset.path=key;row.append(node("strong",paths[key] || key),node("span"),node("small"));$("xhs-paths").append(row);}
      text(row.querySelector("span"),labels[value.state] || value.state);
      text(row.querySelector("small"),"最近成功 " + date(value.last_success_at ? value.last_success_at*1000 : null) + (value.retry_at ? " · 下次检查 " + date(value.retry_at*1000) : ""));known.delete(key);
    }
    for(const row of known.values())row.remove();
    const c=state?.counters || {};
    const net=state?.network;
    text($("xhs-network"),!net ? "节点服务尚未配置。" : "当前节点 " + net.node + "/" + net.nodes + " · 本节点已下载 " + net.completed + "/" + net.batch_size + " 条 · 累计 " + net.total_completed + " 条" + (net.paused ? " · 已暂停" : net.cooldown_until ? " · 冷却至 " + date(net.cooldown_until*1000) : "") + " · 最近切换原因：" + ({initial:"首次启动",batch_complete:"本批完成",first_node_recheck:"整轮后复查首节点",pool_exhausted:"全部节点暂不可用",cooldown_recheck:"冷却后重查",XHS_NETWORK_ERROR:"线路故障",XHS_PLATFORM_BLOCKED:"IP 访问受限"}[net.reason] || net.reason));
    const recovery=state?.recovery, budget=state?.budget, ready=["observation","steady"].includes(recovery?.phase);
    $("xhs-network-pause").hidden=!net || net.paused;$("xhs-network-resume").hidden=!net?.paused || !ready;
    $("xhs-acceptance").hidden=recovery?.phase!=="validated";
    text($("xhs-budget"),!budget ? "" : "页面/API 操作 " + budget.operations_used + "/" + budget.operations_per_hour + " 次/小时 · 至少间隔 " + budget.interval_seconds + " 秒 · 笔记尝试 " + budget.notes_used + "/" + budget.notes_per_hour + " 条/小时 · 24 小时 " + budget.notes_used_24h + "/" + budget.notes_per_24h + " 次。附带资源与媒体请求另计。");
    const phase={initial:"等待恢复检查",paused:"已暂停",diagnostic:"路线诊断",validated:"读取通过，等待三条入库验收",acceptance:"三条入库验收",observation:"低速观察（至少 2 小时及额外 6 条入库）",steady:"每小时最多 12 条"};
    text($("xhs-recovery"),"当前路线：" + (state?.route==="direct_diagnostic" ? "本机直连诊断" : "VPN 池") + " · " + (phase[recovery?.phase] || "等待检查") + " · 最近验证 " + date(state?.checked_at ? state.checked_at*1000 : null) + (state?.pause_reason ? " · 暂停原因：" + (messages[state.pause_reason] || state.pause_reason) : "") + " · 本轮完整入库 " + Object.keys(recovery?.accepted || {}).length + " 条");
    text($("xhs-diagnostics"),Object.entries(state?.diagnostics || {}).map(([route,item])=>(route==="direct_diagnostic" ? "直连诊断" : "VPN 诊断") + "：" + ({checking:"检查中",complete:"页面、账号及笔记读取通过",failed:"未通过"}[item.state] || item.state) + (item.error_code ? " · " + (messages[item.error_code] || item.error_code) : "") + (item.platform_code ? "（平台码 " + item.platform_code + "）" : "") + " · " + date(item.checked_at ? item.checked_at*1000 : null)).join("；"));
    text($("xhs-counters"),"笔记读取成功 " + (c.note_successes || 0) + " 次 · 登录完成 " + (c.login_completed || 0) + " 次。HTTP 请求分项：页面 " + (c.browser_page_requests || 0) + " · 页面附带资源 " + (c.browser_resource_requests || 0) + " · 显式 API " + (c.api_http_requests || 0) + " · 媒体 " + (c.media_http_requests || 0) + "。");
    ["xhs-login","xhs-check","xhs-cancel","xhs-frame-refresh","xhs-network-pause","xhs-network-resume","xhs-acceptance"].forEach(id=>$(id).disabled=working || !fresh);
    $("xhs-otp-form").querySelector("button").disabled=working || !fresh || !frame;
    if(!interactive)clearImages();
    document.dispatchEvent(new CustomEvent("xhs-state",{detail:fresh ? state : null}));
  }
  async function getFrame() {
    if(!active || document.hidden || view!=="account" || !state?.login || working || drawing || state.login.state!=="waiting" || frameLoading)return;
    const epoch=generation,attempt=state.login.id;frameLoading=true;
    try {
    const result=await api("/api/xhs/frame?attempt_id="+encodeURIComponent(attempt));
    if(!active || generation!==epoch || drawing || result.attempt_id!==state?.login?.id)return;
    clearImages(false);frame=result;
    text($("xhs-frame-time"),"截图捕获于 " + date(result.captured_at ? result.captured_at*1000 : null) + "。截图刷新不等于重新验证。");
    $("xhs-frame").width=result.width || 1280;$("xhs-frame").height=result.height || 900;
    $("xhs-frame").src=picture(result.image,"image/jpeg");$("xhs-qr").hidden=!result.qr;
    if(result.qr)$("xhs-qr").src=picture(result.qr,"image/png");
    $("xhs-qr-link").hidden=!result.qr_link;
    if(result.qr_link)$("xhs-qr-link").href=result.qr_link;
    $("xhs-otp-form").querySelector("button").disabled=working || !fresh;
    } finally {if(generation===epoch)frameLoading=false;}
  }
  async function refreshXhs() {
    if(!active){clearImages();state=null;fresh=false;return;}
    if(document.hidden || polling || working || drawing)return;
    const epoch=generation;polling=true;
    try{const next=await api("/api/xhs/session");if(!active || generation!==epoch)return;state=next;fresh=true;render();await getFrame();}
    catch(error){if(active && generation===epoch){fresh=false;render();report(error);}}
    finally{if(generation===epoch)polling=false;}
  }
  async function action(name,extra={}) {
    if(!active || working || !fresh)return;
    const epoch=generation;working=true;$("xhs-error").hidden=true;render();
    try{await api("/api/xhs/action",{action:name,request_id:crypto.randomUUID(),...extra});}
    catch(error){if(active && generation===epoch)report(error);}
    finally{if(generation===epoch)working=false;if(active && generation===epoch){render();setTimeout(refreshXhs,1200);}}
  }
  $("xhs-login").onclick=()=>action("login_start");$("xhs-check").onclick=()=>action("recover");
  $("xhs-network-pause").onclick=()=>action("network_pause");$("xhs-network-resume").onclick=()=>action("network_resume");
  $("xhs-acceptance").onclick=()=>action("acceptance_start");
  $("xhs-cancel").onclick=()=>state?.login && action("login_cancel",{attempt_id:state.login.id});
  $("xhs-frame-refresh").onclick=()=>getFrame().catch(report);
  $("xhs-frame-expand").onclick=()=>{const enlarged=$("xhs-frame-container").classList.toggle("enlarged");$("xhs-frame-expand").setAttribute("aria-pressed",String(enlarged));text($("xhs-frame-expand"),enlarged ? "还原验证画面" : "放大验证画面");};
  $("xhs-otp-form").onsubmit=event=>{
    event.preventDefault();if(!frame)return;
    const code=$("xhs-otp").value;$("xhs-otp").value="";
    action("otp",{attempt_id:frame.attempt_id,frame_id:frame.frame_id,code});
  };
  const canvas=$("xhs-frame");
  function point(event){const r=canvas.getBoundingClientRect(),w=frame?.width || canvas.naturalWidth || 1280,h=frame?.height || canvas.naturalHeight || 900;return [Math.min(w-1,Math.max(0,Math.round((event.clientX-r.left)*w/r.width))),Math.min(h-1,Math.max(0,Math.round((event.clientY-r.top)*h/r.height)))];}
  canvas.onpointerdown=event=>{if(!frame || working || polling || frameLoading || !fresh || !canvas.complete || !canvas.naturalWidth)return;event.preventDefault();drawing=true;points=[point(event)];canvas.setPointerCapture(event.pointerId);};
  canvas.onpointermove=event=>{if(drawing && points.length<79)points.push(point(event));};
  canvas.onpointerup=event=>{if(!drawing)return;drawing=false;points.push(point(event));action("pointer",{attempt_id:frame.attempt_id,frame_id:frame.frame_id,points});};
  canvas.onpointercancel=()=>{drawing=false;points=[];};
  document.addEventListener("console-auth",event=>{if(!event.detail){drawing=false;polling=false;frameLoading=false;working=false;clearImages();state=null;fresh=false;$("xhs-paths").replaceChildren();$("xhs-error").hidden=true;}else refreshXhs();});
  document.addEventListener("console-view",()=>{if(view==="account")refreshXhs();});
  document.addEventListener("console-refresh",refreshXhs);
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)refreshXhs();});
  setInterval(refreshXhs,5000);refreshXhs();
})();
