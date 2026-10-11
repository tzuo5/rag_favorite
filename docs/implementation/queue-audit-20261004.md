# 视频知识库状态与导入队列核查

采样时间：2026-10-04T14:00:27.421674-05:00，America/Chicago；核验读取完成：2026-10-04T14:00:27.672106-05:00。这是队列继续运行期间的快照，不承诺之后阶段不变。范围仅为当前所有者库，不包含三个隔离的合成模型验证任务。此次核查使用只读数据库事务，没有重排、删除或重新导入任务。

当前 207 条导入任务对应 203 条独立笔记；完成 33 条任务（31 条视频、2 条图文正文），对应 30 个独立来源；处理中 1 条、等待 173 条，当前 blocked、failed、published 均为 0。54 个任务有历史错误：15 个已完成、1 个正在恢复、38 个等候。既有 4 组重复来源仍需治理。

## 新架构与竖向状态图

处理不依赖 cooking 类别；旧 collection 只作存储兼容。文字 embedding 经 LM Studio 本地执行，视频 embedding 由本地 ImageBind 提供，VLM 知识抽取经现有 CCR。无语音和语音抽帧频率描述的是 VLM 图片分析，与 ImageBind 内部采样不同。累计处理时间预算为 0。

```mermaid
flowchart TD
    A["收藏定时同步 07:00 / 18:00 芝加哥时间<br/>或 Codex MCP 手动提交"] --> Q["queued 持久队列与来源去重"]
    Q --> R["running 单 worker 接管任务"]
    R --> S["fetch_source 读取小红书笔记"]
    S -.->|浏览器独占会话忙| BW["queued / retry_wait<br/>60 秒后重试"]
    BW --> Q
    S --> N{"图文还是视频"}
    N -->|图文| NT["标题与作者正文生成 Markdown<br/>图文图片目前不做 OCR"]
    NT --> NE["LM Studio 本地文本 embedding"]
    NE --> NC["complete / text_only 正文已入库"]
    N -->|视频| V["受管下载与 probe 探测真实时长"]
    V --> AS["transcript 优先字幕文件<br/>否则本地 Faster Whisper 按 300 秒转录"]
    AS --> L{"原视频时长超过 600 秒"}
    L -->|是| TX["全部视觉工作跳过<br/>使用转录或标注来源的作者正文"]
    TX --> HT{"有可用文字"}
    HT -->|否| BL["blocked 保存说明与检查点<br/>无可用文字不能生成知识"]
    HT -->|是| TE["LM Studio 本地文本 embedding"]
    L -->|否 包含 600 秒| HS{"有语音转录或提供字幕文件"}
    HS -->|有| F1["每 1 秒一帧"]
    HS -->|确认无语音| F05["每 0.5 秒一帧"]
    F1 --> VF["extract 每批最多 8 帧 CCR 逐帧分析<br/>批次检查点与覆盖校验"]
    F05 --> VF
    F1 --> VB["本地 ImageBind 视频向量"]
    F05 --> VB
    VF --> TE
    TE --> PUB["published 数据库提交知识与向量<br/>Markdown 与截图已保存至受管目录"]
    VB --> PUB
    PUB --> CL["cleanup 核验切片与证据摘要<br/>删除托管原视频 音轨 临时帧和切片"]
    CL --> CO["complete 原视频清理已完成<br/>Codex MCP 可检索及读取证据"]
    R -.->|provider 或前置条件不可用| BL
    R -.->|其他执行异常| FA["failed 执行失败<br/>保存脱敏错误与检查点"]
    BL -->|条件恢复后显式 retry| Q
    FA -->|修复后显式 retry| Q
    CL -->|清理失败 保持 published| CR["published / cleanup_failed<br/>60 秒后仅重试清理"]
    CR --> CL
```

600 秒为视频时长阈值，和已取消的“累计处理耗时 600 秒”不同。没有语音但只有烧录字幕时，仍按 0.5 秒由视觉模型读取；提供字幕文件时按 1 秒。长期保存逐帧文字描述，采样图在成功后清理，每个片段仅保留少量证据图。ASR 服务故障不得当作没有语音。

数据库真正的 state 只有 queued、running、published、complete、blocked、failed；fetch_source、transcript、extract:N、cleanup 是 stage，不是另外的 state。图文正文走 text_only 直接 complete，没有视频 published/删源环节。

## 队列与服务概况

| 状态 | 数量 | 含义 |
|---|---|---|
| complete | 33 | 已成功写入并完成对应清理；图文正文无需视频删源 |
| running | 1 | worker 当前正在处理；还不能当作完整入库 |
| queued | 173 | 尚未轮到当前单 worker，包括修复后重排的任务 |
| published | 0 | 视频知识已提交，但清理尚未完成 |
| blocked | 0 | provider 或前置条件不可用 |
| failed | 0 | 其他执行异常 |

| 服务 | 当前状态 | 结果 |
|---|---|---|
| rag-favorite-video-worker.service | active / running | success |
| rag-favorite-imagebind.service | active / running | success |
| rag-favorite-favorites.timer | active / waiting | success |
| rag-favorite-favorites.service | inactive / dead | success |

收藏最近同步 complete：21 页，203 次去重命中、0 条新增，error_code=null；更新时间 2026-10-04T17:43:25.955663+00:00（UTC）。定时任务 service 为 inactive/dead 是该次运行结束，不表示失败；timer active/waiting，下一次仍按芝加哥时间 18:00/07:00 触发。

完成性核验：31 条已完成视频的数据库切片数与 record 一致、证据图摘要通过、source_deleted=true、托管原视频和 work 目录不存在、Markdown 存在；2 条图文正文的文档摘要匹配且各已索引 1 个文本 chunk。没有查到“知识已经发布但清理失败”的当前任务。这个检查证明持久化与清理，不证明所有模型事实经人工核实。

## 当前正在处理

### 社交场上的潜规则大合集

任务 `ea9ce021-a020-4414-a89b-20de00009b1b`，提交序号 26，当前 `running / extract:21`；时长 2637.653 秒（约 43.96 分钟）。

已完成本地语音转录，共 869 条；按 60 秒组织 44 个文字切片，保存了 21 个符合当前文字模态的切片检查点。`extract:N` 的 N 从 0 开始，是正在进入/处理的切片，不能把它当成 N+1 条已完成。当前没有完整数据库发布，原视频和临时目录仍保留。

旧问题是累计视觉时间预算在 extract:11 被触发；目前预算已取消。该视频超过 600 秒，当前已跳过全部视觉，最新模型请求 images=0。账本中的旧图片请求属于历史调用，不意味着新策略仍在为长视频做视觉。

另有一条相同 note_id、相同视频 SHA-256 的较早任务已完成，见重复来源章节。当前正在处理的是另一个任务编号，不是那条完成记录又变坏了。

## 全部已完成任务

逐条列出 33 条任务；同来源的多条任务暂不合并。视频源清理均经本次核验通过；图文正文标为正文。多数任务完成于密集抽帧规则启用前，不能把旧截图数量当作 0.5/1 秒抽帧覆盖。

