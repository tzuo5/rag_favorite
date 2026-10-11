# Phase 0–6 实施与交付记录

日期：2026-10-04。实际仓库目录：`/home/tzuo5/gpt projects/rag_favorite`。本记录区分可运行的实现、实际机械验证和仍未通过的质量关口；主计划见 [Dev Doc](../videorag-dev.md)。部署与数据仍为隔离 MVP，没有生产库迁移、Git 提交或推送。

当前策略按用户最新要求取消强制分类和 cooking 视觉准入，默认全库检索；各主题视频均可使用已启用的视觉能力。下文涉及 cooking gate 和无语音 blocked 的描述为早期实施历史，现行行为及真实验证见 [统一知识库修改记录](unified-library-update.md)。

## Phase 0：本地视频编码 MVP

独立 Python 3.11 模型环境已安装 PyTorch 2.1.2 + CUDA 12.1、ImageBind、FFmpeg/PyAV 和 faster-whisper。主应用继续使用 Python 3.12，不把 torch 加入轻量核心依赖。文本编码保留 LM Studio Qwen3 Embedding 0.6B；视频及视觉查询用 ImageBind 的独立空间。

固定来源：ImageBind `53680b02d7e37b19b124fa37bae4b6c98c38f5be`；VideoRAG 对照 `c412a093a820ef7a0e0dda31076ed871136198b3`；ImageBind 权重 SHA-256 `d6f6c22bedcc90708448d5d2fbb7b2db9c73f505dc89bd0b2e09b23af1b62157`，4803584173 字节。模型代码/权重保留 [CC-BY-NC 4.0 来源](https://github.com/facebookresearch/ImageBind)。当前 Whisper Medium 采用 Systran revision `08e178d48790749d25932bbc082711ddcfdfbc4f`，本地 CPU INT8 推理。最初的 Small revision `536b0662742c02347bc0e980a01041f333bce120` 仅保留为历史对照，已完成视频的知识不会自动重写。相同来源 Medium 原始输出与验证元数据见 [转录文本](asr-original-shrimp-medium.txt) 和 [测试记录](asr-medium-test-metadata.json)。

每片段仍采用 5 个时间 clip、3 个空间 crop，共 15 视图。逐视图推理后 FP32 mean/L2，GPU profile 显式 FP16，CPU profile FP32；不同 dtype/权重/预处理生成不同 space_id。文本查询 75 个内容 BPE token，上限错误会要求缩短，不能静默截断。

初始小尺寸合成实测：GPU 编码约 0.78 秒、模型峰值约 1.99 GB；CPU FP32 整批/逐视图数值一致，CPU/GPU cosine 接近 1。见 [初始基准](imagebind-benchmark-results.json)。真实 4K 视频随后暴露整段解码 RAM OOM，已改为 **最长边 512、8 FPS 的临时编码片段**；原片仍用于截图/数字复核。模型端也检查最长 120 秒并对未限制的输入转码，避免直接调用绕过界限。该预处理已写入新 space_id；不能把初始小尺寸 benchmark 当作真实 4K 可行性证据。

```bash
cd '/home/tzuo5/gpt projects/rag_favorite'
.venv/bin/python examples/prepare_video_models.py
# 新机器：先安装用户目录中的 uv，然后显式准备隔离模型。
.venv/bin/python examples/prepare_video_models.py --prepare
```

依赖快照见 [模型 lock](../../examples/imagebind-requirements.lock.txt)，配置见 [video-local.toml](../../examples/video-local.toml.example)。首次下载之后，服务设置 HF/Transformers offline，并通过 loopback 提供带 token 的模型接口。

## Phase 1 / 1A：文本与 Codex 接入 MVP

恢复目录移动后的 editable 包与含空格的 PG socket 参数。LM Studio/llmster 监听 1234，隔离 PG/pgvector 监听 55432，数据库 `rag_phase1`。保留旧文本代际，先建立 `relocated-20261004-a`，随后为社交类样本增加隔离 `social-conduct` collection，建立 `samples-20261004-a`。随后为真实来源建立 `owner-20261004-a`，根为 `.runtime/videorag/knowledge`；合成视频独立到 test-fixtures library，普通示例文档留在旧代际，避免真实问答命中测试配方。各代文本 space_id 相同，但 collection 根集合不同；回退要配对保存的旧配置，不能直接激活不匹配根集合的代际。

实际 Codex CLI 调用旧 `rag_status/rag_search`，中文事实与来源正确。全局 `rag-favorite` 保留原两个工具；读工具按官方 `tools.<name>.approval_mode` 设置。当前聊天工具目录不会热加载，新的 Codex 会话会读取更新后的配置。[客户端证据](codex-mcp-actual-results.json)

## Phase 2：持久入库、删源 MVP

`video_cli` 提交受控资产或小红书链接，立即获得任务 ID。单 worker 持有 PG advisory lock；新任务优先级 10，回填 0。PG 中记录 queued/running/blocked/failed/published/complete。模型未就绪时不消耗待入库任务；异常只影响当前任务。

字幕优先，缺失时使用本地 Whisper small CPU int8；保留词时间。先分类，只有 cooking 内容、cooking collection 与显式启用视觉都成立才抽帧/VLM/ImageBind。默认 30 秒片段，超过 180 秒的视频采用可配置 60 秒片段，减少请求数以适应视觉预算；粒度变化仍需质量验证。图文笔记只导入笔记正文，没有图片 OCR，不伪装为视频。

44 分钟真实输入暴露了整段 ASR 请求期限问题，已改成最多 300 秒音频分段，词/句时间加回真实偏移，每段写检查点并删除音频片段。检查点绑定源 SHA 与 ASR model.bin SHA，重启可复用完成的分段。无语音片段不编造字幕；整条无语音则 blocked，需提供转录。单个 ASR 请求不会处理超过 300 秒的音频。

戴安娜牛排输入已确认无语音，但作者正文包含完整食材/步骤。已独立写入生成的 Markdown 并使用本地文本 embedding 索引，实际检索命中淡奶油 150 ml。该来源标为作者笔记、没有视频时间，不计为成功视频入库。视频仍 blocked，托管媒体在失败 TTL 内保留。可以为现有任务补充字幕，无需再次下载：

```bash
bash examples/video_runtime.sh cli stage /absolute/path/subtitles.srt
bash examples/video_runtime.sh cli attach-transcript JOB_ID TRANSCRIPT_ASSET_ID
bash examples/video_runtime.sh cli index-author-note JOB_ID
```

小红书使用已确认登录的专用 Chrome profile。只接受指定平台的 HTTPS 分享/笔记 URL；临时签名 URL 不进入公开日志。优先选含音频的 MP4 流，避免把独立 DASH 视频轨误当作完整音视频。直接下载到托管资产目录，不额外保留另一份下载原件。会话失效时 blocked，保留任务供重新登录/重试。

登录成功后抓取始终 headless，实际无 DISPLAY 抓取已通过；退出桌面不依赖 Chrome 窗口。cookie 保存在私有 0600 文件，不能把匿名会话当登录成功。

知识 JSON/向量/关系/必要截图先持久化，再校验 DB 片段数与截图 hash，最后删托管视频、字幕副本、音轨与临时切片。published 之后崩溃仅重试 cleanup；源缺失不阻止已经发布的 cleanup 恢复。完成后检索只读派生知识，不重开/重下原视频。CLI stage 的外部原件由所有者保留；链接下载没有另留外部原件。

3 个合成任务已走过真实模型/CCR/PG并完成清理，[结果](video-synthetic-integration-results.json)。真实输入正在处理，[动态快照](real-sample-ingestion-results.json) 的 complete/remaining 字段可核查。合成视频不能计入真实质量样本。

数字抽取保留 literal quote、entity、attribute、unit、uncertain/conflict。高清复核不覆盖原事实；不同值/单位的证据并存。真实 ASR 已暴露食材/单位错字，新增数字口述时刻抽帧和 ASR 数值保守校验。未被视觉事实确认的自动转录数值标 unknown。重查已持久化的 ASR 事实不读取媒体：

```bash
bash examples/video_runtime.sh cli recheck-asr JOB_ID
```

字幕与 ASR 的可靠性不同，unknown 增多是当前质量限制；不能把转录正确的可能性当作数字已经验收。分段知识 checkpoint 用源码/文本空间/视频空间/模型/抽取指令指纹校验，重试可复用已持久片段；源码改变或换空间会重建。

## Phase 3：删源检索与质量 MVP

真实来源代际与合成测试 library 隔离，回填演练复用 test-fixtures 中的合成抽取。当前用户查询实际未命中测试视频。同一 Qwen 空间中的普通文档和视频派生文本先共同排序；ImageBind/图关系使用独立排名融合，不能直接把不同空间 cosine 相加。明确 collection/library/space 过滤；MCP 不隐式跨库。截图通过 opaque evidence ID 与 SHA 校验读取，回答最多两张。

真实芥末虾球视频删源后，中文查询已返回相关片段和带时间/事实的结果。当前效果尚有 ASR 错字、数字保守 unknown、短暂屏显覆盖不足等问题。最终 **10 cooking 视频 / 60 人工核对问题 / Top 3 ≥90%** 仍未验收。

质量工具会拒绝模型生成而未人工确认的 gold。draft 仅供核对，不能把自动问题与自动答案当成质量通过。

```bash
bash examples/video_runtime.sh cli draft-questions .runtime/videorag/samples/questions-draft.json --manifest .runtime/videorag/samples/manifest.json
# 核对真实来源的 draft、reference_quote、截图和 expected_answer；人工设置 human_verified=true。
bash examples/video_runtime.sh cli evaluate /absolute/path/gold.json
bash examples/video_runtime.sh cli evaluate /absolute/path/gold.json --final
```

报告只评检索，不声称生成答案/数字准确率通过。负例、冲突、unknown 和实际 Codex 回答需要另行核对。

最终评测检查真实 cooking 分类、完整发布、已删源、collection 和片段 ID；不同 job 但相同视频 hash/笔记 ID 只能计一条来源。需要 60 个不同问题及人工确认的标准答案，重复问题不能凑数。`--manifest` 限定用户来源，避免把合成 fixture 自动加入草稿。

## Phase 4：扩展 MCP MVP

独立 `rag-favorite-video` stdio 服务提供 `rag_status/rag_search/video_import/ingestion_status/evidence_get`。实际 Codex 已调用五工具、提交合成任务并识别返回图片；旧双工具 profile 保留。写工具全局保持 prompt，仅受控验收命令对指定样例允许写入。

真实客户端复验也已完成：芥末虾球问题正确回答芒果/菠萝，返回 30.0–45.4 秒截图；重复来源导入返回原任务，确认 source_deleted=true。见 [真实 Codex 证据](codex-real-video-results.json)。

`video_import` 可选择 asset_id 或 source_url，必须指定 knowledge_base；链接下载/ASR/入库由后台做，不阻塞 MCP 等待整个视频。它接受受限小红书来源，不接受任意服务器文件路径。其他 Agent 可使用相同标准 MCP 工具。

```bash
bash examples/video_runtime.sh cli import-url 'https://xhslink.cn/o/SHARE_ID' --collection cooking
bash examples/video_runtime.sh cli stage /absolute/path/video.mp4
bash examples/video_runtime.sh cli import ASSET_ID --collection cooking
bash examples/video_runtime.sh cli status JOB_ID
bash examples/video_runtime.sh cli search '问题' --collection cooking
# MCP 进程由客户端启动；该命令的 stdout 仅用于协议。
bash examples/video_runtime.sh mcp
```

同机 stdio 已部署。跨机可先经认证 SSH 启动远端 stdio；公开 HTTP/OAuth 尚未实现，不在同机 MVP 中暴露无保护端口。

另一台电脑上可配置 SSH stdio。先确保客户端能使用 SSH key 登录该服务器，替换 SERVER_HOST 后执行：

```bash
codex mcp add rag-favorite-video-remote -- ssh -T tzuo5@SERVER_HOST "bash '/home/tzuo5/gpt projects/rag_favorite/examples/video_runtime.sh' mcp"
```

服务与凭据留在服务器；SSH stdout 只传 MCP 协议，远端 shell 的启动文件不能打印欢迎文字。此示例已按本机 CLI 参数核对，跨机握手还需实际主机/SSH key，尚未声称已验证。[Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

## Phase 5

按用户最新目标取消 Telegram；没有新 Bot、token 或 Telegram 依赖。

## Phase 6：运行、迁移与恢复 MVP

3 个 systemd 用户服务已启用：运行时（PG/LM Studio/已配置 CCR 启动检查）、ImageBind、worker。配置带空格路径已通过 systemd 校验。用户 `Linger=yes`，启用时没有要求 Linux 密码；服务可在退出桌面会话后运行。尚未实际重启整机，不能将安装/enable 等同 reboot 验收。

```bash
bash examples/video_runtime.sh status
bash examples/video_runtime.sh logs
bash examples/video_runtime.sh progress
bash examples/video_runtime.sh report
bash examples/video_runtime.sh restart
bash examples/video_runtime.sh stop
bash examples/video_runtime.sh uninstall
```

uninstall 删除本工具管理的用户 unit，恢复备份的同名 unit 文件，保留模型/凭据/知识；PG、LM Studio、CCR 为共享用户进程，不通过卸载自动删除数据或强行终止共享配置。

回填先盘点；保留人工 Markdown/旧摘要，缺视频者标 source_missing_text_only。重复运行使用收据，优先级低于新任务。清单必须写在旧目录外，不修改旧正文。

回填键包含来源根、collection、源 hash 与字幕 hash，持久化到 PG；即使入队后进程中断、文件收据丢失，再次执行也返回原任务，不重复建立新任务。既有文件收据保留按源 hash 的去重行为，修改字幕不自动覆盖原知识。

```bash
bash examples/video_runtime.sh cli backfill /absolute/legacy --collection cooking --manifest .runtime/videorag/legacy-manifest.json
bash examples/video_runtime.sh cli backfill /absolute/legacy --collection cooking --manifest .runtime/videorag/legacy-manifest.json --apply
```

真实 pg_dump/pg_restore 已在新建 scratch 数据库恢复知识/图与截图备份，恢复后检索成功，scratch 删除。见 [备份恢复结果](video-backup-restore-results.json) 与脚本 `examples/backup_restore_mvp.py`。不含原媒体和认证 cookie；本脚本仅允许 `rag_phase1` 系列隔离库。实际生产库默认配置当前不存在，未执行生产切换。

真实 PG/文件系统故障演练已通过 9 项：running 恢复与新任务优先、回填收据丢失、encoder 离线时 published 清理、进程在删源后提交前退出、截图损坏保留媒体、失败 TTL、补充字幕重试、TTL 后显式重新提交、外部原件与人工正文不变。[恢复结果](video-recovery-results.json) 使用保存的合成抽取数据重放，不声明模型质量已验证。

备份使用 PG exported snapshot 保证数据库一致性，派生归档只复制该快照中视频的知识。恢复到不同 scratch 目录时只调整 scratch DB 的 library_id，实际从恢复目录读取截图并核对 hash；原库和原媒体不参与证据读取。最新恢复结果中 restored_image_verified=true。

失败/blocked 媒体默认保留 72 小时，可设 `failed_media_ttl_hours`。worker 每 5 分钟在任务间清理过期失败副本，活跃/排队/published 任务不进入 TTL。保存已有转录/ASR 分段后删除托管资产与 work，仍保留失败状态并标记 media_expired；重试要求重新提交来源，不自动重新下载。手动应用需要先停 worker，避免与活跃任务竞争。

```bash
bash examples/video_runtime.sh cli expire-media          # 只报告到期失败任务
# 需要主动应用时：先 stop worker，再运行下面命令，之后 start。
bash examples/video_runtime.sh cli expire-media --apply
```

## 仍需完成的验收

- 实际样本全部处理；处理失败/预算不完整必须保留源，不冒充 complete。
- 真实 2 cooking + 1 非 cooking 视频 PoC，随后补足 10 cooking / 60 人工标准问题。目前原始输入按标题只有 9 cooking 候选，图文不计作视频。
- 分段 ASR 与视觉 checkpoint 已实现，关键 PG/删源/回填故障演练通过；长视频分段转录和全队列实际运行仍在继续。
- 生产盘点/切换与整机重启验收；当前只操作隔离库与用户 unit。
- 72 小时 TTL 的可控时间推进演练通过，实际失败来源当前未到期；不将时间推进演练当成已经等待 72 小时运行。
- 请求/图像数量预算保留；累计时间预算已按用户要求取消，美元成本仍 unknown。CCR 未提供可信计费信息，0.03 美元停止阈值尚不能验证，不能宣称费用上限已满足。

用户已明确取消累计时间预算，默认值和当前运行配置均改为 0（无限制）。此前因 600 秒停止的两条任务已保留检查点重新入队；累计耗时不清零，继续记录。恢复命令：

```bash
# 对受阻任务取消累计时间限制并恢复；已用请求与耗时记录保留。
bash examples/video_runtime.sh cli retry JOB_ID --visual-seconds 0
```

持续任务 `videorag-codex-mcp` 每小时接续。后台 worker 不依赖 Codex 窗口处理队列，但自动开发接续需要电脑开机、Codex App 运行。[官方自动化说明](https://learn.chatgpt.com/docs/automations?surface=app)

## 更新后的编码复验

[bounded 编码结果](imagebind-bounded-benchmark-results.json)：保留 15 视图，CPU/GPU cosine 0.999998629，GPU FP16 0.607 秒、峰值约 1.99 GB，CPU FP32 11.417 秒、进程峰值约 10.11 GB。CPU 整批与逐视图 cosine 0.99999994，非 loopback TCP guard 通过。输入是合成短片，仅用于编码/资源验证。Decord bridge 在每次解码的当前线程显式设置，修复新 HTTP worker 线程的 NDArray 类型错误。

[并发编码结果](imagebind-concurrent-results.json)：3 个同时提交的实际 HTTP 请求全部完成，均为 15 视图且空间一致；测试暂存副本已清理。


## 小红书收藏分页导入（2026-10-04）

新增入口直接读取已登录账号的收藏，签名请求参考 ReaJason/xhs 的
`/api/sns/web/v2/note/collect/page`，使用已有 xhshow 组件；账号由
`/api/sns/web/v2/user/me` 确定，不接受任意他人账号 ID。

```bash
bash examples/video_runtime.sh cli import-favorites --collection cooking
bash examples/video_runtime.sh cli favorites-status --collection cooking
```

默认持续分页至 `has_more=false`；`--max-pages 1` 可用于首批验证。
失败或限页后再次执行会续用游标；`--restart` 从头重新扫描，并按笔记 ID 去重。
游标重复、接口拒绝、登录失效和结构变化会阻止“完成”状态。
状态保存在私有运行目录，收藏内容提交现有 PostgreSQL 持久化导入队列。
数据库入队与游标写入之间若中断，重读本页依靠数据库去重恢复。
已有分享链接任务通过保留的 source.json 补充笔记 ID，以避免再次下载。
同一个目标知识库仅允许一个扫描进程；分页每次间隔 2 秒。

MCP 新增 `favorites_import(knowledge_base, restart=false)`，派发独立后台扫描；
`favorites_status(knowledge_base)` 查询进度。新 Codex 会话可加载工具。
扫描状态 complete 仅表示全部可读取收藏已入队，不表示所有内容已处理成功；
处理结果仍使用 ingestion_status/rag_status。原视频在成功入库后按既有规则删除。
收藏包含图文时，当前导入作者正文，不执行图片 OCR；无正文的图文不能视为完整知识提取。
收藏条目失效、删除或需要验证时应分别查看处理失败，不保证不可访问内容可下载。

此入口复用现有会话，不创建永久 Cookie，也不新增定时自动同步。
全量内容当前按调用指定的 collection 入队，不自动根据收藏分组创建知识库。
开源接口参考：https://github.com/ReaJason/xhs/blob/master/xhs/core.py
