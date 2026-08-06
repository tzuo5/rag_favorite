# 单作者多作品抓取与批量入库设计

状态：设计完成
更新时间：2026-07-23（Asia/Shanghai）
适用项目：rag-favorite `ingestion/` 组件

## 1. 目标与范围

### 1.1 目标

当授权 Telegram 用户发送受支持平台的作者主页时，系统先只读发现作品并展示预览；用户明确选择范围和知识库并确认后，系统创建一个持久批次，把选中的作品逐个交给现有单视频流水线。批次在 worker 重启后可恢复，可暂停、继续和取消，并以聚合方式通知进度。

第一版只交付 YouTube 作者主页的“最近 10 个作品”闭环。架构保留日期范围、最近 50 个、Bilibili 和小红书扩展点，但未完成对应验收前不对用户暴露。

### 1.2 核心术语

- **作者源（author source）**：经过平台识别和规范化的作者主页，身份由 `platform + author_id` 表示。
- **发现（discovery）**：只获取作者与作品 flat metadata，不下载字幕、音频、视频或缩略图。
- **预览（preview）**：有过期时间的发现结果，尚未创建单视频 jobs，也不代表用户同意执行。
- **批次（batch）**：一次已确认的作者作品选择，固定目标知识库和选择策略。
- **批次条目（batch item）**：批次中的一个作品快照，以 `platform + source_id` 标识。
- **子任务（child job）**：复用现有 `video_ingestion_jobs` 的实际处理任务，一个批次条目最多绑定一个子任务。

### 1.3 用户旅程

1. 用户发送一个作者主页 URL。
2. 系统识别它是作者主页而非单作品 URL，创建发现请求。
3. 系统使用受限 flat extraction 获取作者信息和最多 `discovery_scan_limit` 个候选作品。
4. 系统查询已有文档及活动 jobs，返回作者、候选数量、预计新增数、已存在数、时长已知/未知数量。
5. 用户选择处理范围，再从八个可写主题知识库中选择一个目标库。
6. 系统展示不可变的确认摘要；只有点击“确认开始”才创建批次及条目。
7. worker 按发布时间倒序、同时间按稳定 position 顺序逐个创建/领取子任务。
8. 子任务继承批次目标库，不出现逐视频目标库选择。
9. 系统在当前视频进入关键处理阶段、每完成 5 条或每 10 分钟发送一条聚合状态；每条新状态发送成功后删除上一条批次状态消息。平台认证或限流异常会暂停整个批次。
10. 批次进入终态后发送成功、已存在跳过、失败、取消及文档/向量结果汇总。

### 1.4 第一版明确支持

- YouTube channel URL、`/@handle`、`/channel/<id>`、`/c/<name>`、`/user/<name>` 及其 `/videos` 页面。
- 仅选择最近 10 个公开、可枚举的视频。
- 整个批次固定选择一个目标知识库。
- 发现预览、确认、状态查询、暂停、恢复、取消。
- 子任务串行执行，复用现有字幕优先与 Whisper fallback。
- 发现时和执行时两层去重。
- worker 重启恢复、平台认证/限流熔断、聚合通知。

### 1.5 第一版明确不支持

- 不默认或无上限抓取“全部作品”。
- 不支持多个作者合并为一个批次。
- 不支持一个批次写入多个知识库。
- 不保证 private、members-only、付费、已删除、地区限制或直播中的内容。
- 不在第一版开放 Shorts、直播回放的独立筛选；发现结果可以记录这些类型，默认与普通公开视频一起按发布时间排序，无法确定类型时不误删。
- 不承诺精确的 Whisper 数量预估；发现阶段不发起字幕下载请求，只显示“可能需要转写/未知”。
- 不在第一版交付 Bilibili、小红书生产入口；它们必须分别完成平台适配和风控验收。

### 1.6 产品级约束与默认值

| 项目 | 第一版默认 | 说明 |
|---|---:|---|
| 用户可选数量 | 10 | 第一版固定值 |
| 单批硬上限 | 10 | 后续扩容必须先评估总时长与磁盘 |
| 发现扫描上限 | 50 | 用于填补已删除/不可用条目并计算预览 |
| 预览有效期 | 30 分钟 | 过期后重新发现，避免确认陈旧列表 |
| 活动批次数/用户 | 1 | `DRAFT/DISCOVERING` 预览不计，已确认未终止批次计入 |
| Whisper 并发 | 1 | 保持当前主机资源模型 |
| 聚合通知阈值 | 5 条或 10 分钟 | 两者任一先到 |

### 1.7 成功定义

批次“完成”不等于所有条目都成功。父批次终态由所有条目均进入终态决定：

- `COMPLETED`：所有条目均为完成或已存在跳过；全部已存在跳过也属于成功完成。
- `COMPLETED_WITH_ERRORS`：没有用户取消，所有条目均终止，但至少一个条目失败。
- `CANCELLED`：用户已请求取消、所有条目均终止且至少一个条目被取消；已经完成的文档保留，不执行回滚。
- `FAILED`：批次在创建或编排层发生不可恢复错误，无法形成可信的条目终态集合。

用户看到的最终回执必须展示每一类计数，不能只显示笼统的“成功/失败”。

## 2. 持久化数据模型

设计把“尚未确认的发现预览”和“已经确认的执行批次”分开，避免过期预览污染正式批次统计。所有枚举值由数据库 `CHECK` 与应用枚举同时约束；所有时间使用 `timestamptz`。

### 2.1 `video_author_discoveries`

一行代表一次作者主页只读发现请求。

| 字段 | 类型 | 约束/用途 |
|---|---|---|
| `id` | uuid | 主键 |
| `telegram_user_id/chat_id/message_id` | text | 非空，权限和回复目标 |
| `input_url` | text | 用户原始输入 |
| `platform` | text | 首版只允许 `youtube` |
| `author_id` | text | 发现成功后非空 |
| `canonical_author_url` | text | 入队前由分类器生成的非空规范主页 |
| `author_name` | text | 展示名 |
| `state` | text | `QUEUED/DISCOVERING/READY/FAILED/EXPIRED/CONSUMED` |
| `scan_limit` | integer | 本次发现硬上限 |
| `discovered_count` | integer | 持久化条目数 |
| `eligible_count` | integer | 首版规则下可选择数 |
| `existing_destination_counts` | jsonb | 八个目标库的预览去重计数，仅供展示 |
| `existing_main_count/existing_cooking_count` | integer | 旧版兼容字段 |
| `error_code/error_message` | text | 对用户安全的错误分类及内部摘要 |
| `expires_at` | timestamptz | 默认创建后 30 分钟 |
| `created_at/updated_at/finished_at` | timestamptz | 审计时间 |

关键索引：

- `(telegram_user_id, created_at DESC)`：查询用户最近预览。
- `(state, expires_at)`：清理过期预览。
- `(platform, author_id)`：作者级观测和故障分析，允许多次发现。
- `UNIQUE(telegram_user_id, platform, canonical_author_url) WHERE state IN ('QUEUED','DISCOVERING')`：相同用户的重复发现请求复用已有任务。

### 2.2 `video_author_discovery_items`

保存发现时的不可执行 flat metadata，避免 Telegram callback 携带或信任作品列表。