| 提交序号 | 标题 | 任务编号前 8 位 | 文本切片 | 视频向量切片 | 证据图 | 历史错误 |
|---|---|---|---|---|---|---|
| 1 | 过年C位菜🧨搅一搅搞定❗大人小孩都爱吃🔥🔥 | 5cbfcb11 | 2 | 2 | 4 | ProviderUnavailable |
| 2 | 过年必备小凉菜第五道皮蛋豆腐 | 20567f31 | 2 | 2 | 4 | ProviderUnavailable |
| 3 | 蒜香鸡排超详细教程！做法简单巨好吃 | 809ad78e | 9 | 9 | 18 | RuntimeError |
| 4 | 这样做简单好吃入味 | 3aa899a2 | 7 | 7 | 14 | VISUAL_TIME_BUDGET_EXCEEDED |
| 5 | 温莎公爵最喜欢，蒙巴顿每次必点的菜 | 63106943 | 5 | 5 | 10 | ProviderUnavailable, TRANSCRIPT_REQUIRED_NO_SPEECH |
| 6 | 只需2个鸡蛋🔥0面粉版双重巧克力蛋糕～ | 28fd50b6 | 2 | 2 | 4 | 未查到 |
| 7 | 社交场上的潜规则大合集 | 8abed680 | 44 | 0 | 0 | LOCAL_ENCODER_UNAVAILABLE |
| 8 | 红烧牛肉面🍜 | 73cd740b | 4 | 4 | 8 | 未查到 |
| 9 | 杂粮煎饼制作教程分享 | d256e175 | 4 | 4 | 8 | 未查到 |
| 10 | 如果ai设计得总是很丑，试试这个skills | 4a29718b | 2 | 0 | 0 | 未查到 |
| 11 | 两人三餐｜在家给老婆做超下饭的公瑾爆蛋 | 0a96aef8 | 1 | 1 | 2 | TRANSCRIPT_REQUIRED_NO_SPEECH |
| 12 | 好吃到疯了【肥牛虾滑响铃卷】虾滑新吃法 | 98b5803d | 2 | 2 | 4 | 未查到 |
| 13 | 雪王冰城配方分享\|\|\|#配方分享 #配方 #蜜雪冰城 #蜜雪 #蜜雪冰城奶茶 # | ad0f6b83 | 1 | 1 | 2 | XHS_LOGIN_EXPIRED_OR_NOTE_UNAVAILABLE |
| 14 | 10分钟解锁🍋外焦里嫩清爽开胃柠檬三文鱼 | a0f11495 | 4 | 4 | 8 | 未查到 |
| 15 | 双人脊柱拉伸 | 887fc0b4 | 1 | 0 | 0 | 未查到 |
| 16 | Coze搭建电影解说工作流教程，纯干货 | 3bb4beb5 | 2 | 0 | 0 | 未查到 |
| 17 | 每日一个神级网站【第102期】 | b9381075 | 1 | 0 | 0 | 未查到 |
| 18 | 全球各种免费API，收录5321个API接口！ | b2de4377 | 2 | 0 | 0 | 未查到 |
| 19 | 关东煮｜在家实现关东煮自由就是这么简单！ | a1f0d068 | 3 | 3 | 6 | 未查到 |
| 20 | 过年C位菜🧨搅一搅搞定❗大人小孩都爱吃🔥🔥 | 642dad56 | 2 | 2 | 4 | 未查到 |
| 21 | 过年必备小凉菜第五道皮蛋豆腐 | 7c783b61 | 2 | 2 | 4 | 未查到 |
| 22 | 滑蛋鸡排饭，巨下饭 | 5349109c | 4 | 4 | 8 | CCR_REQUEST_FAILED |
| 23 | 天冷就爱这一口🥣上海家常正宗老上海罗宋汤 | 4ee71fc2 | 3 | 3 | 6 | TRANSCRIPT_REQUIRED_NO_SPEECH |
| 25 | 创业全流程【第二集】 | 8a296eaf | 6 | 0 | 0 | 未查到 |
| 28 | 没有正反馈时，如何保持极高的执行力？ | 2abab64b | 5 | 0 | 0 | ImportError |
| 29 | 如果ai设计得总是很丑，试试这个skills | 157c1756 | 2 | 0 | 0 | ImportError |
| 30 | 七种不同的下拉框 | 24f720b9 | 2 | 0 | 0 | ImportError |
| 31 | 一周时间速通leetcode hot到达面试水平 | 66556614 | 正文 1 chunk | — | — | ImportError |
| 32 | 绝命毒师铁粉朝圣路线 | ccad4071 | 正文 1 chunk | — | — | ImportError |
| 68 | 这是一个能随意修改图片文字的硬核工具 | 9db52d5d | 2 | 0 | 0 | 未查到 |
| 69 | 时悦 \| 商用牛骨高汤配方实测 | fed56db1 | 6 | 6 | 12 | 未查到 |
| 70 | AI帮你投简历？有人用它拿下高薪的AI总监 | e80d3a86 | 2 | 0 | 0 | 未查到 |
| 72 | Ai应用开发如何从0到40k？ | f05b0e0b | 8 | 0 | 0 | 未查到 |

## 尚存的来源与诊断问题

### 重复来源影响队列与检索

已确认 4 组重复，不只是标题相同：每组 note_id 相同且视频 SHA-256 相同。三组有两条 complete，另一组一条 complete、一条 running。重复任务提高处理成本，也会让“33 个成功任务”看起来像 33 个独立视频；实际只有 30 个独立来源完成。

| 内容 | 来源 note_id | 任务与状态 |
|---|---|---|
| 过年C位菜🧨搅一搅搞定❗大人小孩都爱吃🔥🔥 | 6958c7cc000000001f005446 | 5cbfcb11 complete (cooking)；642dad56 complete (cooking) |
| 过年必备小凉菜第五道皮蛋豆腐 | 697f19a4000000002202c743 | 20567f31 complete (cooking)；7c783b61 complete (cooking) |
| 社交场上的潜规则大合集 | 6a93d0df0000000003029eca | 8abed680 complete (social-conduct)；ea9ce021 running (cooking) |
| 如果ai设计得总是很丑，试试这个skills | 6a5ed19c000000001b01d0c1 | 4a29718b complete (tech)；157c1756 complete (cooking) |

具体位置：`video_store.enqueue_url` 的 legacy 笔记 ID 回填查询与重复查询均含 `collection=%s`。因此旧 social-conduct/tech 与统一默认 cooking 的同一来源可成为两个任务。C 位菜/皮蛋豆腐的同分区重复已在旧短链接与收藏 canonical-ID 入口之间形成；当前新增了 source.json 的 legacy-ID 回填，但它不会自动合并早已存在的重复任务。

检索端 `video_retrieval.reciprocal_rank_fusion` 按 `(document_id, section)` 去重，其中 document_id 基于 video job UUID；即使 content_hash 相同，不同任务仍可重复占用召回名额。

解决方案：统一来源身份为 `(library_id, note_id)`，跨旧物理分区查重；旧短链接在成功解析时回填 ID，建立明确的 canonical source 与任务/版本映射。既有四组先比较知识与证据，保留所有有效事实和引用，再选 canonical 记录；检索按来源与片段区间去重，独立来源计数也按 note_id。数据库唯一约束须在兼容历史重复、回填与版本语义后建立，不能直接删除重复行或给现表硬加唯一索引。当前运行重复任务的处理取舍需保留其检查点，不能删除使用中的媒体。此次报告没有更改这些任务。

### 错误日志不足以还原所有根因

当前 `video_worker.run_worker` 的 failure.json 只保存 stage/error_code，每次失败覆盖旧文件；journal 一般仅记 job_id/error_code。`CCRProvider.json` 合并网络、HTTP 与事件解析异常，`local_encoder` 合并网络与响应解析异常，`ffmpeg` 没有保存 returncode/stderr。因此 CCR_REQUEST_FAILED、LOCAL_ENCODER_UNAVAILABLE、ImportError 和早期通用异常无法从当前保留记录精确到唯一底层根因。

解决方案：新增只追加的 task_error_events，记录失败时间、state/stage、operation、异常类型、脱敏 cause/traceback、HTTP status 或 FFmpeg returncode、是否发送请求/完成响应及恢复动作。私有日志权限受控，删除 token、cookie、Authorization、带签名的来源 URL 及完整响应正文；告警文本只给公开错误摘要。补日志不能恢复已经丢失的旧异常细节。

### 已完成记录不等于新密集策略质量验收

当前 33 个完成任务均没有新 visual_policy/analysed_frame_count 记录；它们保留旧文字或稀疏视觉知识。已经成功删源，因此不能用旧向量补算完整 0.5/1 秒视觉覆盖。新密集策略已通过隔离合成视频的真实模型链路，但本所有者队列尚未发布带新密集覆盖记录的真实任务；10 个不同来源/60 个人工核实问题的最终质量验收也未完成。

解决方案：先让后续符合时长的新视频进入新流程并记录实际帧数、分批恢复和事实质量；历史需要升级时重新提供可用原视频或单独明确允许再下载，以新版本增补知识、保留旧事实与引用。不能把已完成任务改回失败，也不能无来源生成视频 embedding。

## 历史故障按功能逐项诊断

以下范围为 journal、failure.json 及原受阻快照能够确认的全部 54 个任务。一个任务可能在不同阶段遇到多个错误，因此各功能表的人数不可直接相加。当前 state/error_code 优先于历史错误；complete 的旧错误已经恢复，queued 的旧错误已清除并等待重跑。

### VISUAL_TIME_BUDGET_EXCEEDED

涉及 2 条任务；当前分布：已完成 1，处理中 1。

**发生功能：** src/rag_favorite/video_provider.py · CCRProvider.check_budget / start_visual_budget；video_worker.process 切片循环。

**原因与机制：** 旧配置将单任务累计视觉处理耗时限制为 600 秒。启动时读取 provider-usage.jsonl 的历史视觉耗时，再加本次运行 elapsed time；达到阈值会在下一个切片或 SSE 事件读取中抛错。这里的 600 秒是处理耗时，不是视频长度；它和新的“原视频超过 600 秒不做视觉”是两个独立规则。

**证据与边界：** 系统 journal 的该错误码、原受阻快照、时间预算解除记录。已保存切片可恢复，达到预算不表示此前切片或数据库发生损坏。 机制及两条任务的历史预算错误已确认；当前预算已取消。

**解决及恢复：** 已将当前默认及所有者 visual_budget_seconds 设为 0，并用 retry --visual-seconds 0 恢复同一任务编号，不清零用量账本。视频 >600 秒还会跳过视觉并重新产生符合文字模态的检查点。等待 running 任务完整发布与清理；不要将旧 sparse 检查点数量当作新模态进度。

| 提交序号 | 内容 | 任务编号 | 当前状态与阶段 | 现在保存的结果 | 下一步 |
|---|---|---|---|---|---|
| 4 | 这样做简单好吃入味 | 3aa899a2-bf1e-49ec-8046-dd12619d1eb8 | complete / complete | 已发布 7 切片，删源通过 | 无需重复处理；保留历史记录 |
| 26 | 社交场上的潜规则大合集 | ea9ce021-a020-4414-a89b-20de00009b1b | running / extract:21 | 21 个切片检查点，未完整发布 | 正在按现行规则续跑，完成后发布与清理 |

### TRANSCRIPT_REQUIRED_NO_SPEECH

涉及 3 条任务；当前分布：已完成 3。

**发生功能：** imagebind_server.py · transcribe → video_provider.local_encoder → video_worker.transcribe_chunks / prepare_transcript。

