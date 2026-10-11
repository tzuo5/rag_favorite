# 自动开发进度

更新时间：2026-10-04。实际目录：`/home/tzuo5/gpt projects/rag_favorite`。

用户授权执行 Phase 1–6；Phase 5 Telegram 仍取消。自动续作 heartbeat：`videorag-codex-mcp`，每小时检查本会话并推进。电脑需保持开机，Codex App 需保持运行；远程 Terminal 可查看本文件、执行命令和提供样本。

## 已运行并验证

- Phase 1 / 1A：修复目录移动后的 editable 与 PG socket 空格；LM Studio Qwen3 Embedding 0.6B（1024 维）与隔离 PG/pgvector 已恢复。当前代际 `owner-20261004-a` 包含 cooking / tech / social-conduct，根为 `.runtime/videorag/knowledge`，旧代际与配套配置保留。实际 Codex CLI 成功调用旧 MCP 两工具并回答中文问题。
- Phase 0：独立 Python 3.11、PyTorch 2.1.2/CUDA 12.1、ImageBind 已安装。官方权重 4803584173 字节，SHA-256 `d6f6c22bedcc90708448d5d2fbb7b2db9c73f505dc89bd0b2e09b23af1b62157`。保留 5 clips × 3 crops，15 视图逐个推理；GPU FP16 峰值约 1.99 GB，4 秒合成片段编码约 0.78 秒；CPU FP32 与 GPU 结果近似一致。见 [实测](imagebind-benchmark-results.json)。本地 Whisper small 已安装，英文真实音频 ASR 验证通过；中文质量待样本。
- Phase 2 / 3：受控暂存、异步持久任务、字幕优先/本地 ASR、cooking gate、本地视频向量、CCR Responses 知识抽取、同 PG 图关系、独立空间排名融合、截图证据均已实现。3 个合成任务真实走过模型/CCR/PG，完成后托管原视频、字幕副本、音轨、临时切片均已删除。非 cooking 样例视觉为零。合成素材不算真实 cooking 质量验收。
- Phase 4：全局 Codex 已注册 `rag-favorite-video`；实际 Codex CLI 调过全部五工具并识别 MCP 图片。第二次真实视频测试正确回答“芒果或菠萝”，读取 30.0–45.4 秒图片证据；重复 URL 导入返回原任务。旧 `rag-favorite` 保留两工具兼容；写入工具全局仍需调用端确认。新会话才加载更新后的 MCP 配置。见 [真实客户端结果](codex-real-video-results.json)。
- Phase 6：3 个 systemd 用户服务已启用并运行；隔离库真实 pg_dump/restore、PG 一致性快照、派生知识备份、恢复后检索与实际截图读取通过。数据库/派生目录备份不含原媒体。已无密码启用 `Linger=yes`，支持用户退出登录后运行；整机重启尚未演练。小红书无 DISPLAY 抓取已实际成功。生产迁移未执行。
- 最近完整验证：核心 141 passed / 8 skipped；ingestion 230 passed / 32 skipped；实际 PG/LM Studio 索引集成 8 passed；后两项为本轮早期结果。后续修改需要复验。

## 正在推进与剩余关口

1. 原始 11 条输入逐字保存于 `.runtime/videorag/samples/original-input-20261004.txt`；结构化 manifest 保留 SHA-256 与每条来源。重新扫码与手机验证已成功，session 为 `cookies_exported`；cookie 文件 0600，专用 profile 保留。登录辅助进程已正常结束；无需再次扫码或提供验证码。不要输出凭据。
2. 11 个来源任务已入队；目前 5 条真实 cooking 视频完成，托管资产与 work 均为零；第四条长视频因 600 秒视觉预算 blocked，第五条无语音视频保持 blocked，作者文字已单独入库；其他输入继续处理。按标题只有 9 条 cooking 候选、2 条其他候选；最终质量要求 ≥10 个不同的 cooking 视频 / ≥60 人工核对问题，不能让 LLM 自评分冒充 gold。最新数量以 [报告](real-sample-ingestion-results.json) 为准，运行 `report` 可刷新。
3. 已补齐 ASR 数量需视觉佐证、定向抽帧、数字复核保留原事实、分段检查点、清理退避和依赖锁。真实 4K 视频暴露整体解码内存问题，已在 worker 与 encoder 双层限制 512 边长 / 8 FPS，保留 ImageBind 15 视图并使用独立空间；三条真实任务已通过。大于 180 秒的视频默认按 60 秒分段，其他按 30 秒，粒度变化需纳入质量验收。回填丢失收据去重、恢复故障剧本和失败 TTL 的真实 PG/文件系统演练已通过，见 video-recovery-results.json。44 分钟输入已推动 ASR 改成 300 秒分段、保留词时间偏移与检查点。新任务优先于回填；保留人工正文/旧摘要。
4. 视觉请求/图像/时间预算正在实现；CCR 未返回可信计费单价，美元费用仍 unknown，0.03 美元停止阈值尚不能验证。
5. 远程 HTTP MCP 未启用；当前同机 stdio 与 SSH Terminal 可用。旧库全量迁移与质量门槛未验证，不能将代码完成写成全部 Phase 验收完成。
6. 没有 sudo、Linux 密码、提交或推送；已有大量未提交修改必须保留。当前数据与部署仍是隔离 MVP。