| 字段 | 类型 | 约束/用途 |
|---|---|---|
| `id` | bigserial | 主键 |
| `discovery_id` | uuid | 外键，删除过期 discovery 时级联删除 |
| `platform/source_id` | text | 平台作品身份，均非空 |
| `canonical_url` | text | 规范作品链接 |
| `title` | text | 可空，平台可能只返回 ID |
| `published_at` | timestamptz | 可空 |
| `duration_seconds` | double precision | 可空且非负 |
| `content_type` | text | `VIDEO/SHORT/LIVE_REPLAY/UNKNOWN` |
| `position` | integer | extractor 返回的稳定次序，从 1 开始 |
| `eligibility` | text | `ELIGIBLE/UNAVAILABLE/UNSUPPORTED` |
| `raw_metadata` | jsonb | 只保存允许字段，禁止 cookies/token/完整响应 |
| `created_at` | timestamptz | 审计时间 |

约束与索引：

- `UNIQUE(discovery_id, platform, source_id)`，防止同一预览重复作品。
- `UNIQUE(discovery_id, position)`，保证确认时次序确定。
- `(discovery_id, eligibility, position)`，支持最近 N 个可用作品选择。

### 2.3 `video_ingestion_batches`

只有确认操作才创建正式批次；批次保存确认时的作者与策略快照，不依赖后续平台页面变化。

| 字段 | 类型 | 约束/用途 |
|---|---|---|
| `id` | uuid | 主键 |
| `discovery_id` | uuid | 非空、唯一，保证一个预览最多确认一次 |
| `telegram_user_id/chat_id/message_id` | text | 非空 |
| `platform/author_id/author_url/author_name` | text | 确认时快照 |
| `selection_policy` | jsonb | 版本化策略，如 `{"version":1,"kind":"latest","limit":10}` |
| `selected_destination` | text | 八个显式目标库之一，创建后不可修改 |
| `state` | text | 见第 3 节状态机 |
| `total_count` | integer | 确认后固定，不随作者新增作品变化 |
| `queued_count/running_count/completed_count` | integer | 非负缓存计数 |
| `skipped_existing_count/failed_count/cancelled_count` | integer | 非负缓存计数 |
| `pause_code/pause_message` | text | 暂停原因 |
| `resume_not_before` | timestamptz | 限流暂停的最早探测时间 |
| `cancel_requested_at` | timestamptz | 取消意图审计 |
| `last_notified_at/notified_terminal_count` | timestamptz/integer | 聚合通知节流 |
| `created_at/started_at/updated_at/finished_at` | timestamptz | 审计时间 |

数据库规则：

- `discovery_id` 外键采用 `ON DELETE RESTRICT`；正式批次可永久追溯原始预览。
- 所有计数必须非负。
- 终态必须有 `finished_at`；由 repository 的单一转换方法保证，另设一致性巡检。
- 使用 partial unique index 强制每个用户最多一个活动批次，predicate 固定覆盖 `QUEUED/RUNNING/PAUSED_USER/PAUSED_AUTH/PAUSED_RATE_LIMIT/PAUSED_RESOURCE/CANCELLING`，不依赖时间函数。

关键索引：

- `(state, updated_at)`：编排器扫描。
- `(telegram_user_id, created_at DESC)`：状态命令。
- `(platform, author_id, created_at DESC)`：作者历史。
- `UNIQUE(telegram_user_id) WHERE state IN (...)`：数据库级活动批次互斥。

### 2.4 `video_ingestion_batch_items`

条目是确认瞬间从 discovery items 复制的作品快照，之后不再读取 discovery 列表决定执行范围。

| 字段 | 类型 | 约束/用途 |
|---|---|---|
| `id` | uuid | 主键 |
| `batch_id` | uuid | 外键，`ON DELETE RESTRICT` |
| `platform/source_id/canonical_url` | text | 非空作品身份 |
| `title/published_at/duration_seconds/content_type` | mixed | 确认时快照 |
| `position` | integer | 批次内执行顺序 |
| `state` | text | `PENDING/QUEUED/RUNNING/COMPLETED/SKIPPED_EXISTING/FAILED/CANCELLED` |
| `job_id` | uuid | 可空、唯一，外键到现有 jobs |
| `existing_document_record_id` | bigint | `SKIPPED_EXISTING` 时引用 `video_knowledge_documents.record_id` 作为证据 |
| `error_code/error_message` | text | 条目最终错误 |
| `created_at/queued_at/started_at/finished_at/updated_at` | timestamptz | 审计时间 |

约束与索引：

- `UNIQUE(batch_id, platform, source_id)` 和 `UNIQUE(batch_id, position)`。
- `UNIQUE(job_id)`：一个子任务只属于一个批次条目。
- `(batch_id, state, position)`：编排与状态汇总。
- `(platform, source_id)`：跨批次诊断和去重预查。
- `job_id` 只允许在 `QUEUED/RUNNING/COMPLETED/FAILED/CANCELLED` 状态存在；`SKIPPED_EXISTING` 必须有 `existing_document_record_id` 且无 `job_id`。由数据库约束、事务方法和测试共同覆盖。

### 2.5 对 `video_ingestion_jobs` 的兼容扩展

新增以下带安全默认值的字段，不改变现有单视频入口：

| 字段 | 类型 | 用途 |
|---|---|---|
| `notification_mode` | text | `INDIVIDUAL/BATCH_SILENT`，默认 `INDIVIDUAL` |
| `destination_locked` | boolean | 默认 false；批次子任务为 true |

批次创建子任务时一次写入：

- `selected_destination = batch.selected_destination`
- `notification_mode = BATCH_SILENT`
- `destination_locked = true`
- 发现得到的 `source_platform/source_id/canonical_url/title/author/duration_seconds`

现有 `create_job()` 增加独立的 `create_batch_child_job()`，不把大量批次可选参数塞进单视频 API。`_stage()` 根据 `destination_locked` 将子任务直接转换到 `PERSISTING`；单视频 job 仍进入 `AWAITING_DESTINATION`。

### 2.6 知识库级去重语义

“已存在”必须相对于目标知识库判断：

- 仅当相同 `platform + source_id`（其次 canonical URL）的 `video_knowledge_documents` 在本批次目标库中为 `completed`，条目才直接标为 `SKIPPED_EXISTING`。
- 如果内容只存在于另一个知识库，不能跳过；允许复用已有 Markdown/transcript 并写入本批次目标库。
- 预览计数是时间点快照，确认事务和子任务开始前都必须重新检查，避免在预览后新增文档造成重复处理。

### 2.7 迁移顺序与回滚

建议新增 `0003_author_batch_ingestion.sql`，在单事务中：

1. 创建 discovery、discovery items、batches、batch items、平台请求 gate 和批次通知 outbox 六张表及索引。
2. 为 jobs 增加两个兼容字段及约束。
3. 增加 `batch_items.job_id -> jobs.id` 和 `batch_items.existing_document_record_id -> video_knowledge_documents.record_id` 外键。job 关联只保留一个事实来源，避免 jobs 与 item 双向外键形成循环和不一致。
4. 所有新字段对历史 jobs 使用安全默认值，不回填伪批次。

回滚只允许在尚未创建生产批次时删除新增表/列。已有批次后采用前向修复迁移，不删除审计数据。

## 3. 状态机、事务与恢复

### 3.1 Discovery 状态机

```text
QUEUED ──discovery worker 领取──> DISCOVERING ──成功──> READY ──确认──> CONSUMED
                                      │                  │
                                      └──不可恢复错误──> FAILED
READY ──超过 expires_at────────────────────────────────> EXPIRED
```

- `QUEUED` 由 Telegram 入站事务创建；`DISCOVERING` 只有 discovery worker 领取后可写。
- `READY` 必须已经持久化至少一个 discovery item，并且计数与明细一致。
- `CONSUMED` 只能在正式批次和条目创建成功的同一事务中写入。
- `FAILED/EXPIRED/CONSUMED` 为终态；重新预览必须创建新的 discovery。