**原因与机制：** 本地 Faster Whisper 转录返回零条语音，/transcribe 使用 422 表达没有检测到语音，客户端转为 TRANSCRIPT_REQUIRED_NO_SPEECH。旧 prepare_transcript 没有视觉专用分支，直接把没有语音当成无法入库；这是流程缺口，不是 embedding 坏了，也不等于视频没有字幕或没有知识。

**证据与边界：** 三条任务 failure.json 均定位到 transcript:1/1；修复后的持久 record 为 no_speech_visual_only 并有本地视频向量与证据图。 故障阶段、异常转换与恢复结果已确认；无语音检测不等同于人工审核影片是否含语音。

**解决及恢复：** 已增加“无音轨/确认没有语音 → 空转录、真实视频时间轴 → 视觉分析”的分支。新任务 ≤600 秒以 0.5 秒间隔分析，>600 秒只用明确来源的作者正文，没有正文则明确受阻。ASR 网络或服务故障不会伪装成无语音。三条任务已恢复完成。

| 提交序号 | 内容 | 任务编号 | 当前状态与阶段 | 现在保存的结果 | 下一步 |
|---|---|---|---|---|---|
| 5 | 温莎公爵最喜欢，蒙巴顿每次必点的菜 | 63106943-707f-4a50-afe2-c3d3f14d2c81 | complete / complete | 已发布 5 切片，删源通过 | 无需重复处理；保留历史记录 |
| 11 | 两人三餐｜在家给老婆做超下饭的公瑾爆蛋 | 0a96aef8-0b4e-4966-9c88-503d1c68904c | complete / complete | 已发布 1 切片，删源通过 | 无需重复处理；保留历史记录 |
| 23 | 天冷就爱这一口🥣上海家常正宗老上海罗宋汤 | 4ee71fc2-51d7-4bf6-8ab5-5716f5f95c8b | complete / complete | 已发布 3 切片，删源通过 | 无需重复处理；保留历史记录 |

### XHS_LOGIN_EXPIRED_OR_NOTE_UNAVAILABLE

涉及 4 条任务；当前分布：已完成 1，等待处理 3。

**发生功能：** src/rag_favorite/xhs_note.py · parse_note（候选笔记筛选和标题回退）；video_sources.fetch_note 包装异常。

**原因与机制：** 四条页面的 __INITIAL_STATE__ 中目标 noteId 存在，正文和 video 存在，但独立 title 为空。旧候选条件要求 title，导致真实笔记被排除，parse_note 抛“requested note unavailable”；fetch_note 将它包装成同时含登录过期/内容不可用的泛化错误码。该错误码在这四个具体案例中不是 Cookie 过期证据。

**证据与边界：** source-failure-diagnostics-20261004.json 保留每个笔记匹配对象及 has_title=false，正文分别为 164、41、88、103 字，video=true；修复后四条均真实读取成功。 四条根因已逐页验证；不据此保证其他未来登录错误也同因。

**解决及恢复：** 已修改筛选条件：匹配 noteId 且至少有标题、正文、video 或 imageList；无标题用正文第一行或笔记 ID。仍拒绝只有裸 ID 的占位对象，媒体仍限可信 CDN。雪王已完成；其余三条重新排队后尚未轮到 worker，不能把“可读取”写成已完成。无需为这次解析错误重复登录。

| 提交序号 | 内容 | 任务编号 | 当前状态与阶段 | 现在保存的结果 | 下一步 |
|---|---|---|---|---|---|
| 13 | 雪王冰城配方分享\|\|\|#配方分享 #配方 #蜜雪冰城 #蜜雪 #蜜雪冰城奶茶 # | ad0f6b83-f00a-4523-9dcd-1e6a90ae10dc | complete / complete | 已发布 1 切片，删源通过 | 无需重复处理；保留历史记录 |
| 24 | 外面卖好几十一个的泡芙，咱自己在家就能做，个个都是大空心，又香又酥的，比买的还好 | 6caca8a7-783e-4660-ab13-de651a34de5f | queued / recovered | 尚未成功入库；排队第 1 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 27 | 麻辣鲜香，黏黏糊糊真的巨香，在家做美食想放什么就放什么，轻松做出好吃的麻辣拌！# | 860b6d86-41d7-42b2-a8e2-5e941e69094b | queued / queued | 尚未成功入库；排队第 2 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 71 | 18分钟，创业必修课：普通人如何快速具备创业的基础必备思维➕财商观念，必须看完的 | 343bb6f9-7ea4-456f-a909-9e1f423775c2 | queued / queued | 尚未成功入库；排队第 3 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |

### CCR_REQUEST_FAILED

涉及 1 条任务；当前分布：已完成 1。

**发生功能：** src/rag_favorite/video_provider.py · CCRProvider.json，urllib.request.urlopen 与 SSE 读取/JSON 解析；调用点为 video_worker.process 的知识抽取。

**原因与机制：** 《滑蛋鸡排饭》在 extract:1 调用 CCR 的抽取请求失败。当前代码在网络 OSError、HTTPError（其属于 OSError）或 SSE 事件解析 ValueError 时返回同一错误码。旧记录没有 HTTP 状态、底层 errno、是否已建立连接或 SSE 事件详情，因此无法精确判定是断线、超时、HTTP 拒绝还是事件格式异常，也没有证据说 API key 缺失。

**证据与边界：** failure.json 的 extract:1 / CCR_REQUEST_FAILED、journal 错误事件；同一任务随后 complete，数据库切片及证据摘要核验通过。 抽取功能与故障切片明确；具体传输根因没有被旧日志保存。

**解决及恢复：** 这条任务已按同一编号恢复并完成，无需再导入。若复发，先定位 CCRProvider.json 的连接/读取/事件解析哪个子步骤，再检查对应的路由器请求记录或 HTTP 状态；保留失败请求用量和已完成检查点，进行有限重试。日志应新增脱敏的 operation、HTTP status、exception type、completion 状态，不清账本或无条件重置 key。

| 提交序号 | 内容 | 任务编号 | 当前状态与阶段 | 现在保存的结果 | 下一步 |
|---|---|---|---|---|---|
| 22 | 滑蛋鸡排饭，巨下饭 | 5349109c-2373-42af-8562-faf19be9b3bc | complete / complete | 已发布 4 切片，删源通过 | 无需重复处理；保留历史记录 |

### LOCAL_ENCODER_UNAVAILABLE

涉及 1 条任务；当前分布：已完成 1。

**发生功能：** src/rag_favorite/video_provider.py · local_encoder；video_worker.transcribe_chunks → /transcribe。

**原因与机制：** 《社交场上的潜规则大合集》的较早任务在 transcript:1/9 调用本地编码器失败；这条操作是本地 ASR，不是 ImageBind 视频 embedding。客户端将非 HTTP 的网络 OSError 或响应解析 ValueError 包装为 LOCAL_ENCODER_UNAVAILABLE。旧日志没有保留 connect refused、超时或 JSON 解析的底层信息，不能武断归因到显存不足或模型权重损坏。

**证据与边界：** 该任务 failure.json 为 transcript:1/9 / LOCAL_ENCODER_UNAVAILABLE，当前已完成；现在编码服务 active，新的同源任务已取得 869 条转录并继续文字抽取。 历史操作与当前恢复结果确认；旧底层网络错误未知。

**解决及恢复：** 历史任务已完成。复发时检查 rag-favorite-imagebind.service、loopback /health 的配置空间一致性和 ASR /transcribe 子功能；以返回的 HTTP/网络异常分别排障，恢复服务后重试同一任务，复用 300 秒 ASR chunk 检查点。不能通过把服务故障改成“无语音”绕过。

| 提交序号 | 内容 | 任务编号 | 当前状态与阶段 | 现在保存的结果 | 下一步 |
|---|---|---|---|---|---|
| 7 | 社交场上的潜规则大合集 | 8abed680-7976-4f12-b52d-a4e37516dc21 | complete / complete | 已发布 44 切片，删源通过 | 无需重复处理；保留历史记录 |

### ImportError

涉及 41 条任务；当前分布：等待处理 36，已完成 5。

**发生功能：** video_worker.run_worker 中 process 调用异常捕获；执行尚处 queued 阶段，发生于 Python 模块/符号导入、进入 fetch_source 之前。

**原因与机制：** journal 确认代码更新期间 41 个任务出现 ImportError，当时阶段仍为 queued，尚未读取目标笔记和下载视频。failure.json 通常只保存 ImportError 类名，不记录具体模块、缺失符号或 traceback；因此无法诚实地精确到某个 import 语句，不能把“共享浏览器锁相关”推断写成已确认根因。

**证据与边界：** 41 个不同任务 ID 的 journal ImportError 事件；其中 40 个最新 failure.json 仍为 ImportError，另一个“麻辣拌”随后遇到标题解析错误，最新文件已覆盖旧错误。autonomous-progress.md 记录了当时的 41 条恢复操作。 影响数量、发生阶段和恢复操作确认；具体缺失符号未被记录，现有证据不足。

**解决及恢复：** 当前源模块及共享浏览器锁已能导入，41 条已用既有 retry_job 重排；5 条完成，36 条仍等候，其中麻辣拌还记录过后续解析故障。若再发生，应先停止 worker，完成代码原子更新，在实际服务 venv 中做 worker/source/lock 模块导入检查，再启动；日志需保存脱敏 traceback 和 import 模块名。仅通过重新排队不能永久修复缺失符号。

