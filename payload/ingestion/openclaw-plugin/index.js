import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseEnv, promisify } from "node:util";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { isStatusIntent } from "./status-intent.js";

const execFileAsync = promisify(execFile);
const URL_RE = /https?:\/\/[^\s<>\]\["']+/i;
const MEDIA_RE = /^(video|audio)\//i;
const MEDIA_EXT_RE = /\.(?:mp4|m4v|mov|mkv|webm|avi|mpeg|mpg|mp3|m4a|wav|ogg|opus|flac|aac)$/i;
const UUID_RE = /^[0-9a-f-]{36}$/i;
const START_REQUEST_TTL_MS = 10 * 60 * 1000;
const PLATFORM_NAMES = {
  youtube: "YouTube",
  bilibili: "Bilibili",
  xiaohongshu: "小红书",
};
const DESTINATIONS = [
  { id: "thought-politics", code: "tp", name: "思想、政治与社会议题" },
  { id: "tech", code: "te", name: "技术" },
  { id: "finance", code: "fi", name: "金融与投资" },
  { id: "career", code: "ca", name: "职业发展" },
  { id: "social-conduct", code: "sc", name: "中国人情世故" },
  { id: "literature-culture", code: "lc", name: "文学与文化" },
  { id: "general", code: "ge", name: "综合资料" },
  { id: "cooking", code: "co", name: "烹饪" },
];
const LEGACY_DESTINATIONS = [
  { id: "main", code: "main", name: "旧主知识库" },
  { id: "cooking", code: "cooking", name: "烹饪" },
];
let pluginVersion = "unknown";
try {
  pluginVersion = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8")).version || "unknown";
} catch {
  // Version is informational; capability lookup must remain available.
}

function jsonOutput(result) {
  return JSON.parse(String(result.stdout || "{}"));
}

function enqueueReply(result) {
  const payload = jsonOutput(result);
  if ((payload.discoveries || []).length > 0) {
    return "🔎 已收到作者主页，正在读取有限作品列表；确认前不会下载或入库。";
  }
  if (payload.status === "author_discovery_disabled") {
    const platform = PLATFORM_NAMES[payload.platform] || payload.platform || "该平台";
    return `已识别为 ${platform} 作者主页，但作者批量功能目前未启用；单个作品链接仍可继续录入。`;
  }
  if (payload.status === "unsupported_collection") {
    return "暂不支持合集、收藏夹或动态页；请发送单个作品链接或受支持的作者主页。";
  }
  if (payload.status === "short_link_resolution_failed") {
    if (payload.platform === "bilibili") {
      return "已识别为 Bilibili 分享短链接，但暂时无法安全展开；请重新复制分享链接，或发送完整作者主页地址。";
    }
    return "已识别为小红书分享短链接，但暂时无法安全展开；请重新复制分享链接，或发送完整作者主页地址。";
  }
  if (payload.status === "xiaohongshu_share_context_failed") {
    return "小红书作品链接已识别，但平台没有返回可用的作品信息。请确认登录状态；若已登录，请从小红书“分享→复制链接”重新发送。";
  }
  if (payload.status === "xiaohongshu_auth_required") {
    return payload.login_triggered
      ? "🔐 小红书登录已失效，登录二维码正在发送。扫码并确认后，请点击“重新开始”。"
      : "🔐 小红书登录已失效，且登录服务暂时无法启动。请使用 /video_login 手动发起登录。";
  }
  if (!payload.ok) {
    return "没有识别到可录入的视频、音频或受支持作者主页。";
  }
  return "📥 已收到，任务已进入处理队列。";
}

function safeErrorCode(error) {
  return String(error?.code || error?.name || "UNKNOWN").replace(/[^A-Za-z0-9_.-]/g, "").slice(0, 80);
}

function presentationButton(label, value, style) {
  return { label, value, ...(style ? { style } : {}) };
}

function destinationFromCallback(value) {
  return DESTINATIONS.find((destination) => (
    destination.code === value || destination.id === value
  )) || LEGACY_DESTINATIONS.find((destination) => destination.code === value);
}

function destinationButtonRows(prefix, suffix) {
  const buttons = DESTINATIONS.map((destination) => ({
    text: destination.name,
    callback_data: `${prefix}:${destination.code}:${suffix}`,
  }));
  const rows = [];
  for (let index = 0; index < buttons.length; index += 2) {
    rows.push(buttons.slice(index, index + 2));
  }
  return rows;
}

function batchSelection(parts, offset) {
  const isAllPreview = parts[offset] === "all";
  const rawLimit = parts[offset + (isAllPreview ? 1 : 0)];
  const limit = Number(rawLimit);
  if (!Number.isSafeInteger(limit) || limit < 1) {
    throw new Error("invalid batch selection");
  }
  return {
    kind: isAllPreview ? "all_preview" : "latest",
    limit,
    token: isAllPreview ? `all:${limit}` : String(limit),
    label: isAllPreview
      ? `下载本次预览中的全部 ${limit} 个视频`
      : `最近 ${limit} 个可下载视频`,
  };
}

function batchStartError(result) {
  if (result.status === "active_batch_exists") {
    return "已有一个批次正在处理；请等待完成或先取消当前批次。";
  }
  if (result.status === "duration_budget_exceeded") {
    return "所选范围超过服务器配置的总时长预算，请缩小范围或关闭该可选保护。";
  }
  if (result.status === "insufficient_resources") {
    return "服务器当前可用磁盘空间不足，请清理后重试。";
  }
  if (result.status === "platform_paused") {
    return "平台当前因登录、限流或风控暂停，请稍后重试。";
  }
  if (result.status === "execution_disabled") {
    return "该平台的作者批次执行功能尚未启用。";
  }
  if (result.status === "forbidden") return "无权操作此批次。";
  if (result.status === "not_found" || result.status === "not_ready") {
    return "本次预览已过期或状态已变化，请重新发送作者主页。";
  }
  return `无法创建批次：${result.status || "未知错误"}`;
}

function singleDestinationFallback(job) {
  const buttons = DESTINATIONS.map((destination) => (
    presentationButton(destination.name, `vki:${destination.code}:${job.id}`)
  ));
  const rows = [];
  for (let index = 0; index < buttons.length; index += 2) {
    rows.push({ type: "buttons", buttons: buttons.slice(index, index + 2) });
  }
  rows.push({
    type: "buttons",
    buttons: [presentationButton("取消", `vki:cancel:${job.id}`, "danger")],
  });
  return {
    text: "已识别为单个作品链接，请选择目标知识库。选择后才会开始处理。",
    presentation: { blocks: rows },
  };
}

function startConfirmationFallback(requestId) {
  return {
    text: "📥 已收到。点击“开始”后才会读取链接、字幕、下载、转录或创建任务。",
    presentation: { blocks: [{ type: "buttons", buttons: [
      presentationButton("开始", `vks:start:${requestId}`, "success"),
      presentationButton("取消", `vks:cancel:${requestId}`, "danger"),
    ] }] },
  };
}

const WORK_CONTROL_LABELS = {
  status: "查看进度",
  pause: "暂停",
  resume: "恢复",
  cancel: "取消",
};

function workCallback(action, kind, objectId) {
  const actionCode = {
    status: "s",
    pause: "p",
    resume: "r",
    cancel: "c",
    cancel_confirm: "cc",
  }[action];
  const kindCode = { job: "j", discovery: "d", batch: "b" }[kind];
  return `vkw:${actionCode}:${kindCode}:${objectId}`;
}

function workButtonRows(result) {
  const controls = Array.isArray(result.controls) ? result.controls : [];
  return [controls.map((action) => ({
    text: WORK_CONTROL_LABELS[action] || action,
    callback_data: workCallback(action, result.kind, result.object_id),
    ...(action === "cancel" ? { style: "danger" } : {}),
  }))].filter((row) => row.length > 0);
}

function workReply(result) {
  const controls = Array.isArray(result.controls) ? result.controls : [];
  const buttons = controls.map((action) => presentationButton(
    WORK_CONTROL_LABELS[action] || action,
    workCallback(action, result.kind, result.object_id),
    action === "cancel" ? "danger" : undefined,
  ));
  return {
    text: result.text || "当前没有视频或作者任务。",
    ...(buttons.length > 0 ? {
      presentation: { blocks: [{ type: "buttons", buttons }] },
    } : {}),
  };
}

function discoveryFallback(discoveryId) {
  return {
    text: "🔎 已收到作者主页，正在读取作品列表；确认范围前不会下载或入库。",
    presentation: { blocks: [{ type: "buttons", buttons: [
      presentationButton("查看进度", workCallback("status", "discovery", discoveryId)),
      presentationButton("暂停", workCallback("pause", "discovery", discoveryId)),
      presentationButton("取消", workCallback("cancel", "discovery", discoveryId), "danger"),
    ] }] },
  };
}

function asStrings(value) {
  return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
}

function allowed(senderId, owner, env) {
  const ids = new Set((env.TELEGRAM_ALLOWED_USER_IDS || "").split(",").map((v) => v.trim()).filter(Boolean));
  return Boolean(owner) || (senderId && ids.has(String(senderId)));
}

function loadProjectEnv(projectDir) {
  try {
    return parseEnv(readFileSync(`${projectDir}/.env`, "utf8"));
  } catch {
    return {};
  }
}

function telegramChatId(value) {
  const raw = String(value || "");
  const match = raw.match(/(?:^|:)(-?\d+)$/);
  return match ? match[1] : raw;
}

function inboundRequest(event, ctx) {
  const metadata = event.metadata || {};
  const paths = asStrings(metadata.mediaPaths);
  if (paths.length === 0 && typeof metadata.mediaPath === "string") paths.push(metadata.mediaPath);
  const types = asStrings(metadata.mediaTypes);
  if (types.length === 0 && typeof metadata.mediaType === "string") types.push(metadata.mediaType);
  const content = String(event.body || event.content || "");
  const hasSupportedMedia = paths.some((path, index) => MEDIA_RE.test(types[index] || "") || MEDIA_EXT_RE.test(path))
    || (paths.length > 0 && /video|audio|voice/i.test(String(metadata.mediaType || content)));
  const hasUrl = URL_RE.test(content);
  if (!hasSupportedMedia && !hasUrl) return null;

  const senderId = event.senderId || ctx.senderId;
  const conversationId = event.conversationId || ctx.conversationId;
  if (!senderId || !conversationId) return null;
  const args = ["enqueue", "--user-id", String(senderId), "--chat-id", telegramChatId(conversationId), "--message-id", String(event.messageId || ctx.messageId || "0"), "--text", content];
  if (hasSupportedMedia) args.push("--media-path", paths[0], "--media-type", types[0] || String(metadata.mediaType || "application/octet-stream"));
  const caption = String(event.body || event.content || "").trim();
  if (caption) args.push("--caption", caption.slice(0, 1000));
  return args;
}

export default definePluginEntry({
  id: "video-knowledge-ingest",
  name: "Video Knowledge Ingest",
  description: "Deterministic Telegram video ingestion bridge",
  register(api) {
    const cfg = api.pluginConfig || {};
    const cwd = cfg.projectDir || fileURLToPath(new URL("..", import.meta.url));
    const python = cfg.python || process.env.RAG_FAVORITE_PYTHON || join(cwd, ".venv", "bin", "python");
    const childEnv = { ...process.env, ...loadProjectEnv(cwd) };
    const pendingBySession = new Map();
    const pendingStartRequests = new Map();
    const run = async (args) => execFileAsync(python, ["-m", "backend.ingestion.cli", ...args], {
      cwd,
      env: childEnv,
      timeout: 10000,
      maxBuffer: 1024 * 1024,
    });
    const runJsonAware = async (args) => {
      try {
        return await run(args);
      } catch (error) {
        if (error?.stdout) {
          try {
            JSON.parse(String(error.stdout));
            return { stdout: String(error.stdout) };
          } catch {
            // Fall through to the safe operational error.
          }
        }
        throw error;
      }
    };
    const enqueueAndNotify = async (args, senderId, conversationId) => {
      const result = await runJsonAware(args);
      const replyText = enqueueReply(result);
      const payload = jsonOutput(result);
      const destinationJobs = Array.isArray(payload.destination_jobs)
        ? payload.destination_jobs
        : [];
      const discoveryIds = Array.isArray(payload.discoveries)
        ? payload.discoveries
        : [];
      const fallbackReply = destinationJobs.length > 0
        ? singleDestinationFallback(destinationJobs[0])
        : discoveryIds.length > 0
          ? discoveryFallback(discoveryIds[0])
          : { text: replyText };
      try {
        let promptsDelivered = true;
        for (const job of destinationJobs) {
          const prompt = jsonOutput(await run([
            "destination-prompt",
            "--user-id", String(senderId),
            "--chat-id", telegramChatId(conversationId),
            "--job-id", String(job.id),
            "--platform", String(job.platform || "other"),
          ]));
          promptsDelivered = (
            promptsDelivered && prompt.ok === true
          );
        }
        for (const discoveryId of discoveryIds) {
          const prompt = jsonOutput(await run([
            "discovery-prompt",
            "--user-id", String(senderId),
            "--chat-id", telegramChatId(conversationId),
            "--discovery-id", String(discoveryId),
          ]));
          promptsDelivered = promptsDelivered && prompt.ok === true;
        }
        if (destinationJobs.length > 0 || discoveryIds.length > 0) {
          return {
            result,
            replyText,
            fallbackReply,
            notificationDelivered: promptsDelivered,
          };
        }
        const notification = jsonOutput(await run([
          "telegram-notify",
          "--user-id", String(senderId),
          "--chat-id", telegramChatId(conversationId),
          "--text", replyText,
        ]));
        return {
          result,
          replyText,
          fallbackReply,
          notificationDelivered: notification.ok === true,
        };
      } catch (error) {
        api.logger.error?.(`video ingest acknowledgement failed code=${safeErrorCode(error)}`);
        return {
          result,
          replyText,
          fallbackReply,
          notificationDelivered: false,
        };
      }
    };
    const stageStartAndNotify = async (args, senderId, conversationId) => {
      const requestId = randomUUID();
      const fallbackReply = startConfirmationFallback(requestId);
      const expiresAt = Date.now() + START_REQUEST_TTL_MS;
      pendingStartRequests.set(requestId, {
        args,
        senderId: String(senderId),
        conversationId,
        expiresAt,
      });
      setTimeout(() => {
        const pending = pendingStartRequests.get(requestId);
        if (pending?.expiresAt === expiresAt) {
          pendingStartRequests.delete(requestId);
        }
      }, START_REQUEST_TTL_MS).unref?.();
      try {
        const notification = jsonOutput(await run([
          "start-prompt",
          "--user-id", String(senderId),
          "--chat-id", telegramChatId(conversationId),
          "--request-id", requestId,
        ]));
        return {
          fallbackReply,
          notificationDelivered: notification.ok === true,
        };
      } catch (error) {
        api.logger.error?.(`video start prompt failed code=${safeErrorCode(error)}`);
        return { fallbackReply, notificationDelivered: false };
      }
    };

    api.registerTool({
      name: "video_ingestion_capabilities",
      label: "Video ingestion capabilities",
      description: "Read the live, side-effect-free platform support, author-batch feature flags, limits, and commands for the installed video ingestion plugin. Use this before answering whether YouTube, Bilibili, or Xiaohongshu ingestion is installed, enabled, experimental, or unsupported.",
      promptSnippet: "Check live video ingestion platform support and limits.",
      promptGuidelines: [
        "For questions about supported video platforms or author batch availability, call video_ingestion_capabilities instead of inferring installation from sandbox file visibility.",
        "Distinguish single-work support from author-batch discovery and execution flags.",
      ],
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {},
      },
      async execute() {
        try {
          const capabilities = jsonOutput(await run(["capabilities"]));
          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                plugin: { loaded: true, version: pluginVersion },
                ...capabilities,
              }),
            }],
          };
        } catch (error) {
          api.logger.error?.(`video capability lookup failed code=${safeErrorCode(error)}`);
          return {
            content: [{
              type: "text",
              text: JSON.stringify({
                plugin: { loaded: true, version: pluginVersion },
                runtime_status: "backend_unavailable",
              }),
            }],
          };
        }
      },
    });

    api.on("inbound_claim", async (event, ctx) => {
      if (ctx.channelId !== "telegram" || event.isGroup) return;
      if (!allowed(event.senderId, event.senderIsOwner, childEnv)) return;
      const args = inboundRequest(event, ctx);
      if (!args) return;
      try {
        const outcome = await stageStartAndNotify(
          args,
          event.senderId || ctx.senderId,
          event.conversationId || ctx.conversationId,
        );
        if (outcome.notificationDelivered) return { handled: true };
        return { handled: true, reply: outcome.fallbackReply };
      } catch (error) {
        api.logger.error?.(`video ingest enqueue failed code=${safeErrorCode(error)}`);
        return { handled: true, reply: { text: "处理失败：任务队列暂时不可用" } };
      }
    }, { priority: 100, timeoutMs: 12000 });

    // OpenClaw 2026.7.1 only dispatches inbound_claim to conversations already
    // owned by a plugin. Keep that hook for forward compatibility and use this
    // pair for ordinary Telegram DMs: enqueue on observation, then short-circuit
    // before the model is invoked.
    api.on("message_received", (event, ctx) => {
      if (ctx.channelId !== "telegram" || !allowed(event.senderId || ctx.senderId, false, childEnv)) return;
      const args = inboundRequest(event, ctx);
      if (!args) return;
      const key = ctx.sessionKey || `telegram:${ctx.conversationId || event.from}`;
      const operation = stageStartAndNotify(
        args,
        event.senderId || ctx.senderId,
        event.conversationId || ctx.conversationId || event.from,
      );
      pendingBySession.set(key, operation);
      setTimeout(() => {
        if (pendingBySession.get(key) === operation) pendingBySession.delete(key);
      }, 30000).unref?.();
      return operation.then(
        () => undefined,
        (error) => api.logger.error?.(`video ingest enqueue failed code=${safeErrorCode(error)}`),
      );
    });

    api.on("before_agent_reply", async (event, ctx) => {
      if (ctx.messageProvider !== "telegram" || ctx.trigger !== "user") return;
      const key = ctx.sessionKey || `telegram:${ctx.channelId || ctx.chatId}`;
      const operation = pendingBySession.get(key);
      if (operation) {
        try {
          const outcome = await operation;
          if (outcome.notificationDelivered) {
            return { handled: true, reason: "video-ingestion-awaiting-start" };
          }
          return { handled: true, reply: outcome.fallbackReply, reason: "video-ingestion-start-prompt" };
        } catch {
          return { handled: true, reply: { text: "处理失败：任务队列暂时不可用" }, reason: "video-ingestion-enqueue-failed" };
        } finally {
          if (pendingBySession.get(key) === operation) pendingBySession.delete(key);
        }
      }
      if (!ctx.senderId || !allowed(ctx.senderId, false, childEnv) || !isStatusIntent(event.cleanedBody)) return;
      try {
        const result = jsonOutput(await run([
          "work-status", "--user-id", String(ctx.senderId),
        ]));
        return {
          handled: true,
          reply: workReply(result),
          reason: "video-work-status",
        };
      } catch (error) {
        api.logger.error?.(`video ingest status failed code=${safeErrorCode(error)}`);
        return { handled: true, reply: { text: "暂时无法查询录入状态。" }, reason: "video-ingestion-status-failed" };
      }
    }, { priority: 100, timeoutMs: 12000 });

    api.registerCommand({
      name: "video_status",
      description: "查看当前单视频、作者列表或作者批次的确定性状态。",
      channels: ["telegram"],
      requireAuth: true,
      handler: async (ctx) => {
        if (!ctx.senderId) return { text: "无法识别当前用户。" };
        try {
          const result = jsonOutput(await run([
            "work-status", "--user-id", String(ctx.senderId),
          ]));
          return workReply(result);
        } catch (error) {
          api.logger.error?.(`video ingest status failed code=${safeErrorCode(error)}`);
          return { text: "暂时无法查询录入状态。" };
        }
      },
    });

    const workCommand = (name, description, action) => api.registerCommand({
      name,
      description,
      channels: ["telegram"],
      requireAuth: true,
      handler: async (ctx) => {
        if (!ctx.senderId) return { text: "无法识别当前用户。" };
        try {
          if (action === "cancel") {
            const current = jsonOutput(await run([
              "work-status", "--user-id", String(ctx.senderId),
            ]));
            if (!current.ok || !current.controls?.includes("cancel")) {
              return { text: "当前没有可取消的视频或作者任务。" };
            }
            return {
              text: "确认取消当前任务？已完成并入库的内容会保留。",
              presentation: { blocks: [{ type: "buttons", buttons: [
                presentationButton(
                  "确认取消",
                  workCallback(
                    "cancel_confirm", current.kind, current.object_id,
                  ),
                  "danger",
                ),
                presentationButton(
                  "返回",
                  workCallback("status", current.kind, current.object_id),
                ),
              ] }] },
            };
          }
          const result = jsonOutput(await run([
            `work-${action}`, "--user-id", String(ctx.senderId),
          ]));
          return workReply(result);
        } catch (error) {
          api.logger.error?.(`video work command failed code=${safeErrorCode(error)}`);
          return { text: "暂时无法处理视频或作者任务命令。" };
        }
      },
    });
    workCommand("video_pause", "暂停当前单视频、作者列表或作者批次。", "pause");
    workCommand("video_resume", "恢复用户暂停的当前任务。", "resume");
    workCommand("video_cancel", "取消当前任务（二次确认）。", "cancel");

    const batchCommand = (name, description, action) => api.registerCommand({
      name,
      description,
      channels: ["telegram"],
      requireAuth: true,
      handler: async (ctx) => {
        if (!ctx.senderId) return { text: "无法识别当前用户。" };
        try {
          if (action === "cancel_prompt") {
            const result = jsonOutput(await run(["batch-status", "--user-id", String(ctx.senderId)]));
            if (!result.ok) return { text: "当前没有可取消的作者批次。" };
            return {
              text: "确认取消该批次？已经完成的文档会保留。",
              presentation: { blocks: [{ type: "buttons", buttons: [
                presentationButton("确认取消", `vkb:cancel_confirm:${result.batch_id}`, "danger"),
                presentationButton("返回", `vkb:status:${result.batch_id}`),
              ] }] },
            };
          }
          const command = action ? `batch-${action}` : "batch-status";
          const result = jsonOutput(await run([command, "--user-id", String(ctx.senderId)]));
          return { text: result.text || (result.ok ? "操作成功。" : "当前没有可操作的作者批次。") };
        } catch (error) {
          api.logger.error?.(`video batch command failed code=${safeErrorCode(error)}`);
          return { text: "暂时无法处理作者批次命令。" };
        }
      },
    });
    batchCommand("video_batch_status", "查看最近的作者视频批次。", null);
    batchCommand("video_batch_pause", "暂停当前作者视频批次。", "pause");
    batchCommand("video_batch_resume", "恢复用户暂停的作者视频批次。", "resume");
    batchCommand("video_batch_cancel", "取消当前作者视频批次（二次确认）。", "cancel_prompt");

    api.registerCommand({
      name: "video_login",
      description: "通过二维码更新小红书或哔哩哔哩登录 Cookie。",
      channels: ["telegram"],
      requireAuth: true,
      handler: async (ctx) => {
        if (!ctx.senderId) return { text: "无法识别当前用户。" };
        return {
          text: "请选择需要扫码登录的平台：",
          presentation: { blocks: [{ type: "buttons", buttons: [
            presentationButton("小红书", "vka:login:xhs"),
            presentationButton("哔哩哔哩", "vka:login:bili"),
          ] }] },
        };
      },
    });

    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: "vka",
      handler: async (ctx) => {
        if (!ctx.auth.isAuthorizedSender || !ctx.senderId) {
          await ctx.respond.reply({ text: "无权发起登录。" });
          return { handled: true };
        }
        const [action, platformCode] = ctx.callback.payload.split(":", 2);
        const platform = { xhs: "xiaohongshu", bili: "bilibili" }[platformCode];
        if (action !== "login" || !platform) {
          await ctx.respond.reply({ text: "登录请求已过期。" });
          return { handled: true };
        }
        try {
          const result = jsonOutput(await runJsonAware([
            "session-login", "--platform", platform,
          ]));
          if (!result.ok) {
            await ctx.respond.reply({
              text: "扫码登录服务尚未启用或暂时不可用。",
            });
            return { handled: true };
          }
          await ctx.respond.editMessage({
            text: `🔐 已启动${PLATFORM_NAMES[platform]}扫码登录，二维码会单独发送到当前私聊。`,
            buttons: [],
          });
        } catch (error) {
          api.logger.error?.(`video session login failed code=${safeErrorCode(error)}`);
          await ctx.respond.reply({ text: "暂时无法启动扫码登录。" });
        }
        return { handled: true };
      },
    });

    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: "vks",
      handler: async (ctx) => {
        if (!ctx.auth.isAuthorizedSender || !ctx.senderId) {
          await ctx.respond.reply({ text: "无权操作此请求。" });
          return { handled: true };
        }
        const [action, requestId] = ctx.callback.payload.split(":", 2);
        const pending = pendingStartRequests.get(requestId);
        if (
          !["start", "cancel"].includes(action)
          || !UUID_RE.test(requestId || "")
          || !pending
          || pending.expiresAt <= Date.now()
        ) {
          pendingStartRequests.delete(requestId);
          await ctx.respond.reply({ text: "开始请求已过期，请重新发送链接或媒体。" });
          return { handled: true };
        }
        if (pending.senderId !== String(ctx.senderId)) {
          await ctx.respond.reply({ text: "无权操作此请求。" });
          return { handled: true };
        }
        pendingStartRequests.delete(requestId);
        if (action === "cancel") {
          await ctx.respond.editMessage({
            text: "已取消；没有读取链接、下载媒体或创建任务。",
            buttons: [],
          });
          return { handled: true };
        }
        await ctx.respond.editMessage({
          text: "▶️ 已开始，正在识别内容并准备下一步。",
          buttons: [],
        });
        try {
          const outcome = await enqueueAndNotify(
            pending.args,
            pending.senderId,
            pending.conversationId,
          );
          const payload = jsonOutput(outcome.result);
          if (payload.status === "xiaohongshu_auth_required") {
            const expiresAt = Date.now() + START_REQUEST_TTL_MS;
            pendingStartRequests.set(requestId, { ...pending, expiresAt });
            setTimeout(() => {
              const retry = pendingStartRequests.get(requestId);
              if (retry?.expiresAt === expiresAt) {
                pendingStartRequests.delete(requestId);
              }
            }, START_REQUEST_TTL_MS).unref?.();
            await ctx.respond.reply({
              text: "扫码并确认登录后，点击“重新开始”；仍不会在点击前创建任务。",
              buttons: [[{
                text: "重新开始",
                callback_data: `vks:start:${requestId}`,
                style: "success",
              }, {
                text: "取消",
                callback_data: `vks:cancel:${requestId}`,
              }]],
            });
          }
          if (!outcome.notificationDelivered) {
            await ctx.respond.reply(outcome.fallbackReply);
          }
        } catch (error) {
          api.logger.error?.(`video ingest start failed code=${safeErrorCode(error)}`);
          await ctx.respond.reply({ text: "处理失败：任务队列暂时不可用" });
        }
        return { handled: true };
      },
    });

    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: "vki",
      handler: async (ctx) => {
        if (!ctx.auth.isAuthorizedSender || !ctx.senderId) {
          await ctx.respond.reply({ text: "无权操作此任务。" });
          return { handled: true };
        }
        const [destinationCode, jobId] = ctx.callback.payload.split(":", 2);
        const destination = destinationFromCallback(destinationCode);
        if (
          !/^[0-9a-f-]{36}$/i.test(jobId || "")
          || (!destination && destinationCode !== "cancel")
        ) {
          await ctx.respond.reply({ text: "任务已过期。" });
          return { handled: true };
        }
        let result;
        try {
          const { stdout } = await run([
            "choose", "--job-id", jobId,
            "--user-id", String(ctx.senderId),
            "--destination", destination?.id || "cancel",
          ]);
          result = JSON.parse(stdout);
        } catch (error) {
          api.logger.error?.(`video ingest callback failed code=${safeErrorCode(error)}`);
          await ctx.respond.reply({ text: "操作失败，请稍后重试。" });
          return { handled: true };
        }
        if (result.status === "accepted") {
          await ctx.respond.clearButtons();
          await ctx.respond.reply({
            text: `📚 已选择${destination.name}，开始处理视频；完成后会写入并构建向量索引。`,
            buttons: [[
              {
                text: "查看进度",
                callback_data: workCallback("status", "job", jobId),
              },
              {
                text: "暂停",
                callback_data: workCallback("pause", "job", jobId),
              },
              {
                text: "取消",
                callback_data: workCallback("cancel", "job", jobId),
                style: "danger",
              },
            ]],
          });
        }
        else if (result.status === "cancelled") await ctx.respond.editMessage({ text: "已取消。" });
        else if (result.status === "forbidden") await ctx.respond.reply({ text: "无权操作此任务。" });
        else if (result.status === "finished") await ctx.respond.reply({ text: "任务已处理。" });
        else await ctx.respond.reply({ text: "任务已过期。" });
        return { handled: true };
      },
    });

    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: "vkw",
      handler: async (ctx) => {
        if (!ctx.auth.isAuthorizedSender || !ctx.senderId) {
          await ctx.respond.reply({ text: "无权操作此任务。" });
          return { handled: true };
        }
        const [actionCode, kindCode, objectId] = (
          ctx.callback.payload.split(":", 3)
        );
        const action = {
          s: "status",
          p: "pause",
          r: "resume",
          c: "cancel",
          cc: "cancel_confirm",
        }[actionCode];
        const kind = { j: "job", d: "discovery", b: "batch" }[kindCode];
        if (
          !["status", "pause", "resume", "cancel", "cancel_confirm"].includes(action)
          || !["job", "discovery", "batch"].includes(kind)
          || !UUID_RE.test(objectId || "")
        ) {
          await ctx.respond.reply({ text: "任务操作已过期。" });
          return { handled: true };
        }
        try {
          if (action === "cancel") {
            await ctx.respond.reply({
              text: "确认取消该任务？已完成并入库的内容会保留。",
              buttons: [[
                {
                  text: "确认取消",
                  callback_data: workCallback(
                    "cancel_confirm", kind, objectId,
                  ),
                  style: "danger",
                },
                {
                  text: "返回",
                  callback_data: workCallback("status", kind, objectId),
                },
              ]],
            });
            return { handled: true };
          }
          const command = action === "status"
            ? "work-status"
            : `work-${action === "cancel_confirm" ? "cancel" : action}`;
          const result = jsonOutput(await runJsonAware([
            command,
            "--user-id", String(ctx.senderId),
            "--kind", kind,
            "--object-id", objectId,
          ]));
          const response = {
            text: result.text || "当前状态不能执行此操作。",
            buttons: workButtonRows(result),
          };
          if (action === "cancel_confirm") {
            await ctx.respond.editMessage(response);
          } else {
            await ctx.respond.reply(response);
          }
        } catch (error) {
          api.logger.error?.(`video work callback failed code=${safeErrorCode(error)}`);
          await ctx.respond.reply({ text: "操作失败或任务已结束。" });
        }
        return { handled: true };
      },
    });

    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: "vkb",
      handler: async (ctx) => {
        if (!ctx.auth.isAuthorizedSender || !ctx.senderId) {
          await ctx.respond.reply({ text: "无权操作此批次。" });
          return { handled: true };
        }
        const parts = ctx.callback.payload.split(":");
        const action = parts[0];
        const objectId = parts.at(-1);
        if (!UUID_RE.test(objectId || "")) {
          await ctx.respond.reply({ text: "操作已过期。" });
          return { handled: true };
        }
        try {
          if (action === "range") {
            const selection = batchSelection(parts, 1);
            const preview = jsonOutput(await run([
              "batch-preview", "--discovery-id", objectId,
              "--user-id", String(ctx.senderId),
              "--limit", String(selection.limit),
              "--selection-kind", selection.kind,
            ]));
            if (!preview.ok) throw new Error("invalid discovery selection");
            await ctx.respond.editMessage({
              text: `作者：${preview.author_name || "未知"}\n范围：${selection.label}\n\n请选择目标知识库：`,
              buttons: [
                ...destinationButtonRows("vkb:dest", `${selection.token}:${objectId}`),
                [{ text: "放弃", callback_data: `vkb:abandon:${objectId}` }],
              ],
            });
          } else if (action === "dest") {
            const destination = destinationFromCallback(parts[1]);
            const selection = batchSelection(parts, 2);
            if (!destination || destination.id === "main") throw new Error("invalid destination");
            const preview = jsonOutput(await run([
              "batch-preview", "--discovery-id", objectId,
              "--user-id", String(ctx.senderId),
              "--limit", String(selection.limit),
              "--selection-kind", selection.kind,
            ]));
            if (!preview.ok) throw new Error("invalid discovery selection");
            const existing = Number(preview.existing_counts?.[destination.id] || 0);
            await ctx.respond.editMessage({
              text: `准备创建批次\n作者：${preview.author_name || "未知"}\n范围：${selection.label}\n知识库：${destination.name}\n预计新增：${Math.max(0, selection.limit - existing)}\n预计已存在跳过：${existing}\n\n作品会逐个处理；开始后可暂停或取消，取消不会删除已经完成的文档。`,
              buttons: [[{ text: "确认开始", callback_data: `vkb:start:${destination.code}:${selection.token}:${objectId}`, style: "success" }],
                [{ text: "返回选择", callback_data: `vkb:range:${selection.token}:${objectId}` }, { text: "放弃", callback_data: `vkb:abandon:${objectId}` }]],
            });
          } else if (action === "start") {
            const destination = destinationFromCallback(parts[1]);
            const selection = batchSelection(parts, 2);
            if (!destination || destination.id === "main") throw new Error("invalid destination");
            const result = jsonOutput(await runJsonAware([
              "confirm-batch", "--discovery-id", objectId,
              "--user-id", String(ctx.senderId),
              "--destination", destination.id,
              "--limit", String(selection.limit),
              "--selection-kind", selection.kind,
            ]));
            if (!result.ok) {
              await ctx.respond.reply({ text: batchStartError(result) });
              return { handled: true };
            }
            await ctx.respond.editMessage({
              text: `✅ 批次已创建 · ${result.batch_id.slice(0, 8)}\n作品会逐个进入现有入库流水线。`,
              buttons: [[
                { text: "查看进度", callback_data: `vkb:status:${result.batch_id}` },
                { text: "暂停", callback_data: `vkb:pause:${result.batch_id}` },
                { text: "取消", callback_data: `vkb:cancel:${result.batch_id}`, style: "danger" },
              ]],
            });
          } else if (action === "status") {
            const result = jsonOutput(await run(["batch-status", "--batch-id", objectId, "--user-id", String(ctx.senderId)]));
            await ctx.respond.reply({ text: result.text || "批次不存在。" });
          } else if (action === "pause" || action === "resume") {
            const result = jsonOutput(await run([`batch-${action}`, "--batch-id", objectId, "--user-id", String(ctx.senderId)]));
            await ctx.respond.reply({ text: result.text || "当前状态不能执行此操作。" });
          } else if (action === "cancel") {
            await ctx.respond.reply({
              text: "确认取消该批次？已经完成的文档会保留。",
              buttons: [[
                { text: "确认取消", callback_data: `vkb:cancel_confirm:${objectId}`, style: "danger" },
                { text: "返回", callback_data: `vkb:status:${objectId}` },
              ]],
            });
          } else if (action === "cancel_confirm") {
            const result = jsonOutput(await run(["batch-cancel", "--batch-id", objectId, "--user-id", String(ctx.senderId)]));
            await ctx.respond.editMessage({ text: result.text || "批次取消请求已提交。", buttons: [] });
          } else if (action === "abandon") {
            await ctx.respond.editMessage({ text: "已放弃，本次预览不会创建入库任务。", buttons: [] });
          } else {
            await ctx.respond.reply({ text: "操作已过期。" });
          }
        } catch (error) {
          api.logger.error?.(`video batch callback failed code=${safeErrorCode(error)}`);
          await ctx.respond.reply({ text: "操作失败或预览已过期，请重新发送作者主页。" });
        }
        return { handled: true };
      },
    });
  },
});