### 3.2 Batch 状态机

```text
QUEUED ──首个子任务领取──> RUNNING ──全部条目终止──> COMPLETED
   │                         │                         或 COMPLETED_WITH_ERRORS
   │                         ├──用户暂停──> PAUSED_USER ──恢复──┐
   │                         ├──认证失效──> PAUSED_AUTH ──恢复──┤
   │                         ├──资源不足──> PAUSED_RESOURCE ──恢复──┤
   │                         └──平台限流──> PAUSED_RATE_LIMIT ──到期/恢复──┘
   │
   ├────────用户取消────────> CANCELLING ──无运行条目──> CANCELLED
   └────────编排不可恢复错误──────────────────────────> FAILED
```

规则：

- 可领取子任务的父状态只有 `QUEUED/RUNNING`。
- 暂停只阻止领取新任务；已经进入下载、转写或持久化的单个任务在安全边界结束。第一版不强杀 ffmpeg、yt-dlp、Whisper 或向量写入进程。
- 用户取消后，尚未领取的条目和 jobs 在同一事务中标为 `CANCELLED`。运行中的 job 在阶段边界看到 `cancel_requested_at` 后清理临时媒体并取消；若已进入 `PERSISTING`，允许完成原子写入后再结束批次，避免 Markdown/SQL/vector 半写。
- `PAUSED_AUTH` 只能在管理员刷新 cookies 并通过只读平台探测后恢复；用户点击恢复但探测失败时保持暂停。
- `PAUSED_RATE_LIMIT` 保存 `resume_not_before`；到期后进行一次探测，成功才恢复，失败则指数延后。
- 父批次只有在所有 items 都是终态时才进入完成类终态。

### 3.3 Batch item 状态机

```text
PENDING ──确认事务创建 job──> QUEUED ──worker 领取──> RUNNING
   │                            │                     ├──成功──> COMPLETED
   ├──目标库已存在────────> SKIPPED_EXISTING          ├──重试──> QUEUED
   └──取消────────────────> CANCELLED                 ├──耗尽──> FAILED
                                                        └──取消──> CANCELLED
```

- `SKIPPED_EXISTING/COMPLETED/FAILED/CANCELLED` 是终态。
- `RUNNING -> QUEUED` 只用于有界重试或 worker 重启恢复，保留错误码和 retry 证据。
- job 的细粒度处理状态仍以 `video_ingestion_jobs.state` 为准；item 只保存编排级状态。repository 负责成对更新，业务代码不得分别裸写两张表。
- 第一版确认后固定创建 10 个 batch items；对目标库已存在的 items 不创建伪 child job，而是保存 `existing_document_record_id`。其余 items 在确认事务内创建幂等 child jobs。验收措辞应以“10 个持久 items，所有需处理 item 都有唯一 child job”为准。

### 3.4 确认事务

`confirm_discovery(discovery_id, user_id, destination)` 是唯一确认入口：

1. `SELECT ... FOR UPDATE` 锁定 discovery。
2. 校验归属用户、`state=READY`、未过期、destination 合法。
3. 查询用户是否已有活动批次；partial unique index 是最终并发保护，冲突时返回确定性结果，不部分创建。
4. 按 `eligibility=ELIGIBLE, position ASC` 选定 10 条。平台 author videos tab 的次序是本次发现的“最近”语义；`published_at` 仅用于展示和质量检查，不能因缺失而把近期作品排到末尾。
5. 创建 batch；`UNIQUE(discovery_id)` 使 Telegram callback 重放返回同一个 batch。
6. 复制 10 个 item 快照。
7. 对每项按目标库重新查重：
   - 已完成：写 `SKIPPED_EXISTING` 和证据 document ID。
   - 未完成：用 `uuid5(batch_id, platform:source_id)` 创建 job，写入锁定 destination 和 `BATCH_SILENT`，再把 job ID 绑定到 item。
8. 从 item 明细计算 batch 缓存计数。
9. discovery 转为 `CONSUMED` 并提交。

任何一步失败都整体回滚。callback 重试只读取既有 batch，不重复创建条目或 jobs。

### 3.5 Worker 领取事务

扩展 `claim_next()`，但保持对单视频 job 的兼容：

- 普通 job：沿用当前 `RECEIVED/PERSISTING + next_attempt_at` 条件。
- 批次 child job：额外 join batch item 与 batch，并要求父状态为 `QUEUED/RUNNING`、item 为 `QUEUED`。
- 使用 `FOR UPDATE SKIP LOCKED` 选中 job 后，在同一事务把 item 改为 `RUNNING`、batch 首次从 `QUEUED` 改为 `RUNNING`，并更新计数/时间。
- 单 worker 配置继续是主保护；事务语义确保未来即使启动多个非 Whisper 编排 worker，也不会重复领取同一个 job。

队列公平性采用“普通单视频优先但防饥饿”：

- 按 `effective_priority, created_at` 排序。
- 单视频默认优先级高于批量 child。
- 每连续完成 3 个单视频后允许 1 个最老批量 child，或为等待超过 30 分钟的批量 child 提升优先级。
- 第一版只有一个执行 worker时，可先使用等待时长提升，避免引入进程内计数器。

### 3.6 子任务完成事务与计数

为批次 child 新增 repository 包装方法：

- `complete_batch_child(job_id, document_id)`
- `retry_batch_child(job_id, error, next_attempt_at)`
- `fail_batch_child(job_id, error)`
- `cancel_batch_child(job_id)`

这些方法在一个事务中：

1. 锁定 job、item、batch。
2. 校验允许的前置状态，重复调用时返回现有结果。
3. 更新 job 和 item。
4. 从 items 使用带 `FILTER` 的聚合查询重新计算父计数，而不是用易漂移的 `count + 1`。
5. 若全部 item 终止，确定父终态并写 `finished_at`。

批次规模首版只有 10，重新聚合成本可忽略，换取崩溃和重试后的统计可信度。

### 3.7 两阶段去重与运行时竞态

1. discovery 阶段仅生成“可能已存在”预览。
2. 确认事务按目标知识库重新检查并直接跳过。
3. child job 被领取后、发起平台 metadata/subtitle/media 请求前再检查一次；若期间其他入口已经完成同目标库文档，原子转换为 `SKIPPED_EXISTING`。
4. 当前部署只有一个执行 worker，因此不会同时下载两个 child。单视频入口可能先完成相同作品，第三层检查负责收敛。
5. 若未来启用多执行 worker，必须先增加基于 `platform + source_id + destination` 的数据库 lease；不能只依赖应用内 mutex。

### 3.8 重启恢复

worker 启动时按顺序执行：

1. 沿用现有 orphan job 恢复，但批次 child 必须通过 batch-aware repository 同步把 item 从 `RUNNING` 改回 `QUEUED` 或在重试耗尽后改为 `FAILED`。
2. 对所有非终态 batch 从 items 重算缓存计数。
3. `CANCELLING` 且无运行 item 的 batch 收敛为 `CANCELLED`。
4. 全部 item 已终止但父状态仍活动的 batch 收敛到完成类终态。
5. 将过期 `READY` discovery 标为 `EXPIRED`；清理策略只删除无正式 batch 引用且超过审计保留期的 discovery 明细。

恢复操作全部幂等，并记录结构化日志：batch ID、item ID、旧状态、新状态、原因。

### 3.9 错误分类

错误至少分三层：