| 提交序号 | 内容 | 任务编号 | 当前状态与阶段 | 现在保存的结果 | 下一步 |
|---|---|---|---|---|---|
| 27 | 麻辣鲜香，黏黏糊糊真的巨香，在家做美食想放什么就放什么，轻松做出好吃的麻辣拌！# | 860b6d86-41d7-42b2-a8e2-5e941e69094b | queued / queued | 尚未成功入库；排队第 2 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 28 | 没有正反馈时，如何保持极高的执行力？ | 2abab64b-c4ee-45b6-964f-de14ebc741ef | complete / complete | 已发布 5 切片，删源通过 | 无需重复处理；保留历史记录 |
| 29 | 如果ai设计得总是很丑，试试这个skills | 157c1756-8e86-449f-a819-9b6ce990fc8b | complete / complete | 已发布 2 切片，删源通过 | 无需重复处理；保留历史记录 |
| 30 | 七种不同的下拉框 | 24f720b9-051e-4fe0-a022-372053bac6c9 | complete / complete | 已发布 2 切片，删源通过 | 无需重复处理；保留历史记录 |
| 31 | 一周时间速通leetcode hot到达面试水平 | 66556614-eefc-4959-9557-20cfed1ea6ad | complete / text_only | 正文 1 chunk 已索引 | 无需重复处理；保留历史记录 |
| 32 | 绝命毒师铁粉朝圣路线 | ccad4071-b917-4f29-b2e6-42da25b49b83 | complete / text_only | 正文 1 chunk 已索引 | 无需重复处理；保留历史记录 |
| 33 | 让某宝某鱼虚拟资料卖家原地失业的网站 | af1a7b17-4515-4817-b8ba-9283a4ddbbe8 | queued / recovered | 尚未成功入库；排队第 4 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 34 | 一个Skill让Agent操作浏览器，告别重复任务 | 16afe22f-f7ef-44b2-be27-4422be91e1b4 | queued / queued | 尚未成功入库；排队第 5 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 35 | 被3w人看过的Codex找工作流 后续来了 | 5e13a3af-f0cd-4895-a651-ec440e17955a | queued / queued | 尚未成功入库；排队第 6 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 36 | 领英求职野路子第一集：如何找到高质量人脉 | 8d3b27c3-6a30-451a-9e45-21bcd01767f4 | queued / queued | 尚未成功入库；排队第 7 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 37 | 减脂期春夏必吃的第一碗面！低卡鸡丝凉面！ | fc1a3d4b-a8f1-41a8-a1aa-9dfa059a9453 | queued / queued | 尚未成功入库；排队第 8 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 38 | 减脂期必吃榜🔥 268卡蚂蚁上树🍝 | 8e0c6c3c-81cb-4171-bd73-d3ef590074c3 | queued / queued | 尚未成功入库；排队第 9 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 39 | 治愈系一锅端\|法式无水菌菇炖鸡 | 492de922-7cf1-43ca-8971-3df3bb1bf07f | queued / queued | 尚未成功入库；排队第 10 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 40 | 无聊来整理点uiuc有趣好玩的课 | a40fea27-85e9-4acf-b220-23ab5a107ca2 | queued / queued | 尚未成功入库；排队第 11 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 41 | 🔥我把蘑菇做成了米其林！ | 5338302b-b939-4f98-8e46-d8518df02fc4 | queued / queued | 尚未成功入库；排队第 12 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 42 | 绝味皮蛋炒饭‼️太好吃了大家一定要试试‼️ | d43cf034-3998-46b1-8478-2eabeeccc021 | queued / queued | 尚未成功入库；排队第 13 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 43 | 只需三种食材，爆炸好吃！ | c795ae5f-c94f-4b1c-8608-8aa45c63f924 | queued / queued | 尚未成功入库；排队第 14 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 44 | 餐厅级粉嫩厚切牛舌，秘诀全在这条视频里！ | 331caa43-4edf-4c43-a2ba-540e1eef8207 | queued / queued | 尚未成功入库；排队第 15 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 45 | 减脂期的快乐就是吃好吃的😍😍！！！ | ac4be46e-1579-40a3-baf5-38fd8948fe8f | queued / queued | 尚未成功入库；排队第 16 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 46 | 10分钟搞定夏日清爽减脂餐｜金枪鱼意面沙拉 | 7b0f3478-e338-49d2-aeb5-f9ab2f2428c2 | queued / queued | 尚未成功入库；排队第 17 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 47 | 夏季轻断食必喝刷脂神汤！一锅200多大卡！ | 5fd4be9a-28f5-412c-b62e-7b3572ec64f9 | queued / queued | 尚未成功入库；排队第 18 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 48 | 无油减脂菜一锅出🥢 | ed75afbb-12bc-47cc-9477-c5eeebeceef5 | queued / queued | 尚未成功入库；排队第 19 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 49 | 盐水牛肉，是如何被卷到发光的！？ | 958c1820-f3af-41cd-b73d-a02498ca55e1 | queued / queued | 尚未成功入库；排队第 20 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 50 | 挑战uiuc去ord无敌穷鬼攻略 | 596246e2-6b76-479a-8a1b-fdd5fae791e7 | queued / queued | 尚未成功入库；排队第 21 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 51 | 鸡胸肉这么做！减脂期的幸福感直接拉满了！ | 32de42a0-f562-4a3c-a985-7a5849c9ee62 | queued / queued | 尚未成功入库；排队第 22 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 52 | 一张图讲清楚coffee chat如何让对方喜欢你 | 6c4158aa-6956-4481-8f86-7e69302bbbba | queued / queued | 尚未成功入库；排队第 23 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 53 | 竟然已经有了2k | 8b37dc66-8913-4cd3-b884-4f0ba1b53212 | queued / queued | 尚未成功入库；排队第 24 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 54 | FreeDomain让人人拥有域名，狂揽17.2w星标 | b8c06b16-2ff7-425a-9eea-c87a5bf7c017 | queued / queued | 尚未成功入库；排队第 25 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 55 | 原来可以一口气找到我查了几天的信息 | 78f09e84-4b30-4327-96cf-ce0b729599c4 | queued / queued | 尚未成功入库；排队第 26 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 56 | 🇺🇸大沼泽国家公园经典一日自驾行程 | b7a1ea45-d816-47f2-b637-02a8a7663b22 | queued / queued | 尚未成功入库；排队第 27 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 57 | 莴笋炒肉 | 3142c649-4a74-42e7-84f1-50b7eed70d10 | queued / queued | 尚未成功入库；排队第 28 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 58 | #虾仁豆腐蒸蛋 鲜香嫩滑，QQ弹弹好吃解馋还没啥负担！#减脂期 #美食制作 | 4d543d8f-ba18-4f12-85ba-2069e619870f | queued / queued | 尚未成功入库；排队第 29 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 59 | 可以帮你完美复刻大厂UI设计开源项目它来了 | 9e8390a1-6461-4552-9610-9c3417560cc9 | queued / queued | 尚未成功入库；排队第 30 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 60 | 🇬🇧留学生快手饭🍅15分钟搞定的辣番茄意面 | ff83b3da-a006-42b7-ad52-9d4e03378300 | queued / queued | 尚未成功入库；排队第 31 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 61 | 费大厨辣椒炒肉，加了脆嫩的白玉木耳，太好吃 | 9c702023-2199-470d-8f3d-88cfc718838d | queued / queued | 尚未成功入库；排队第 32 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 62 | 这绝对是热干面最好吃的做法！ | f713168f-dce9-4c58-bd8b-81a16a6add55 | queued / queued | 尚未成功入库；排队第 33 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 63 | 这个网站可以一键洗掉你文章里的AI机器味儿 | 44d65a33-e9da-4fe4-98c7-c5be02e9089a | queued / queued | 尚未成功入库；排队第 34 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 64 | 美国搬家地址变更报备清单｜一张表搞定！ | a34669b7-9300-4782-baa9-ba71bc3fd73e | queued / queued | 尚未成功入库；排队第 35 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 65 | 餐厅同款寿司醋教程，厨师长的秘方 | 46ab6f17-c1a4-4333-9d07-200631480d3b | queued / queued | 尚未成功入库；排队第 36 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 66 | 网红生巧蛋糕终于被我复刻出来了，入口即化 | 40d0923b-8a52-4d5f-b1bd-1dd2aa8b69aa | queued / queued | 尚未成功入库；排队第 37 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |
| 67 | 将word文档转换为手写字体！非常自然！ | d37a9f8c-f53d-4fb4-9513-718a4f24ec22 | queued / queued | 尚未成功入库；排队第 38 位 | 修复已部署，等待 worker 顺序处理；成功前不计完成 |

### ProviderUnavailable

涉及 3 条任务；当前分布：已完成 3。

**发生功能：** 早期 worker 捕获 provider 异常的通用错误码；无法从该码区分 local_encoder 和 CCRProvider.json。

**原因与机制：** 早期三个任务的 journal 只保留 ProviderUnavailable，而不是具体操作码：过年 C 位菜、皮蛋豆腐、戴安娜牛排。这个异常类可表示来源、ASR、模型请求或预算不可用；没有当时阶段及 traceback，无法把这几次事件精确归到一个 provider 子功能。戴安娜牛排另有后来的明确 no_speech 记录，不能拿后来的错误倒推出第一次必然同因。

**证据与边界：** journal 记录三个任务的五次 ProviderUnavailable 事件；三条现在均完成。 异常事件与已恢复状态确认；第一次故障子功能不可恢复，已明确标为未知。