## 远程 Terminal

```bash
cd '/home/tzuo5/gpt projects/rag_favorite'
cat docs/implementation/autonomous-progress.md
bash examples/lmstudio_runtime.sh status
bash examples/video_runtime.sh status
bash examples/video_runtime.sh logs
bash examples/video_runtime.sh report
bash examples/video_runtime.sh cli import-url 'https://xhslink.cn/o/SHARE_ID' --collection cooking
# 暂存用户文件，返回 asset_id；不会删除这个外部原件。
bash examples/video_runtime.sh cli stage /absolute/path/video.mp4
# 用上一步的 asset_id 提交；选库必须明确。
bash examples/video_runtime.sh cli import ASSET_ID --collection cooking
bash examples/video_runtime.sh cli status
bash examples/video_runtime.sh cli search '问题' --collection cooking
```

## 接续原则

先读取 Dev Doc 和本文件；保留已有未提交修改。每阶段分别记录 implemented 与 verified，真实视频质量评测需要用户样本和标注，不能把合成测试视频算作真实 corpus。遇到 Linux 密码要求暂停该操作；不要要求在聊天里粘贴密钥。

模型配置分别在 `.runtime/phase1/config.toml` 和 `.runtime/videorag/video.toml`。凭据仅在各自 0600 文件中。登录过期时才运行 `examples/xhs_login.py --headed`；登录后抓取使用 headless，无需桌面。`pending-otp.txt` 是私有一次性输入通道，仅验证码字段存在时消费，不记录内容；未关联手机号时不要盲目提交验证码。

当前是 VideoRAG 思路的本地适配：字幕/ASR、ImageBind、视觉事实与同 PG 关系检索；没有宣称复现上游全部 GraphRAG 社区算法。所有 embedding 本地运行，视觉知识抽取仍走已配置 CCR。

最新故障演练通过 9 项，新增字幕补充重试；CLI attach-transcript 可恢复无语音任务，index-author-note 仅导入已保存的作者正文。第 4 条增加单任务预算的选择请求仍待用户，未收到回答前保持 600 秒。

真实来源与合成测试已经隔离：三个合成任务及 benchmark 移至独立 test-fixtures，普通测试 Markdown 留在旧代际。新 owner 代际已建立/构建/激活；真实删源查询正确命中，测试视频未出现在结果。回退时必须配对 .runtime/review-20261004/config-before-owner.toml 与 samples-20261004-a，不能混用新根与旧代际。44 分钟输入跨 worker 重启复用了 8/9 个 ASR 检查点并进入知识抽取，当前仍在处理。

更新后的 bounded 编码实测已通过：GPU FP16 0.607 秒 / 峰值 1994516480 字节，CPU FP32 11.417 秒 / 进程峰值 10110013440 字节；CPU/GPU cosine 0.999998629。非 loopback TCP guard 通过。见 imagebind-bounded-benchmark-results.json。已修复 Decord bridge 的线程本地配置，避免新的服务线程返回 NDArray 导致编码失败。

并发编码 3/3 通过。真实来源草稿已生成 172 个候选问题，全部 human_verified=false，尚未计入质量验收。当前无需 API key 或 Linux 密码；最终标注与样本不足仍需所有者提供，预算变更请求仍待选择。

本轮交付快照见 current-delivery-status.json。运行时、ImageBind、worker 三个用户服务当前均 active，Linger=yes。工程回归 141 passed/8 skipped；真实队列继续运行，最终质量验收仍未完成。


### 2026-10-04：小红书收藏分页入口已实际验证

使用已有私有 Cookie 与 xhshow 签名，调用本人 /user/me 与 /note/collect/page。
真实扫描 complete：21 页，196 个新增持久化导入任务、7 次重复记录。
再次读取第一页 10 条，10 条均去重、0 个新增任务。
新任务当前按指定 cooking 库入队；扫描完成不等于下载/提取完成，后台 worker 继续运行。
图文目前仅导入作者正文，图片 OCR 未实现。
新增 CLI import-favorites/favorites-status 与 MCP favorites_import/favorites_status。
146 tests passed / 8 skipped，Ruff 与 diff 检查通过。
详见 xhs-favorites-results.json；现有登录可用，不需要 Linux 密码或新增 API Key。


### 2026-10-04：收藏入口复审与每晚自动同步