| 类别 | 示例 | 动作 |
|---|---|---|
| 条目永久错误 | 视频删除、private、地区限制、超时长 | item 失败，批次继续 |
| 条目暂时错误 | 偶发网络、下载中断、向量服务短暂失败 | 沿用有界重试，耗尽后 item 失败 |
| 平台级错误 | cookies 失效、bot check、连续 412/429 | 当前 item 回队，父批次暂停，不继续请求同平台 |
| 编排错误 | 计数不一致、关联缺失、非法状态 | 父批次失败并告警，禁止猜测性修复 |

平台级错误需要新增稳定错误码，例如 `AUTH_EXPIRED/BOT_CHECK/RATE_LIMITED/PLATFORM_BLOCKED`，不能继续全部映射成当前笼统的 `DOWNLOAD_FAILED`。

## 4. 平台发现适配层

### 4.1 统一接口

新增 `backend/ingestion/discovery/`，避免把作者页逻辑继续堆入面向单视频的 `MetadataExtractor`：

```python
class AuthorDiscoveryAdapter(Protocol):
    platform: Platform

    def classify_url(self, url: str) -> UrlKind: ...
    def normalize_author_url(self, url: str) -> str: ...
    async def discover(self, url: str, *, scan_limit: int) -> AuthorDiscoveryResult: ...
    async def probe_access(self, author_url: str) -> AccessProbeResult: ...
```

返回 DTO：

- `AuthorSnapshot(platform, author_id, canonical_url, display_name)`
- `DiscoveredWork(source_id, canonical_url, title, published_at, duration_seconds, content_type, position, eligibility, raw_metadata)`
- `AuthorDiscoveryResult(author, works, extractor_name, truncated)`

接口约束：

- `discover()` 只允许 flat metadata 请求，禁止下载字幕、缩略图和媒体。
- `scan_limit` 是硬上限，adapter 不得自行“翻到全部”。
- 每条 work 必须有非空、平台稳定的 `source_id`；缺失 ID 的条目标为不可执行且不进入确认候选。
- `raw_metadata` 经过字段 allowlist，只保留诊断所需的无敏感字段。
- 所有 URL 先经过现有 SSRF 校验；平台 adapter 还要验证最终 canonical host 属于该平台 allowlist。

### 4.2 URL 分类优先于入队

当前插件看到任何 URL 都直接创建单视频 job。新增统一分类器，入口顺序固定为：

1. 提取 URL 并执行公共 URL 安全校验。
2. 使用明确的 host/path 规则判断 `SINGLE_WORK/AUTHOR_PAGE/UNSUPPORTED_COLLECTION/UNKNOWN`。
3. `SINGLE_WORK` 继续现有 `enqueue`。
4. `AUTHOR_PAGE` 创建 discovery。
5. playlist、搜索页、tag、收藏夹等 `UNSUPPORTED_COLLECTION` 返回明确提示，不误当作者也不无限枚举。
6. `UNKNOWN` 可交给现有单视频 metadata 路径，但不得因 yt-dlp 返回 playlist 就自动批量处理；若结果 `_type=playlist`，安全失败并提示当前只支持作者主页。

首版 YouTube 规则：

| URL | 分类 |
|---|---|
| `youtube.com/watch?v=...`、`youtu.be/...`、`youtube.com/shorts/<id>`、`youtube.com/live/<id>` | `SINGLE_WORK` |
| `youtube.com/@handle[/videos]` | `AUTHOR_PAGE` |
| `youtube.com/channel/<id>[/videos]` | `AUTHOR_PAGE` |
| `youtube.com/c/<name>[/videos]`、`youtube.com/user/<name>[/videos]` | `AUTHOR_PAGE` |
| `youtube.com/playlist?...`、`/results?...`、`/feed/...` | `UNSUPPORTED_COLLECTION` |

author URL 规范化时去除 query/fragment 和已知 tab 后缀，再统一指向 `/videos`。不能把任意一级路径机械追加 `/videos`。

### 4.3 YouTube 首版实现

本机已安装的 yt-dlp 版本为 `2026.07.04`，其本地 API 支持 `extract_flat`、`playlistend` 和 `lazy_playlist`。首版复用现有 cookies、HTTP headers、EJS runtime 与 PO Token Provider 配置，使用独立受限选项：

```python
{
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "playlistend": scan_limit,
    "lazy_playlist": True,
    "skip_download": True,
    "socket_timeout": 30,
    # 复用 cookiefile / extractor_args / js_runtimes / http_headers
}
```

实现注意：

- 输入规范化为 channel videos tab，调用 `extract_info(url, download=False)`。
- 顶层结果必须是 playlist/channel tab 类型；若返回单视频，分类错误并安全失败。
- 从顶层 `channel_id/uploader_id/id` 选择稳定 author ID，拒绝仅以可变 display name 作为身份。
- 遍历 entries 时最多消费 `scan_limit` 条，即使 extractor 提供 lazy generator 也必须主动截断。
- flat entry 优先读取 `id/url/title/duration/timestamp/release_timestamp/live_status`；字段缺失是正常情况，不触发逐视频详情请求。
- canonical work URL 统一构造为 `https://www.youtube.com/watch?v=<validated_id>`，不信任 entry 中的任意外部 URL。
- `live_status=is_live` 标为 `UNAVAILABLE`；已结束直播标 `LIVE_REPLAY`；`url` 或现有字段明确来自 shorts tab 时标 `SHORT`，无法可靠确定时为 `UNKNOWN` 并保留。
- 平台返回次序作为 `position`。发布时间存在时用于展示和稳定排序；不存在时不伪造时间。
- 如果 50 条扫描中可执行项少于 10，预览展示真实可执行数量，确认按钮不得声称会处理 10 条。

### 4.4 发现结果校验

写入 `READY` 前执行：

- author ID 和 canonical author URL 非空且平台匹配。
- work source ID 符合平台 ID 格式，canonical URL 可由 ID 重新生成。
- source ID 去重后再编号；重复项记录结构化 warning。
- `duration_seconds` 非负且不超过合理数值上限；超过当前单视频允许时长的条目标 `UNSUPPORTED`。
- `published_at` 能解析为带时区时间，否则置空并记录字段质量。
- 最终 items 数、eligible 数与 discovery 缓存计数在同一事务写入。

空频道是合法发现结果但不可确认：状态可为 `READY`、`eligible_count=0`，Telegram 显示“未发现可处理公开视频”。平台响应结构不合法则是 `FAILED/EXTRACTOR_SCHEMA_CHANGED`，不能当作空频道。

### 4.5 Access probe

`probe_access()` 用于从 `PAUSED_AUTH/PAUSED_RATE_LIMIT` 恢复，必须比完整 discovery 更轻：

- 只请求 author videos tab 的第 1 条 flat metadata。
- 不更新原批次作品快照。
- 返回 `OK/AUTH_REQUIRED/RATE_LIMITED/BLOCKED/UNAVAILABLE` 和可选 `retry_after`。
- 只有 `OK` 才允许恢复；失败探测也经过批次级退避，避免恢复按钮成为请求放大器。

### 4.6 Bilibili 扩展边界

第二阶段新增 `BilibiliAuthorDiscoveryAdapter`，但必须在 feature flag 后：

- 识别 `space.bilibili.com/<mid>`，author ID 使用数字 mid。
- 发现接口必须分页但受 `scan_limit` 截断；每页之间增加平台级速率限制和抖动。
- 复用现有 Bilibili cookies 与 User-Agent，但将 412、WBI 签名失败、登录失效和 429 分开分类。
- canonical work URL 只从通过格式校验的 BV/AV ID 构造。
- 先完成最近 10 条验收；未验证前不开放“全部”、高页码回溯或并行分页。