**解决及恢复：** 无需重做已完成导入。后续异常必须给 ProviderUnavailable 设置稳定 operation/error_code，并保留阶段、脱敏底层错误和请求是否完成。先根据具体来源/ASR/CCR 端点检查，再重试；不能统一解释成“网络不好”或“缺少 API”。

| 提交序号 | 内容 | 任务编号 | 当前状态与阶段 | 现在保存的结果 | 下一步 |
|---|---|---|---|---|---|
| 1 | 过年C位菜🧨搅一搅搞定❗大人小孩都爱吃🔥🔥 | 5cbfcb11-b2ce-4517-a44b-2a367024b0de | complete / complete | 已发布 2 切片，删源通过 | 无需重复处理；保留历史记录 |
| 2 | 过年必备小凉菜第五道皮蛋豆腐 | 20567f31-1225-4f96-bd6b-91e061ede46f | complete / complete | 已发布 2 切片，删源通过 | 无需重复处理；保留历史记录 |
| 5 | 温莎公爵最喜欢，蒙巴顿每次必点的菜 | 63106943-707f-4a50-afe2-c3d3f14d2c81 | complete / complete | 已发布 5 切片，删源通过 | 无需重复处理；保留历史记录 |

### RuntimeError

涉及 1 条任务；当前分布：已完成 1。

**发生功能：** 《蒜香鸡排》早期任务的通用运行错误；旧记录未保留发生函数或阶段。

**原因与机制：** journal 有两次 RuntimeError 事件，但没有 traceback、FFmpeg returncode/stderr 或发布/清理阶段。当前程序中 FFmpeg 处理失败和持久化/证据/清理校验失败都可能抛 RuntimeError，仅凭异常名不能确定是哪个。该任务现在 complete，已核验数据库切片、截图摘要与原视频清理。

**证据与边界：** 两条 journal RuntimeError 事件对应同一任务 ID；历史详细失败文件缺失，当前完成性检查无问题。 历史异常和当前恢复确认；当时函数未知，不能补编根因。

**解决及恢复：** 这条已恢复任务无需重跑。若再次出错，为 ffmpeg 保存 returncode 与脱敏 stderr 尾段，为 publish/cleanup 保存明确验证项和 traceback；先确定函数，再按解码失败、证据缺失或清理失败分别修复。published 清理失败应维持 published 并只重试清理，不能重新模型抽取或提前删源。

| 提交序号 | 内容 | 任务编号 | 当前状态与阶段 | 现在保存的结果 | 下一步 |
|---|---|---|---|---|---|
| 3 | 蒜香鸡排超详细教程！做法简单巨好吃 | 809ad78e-78ce-4967-aa1b-be713df2d09d | complete / complete | 已发布 9 切片，删源通过 | 无需重复处理；保留历史记录 |

## 全部等待队列

下表按 worker 的 priority 降序、created_at、job_id 排列，共 173 条。“历史错误”不代表当前仍受阻；当前 error_code 均为空。排名是本次快照的顺序，新的高优先级任务、重试和完成会改变它。后 135 条没有查到历史失败，不应把“未处理”解释成“失败”。