已修复背景 MCP 配置传递、手动/收藏去重口径、自动复活过期失败任务、账号绑定、跨恢复游标循环、私有状态文件和 HTTP 重定向边界，新增共享浏览器锁。
真实重复全量扫描 21 页 / 203 次去重 / 0 新任务，service Result=success。
158 普通测试通过 / 12 跳过；4 个真实 PostgreSQL 队列测试另外运行全部通过。
每晚 22:00 America/Chicago 的 systemd 用户定时器 active/enabled，Persistent=true，Linger=yes；不依赖 Codex 窗口。卸载/重装已实际验证，数据保留。
详细审查、边界和管理命令见 xhs-favorites-review.md，机器记录见 xhs-favorites-audit-results.json。
处理队列继续运行，扫描完成不代表全库提取完成。无需 Linux 密码。


### 最新调度更正（2026-10-04）

按用户最终要求改为 America/Chicago 每天 07:00 与 18:00，替换原 22:00 设置。定时器 active，下一次为 2026-10-04 18:00 CDT；安装脚本默认也改为两次，支持 --hour 7 18。systemd 实际配置与 calendar 校验通过。


### 2026-10-04 13:03 CDT：实时资源与流水线核查

实时快照见 runtime-status-20261004.json。内存 8.5/30 GiB，可用 21 GiB；显存 6249/8188 MiB；LM Studio 3582 MiB、ImageBind 服务 2250 MiB。
207 个实际任务：22 完成、1 运行、174 排队、10 blocked。实际 12 条视频执行视觉分支，42 个视频向量、84 张证据图、151 条视觉事实。
核查发现更新代码期间 41 个任务在 failed:queued 阶段遇到 ImportError，尚未读取源笔记。当前 worker 的 source 模块及共享浏览器锁导入正常，已使用既有 retry_job 仅将这 41 个任务重新入队，没有重置视觉预算或重试其他 blocked 任务。已经继续处理被恢复的任务。
图文仍只导入标题和正文；视觉分支仅限分类确认 cooking 且配置已启用。转录使用本机 faster-whisper small / CPU int8；文字向量为 LM Studio Qwen3-Embedding-0.6B；视频向量为本机 ImageBind / GPU FP16；字幕加抽帧知识提取通过 CCR 当前配置模型。


### 2026-10-04 13:21 CDT：取消强制分类并修复受阻任务

按用户最新要求，取消 cooking 视觉准入与强制分类，所有主题视频均可使用已经启用的视觉流程；MCP/CLI 默认全库搜索。默认导入复用旧 cooking 存储 key，仅用于兼容，不限制主题；历史 tech/social-conduct 资料同时可检索，没有重建索引或更改向量空间。
原 10 条 blocked 逐条核查：2 条视觉时间预算耗尽、3 条无语音被旧流程阻断、4 条无标题笔记被解析器误报不可用、1 条 CCR 请求失败。/user/me 真实调用确认登录有效，四条无标题笔记修复后全部可读取。
无语音不再阻断已启用的视觉流程，保留空转录和真实视频时间轴，不伪造语音。戴安娜牛排已真实完成：5 个视觉片段、10 张证据图、原视频与 work 均已删除；新 stdio MCP 默认全库检索命中视频，默认 evidence_get 返回图片。
快照：207 任务，28 complete、176 queued、1 running、2 blocked；其余 7 条原受阻任务已恢复处理。两个预算耗尽任务保持原预算和检查点；失败请求用量未清零，没有无限重试。
普通测试 175 passed / 12 skipped；真实 PostgreSQL 队列测试另行 4 passed。Ruff、compileall、shell 语法与 diff 格式检查通过。worker 显式 SIGINT/SIGTERM 后实际停止 Result=success，更新后 active；定时器保持 America/Chicago 每日 07:00 与 18:00，默认全库入口 active。
详细修改与当前流程见 unified-library-update.md，原始十条明细见 blocked-jobs-20261004.json，真实模型/MCP 证据见 unified-library-live-results.json。代码和服务已更新，没有 Git 提交或推送；人工质量验收仍需完成。


### 2026-10-04：取消累计时间预算

按用户明确要求，默认及运行 profile 的 visual_budget_seconds 改为 0（无限制）。之前的模型耗时仍记账，但不再按累计耗时停止；单次网络断线超时和请求/图像数量限制继续有效。
两条 VISUAL_TIME_BUDGET_EXCEEDED 任务保持原编号，使用 --visual-seconds 0 恢复；恢复前已有 4 个与 11 个切片检查点，兼容缓存继续复用，不兼容的旧切片重新提取；原请求 ledger 保留，worker 已重新运行。新回归 184 passed / 13 skipped；另行真实 PG 队列 5 passed。Ruff、compileall、shell 和 diff 格式检查通过。验证记录见 time-budget-removal-results.json；上述恢复不等于视频已经全部入库。