### 4.7 小红书扩展边界

第三阶段新增 `XiaohongshuAuthorDiscoveryAdapter`：

- 仅支持能解析稳定 user ID 的作者主页。
- 动态签名、cookies 和风控能力必须封装在 adapter 内，不泄漏到通用编排层。
- 首版只能枚举一个有限窗口；响应要求登录时进入 `PAUSED_AUTH`，验证码、签名异常或风控阻断时打开平台 circuit 并暂停批次。
- work canonical URL 由合法 note ID 构造，临时 `xsec_token` 只允许用于当次受控请求，禁止持久化到 `canonical_url/raw_metadata/log`。
- 在真实账号、低频率、无污染环境完成验收前，UI 不显示小红书作者批量入口。

### 4.8 能力注册与 feature flags

adapter registry 返回每个平台的能力：

```text
enabled
max_scan_limit
selectable_limits
supports_date_range
supports_content_type_filter
supports_access_probe
```

Telegram 只渲染 registry 声明且已验收的能力。建议配置：

- `AUTHOR_BATCH_YOUTUBE_ENABLED=true`
- `AUTHOR_BATCH_BILIBILI_ENABLED=false`
- `AUTHOR_BATCH_XIAOHONGSHU_ENABLED=false`
- `AUTHOR_DISCOVERY_SCAN_LIMIT=50`
- `AUTHOR_BATCH_MAX_ITEMS=50`

关闭 feature flag 只阻止新发现；已经确认的批次仍可恢复和完成，避免部署切换制造孤儿任务。

## 5. Telegram 交互协议

### 5.1 入站与即时确认

插件使用第 4.2 节 URL 分类器：

- 单作品保持现有回复：`📥 已收到，任务已进入处理队列。`
- 作者主页回复：`🔎 已收到作者主页，正在读取有限作品列表；确认前不会下载或入库。`
- 不支持的集合页明确说明首版只接受单视频或作者主页。

作者 discovery 只入持久队列，不能在 OpenClaw hook 的 10 秒超时内同步抓取。发现完成后由 notifier 主动发送预览。

### 5.2 预览消息

示例：

```text
🔎 作者作品预览
平台：YouTube
作者：Example Channel
扫描：50 条（已达到扫描上限）
可处理：10 条
主知识库已存在：3 条
菜谱知识库已存在：1 条
时长：已知 8 条，未知 2 条

请选择处理范围。确认前不会下载媒体。
```

第一版只显示一个范围按钮：`最近 10 个`；可处理不足 10 个时显示真实数量，例如 `最近 7 个`。零个可处理项时不显示开始按钮。

预览中的“已存在”是分知识库快照，确认时仍会重新检查。不能显示未经详情请求验证的“预计 Whisper 数量”，避免把未知当事实。

### 5.3 三步 callback

使用新 namespace `vkb`，不与现有单视频 `vki` 混用：

1. `vkb:range:<limit>:<discovery_id>`
2. `vkb:dest:<short-destination-code>:<limit>:<discovery_id>`
3. `vkb:start:<short-destination-code>:<limit>:<discovery_id>`

最终确认消息示例：

```text
准备创建批次
作者：Example Channel
范围：最近 10 个可处理作品
知识库：主知识库
预计新增：7
预计已存在跳过：3

开始后可暂停或取消；取消不会删除已经完成的文档。
```

按钮为“确认开始 / 返回选择 / 放弃”。callback payload 必须保持在 Telegram 64-byte 限制内；UUID 与上述短 action 可满足。服务端不能信任 callback 中的 limit、destination 或 ID：

- 校验 Telegram 授权用户。
- 校验 discovery 归属、状态和过期时间。
- 校验 limit 属于 adapter registry 当前允许集合且不超过 discovery eligible 数。
- 校验 destination 枚举。
- `start` 调用幂等确认事务；重复点击返回同一个 batch。

消息编辑失败（原消息太旧、不可编辑）时发送新消息，业务事务不回滚。

### 5.4 批次开始与控制

确认成功：

```text
✅ 批次已创建
作者：Example Channel
范围：10
知识库：主知识库
待处理：7
已存在跳过：3

[查看进度] [暂停] [取消]
```

控制 callback：

- `vkb:status:<batch_id>`
- `vkb:pause:<batch_id>`
- `vkb:resume:<batch_id>`
- `vkb:cancel:<batch_id>`
- `vkb:cancel_confirm:<batch_id>`

取消必须二次确认，文案明确“已完成内容会保留”。暂停和恢复幂等；对已暂停/已运行状态重复点击返回当前状态，不报内部错误。

认证暂停时普通用户的“恢复”按钮改为“重新检测”。只有 access probe 成功才恢复；否则提示需要管理员安全刷新 cookies，不要求用户通过 Telegram 上传 cookies。

### 5.5 命令语义

- `/video_status`：保持查询最近单视频任务。
- `/video_batch_status`：查询用户最近的活动批次；无活动时显示最近一个终态批次。
- `/video_batch_pause`：暂停用户唯一活动批次。
- `/video_batch_resume`：恢复用户最近的可恢复暂停批次，认证/限流仍需 probe。
- `/video_batch_cancel`：返回二次确认按钮，不直接取消。

首版每用户最多一个活动批次，因此命令无需用户输入 batch ID；响应仍展示 batch ID 短前缀便于诊断。

现有“好了吗/完成了吗/进度”等短问句的确定性路由调整为：

1. 有活动批次时返回批次聚合状态。
2. 无活动批次时沿用最近单视频 job。

### 5.6 聚合状态格式

```text
📚 作者批次处理中 · a1b2c3d4
作者：Example Channel
状态：RUNNING
完成：3
重复：2
失败：0
当前处理：当前视频标题前几个字…
视频进度：audio2txt
```

视频阶段固定映射为 `video downloading`、`parsing audio`、`audio2txt` 和 `embedding`。暂停状态额外显示安全的暂停原因与下一步，不暴露 cookies 路径、平台响应正文或 token。最终回执显示：

- 处理总数
- 完成
- 已存在跳过
- 失败
- 取消
- Markdown/SQL/vector 一致性结果
- 对失败项提供最多 5 个标题/错误码；更多项提示使用状态命令，不刷屏

### 5.7 批次子任务通知抑制

`notification_mode=BATCH_SILENT` 时，现有 service 中以下逐视频通知全部不发送：

- metadata/subtitle 检查
- 下载与 Whisper 开始/完成
- Markdown 和 embedding 阶段
- 单视频完成/普通失败
- `AWAITING_DESTINATION` 按钮

但业务状态与结构化日志照常写入。批次聚合 notifier 在以下条件发送：

- 批次创建。
- 每新增 5 个终态 item，或距离上次通知满 10 分钟且状态有变化。
- 父批次暂停、恢复或进入取消中。
- 父批次进入终态。

通知发送失败不回滚已完成的处理状态；沿用当前最多 3 次重试并记录 delivery 日志。

### 5.8 权限、重放与隐私

- 所有 discovery/batch callback 都校验 `telegram_user_id`，不能只依赖按钮消息所在 chat。
- callback 对终态和过期对象返回用户可理解的确定性结果。
- 消息只展示 author display name、作品标题和聚合计数，不展示平台 cookies、内部文件路径、完整异常正文。
- Telegram message ID 只用于审计/编辑；消息被删除不影响持久批次继续执行。

## 6. 调度、平台保护与可观测性