| 等待次序 | 提交序号 | 内容 | 任务编号 | 当前状态 | 历史错误 |
|---|---|---|---|---|---|
| 1 | 24 | 外面卖好几十一个的泡芙，咱自己在家就能做，个个都是大空心，又香又酥的，比买的还好 | 6caca8a7-783e-4660-ab13-de651a34de5f | queued / recovered | XHS_LOGIN_EXPIRED_OR_NOTE_UNAVAILABLE |
| 2 | 27 | 麻辣鲜香，黏黏糊糊真的巨香，在家做美食想放什么就放什么，轻松做出好吃的麻辣拌！# | 860b6d86-41d7-42b2-a8e2-5e941e69094b | queued / queued | ImportError, XHS_LOGIN_EXPIRED_OR_NOTE_UNAVAILABLE |
| 3 | 71 | 18分钟，创业必修课：普通人如何快速具备创业的基础必备思维➕财商观念，必须看完的 | 343bb6f9-7ea4-456f-a909-9e1f423775c2 | queued / queued | XHS_LOGIN_EXPIRED_OR_NOTE_UNAVAILABLE |
| 4 | 33 | 让某宝某鱼虚拟资料卖家原地失业的网站 | af1a7b17-4515-4817-b8ba-9283a4ddbbe8 | queued / recovered | ImportError |
| 5 | 34 | 一个Skill让Agent操作浏览器，告别重复任务 | 16afe22f-f7ef-44b2-be27-4422be91e1b4 | queued / queued | ImportError |
| 6 | 35 | 被3w人看过的Codex找工作流 后续来了 | 5e13a3af-f0cd-4895-a651-ec440e17955a | queued / queued | ImportError |
| 7 | 36 | 领英求职野路子第一集：如何找到高质量人脉 | 8d3b27c3-6a30-451a-9e45-21bcd01767f4 | queued / queued | ImportError |
| 8 | 37 | 减脂期春夏必吃的第一碗面！低卡鸡丝凉面！ | fc1a3d4b-a8f1-41a8-a1aa-9dfa059a9453 | queued / queued | ImportError |
| 9 | 38 | 减脂期必吃榜🔥 268卡蚂蚁上树🍝 | 8e0c6c3c-81cb-4171-bd73-d3ef590074c3 | queued / queued | ImportError |
| 10 | 39 | 治愈系一锅端\|法式无水菌菇炖鸡 | 492de922-7cf1-43ca-8971-3df3bb1bf07f | queued / queued | ImportError |
| 11 | 40 | 无聊来整理点uiuc有趣好玩的课 | a40fea27-85e9-4acf-b220-23ab5a107ca2 | queued / queued | ImportError |
| 12 | 41 | 🔥我把蘑菇做成了米其林！ | 5338302b-b939-4f98-8e46-d8518df02fc4 | queued / queued | ImportError |
| 13 | 42 | 绝味皮蛋炒饭‼️太好吃了大家一定要试试‼️ | d43cf034-3998-46b1-8478-2eabeeccc021 | queued / queued | ImportError |
| 14 | 43 | 只需三种食材，爆炸好吃！ | c795ae5f-c94f-4b1c-8608-8aa45c63f924 | queued / queued | ImportError |
| 15 | 44 | 餐厅级粉嫩厚切牛舌，秘诀全在这条视频里！ | 331caa43-4edf-4c43-a2ba-540e1eef8207 | queued / queued | ImportError |
| 16 | 45 | 减脂期的快乐就是吃好吃的😍😍！！！ | ac4be46e-1579-40a3-baf5-38fd8948fe8f | queued / queued | ImportError |
| 17 | 46 | 10分钟搞定夏日清爽减脂餐｜金枪鱼意面沙拉 | 7b0f3478-e338-49d2-aeb5-f9ab2f2428c2 | queued / queued | ImportError |
| 18 | 47 | 夏季轻断食必喝刷脂神汤！一锅200多大卡！ | 5fd4be9a-28f5-412c-b62e-7b3572ec64f9 | queued / queued | ImportError |
| 19 | 48 | 无油减脂菜一锅出🥢 | ed75afbb-12bc-47cc-9477-c5eeebeceef5 | queued / queued | ImportError |
| 20 | 49 | 盐水牛肉，是如何被卷到发光的！？ | 958c1820-f3af-41cd-b73d-a02498ca55e1 | queued / queued | ImportError |
| 21 | 50 | 挑战uiuc去ord无敌穷鬼攻略 | 596246e2-6b76-479a-8a1b-fdd5fae791e7 | queued / queued | ImportError |
| 22 | 51 | 鸡胸肉这么做！减脂期的幸福感直接拉满了！ | 32de42a0-f562-4a3c-a985-7a5849c9ee62 | queued / queued | ImportError |
| 23 | 52 | 一张图讲清楚coffee chat如何让对方喜欢你 | 6c4158aa-6956-4481-8f86-7e69302bbbba | queued / queued | ImportError |
| 24 | 53 | 竟然已经有了2k | 8b37dc66-8913-4cd3-b884-4f0ba1b53212 | queued / queued | ImportError |
| 25 | 54 | FreeDomain让人人拥有域名，狂揽17.2w星标 | b8c06b16-2ff7-425a-9eea-c87a5bf7c017 | queued / queued | ImportError |
| 26 | 55 | 原来可以一口气找到我查了几天的信息 | 78f09e84-4b30-4327-96cf-ce0b729599c4 | queued / queued | ImportError |
| 27 | 56 | 🇺🇸大沼泽国家公园经典一日自驾行程 | b7a1ea45-d816-47f2-b637-02a8a7663b22 | queued / queued | ImportError |
| 28 | 57 | 莴笋炒肉 | 3142c649-4a74-42e7-84f1-50b7eed70d10 | queued / queued | ImportError |
| 29 | 58 | #虾仁豆腐蒸蛋 鲜香嫩滑，QQ弹弹好吃解馋还没啥负担！#减脂期 #美食制作 | 4d543d8f-ba18-4f12-85ba-2069e619870f | queued / queued | ImportError |
| 30 | 59 | 可以帮你完美复刻大厂UI设计开源项目它来了 | 9e8390a1-6461-4552-9610-9c3417560cc9 | queued / queued | ImportError |
| 31 | 60 | 🇬🇧留学生快手饭🍅15分钟搞定的辣番茄意面 | ff83b3da-a006-42b7-ad52-9d4e03378300 | queued / queued | ImportError |
| 32 | 61 | 费大厨辣椒炒肉，加了脆嫩的白玉木耳，太好吃 | 9c702023-2199-470d-8f3d-88cfc718838d | queued / queued | ImportError |
| 33 | 62 | 这绝对是热干面最好吃的做法！ | f713168f-dce9-4c58-bd8b-81a16a6add55 | queued / queued | ImportError |
| 34 | 63 | 这个网站可以一键洗掉你文章里的AI机器味儿 | 44d65a33-e9da-4fe4-98c7-c5be02e9089a | queued / queued | ImportError |
| 35 | 64 | 美国搬家地址变更报备清单｜一张表搞定！ | a34669b7-9300-4782-baa9-ba71bc3fd73e | queued / queued | ImportError |
| 36 | 65 | 餐厅同款寿司醋教程，厨师长的秘方 | 46ab6f17-c1a4-4333-9d07-200631480d3b | queued / queued | ImportError |
| 37 | 66 | 网红生巧蛋糕终于被我复刻出来了，入口即化 | 40d0923b-8a52-4d5f-b1bd-1dd2aa8b69aa | queued / queued | ImportError |
| 38 | 67 | 将word文档转换为手写字体！非常自然！ | d37a9f8c-f53d-4fb4-9513-718a4f24ec22 | queued / queued | ImportError |
| 39 | 73 | 蒜苗炒腊肉，这样做不会那么咸，也不会油腻腻的那种#家常菜 #腊肉 #蒜苗炒腊肉 | 54882802-e977-4038-b188-65d511c1dc3f | queued / queued | 未查到 |
| 40 | 74 | 开源文档提取神器！ | 3c1967ef-27a3-4760-8f3b-62ef42a82758 | queued / queued | 未查到 |
| 41 | 75 | 真正厉害的人，都有滴水不漏的“城府修炼术” | d8d1232b-f5a1-41d4-8a14-7fa296792725 | queued / queued | 未查到 |
| 42 | 76 | 拥有闲谈能力，是社会化程度高的一种表现！ | e30fc98d-dfea-4d09-bfec-da4a6bc17543 | queued / queued | 未查到 |
| 43 | 77 | 酸辣清爽！鸡肉鲜嫩～太适合夏天了！😭😭 | 3d80e8fa-abe6-4e22-91d0-05d6773a48ae | queued / queued | 未查到 |
| 44 | 78 | 好吃到跺脚把子肉详细教程！做法简单巨好吃 | 483ee59f-0396-4ab9-90f4-9dbcf6e21142 | queued / queued | 未查到 |
| 45 | 79 | 终于找到了Costco大块牛胸肉最好吃的做法！ | 3e425b36-ddb4-47d0-a9e7-df91537bffb1 | queued / queued | 未查到 |
| 46 | 80 | 厨师长教你：“擂椒皮蛋”的家常做法 | 71598e4e-d7e3-46a2-9ef9-66bdf7b2d5a8 | queued / queued | 未查到 |
| 47 | 81 | ADHD /效率控必备 ｜小众宝藏独立开发作品 | f4778ccf-81ff-4a2d-9977-0135fe2c6812 | queued / queued | 未查到 |
| 48 | 82 | 手机也能 VibeCoding | 48aa7056-71e3-451e-8b72-5d6b33e4a587 | queued / queued | 未查到 |
| 49 | 83 | 冬去春来蚕豆饭 | d1462816-66f4-4d91-8b1d-c4369fe46230 | queued / queued | 未查到 |
| 50 | 84 | 松露雷笋菜饭 | 4efb4dfc-d0e5-428c-93c2-8fb493e88906 | queued / queued | 未查到 |
| 51 | 85 | 干锅土豆鸡翅超详细教程！做法简单巨好吃 | 1b15d8ae-c5ef-4912-8c54-bebbfc134593 | queued / queued | 未查到 |
| 52 | 86 | 让短剧漫剧工作者集体失业的ai神器 | 2cf35ee8-d2ca-4407-819d-2d5305889b26 | queued / queued | 未查到 |
| 53 | 87 | 是谁还不会用AI提示词绘图、写文章？😎😎 | f9235424-138d-4837-a2af-06bdee2654dc | queued / queued | 未查到 |
| 54 | 88 | 一个自称免费不限量生成AI视频的网站 | dbd42ffb-ffad-4183-b4e3-c412c94c12c8 | queued / queued | 未查到 |
| 55 | 89 | 批量短视频”的桌面端 AI 工具 | 160280d5-ea04-40b2-8dd5-d5fbc539604b | queued / queued | 未查到 |
| 56 | 90 | AI帮你做内容营销，帮你赚 | eaf525ab-05cc-4164-8e9f-acb786124fbd | queued / queued | 未查到 |
| 57 | 91 | 💰 赚美金不香吗？下班开启副业模式 | 1d6b0a75-4c42-43cd-86e5-2cebd0022a69 | queued / queued | 未查到 |
| 58 | 92 | 非常香辣下饭的小炒鸡 | 9e4d6957-3b76-4831-8db0-9b785bd3dd24 | queued / queued | 未查到 |
| 59 | 93 | 让某鱼卖家集体破防的自动卖货助手 | b095a2ad-36b1-47e8-aa59-80db9e26bd68 | queued / queued | 未查到 |
| 60 | 94 | 家的味道！山珍海味也不换🤩\| 上海咸肉菜饭 | a2518b7a-bcfc-4874-9ca1-5178441b0e32 | queued / queued | 未查到 |
| 61 | 95 | 编程救星，不让AI瞎改代码，可视化代码、函数、依赖关系、调用链等 #开源 #gi | 0010c6ad-4009-4804-9a1e-334ee2001b18 | queued / queued | 未查到 |
| 62 | 96 | openclaw的十个真实用列 | eab0eec2-6027-407e-a1ab-f37534aa027d | queued / queued | 未查到 |
| 63 | 97 | 金灿灿的～黄芽菜肉丝春卷｜皮脆馅馅太好吃啦 | 5ed54194-a01b-4627-9e58-24d13a17dab4 | queued / queued | 未查到 |
| 64 | 98 | 我也太会做辣子鸡了吧‼️年夜饭安排上🧨 | 2fd546a1-433e-4ac8-be20-b600086e707a | queued / queued | 未查到 |
| 65 | 99 | 上海土著 烂糊肉丝春卷 | 6898a023-e66d-4c16-b3c9-bc757948f33b | queued / queued | 未查到 |
| 66 | 100 | 酸甜浓郁！营养丰富🤩\| 满配版上海罗宋汤🍅 | 8b90c990-669b-442e-82fd-8336a24b391e | queued / queued | 未查到 |
| 67 | 101 | ClawWork：让 OpenClaw 懂经济学为你赚钱 | 1f8b6062-9e02-44c1-8371-71f594f0ffff | queued / queued | 未查到 |
| 68 | 102 | Nanobrowser:OpenAI Operator的免费平替 | 7cb25ef2-bdf6-4a75-95c3-ba80293b660e | queued / queued | 未查到 |
| 69 | 103 | Chrome+AI扩展，不就是 AI Browser？ | 7a2e4bf5-9a03-45fd-896b-c2d41034a154 | queued / queued | 未查到 |
| 70 | 104 | 不用炒糖色，怎么做到软烂不柴，酸甜适中‼️ | b287df03-1d29-4a69-bf9d-74d8f74a48a8 | queued / queued | 未查到 |
| 71 | 105 | 收藏了1000TB资源的免费电子书网站 | 3d24ca4f-c7b2-4c3e-941e-d95adf68b560 | queued / queued | 未查到 |
| 72 | 106 | 鸡胸肉版鱼香肉丝！香麻了！ | 3c94b80b-a0fc-40fe-a238-4281006cc0ba | queued / queued | 未查到 |
| 73 | 107 | 上海年夜饭 春卷 皮脆爆汁 年夜饭点心做法 | b7790e61-5dcb-44e0-be13-11461026d541 | queued / queued | 未查到 |
| 74 | 108 | 辣子鸡的教程方法 保证一次教会你😉 | 5750b9f1-c1a1-4dbc-afd8-8998159bb7d3 | queued / queued | 未查到 |
| 75 | 109 | 🥔土豆神仙吃法！法式脆烤！一压就OK！ | 1b020d4a-82f4-4bb9-83a2-7120f5534573 | queued / queued | 未查到 |
| 76 | 110 | 480元做年夜饭三菜一汤 | 77064762-2c38-4c82-abac-1ebcfc9534ff | queued / queued | 未查到 |
| 77 | 111 | ㊙️在家解锁西餐厅威士忌熟成牛排做法！ | 646dbc5d-2093-45ae-aaa3-f1f26ec01627 | queued / queued | 未查到 |
| 78 | 112 | 这是一个让卖课博主集体破防的网站 | b6dd6ecc-f53f-4ed5-a158-d4873634b0c5 | queued / queued | 未查到 |
| 79 | 113 | 上汤娃娃菜 | c95f6350-23f7-4f31-9f3a-f354f6dbffa9 | queued / queued | 未查到 |
| 80 | 114 | 无需服务器！用AI免费搭建私人云助手 | a2456c95-761f-4bf0-ac86-c6004c95b000 | queued / queued | 未查到 |
| 81 | 115 | 🇩🇪区年夜饭主理人\|8分钟玉米烙 SOP | ec9bce0f-5009-49d8-a337-2b2d22702a26 | queued / queued | 未查到 |
| 82 | 116 | 45.5K星标开源神器，快速打破信息差 | 68625732-0b3b-4e71-8eec-2d0d8b1af908 | queued / queued | 未查到 |
| 83 | 117 | “用 AI 赚钱” 的实战向资源合集 | 1e1a87cf-c749-4bce-800a-19b917b2c8d2 | queued / queued | 未查到 |
| 84 | 118 | 这个网站能无限生成临时邮箱！ | 4f5af877-deac-4ca0-beaa-9ac05899c50d | queued / queued | 未查到 |
| 85 | 119 | 烧卖这么好吃，怎么能有人不会做？收下这个保姆级教程，比纸皮还薄，在家怎么做怎么好 | 3a4faa1b-5326-4c19-8ab9-6bb986189ee5 | queued / queued | 未查到 |
| 86 | 120 | 做法简单，配料家常‼️超绝西红柿土豆炖牛腩 | d2f6c67c-5917-449f-b184-7efb799b00fc | queued / queued | 未查到 |
| 87 | 121 | 我愿称之为香迷糊界的牌面‼️ | 9cc52428-7f52-428e-b5cf-afaad1159364 | queued / queued | 未查到 |
| 88 | 122 | 在家做出媲美饭店的小炒牛肉‼️ | da96f252-43fe-4d02-be76-a390178df9f3 | queued / queued | 未查到 |
| 89 | 123 | 免烤箱‼️伯爵红茶芝士慕斯\|搅一搅就完成✅ | d7d1e217-5a8b-4da4-b1a7-e5b64a0737d0 | queued / queued | 未查到 |
| 90 | 124 | 年夜饭必备菜糖醋小排，做法简单又好吃！ | 58bcb063-4d33-4951-903a-b1db275da5e6 | queued / queued | 未查到 |
| 91 | 125 | 保罗博古斯名菜🍗羊肚菌奶油炖鸡 | c2dab47a-1efc-42eb-bf78-9a1df9740229 | queued / queued | 未查到 |
| 92 | 126 | 一锅出的快乐！奶fufu蘑菇鸡🍄暖透秋冬 | 79111056-4697-4859-adc3-6a2f08810bd1 | queued / queued | 未查到 |
| 93 | 127 | 🇺🇸留子也喝上了腌笃鲜🥹一口江南的春天 | 6f49f6e5-05e5-4940-82aa-6a025accdb5d | queued / queued | 未查到 |
| 94 | 128 | 瓦罗兰特颜文字 | 8f1ec662-a0c2-45ac-a6cb-37fbed5a89c6 | queued / queued | 未查到 |
| 95 | 129 | 免烤箱‼️ 抹茶大理石慕斯🍃入口即化超丝滑 | d37cef7c-f422-4ac7-a0df-80ed7979c513 | queued / queued | 未查到 |
| 96 | 130 | 能让我一口喝进大海的汤～ | f090393e-33fd-42e8-ab84-12181eda53af | queued / queued | 未查到 |
| 97 | 131 | 漂亮饭丨O'eat西餐厅60道菜品配方sop分享 | 7bc8d74e-c709-4d0a-acbf-4a7b1654ed6a | queued / queued | 未查到 |
| 98 | 132 | 【第38碗面】江南的面 上海双菇面筋面 | 68c562d1-48c2-457a-98d5-02f65c549107 | queued / queued | 未查到 |
| 99 | 133 | 宴客前菜天花板！10分钟搞定米其林同款精致 | f21b109e-f437-4e9c-aa19-8674bd04d572 | queued / queued | 未查到 |
| 100 | 134 | ❗️年夜饭食谱之～红烧鸡肉焖土豆好吃到舔盘 | 7fcc4b7e-89f9-4ae7-85f6-988a163e9adb | queued / queued | 未查到 |
| 101 | 135 | 🔥日本的牛丼饭🇯🇵复刻吉野家牛肉饭✈️爽飞 | dfe96ff1-51f8-4c34-a21f-f11e4d1537ca | queued / queued | 未查到 |
| 102 | 136 | 酸香辣爽❗️泰美味🌴浓郁椰香冬阴功炖鸡 | 43148d6f-b045-487c-ab64-8a8942681b68 | queued / queued | 未查到 |
| 103 | 137 | 不是吧！！自律期你们都不吃炸鸡的吗？ | 33abd2f2-b8a6-4369-8bab-9f4eaaade0a0 | queued / queued | 未查到 |
| 104 | 138 | 自制寿司醋比例 亲测拌的寿司饭好吃 | 317fb10c-7d3b-4523-803b-0b0bf08bdf7b | queued / queued | 未查到 |
| 105 | 139 | 鸡腿最好吃的做法、小炒鸡腿肉 | b8bddf5a-477b-4540-8126-d836b4844d14 | queued / queued | 未查到 |
| 106 | 140 | 用这个浓郁咸鲜的奶油炖鸡给你冬天的温暖 | 11e669d1-b9fa-4a57-987c-b85717544326 | queued / queued | 未查到 |
| 107 | 141 | 法式干邑浓虾汤｜简单易学、有龙虾汤的味道 | dc1ab29a-6065-44a1-a72a-14310197737a | queued / queued | 未查到 |
| 108 | 142 | 就这个盐葱鸡肉好吃也没什么负担#鸡腿的做法 #减脂餐 #懒人美食 | a5ad8529-a34c-466c-bfb5-2ac417df73fe | queued / queued | 未查到 |
| 109 | 143 | 抹茶香柠芝士蛋糕🍋‍🟩酸甜超清爽 0难度❗️ | ac2b15b7-1b14-4986-9c6a-b1f6adc10924 | queued / queued | 未查到 |
| 110 | 144 | 年夜大菜东坡肉‼️肉中极品‼️ | 2ba7f616-0bca-4c2d-b8d6-0a443399490a | queued / queued | 未查到 |
| 111 | 145 | 🔥土豆的颜值天花板！公爵夫人土豆！ | 461b4a6c-1f98-4d8d-85d3-4a6cf0597dbf | queued / queued | 未查到 |
| 112 | 146 | 哇塞！绿色的羊排？超详细法式香草羊排教程 | 2f828acc-2836-4946-81e5-d10ecb4f535d | queued / queued | 未查到 |
| 113 | 147 | 入门级西餐在家轻松做！鲜香浓郁的意式烩饭 | b254191d-20b6-4c2b-a1f3-5bf7a2d655b9 | queued / queued | 未查到 |
| 114 | 148 | 操作极其简单的餐厅精致前菜 | 27952a19-3085-4b65-85df-7517e272fca3 | queued / queued | 未查到 |
| 115 | 149 | 平替版家庭黑松露奶油菌菇饭｜一口沦陷 | a1c5ad64-5262-46eb-974a-a17645f65a30 | queued / queued | 未查到 |
| 116 | 150 | 🔥香煎带子怎么做？鲜嫩焦香 | ec21b9ab-e162-4154-8a40-93cf670b28df | queued / queued | 未查到 |
| 117 | 151 | 0基础在家做法餐｜EP7边角料让菜品颜值飙升 | 41a0f727-6d48-48d6-bd11-20798d9de85d | queued / queued | 未查到 |
| 118 | 152 | 🎉聚会必备｜❾款高颜值西式小食合集4.0教程 | 72e400fa-c4ff-42a4-adb0-3d12bcbe0b00 | queued / queued | 未查到 |
| 119 | 153 | 🇫🇷留学生的圣诞前菜🎄牛油果三文鱼塔可 | e9f4aea7-faef-4c4d-a976-d773a9d0cb46 | queued / queued | 未查到 |
| 120 | 154 | 🇺🇸邪修超简单塔壳，在家做米其林漂亮小食 | 80122b17-ce4c-433a-8f15-e3adf2514cf7 | queued / queued | 未查到 |
| 121 | 155 | 鲜美的菌菇&清新芦笋浓汤 | 8c654212-3e03-4543-ba3b-234be1cd0943 | queued / queued | 未查到 |
| 122 | 156 | 保姆级香煎带子步骤❗️外焦里嫩绝不翻车 | 4dc46c30-1a36-4b41-8a2e-566af59db06d | queued / queued | 未查到 |
| 123 | 157 | 超好喝的车厘子饮品！ | 02803bd7-749f-463b-8354-d27d1d83bdfa | queued / queued | 未查到 |
| 124 | 158 | 没有酒味为什么会醉？ | 5afafe49-d6d6-4f24-b8a6-82431261925e | queued / queued | 未查到 |
| 125 | 159 | 好看精致的摆盘一眼👀就会｜米其林摆盘👩‍🍳 | 32ac9640-cecf-4b26-ad4f-9fcdab79566a | queued / queued | 未查到 |
| 126 | 160 | 可爱暴击！炸虾尾土豆芝士虾球登场🍤 | 01e30518-fb72-4587-b22d-2a7beaf84a01 | queued / queued | 未查到 |
| 127 | 161 | 少女百利甜，专属的微醺甜酒 | 9c93adf1-38db-498e-80f1-af0d5d0de33b | queued / queued | 未查到 |
| 128 | 162 | 中式•微醺特调 \| 苍山负雪❄️ | afc770c8-1ae1-44fa-b055-8f6ecad53dcc | queued / queued | 未查到 |
| 129 | 163 | 🍹 挑战100杯微醺治愈酒 \| 进度97%🧊 | 4c012e41-300a-4161-a890-584a308924c4 | queued / queued | 未查到 |
| 130 | 164 | 🍹 挑战100杯微醺治愈酒 \| 进度35% 🧊 | fd5abad8-6b00-470c-83d1-b85c8c5a1cd7 | queued / queued | 未查到 |
| 131 | 165 | 24 款超火鸡尾酒配方来啦📝 | e1691c88-2d74-40a0-ae57-8a399ac951a5 | queued / queued | 未查到 |
| 132 | 166 | 懒人减脂餐，碳水+蛋白质+膳食纤维全都有！ | e7af9a11-fa1a-4211-a7fc-b2861c7f9d7f | queued / queued | 未查到 |
| 133 | 167 | 太实用啦！发现一个汇聚全球顶尖课程的网站 | 86920299-e869-4ef1-aee0-08fa616f2e07 | queued / queued | 未查到 |
| 134 | 168 | apm对戒｜把「微笑」的摩斯密码藏在指间 | f437c8d0-d575-4477-bcef-7968e0e75ce4 | queued / queued | 未查到 |
| 135 | 169 | 微醺特调｜思绪如水流过 微眠无法入梦🫧 | a51a2be3-782a-4d4d-811b-b21167ca49be | queued / queued | 未查到 |
| 136 | 170 | 只用12周转行MLE！这份路线图让你少走弯路 | 694bb5bb-d994-458b-aa49-71684f28582d | queued / queued | 未查到 |
| 137 | 171 | 6万人关注的“一人公司”开源项目，输入一行需求给你拉一整个软件开发AI团队（产品 | 7a700fba-29f5-4560-904a-6f7267061c79 | queued / queued | 未查到 |
| 138 | 172 | 走路腰痛，有可能是过度扭屁股 | 0b48509c-17f3-4cf4-8305-a7e053ed7bca | queued / queued | 未查到 |
| 139 | 173 | 🔥米其林级法式土豆泥，轻松复刻主厨秘方！ | 9daeb7fc-f97d-4962-8ce2-acad58ba0c37 | queued / queued | 未查到 |
| 140 | 174 | 被爱当然值得记录🧋｜微醺伯牙绝弦 | 9e7328f6-06a4-47d0-b551-73db5997299a | queued / queued | 未查到 |
| 141 | 175 | 酱汁配方分享✅教你做罗勒青酱🌿 | 4bc56ffe-57dd-4f4a-a907-8d69c823fc60 | queued / queued | 未查到 |
| 142 | 176 | 周末在家做一顿漂亮饭要多久？ | 443dd351-02c5-42f0-89c6-229a0ed38a9d | queued / queued | 未查到 |
| 143 | 177 | 90%还原兰州牛肉面味道‼️ | 84afb397-c897-41ad-9cb5-7cbd92f7471a | queued / queued | 未查到 |
| 144 | 178 | 第187: 法式松茸玉米浓汤 | 818ad3e8-401b-4a8d-a38e-0ba43b3ca8e4 | queued / queued | 未查到 |
| 145 | 179 | 十九世纪经典法国菜🔥｜罗西尼牛排 | 6547de58-1aac-42e7-ab7d-5be8278f7b93 | queued / queued | 未查到 |
| 146 | 180 | 🔥人均3000的菜单，我不到500块做出来 | 1d043179-05e3-4d0c-9cb6-4c8fc15a0c26 | queued / queued | 未查到 |
| 147 | 181 | 美区黑五\|第二波开始了，别错过 | dd1f53e7-a72b-4c2d-af36-de5fcc487811 | queued / queued | 未查到 |
| 148 | 182 | 谁能不爱这杯液体小蛋糕🍮 | 0470a7cc-c4dd-4892-92d5-7583568b0d22 | queued / queued | 未查到 |
| 149 | 183 | 被好多人私信问的面汤做法，详细教程来喽！ | 8ac407e4-0f06-410c-ab21-a99b2dfe5156 | queued / queued | 未查到 |
| 150 | 184 | 免油炸‼️我心中鸡翅天花板～酥脆多汁好吃哭！ | 5fa002e4-206e-49b2-9315-cab8eeb46311 | queued / queued | 未查到 |
| 151 | 185 | 凭咱俩的交情，送俺一只小煤球不过分吧😭 | 5c59b602-f72d-40d5-8b0e-8aa0f9830018 | queued / queued | 未查到 |
| 152 | 186 | 一口下去，这锅气，绝了‼️ | c275a9a6-f47e-4bc9-9b19-2546174ff9ae | queued / queued | 未查到 |
| 153 | 187 | 口感嫩的像千页豆腐‼️水嫩鸡排精准比例教程 | 3b65feaf-ba31-42d0-b65e-41885a74c4ba | queued / queued | 未查到 |
| 154 | 188 | 这真是听劝的减脂零食！碾压所有油炸薯片！ | 5b11b33f-1959-4bf3-8786-e75991fff37c | queued / queued | 未查到 |
| 155 | 189 | 魅魔速成班：男生极速提高颜值 细节跟练版 | f36bcd21-3c61-4dee-90b4-879ae030f77b | queued / queued | 未查到 |
| 156 | 190 | 美国机械工程专业留学就是撑S胆大的饿S胆小 | a7683308-95de-4461-983a-d98537c5b131 | queued / queued | 未查到 |
| 157 | 191 | 我发现减脂餐做法也简单，瘦的真就越快啊！ | d8ae6a74-8499-4833-b297-5f6221d620a5 | queued / queued | 未查到 |
| 158 | 192 | 🔥🔥男士形象改造/穿搭指导咨询/搭配师帮搭 | d235f317-8a84-4704-87a5-7c98f72e60ed | queued / queued | 未查到 |
| 159 | 193 | 🇰🇷韩男太会穿了吧｜简约男友风🍃 | 093b89c7-0d25-409c-854b-8bfd43d310f1 | queued / queued | 未查到 |
| 160 | 194 | 极简科幻，土星之环｜壁纸分享｜392 | 3f4c1589-a4b4-48b2-ab56-c544c4dce539 | queued / queued | 未查到 |
| 161 | 195 | 部队锅韩餐店的做法 | 79f787ef-a13b-4da4-8176-9d012e8a7e6e | queued / queued | 未查到 |
| 162 | 196 | 1菜N用！万能肉酱备餐，12种吃法大全✌🏻 | b878b605-6cef-4b2d-a5f2-312163db7da7 | queued / queued | 未查到 |
| 163 | 197 | 家里只剩鸡蛋和牛奶，也能做米其林级别甜品 | cde7546b-a579-4975-b40d-77755ff8abc4 | queued / queued | 未查到 |
| 164 | 198 | 鲜鲜嫩嫩的虾仁豆腐抱蛋，巨嫩滑❗️巨好吃❗️ | dde35652-594c-4746-83b0-5f9216ccdb39 | queued / queued | 未查到 |
| 165 | 199 | 可乐鸡翅到底放多少可乐才不腻人‼️ | 6f9865f4-9655-44bb-bf0e-fffa3be55f47 | queued / queued | 未查到 |
| 166 | 200 | 当你不知道吃什么的时候！那就看看这个视频 | 0ad43591-89b1-4cf3-8d56-a445a2c66d78 | queued / queued | 未查到 |
| 167 | 201 | 帅小伙做的脆皮烤五花，摆出爱你的形状 | 7710e3ad-ad45-4010-9708-2f83d37b88dd | queued / queued | 未查到 |
| 168 | 202 | 春节情人节，帅小伙做小酥肉摆成爱你的形状 | e79669c4-96c3-4525-a15e-86ea6f2fe22e | queued / queued | 未查到 |
| 169 | 203 | UIUC 北边超级推荐的衣服店（二手） | 7663bf89-9e70-46b7-bfc1-a818b6bf52da | queued / queued | 未查到 |
| 170 | 204 | 160/115斤｜谁懂啊❗️梨形这样穿也太藏肉了❗️ | ba9b0d7e-33f6-456f-9940-237e2c22e601 | queued / queued | 未查到 |
| 171 | 205 | 👕男生如何找到适合自己的穿衣风格✨ | 5b21a066-025d-42f3-8056-24aa56fc61ab | queued / queued | 未查到 |
| 172 | 206 | 10s学会男生春夏穿搭技巧！ | 3db5a690-1f91-45da-bbfc-e8f3d7880a61 | queued / queued | 未查到 |
| 173 | 207 | 挖空家底🔥5家私藏店铺分享 ❗️便宜又好看 | b5c64333-f088-491d-b763-84e2239b0508 | queued / queued | 未查到 |

## 证据文件与后续查询

- [全部 207 条任务的 JSON 快照](queue-audit-20261004.json)
- [完整 CSV 清单](queue-audit-20261004.csv)
- [新抽帧实现与真实模型合成验证](dense-sampling-update.md)
- [旧十条受阻诊断快照](blocked-jobs-20261004.json)
- [四条标题缺失来源诊断](source-failure-diagnostics-20261004.json)

```bash
cd "/home/tzuo5/gpt projects/rag_favorite"
bash examples/video_runtime.sh cli status ea9ce021-a020-4414-a89b-20de00009b1b
bash examples/video_runtime.sh favorites-nightly-status
journalctl --user -u rag-favorite-video-worker.service -n 40 --no-pager
```

本次只做只读核查与报告生成，没有修改登录凭据、重试预算、队列状态、历史来源或正在使用的原视频。读取状态无需 Linux 密码。
