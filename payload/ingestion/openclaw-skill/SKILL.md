---
name: video-knowledge-ingest
description: Telegram 视频、音频、单作品链接及受支持的作者主页由确定性的 video ingestion plugin 自动接管；回答平台支持、批量能力、命令或限制前应调用只读 capability tool。
user-invocable: false
---

# Video Knowledge Ingest

Telegram 入站视频、音频和视频链接由 `video-knowledge-ingest` plugin 在模型调用前处理。
若消息未被 plugin 接管，正常回答；不要自行模拟下载、转录、清理或入库流程。

## 能力判断

`video_ingestion_capabilities` 是当前运行态的权威只读接口。用户询问插件是否安装、支持哪些平台、作者批量是否启用、可用命令或数量限制时，优先调用它。

不要因为隔离 sandbox 看不到宿主机源码、CLI 或项目路径，就推断插件未安装。若 capability tool 调用失败，应说“插件 hook 已加载，但暂时无法验证后端运行态”，不能编造“未安装”。

静态能力边界：

| 平台 | 单作品录入 | 作者主页批量 |
| --- | --- | --- |
| YouTube | supported | 由 discovery/execution 两个运行态 flag 决定；“全部”遍历完整作者作品列表 |
| Bilibili | supported | flag 控制；“全部”遍历完整作者作品列表 |
| 小红书 | supported_with_auth_limits | flag-off-by-default；登录签名分页后“全部”遍历到末页 |

小红书作者批量先读取公开主页，并在配置本地 Cookie 时使用签名接口按游标分页；执行时通过短期内存令牌解析视频流，避免依赖易变的页面提取器。Cookie、签名、`xsec_token` 和 CDN 能力地址不会持久化。登录失效、验证码、签名异常或风控时会安全停止，不会绕过平台安全机制。

## 操作与状态

- 所有作品链接、作者主页和 Telegram 媒体先显示“开始/取消”按钮。
  点“开始”前不展开短链、不访问平台、不创建数据库任务、不读取字幕、
  不下载且不转录；开始请求 10 分钟后失效。
- 单作品链接：点“开始”后再选择目标知识库，选择前不读取字幕、不下载、
  不转录；无需 slash command。
- 作者主页：点“开始”后读取作品预览，再选择范围与一个目标知识库，
  二次确认后才下载和入库，所有子任务锁定到该库。
- `/video_login`：选择小红书或哔哩哔哩并发送扫码登录二维码。两个平台
  使用独立的私有 Cookie 文件；Cookie 值不会进入聊天、日志或数据库。
- 可写目标为思想政治、技术、金融投资、职业发展、中国人情世故、
  文学文化、综合资料和烹饪；`legacy` 不可写。
- 统一状态与控制：`/video_status`、`/video_pause`、`/video_resume`、
  `/video_cancel`。它们自动定位当前的单视频、作者列表发现或作者批次；
  所有三个平台使用同一套确定性状态。
- 作者批次：`/video_batch_status`、`/video_batch_pause`、`/video_batch_resume`、`/video_batch_cancel`

不要根据普通 RAG 搜索结果猜测任务或运行状态。