### 6.1 进程职责

保持三个边界清晰的运行单元：

| 运行单元 | 并发 | 职责 |
|---|---:|---|
| OpenClaw plugin | 事件驱动 | URL 分类、入队、callback/命令，不做平台抓取 |
| `video-author-discovery-worker` | 1 | 领取 discovery、flat extraction、预览通知、access probe |
| 现有 `video-ingestion-worker` | 1 | 单视频与批次 child 的字幕/下载/Whisper/持久化 |

独立 discovery worker 的原因是作者预览不应排在一个可能数小时的 Whisper job 后面。它不改变 Whisper 单并发，也不允许媒体下载。新增 systemd unit 使用与现有 worker 相同的沙箱、secret env 和重启策略。

第一版不需要额外 scheduler：确认事务已经为所有需处理 items 创建 `RECEIVED` child jobs，batch-aware `claim_next()` 即为调度器。聚合通知与一致性收敛可在 ingestion worker 每轮空闲或每分钟执行一次。

### 6.2 平台请求 gate

新增 `video_platform_request_gates`：

| 字段 | 用途 |
|---|---|
| `platform`（主键） | 平台身份 |
| `circuit_state` | `CLOSED/OPEN/HALF_OPEN` |
| `next_allowed_at` | 下一次普通 yt-dlp 操作最早开始时间 |
| `blocked_until` | 限流/风控后的禁止请求时间；认证失效可为空并保持 OPEN |
| `consecutive_failures` | 平台级连续失败数 |
| `last_error_code` | 安全错误码 |
| `probe_in_flight` | 保证 HALF_OPEN 只有一个探测 |
| `opened_at/updated_at/last_success_at` | 审计时间 |

discovery worker 和 ingestion worker 在每次 yt-dlp 平台操作前调用同一 repository gate：

1. `SELECT ... FOR UPDATE` 锁定平台行。
2. `OPEN` 且未到期：不发请求，批次暂停/单视频延迟重试。
3. 已到 `blocked_until`：原子进入 `HALF_OPEN` 并只允许一个 probe。
4. `CLOSED`：预留 `next_allowed_at = now + min_interval + random_jitter` 后提交，再开始请求。
5. 成功关闭 circuit 并清零连续平台失败；失败按分类更新。

数据库 gate 让两个进程和重启后的行为一致。事务中只做时间槽预留，不持锁等待网络。

初始建议值必须配置化并通过生产观测调整：

| 平台 | 操作最小间隔 | jitter | 首次限流退避 |
|---|---:|---:|---:|
| YouTube | 5 秒 | 0–3 秒 | 15 分钟 |
| Bilibili | 10 秒 | 0–5 秒 | 30 分钟 |
| 小红书 | 15 秒 | 0–10 秒 | 60 分钟 |

处理单个视频时 metadata、字幕、媒体下载属于不同平台操作，也要过 gate；但本地 Whisper、Markdown 和向量处理不占平台时间槽。

### 6.3 Circuit breaker

立即打开 circuit：

- `AUTH_EXPIRED`
- `BOT_CHECK`
- 明确验证码/人机验证
- Bilibili 412 / 小红书签名或登录 challenge

阈值打开 circuit：

- 10 分钟窗口内连续 3 次 `RATE_LIMITED` 或平台 5xx。
- HTTP 429 优先尊重合法 `Retry-After`；没有时使用平台首次退避。
- 重复失败采用指数退避并加 jitter，最大 6 小时。

认证失效没有自动 `blocked_until`，必须完成 cookies 安全刷新和成功 probe。平台 gate 打开后：

- 同平台活动批次转为对应暂停状态。
- 同平台未开始 child 不再领取。
- 普通单视频 job 延后且发送一次聚合式平台不可用提示，不进行原有每 job 连续下载重试。
- 本地媒体、其他平台和纯持久化重试不受影响。

错误识别集中在 adapter/error classifier 中，保存稳定 code、HTTP 状态和经过脱敏的短摘要。不得把响应正文、cookies、PO token 或签名参数写入数据库/日志/Telegram。

### 6.4 资源预算

第一版硬保护：

- `AUTHOR_BATCH_MAX_ITEMS=10`
- `AUTHOR_DISCOVERY_SCAN_LIMIT=50`
- `MAX_BATCH_ESTIMATED_DURATION_SECONDS=43200`（12 小时）
- `ENFORCE_BATCH_ESTIMATED_DURATION_BUDGET=false`（串行长批次默认不按总时长拒绝）
- `MAX_BATCH_UNKNOWN_DURATION_RESERVATION_SECONDS=3600`（每个未知时长按 1 小时预留）
- 每个 child 仍受现有单视频 4 小时上限。
- 每次下载前至少保留 `MIN_DISK_FREE_GB=5`；不足时暂停批次为 `PAUSED_RESOURCE`，不把所有 items 标失败。
- Whisper concurrency 始终为 1；不得因为 batch 设置增大 `MAX_CONCURRENT_JOBS`。

确认时仍计算并记录 `已知时长总和 + 未知数 × 1 小时`，用于观察和容量评估。默认串行模式不因该估算拒绝 20/50 条批次；如果运营环境需要恢复总时长硬保护，可设置 `ENFORCE_BATCH_ESTIMATED_DURATION_BUDGET=true`。启用后，超过 12 小时不允许开始；child 获取准确 metadata 后若总预算被突破，该条目标 `BATCH_DURATION_LIMIT`。

### 6.5 公平性与背压

- 单视频 job 比 batch child 优先，但等待超过 30 分钟的最老 child 获得优先提升。
- discovery 队列每用户最多一个 `QUEUED/DISCOVERING` 请求；重复发送相同规范作者 URL 返回现有 discovery。
- 每用户最多一个活动 batch；平台 circuit 打开时仍不允许不断创建同平台新批次。
- 队列长度或磁盘不足时，确认入口拒绝新增批次并给出可恢复提示；预计总时长只在启用总时长预算保护时参与拒绝。
- OpenClaw hook 只做快速数据库事务；所有平台网络操作都在 worker。

### 6.6 聚合通知调度

repository 保存 `last_notified_at/notified_terminal_count`。notifier 使用原子“领取通知资格”：

- `terminal_count - notified_terminal_count >= 5`，或
- 状态有变化且距上次通知 >= 10 分钟，或
- 进入暂停/恢复/取消/终态。

先在数据库记录 notification event/outbox，再投递 Telegram；投递成功写 `delivered_at`。如果进程在记录后崩溃，outbox 可重试；如果 Telegram 已收到但成功回写前崩溃，可能出现一次重复消息，因此消息包含 batch 短 ID 和当前绝对计数，重复也不会造成错误操作。

建议把 outbox 纳入 `0003`：

- `video_ingestion_batch_notifications(id, batch_id, event_type, idempotency_key, payload, attempts, next_attempt_at, delivered_at, created_at)`
- 对 `idempotency_key` 使用唯一约束；key 由 batch ID、event type、状态版本和 terminal count 构造。

这比把通知直接放在 job 完成事务里可靠，也避免 Telegram 故障回滚知识入库。

### 6.7 指标、日志与告警

结构化日志公共字段：

- `discovery_id/batch_id/batch_item_id/job_id`
- `platform/author_id/source_id`
- `old_state/new_state`
- `operation/error_code/retry_count/duration_ms`
- `telegram_user_id` 只记录既有内部标识，不记录消息正文

至少提供以下指标或可由 SQL 查询的观测：

- discovery 成功率、耗时、发现/可执行数量
- 批次活动/暂停/终态数量与端到端耗时
- item 完成、跳过、失败率及各阶段耗时
- 平台请求 gate 状态、429/412/bot check/auth 次数
- 下载字节、Whisper 音频时长与 real-time factor
- outbox 待投递数量与最老延迟
- batch 缓存计数与 item 聚合不一致数

告警条件：

- 任一平台 circuit 打开。
- `PAUSED_AUTH` 超过 15 分钟。
- outbox 最老消息超过 10 分钟。
- batch 计数不一致或孤儿 job/item。
- 磁盘低于 5 GiB。
- batch 无状态变化超过 `job_timeout + 15 分钟`。

## 7. 测试、上线与验收

### 7.1 单元测试

URL 与 adapter：

- YouTube 单视频、short、live、作者主页、videos tab、playlist、search/feed 的分类表驱动测试。
- author URL 规范化去掉 query/fragment，但不把任意路径误改成作者页。
- flat result 映射、字段缺失、重复 source ID、非法 ID、空频道、超过 scan limit。
- canonical work URL 必须从 validated source ID 构造。
- raw metadata allowlist 不包含 cookie、token、签名或原始响应。

选择与预算：

- 最近 N 排序在 published time 缺失时仍按 position 稳定。
- eligible 少于 10 时只选择真实数量。
- 目标库分别查重；另一知识库已有不跳过。
- 默认关闭总时长预算时 20/50 个未知时长条目可确认；启用预算后验证已知时长 + 未知预留的 12 小时边界。

状态与幂等：

- discovery、batch、item 的每条合法/非法状态转换。
- 重复确认、重复 callback、重复完成/失败/取消。
- 暂停不领取、恢复后继续、取消不回滚已完成 item。
- `BATCH_SILENT + destination_locked` 直接进入 `PERSISTING`，普通 job 仍进入 `AWAITING_DESTINATION`。
- 计数从 item 聚合，重复事件不重复增加。

平台保护：

- gate 时间槽预留、OPEN/HALF_OPEN/CLOSED。
- 只有一个 half-open probe。
- auth/bot/412 立即打开，连续 429/5xx 达阈值打开。
- Retry-After、指数退避、jitter 上下界。
- 错误摘要和日志脱敏。

Telegram：

- callback payload 长度小于等于 64 bytes。
- unauthorized、过期、已消费、终态和消息编辑失败。
- 取消二次确认。
- 聚合阈值/outbox 幂等及投递重试。

### 7.2 PostgreSQL 集成测试

使用独立测试数据库或 schema 执行真实 migration 和 repository：

- `0001 -> 0002 -> 0003` 全新安装。
- 已有单视频历史数据上升级，旧 jobs 默认行为不变。
- migration 重复执行策略；若 migration 不是幂等，部署器必须只执行一次并验证版本表。
- 两个连接并发确认同一 discovery，最终只有一个 batch。
- 两个连接 `SKIP LOCKED` 领取，不能领取相同 child。
- 确认事务任一步注入失败后无半个 batch/item/job。
- batch pause 与 job claim 竞态。
- job 完成与用户 cancel 竞态。
- worker 恢复后的 item/job/计数收敛。
- outbox 崩溃窗口和重复投递语义。
- 孤儿关系与非法状态 SQL 巡检为零。

测试结束必须 drop 独立 schema/database，不连接生产 Markdown roots 或生产 vector 表。

### 7.3 Adapter 合约与录制 fixture

- 使用脱敏的 yt-dlp flat result fixture 测试正常频道、少于 10 条、空频道、直播、删除条目、字段变化。
- fixture 只保存 allowlist 字段，不保存 cookies、请求 headers、PO token 或签名参数。
- fake adapter 支持延迟、分页、429、auth、bot check、schema changed，供 worker 端到端测试。
- 对本机实际 yt-dlp 版本运行合约测试；升级 yt-dlp 时该测试是发布 gate。

### 7.4 服务级端到端测试

在隔离环境启动 OpenClaw callback 模拟器、discovery worker、ingestion worker、PostgreSQL 和 fake vector destination，覆盖：

1. 作者 URL 入站后得到即时确认。
2. discovery 完成并产生预览。
3. 用户选择范围/目标库/确认。
4. 已存在 items 跳过，其余 child 串行完成。
5. 没有任何 child 进入 `AWAITING_DESTINATION`。
6. 每条只产生一次 Markdown/SQL/vector 结果。
7. 聚合通知数量符合阈值，不出现逐视频刷屏。
8. 最终 batch 统计等于 item、job、document 和 vector 事实。

必须注入以下故障：

- discovery worker 在写一半 items 前崩溃。
- ingestion worker 在下载、Whisper 后、Markdown move 后、vector 写入后重启。
- Telegram 连续三次投递失败。
- cookies 失效与 HTTP 429。
- 磁盘低水位。
- 用户在 queued、running、persisting 三个阶段取消。

### 7.5 真实平台非污染验收

分两层：

**生产凭据只读层**

- 使用真实 YouTube 作者主页执行完整 discovery，并确认遍历到作品列表末尾。
- 验证 author ID、前 10 个 source ID、次序和 canonical URL。
- 不创建 batch、不下载字幕/媒体、不写任何知识库。
- 从日志证明请求受 gate 控制且没有敏感字段。

**隔离写入层**

- 在生产主机但使用独立测试 database/schema、临时 Markdown root 和独立 vector namespace。
- 使用真实平台 metadata/字幕/必要时 Whisper，确认最近 10 个作品。
- 验证重启恢复、认证暂停、聚合通知可使用受控测试 chat。
- 对 10 个 items 逐项核对：
  - completed/skipped/failed 状态
  - Markdown checksum
  - SQL document
  - chunk 数与 1024 维向量
- 验收后删除整个明确命名的测试 namespace/root，并再次查询生产知识库无 test batch ID、fixture title 或临时路径。

生产知识库的最终验收必须使用用户真正希望保留的作者批次，不能创建后再猜测性删除“smoke 文档”。

### 7.6 回归与性能验收

- 现有 19 个测试必须继续通过，并新增批量测试。
- 单视频 Telegram URL、原生 media、目标库选择、状态命令和通知文本保持回归通过。
- discovery 50 条在正常平台响应下目标 60 秒内完成；超时进入可重试错误而不是阻塞 plugin。
- batch child 仍严格单并发；峰值内存不得明显超过现有 Whisper 基线加 discovery worker 的 metadata 开销。
- 10 条批次运行期间，单视频任务进入后能在当前 child 完成后优先领取。

### 7.7 分阶段上线

1. **基线**：先单独 review 并 commit 当前单视频功能，敏感信息扫描通过。
2. **Schema dark launch**：部署 migration、repository 与新 workers，所有 author flags 关闭；运行历史数据兼容检查。
3. **只读 discovery**：只开启 `AUTHOR_BATCH_YOUTUBE_DISCOVERY_ENABLED`，执行真实平台非污染只读验收，确认按钮保持关闭。
4. **隔离执行**：在独立 test destination 开启 execution，完成 10 条及故障注入验收并彻底清理。
5. **生产 canary**：仅 owner allowlist、每用户一个批次、最近 10 条，观察至少一个完整批次。
6. **正式首版**：保持 YouTube only；Bilibili/小红书 flags 继续关闭。

每阶段都有独立开关：

- `AUTHOR_BATCH_YOUTUBE_DISCOVERY_ENABLED`
- `AUTHOR_BATCH_YOUTUBE_EXECUTION_ENABLED`
- `AUTHOR_BATCH_BILIBILI_ENABLED`
- `AUTHOR_BATCH_XIAOHONGSHU_ENABLED`

关闭 execution 阻止新确认，但已确认批次默认继续；紧急情况使用显式“pause all batches”，不能靠回滚代码让新 schema/jobs 变成孤儿。

### 7.8 上线前检查表

- migration 已备份并在副本演练。
- 所有新 SQL 查询有目标索引和 `EXPLAIN` 复核。
- systemd units 使用最小权限、同一 secret 来源和日志限制。
- cookies 文件仍为目录 700、文件 600，未进入 Git/fixture/log。
- OpenClaw 仍是唯一 Telegram polling consumer。
- discovery worker 无媒体写入权限或通过代码/测试明确禁止下载。
- 生产 flags 默认关闭，配置缺失时 fail closed。
- 低磁盘、平台 circuit 和 outbox 告警已接通。
- 孤儿、统计不一致、生产 smoke 残留 SQL 均返回零。

### 7.9 第一版最终验收标准

- 支持规定的 YouTube 作者主页 URL，误传 playlist/search 不会启动批量。
- 预览最多扫描 50 条，只展示可验证数据，确认前无媒体下载和知识库写入。
- 用户可确认最近最多 10 个作品及一个目标知识库。
- 确认后固定 10 个或实际不足数量的 items；所有需处理 item 有唯一 child job，已存在 item 有 document 证据。
- child 自动继承目标库，不逐个弹按钮；Whisper 始终单并发。
- 三层去重确保同目标库已存在内容不重新下载、转写或重复索引。
- worker 重启后从持久状态恢复，统计最终一致。
- cookies/bot/412/429 触发平台级暂停和熔断，不形成请求风暴。
- 批次可暂停、恢复、二次确认取消；取消不破坏已完成内容。
- Telegram 只保留最新一条批次聚合状态；新状态发送并持久记录 message ID 后自动删除旧状态。
- Markdown、SQL、pgvector、items 和 batch 统计逐项一致。
- 测试环境及生产知识库无 smoke 残留，敏感信息扫描无命中。

## 8. 实施任务拆分

每个任务单独 review、测试和更新根目录 `WORK_PROGRESS_UPDATE.md`。除 I0 外，不应把多个任务压成一个不可回滚的大 commit。

| ID | 任务 | 依赖 | 完成定义 |
|---|---|---|---|
| I0 | 建立单视频稳定基线 | 无 | review 当前 diff、敏感扫描、19+ 测试通过、单独 commit |
| I1 | Migration 与应用枚举 | I0 | `0003`、模型枚举和升级/回滚演练通过，flags 默认关闭 |
| I2 | Batch repository 与状态事务 | I1 | 确认、领取、完成、暂停、恢复、取消、恢复收敛的并发集成测试通过 |
| I3 | URL 分类与 discovery adapter | I0 | YouTube 分类/规范化/flat extraction 合约测试通过，无媒体请求 |
| I4 | 平台 request gate 与 circuit | I1, I3 | 跨进程 gate、half-open probe、错误分类和退避测试通过 |
| I5 | Discovery worker 与预览数据 | I2, I3, I4 | 独立 worker 可持久发现、过期、失败恢复，真实平台只读验收通过 |
| I6 | 确认与 child job 创建 | I2, I5 | callback 重放只产生一个 batch；目标库去重和时长预算通过 |
| I7 | Batch-aware ingestion worker | I2, I4, I6 | child 静默、目标库锁定、三层去重、重启/取消/暂停测试通过 |
| I8 | Telegram 批次交互 | I5, I6, I7 | 三步确认、状态/控制命令、权限/过期/64-byte 测试通过 |
| I9 | 通知 outbox、指标与巡检 | I2, I7 | 聚合阈值、投递重试、告警查询和一致性巡检通过 |
| I10 | 隔离 E2E、部署与 canary | I1–I9 | 故障注入、10 条隔离验收、清理证明、owner canary 和回归通过 |

### 8.1 I0 稳定基线

- 只处理当前单视频 feature branch 的已有改动，不混入 batch schema。
- 执行 Git diff、secret/path 扫描、测试和真实服务健康检查。
- commit message 明确是 single-video baseline。
- 若基线本身有失败，先修复并重新验收；禁止在不稳定基础上开始 migration。

### 8.2 I1–I2 数据与事务基础

- I1 只交付 schema、模型枚举、配置项和 migration 验证，不开启入口。
- I2 所有跨 job/item/batch 写操作都收口到 repository 方法；禁止 service 中散落 SQL。
- 增加 `find_completed_document_for_destination()`，不能复用当前不区分 destination 的 `find_duplicate()` 作为批量跳过判定。
- `select_destination()` 对 `destination_locked=true` 的 job 必须拒绝修改，形成服务端最终保护。
- 提供只读一致性 SQL：孤儿 item/job、计数漂移、非法终态时间、重复 source ID。

### 8.3 I3–I5 只读发现闭环

- I3 的 URL classifier 同时供 plugin 入站和 Python CLI 使用，避免 JavaScript/Python 两套规则漂移。插件通过快速 CLI `classify/enqueue` 获得服务端结论。
- I4 先于真实 discovery 上线，确保测试和生产探测都受同一 gate 保护。
- I5 增加 `discover/status/expire/probe` CLI 与独立 systemd unit；只读 flag 开启时不渲染确认开始按钮。
- 完成真实 YouTube author 的只读验收后才进入 I6。

### 8.4 I6–I8 执行与用户控制

- I6 以一个确认事务创建 batch/items/必要 child jobs，加入目标库级重查、活动批次限制和资源预算。
- I7 在现有 service 中最小化分支：媒体处理逻辑复用，差异集中在通知模式、destination 跳转和 batch-aware repository。
- `_friendly_error()` 重构为结构化 classifier；批次 child 不能把平台级错误降级成普通下载重试。
- I8 使用 `vkb` namespace，不改动现有 `vki` 单视频 callback；命令和自然语言状态路由均有回归测试。

### 8.5 I9–I10 可靠性与发布

- I9 先写 outbox 再异步投递，通知故障永不回滚 job/document。
- 一致性巡检初期只报告不自动修改；只有第 3.8 节明确的确定性收敛可以自动执行。
- I10 使用完全隔离的 test database、Markdown root 和 vector namespace；清理命令必须使用显式测试路径/标识并先列出目标。
- 生产 canary 前保留紧急 `pause all batches` 运维入口，execution flag 默认关闭。

## 9. 关键设计决策摘要

1. **批次不复制单视频流水线**：字幕、下载、Whisper、Markdown 和索引继续只有一套实现。
2. **预览与执行分离**：过期 discovery 不等于正式 batch，确认事务固定作品快照。
3. **父级选择目标库**：child 锁定 destination 并跳过 `AWAITING_DESTINATION`。
4. **目标库级去重**：同库已有才跳过，跨库允许复用已有 Markdown/transcript。
5. **单 Whisper、独立 discovery**：保证资源安全，同时避免预览被长转写阻塞。
6. **数据库状态是真相来源**：callback、重启恢复、统计和通知都不依赖会话记忆。
7. **平台故障在父级熔断**：认证、bot、412/429 不转换成几十个独立失败。
8. **通知通过 outbox 聚合**：Telegram 故障与知识入库事务解耦。
9. **范围显式可控**：保留最近 5/10/20/50 个快捷范围；“全部”只在完整遍历作者作品列表后创建固定批次快照。
10. **生产零 smoke 污染**：真实写入测试使用隔离 namespace，生产只保留用户真正需要的内容。
