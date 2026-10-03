# Development MVP：视频视觉知识功能的 Linux 开发计划与验收规格

创建日期：2026-10-01（America/Chicago）。更新：明确当前计划阶段，后续全部实施在 Linux 服务器进行。

项目：rag_favorite。规格依据：[devplan.md](devplan.md) 与 [模型部署/价格调研](docs/research/video-rag-deployment.md)。最新选择为“transcript 先分类、仅 cooking 做视觉增强、文字 LLM 走 CCR/GPT 6 Luna、VLM/画面文字/ASR/两类 embedding 走 OpenRouter、成本优先”。旧本地 OCR/VLM 与 CCR 图片备选路线已由本次修订替代。

**当前状态：planning / planned（计划阶段），尚未开始本文新增功能的代码开发。** 当前产物是需求、模型/费用研究、开发步骤、验收规格与日志模板；所有 Phase 均为 `planned`，所有未执行测试为 `not_run`。本次只更新文档，没有安装依赖或模型、调用付费 API、修改业务代码、迁移数据库或运行测试。

**实施地点：用户的 Linux 服务器。** 从开工准备和 Phase 1 起，代码开发、依赖安装、离线测试、PostgreSQL 实验、CCR/OpenRouter 联调及后续部署都在 Linux 完成。当前 macOS 工作区用于计划整理；其绝对路径、已安装依赖、服务端口和运行结果不作为 Linux 环境事实。

本文中的任务是未来待办；第 10 节目前记录计划修订历史，并预留 Linux 实施日志。任务状态统一使用 planned / in_progress / blocked / implemented / verified；只有具备验收证据才可写 verified。实施时记录真实 Linux 代码版本、环境、命令、结果和报告位置。

内容规模：5 个 Phase、44 个开发步骤、分阶段测试/演练矩阵、7 个跨阶段端到端剧本。原任务 IDs 保留，新增云协议、费用和索引空间验收项。每个开发步骤都列出待做功能、目标与验证方式；每个阶段另有 MVP、前置条件、修改落点、验收关口和退出策略。

## 0. 五阶段总览与编号对应

为了让每个 MVP 可以单独验证，原计划重新拆分如下。**本文的 Phase 编号是新的交付编号，不等同于 devplan.md 中旧的 Phase 编号。** 不改变已有架构和用户确定的范围。

| 新交付阶段 | 核心目标 | 本阶段 MVP 能做到什么 | 对应原计划 | 必须通过的关口 |
| --- | --- | --- | --- | --- |
| Phase 1 | OpenRouter ASR/文本 embedding 基础、结构化 transcript、CCR 分类与 cooking 准入 | 能可靠保留时间、解释分类结果，并证明其他类别完全不启动视觉阶段 | 原 Phase 1 的基础部分 | 分类与兼容测试；非准入视觉调用为零 |
| Phase 2 | OpenRouter 视觉提取、代表帧 embedding artifact、持久证据与回填 | 屏幕上未口述的配方进入 Markdown，可通过现有文本检索找到；可安全回填旧 cooking 视频 | 原 Phase 1 的完整交付 | 合成/失败/恢复测试；至少 10 个真实视频的初步检索基准 |
| Phase 3 | 时间 chunk、文本空间全量迁移、独立视觉索引与召回 | 新文本与视觉空间可正确检索，结果绑定 transcript、时间和证据 ID | 原 Phase 2 | 新旧库迁移、图文时间归属、普通文本兼容 |
| Phase 4 | 时间检索、证据读取与 Telegram 截图回答 | 用户获得配方、出处、时间和最多两张相关截图 | 原 Phase 3 | MCP 契约、路径边界、真实 Telegram 端到端 |
| Phase 5 | Linux 实机验证、质量基准、发布与运行保障 | 在用户 Linux 32GB RAM / RTX 4060 Laptop 上形成云 API 管线的可重复部署与费用/质量报告 | 原验收、部署和运行内容的独立阶段 | 实机报告、完整回归、资源/恢复演练、明确上线配置 |

原计划预留的“按相关区间提取视频片段，再请求视频模型”仍是后续可选增强，**不进入本次五阶段 MVP**。Phase 5 是实机交付阶段，不默认扩展为原生视频 embedding 或新类别项目；当前稀疏代表帧视觉索引进入 Phase 3。

依赖顺序为 Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5。Phase 2 内必须先满足原 Phase 1 的真实视频质量门槛，不能把这个门槛推迟到最终上线。Phase 5 使用最终版本重新验证全链路。

### 0.1 计划阶段与 Linux 开工准备

当前只完善规格，以下均为后续待办。完成准备后从 P1-01 开始；Linux 环境准备属于开工前工作，Phase 5 是最终复验与发布阶段。

| 内容 | 当前状态 | 后续在 Linux 执行 |
| --- | --- | --- |
| 需求、模型路线与费用估算 | 已整理，待实测 | 按当前计划实施；API 联调前重新核对模型目录与价格版本 |
| 新增功能代码与迁移 | planned，未实施 | 从 Phase 1 顺序开发，记录代码与计划版本 |
| 自动测试、真实基准与账单 | not_run | 在 Linux 逐阶段执行，保留原始结果与费用记录 |
| 开发环境、访问与目录 | 待确认 | 记录服务器系统、开发用户、仓库根目录、数据库和媒体路径 |
| 发布与运行保障 | planned | Phase 5 使用最终版本复验，随后按小样本上线步骤执行 |

开工清单：

- [ ] 同步仓库、`devplan.md`、本文件和 `docs/research/video-rag-deployment.md`；记录 commit/branch、工作区改动与计划版本。本次编辑时计划文件与调研目录尚未纳入 Git，同步时应一并提交或复制，不能假定克隆代码会自动携带它们。
- [ ] 确认 Linux 发行版、CPU/RAM、Python、Node、FFmpeg、服务用户、磁盘与网络；记录真实输出，不沿用当前 macOS 工作区的探测结果。
- [ ] 在 Linux 仓库建立独立开发环境，按项目依赖与 lock 安装；此云模型路线只需媒体工具和 API 客户端，不准备本地模型权重。
- [ ] 准备开发用 PostgreSQL/pgvector、测试 collection 和测试数据目录；迁移/故障实验与已有数据分开，保留所需备份。
- [ ] 在 Linux 通过环境/secret 注入 CCR 与 OpenRouter 配置；确认实际可达地址。`127.0.0.1` 仅指 Linux 当前主机，CCR 不在同机时填写真实地址。
- [ ] 固定 corpus、旧附件、临时媒体、证据和 outbound 的 Linux 路径/权限；文档中的相对代码路径以 Linux 仓库根目录为准。
- [ ] 在 Linux 记录原项目测试基线、已有失败、依赖与配置指纹；随后按 P1–P5 开发。当前计划阶段不执行这些命令。
- [ ] 首次实际开发时更新第 10 节实施记录；仅正在执行的任务改为 `in_progress`，完成验收后才改为 `verified`。

## 1. 全阶段必须遵守的产品边界

### 1.1 输入与准入

1. 本轮修改面向现有 Telegram/OpenClaw 的视频链接与附件流程，不改旧 Web 转录页面的产品流程。
2. 所有需要判断的视频先取得 transcript。字幕成功时，不为了“看看是不是 cooking”提前下载视频画面。
3. 文本分类、摘要、标签优先使用 CCR 的 GPT 配置；现有默认选择沿用 `Codex API/gpt-6-luna`。
4. cooking 按 transcript 的内容分类，不能用 collection 名称代替。做饭步骤和食谱制作属于准入；探店、营养闲聊、仅提到食物不能自动准入。
5. 首期视觉允许类别只有 cooking。other、unknown、分类失败和纯音频均不进行视觉专用下载、场景检测、抽帧、VLM文字提取或视觉embedding调用。
6. 分类结果不自动移动知识库，目标 collection 继续由当前目标选择流程决定。
7. 总视觉开关默认关闭；开启后仍要通过分类、允许类别和视频轨检查。
8. 旧视频回填遵循相同准入。扫描多个 collection 是为了发现旧视频，不能意味着给所有文档或所有类别抽帧。

### 1.2 识别与回答

1. 关键帧是知识证据，不是 codec I-frame；FFmpeg 可以解码 P/B 帧。
2. cue 是高优先级入口，同时对准入视频用约 2 秒覆盖采样，记录稀疏采样的局限。
3. OpenRouter Qwen3.7 Flash 同次提取动作、物体关系与画面原文；不部署本地 OCR/VLM，不把图片发给 CCR。
4. 约 30 秒一批、初始最多 15 帧，控制每次输入低于 32K 价格门槛；输入估算接近门槛继续拆分。
5. 模型必须逐 frame ID 返回结果；原文/动作/未知分开，时间由程序绑定，模型不得补造数量。
6. 数字不清最多一次高清/裁剪复核，仍不清楚保留 unknown/conflict；自报 confidence 不当概率。
7. 3 分钟目标 90 帧 VLM 分析、12 帧 Gemini 视觉向量；全部可靠文字进入 Perplexity 文本索引。不同文字状态不能为凑 12 张而合并。
8. 证据截图与视觉向量数量分开；截图保存观察时间，上下文区间不代表文字持续可见。
9. 回答最多两张相关截图，缺图可回答已有可靠文字并说明缺图；取图不调用模型。

### 1.3 架构与运行

1. 保留 PostgreSQL/pgvector、collections、Markdown、采集状态机和 MCP 主契约；全部新模型调用按用户选择路由。
2. 字幕优先，无字幕调用 OpenRouter ASR；本地仅媒体解码/裁剪、场景/清晰度/hash 与数据库工作，不加载模型权重。
3. 新文本 embedding 使用 OpenRouter Perplexity；保留旧 Ollama 索引和 adapter 为迁移兼容，不自动作为云故障 fallback。
4. 本次更换文本 encoder 必须对全部可检索文本重嵌入；同维不等于同空间。仅时间 chunk 升级不要求重跑普通文档。
5. Phase 1–2 使用独立测试索引代际验证 OpenRouter 管线；生产切换在 Phase 3 全量 shadow 重建/追平后完成，切换 encoder 与 active index 必须成对。
6. 代表帧向量使用同 PostgreSQL 的独立 Gemini 空间，检索问题也用 Gemini；RRF 合并不同召回，不直接加 cosine。
7. 不整体移植 VideoRAG，不引入新向量数据库、知识图谱、多代理系统或 UI 重设计。原生视频时序 embedding 为后续范围。
8. 原视频/非证据候选临时保存；证据截图、manifest 和所需向量 artifact 长期/按引用管理。
9. 回填保留人工内容、单 worker、无每日视频限额、新任务优先；冲突 sidecar、不强行覆盖。
10. API 失败保留已有文本/证据；文本 embedding 失败可归档但标 index_pending，不能谎报已可检索。暂停/取消传播，重试/恢复不清零预算。

## 2. 通用数据、状态与预算

### 2.1 全链路数据流

```text
Telegram URL / 附件
    → 字幕优先 / OpenRouter Qwen ASR
    → TranscriptResult（实际时间或明确 coarse 分片范围）
    → CCR/GPT TranscriptAnalysis（分类、依据、基础摘要/标签）
    → gate
        ├─ disabled / other / unknown / audio_only
        │    → 文本归档 + OpenRouter 文本 embedding（不调用图片模型）
        └─ cooking eligible
             → 本地 cue/scene/采样/清晰度/hash
             → OpenRouter Qwen3.7 Flash 动作 + 画面原文
             → 一次有界复核 + manifest/证据截图/Visual Timeline
             → Perplexity 文本 embedding + Gemini 代表帧 embedding artifact
             → Phase 3 新空间完整重建/切换 + 独立视觉表 + RRF
             → Phase 4 search / evidence get / Telegram 最多两图
```

所有 OpenRouter 请求进入同一 role-aware usage ledger；CCR LLM 费用单列。

### 2.2 公共数据契约

| 对象 | 必须表达的信息 | 验证要求 |
| --- | --- | --- |
| TranscriptResult | 完整文本、segments、language、timing_precision | 保留现有字符串 wrapper；时间来自解析/转录 |
| TranscriptSegment / Word | 稳定 ID、start/end、原文、可选概率 | 有限非负浮点秒，end ≥ start；不靠 LLM 生成时间 |
| ContentClassification | cooking/other/unknown、decision、segment 依据、reason | 引用 ID 必须存在；覆盖不足不默认 other |
| VisualCue | segment IDs、时间、命中规则、上下文 | 只能引用已有 transcript；规则命中可解释 |
| Scene / CandidateFrame | 上下文区间、目标采样时间、实际 PTS/观察时间、frame ID | 目标时间与实际时间分别保存；VFR 不按序号推算 |
| VisibleText（FrameDescription 内） | 画面原文、数量/单位 token、readability、conflicts | 无字框/置信度也合法；不把自报 confidence 当校准概率 |
| FrameDescription | frame ID、动作/物体描述、visible_text、readability、conflicts | 合法 ID 一一对应；时间由程序绑定，未见内容不补造 |
| VisualDescription / Evidence | 描述、观察时刻、区间、evidence ID、提取方式、验证状态 | 图片、原文、状态、时间和来源始终绑定 |
| Manifest | schema/revision、来源 hash、transcript、配置、证据、coverage、预算 | 持久目录中的引用存在；可验证、可恢复 |
| ContentChunk（Phase 3） | content、modality、可空 start/end、metadata | 无时间不伪造；证据只能绑定所属区间 |
| EmbeddingSpace / Generation | role、encoder/provider revision、dimensions、preprocessing、active generation | 同维异空间不得互查；切换与查询 encoder 成对 |
| VisualEmbeddingArtifact | evidence/image hash、vector、Gemini space、观察时间、revision | 每张代表图一个向量；不能聚合后假装逐图结果 |
| ModelUsage | role/request ID/model/provider、token/秒/图、cost/预留、状态 | actual 与 estimate 分开；响应丢失保留未知费用 |

内部使用 Pydantic。OpenRouter ASR/VLM/embedding 在 ingestion 中通过异步 HTTP 调用；本地 FFmpeg/scene/hash 阻塞工作不进 event loop。核心同步 embedding 契约保留，provider factory 统一选择与校验。

### 2.3 状态区分

| 范畴 | 计划值 | 应代表什么 |
| --- | --- | --- |
| 分类 category | cooking / other / unknown | 内容判断，不是 collection |
| 分类 decision | eligible / not_eligible / uncertain | 准入结论，与有效 segment 依据匹配 |
| 视觉 status | success / partial / failed / skipped | success 不等于发现整视频所有字卡；仍要看 coverage |
| 视觉 reason | disabled、not_eligible_category、category_uncertain、classification_unavailable、audio_only、no_provider、no_api_key、model_unavailable、rate_limited、usage_unknown、source_unavailable、timeout、budget、parse_error 等 | 可区分“没有做”“没做完”“做了但失败” |
| 证据 readability / verification | readable、unreadable、uncertain、conflict 等受控值 | 描述证据质量，不能与 job 是否成功混用 |
| 回填更新结果 | 已更新、已是同 revision、merge_conflict、source_unavailable、失败待重试/终止 | 可汇总，可恢复，避免重复扫描无限重试 |

这些辅助状态存在 metadata/frontmatter/manifest 中；不增加视觉专用 JobState。原有 `JobState` 与数据库 CHECK 保持一致。具体内部枚举在 Phase 1 固定，后续不可随意换值而不做兼容。

### 2.4 初始预算、模型与费用

| 项目 | 初始值 | 执行规则 |
| --- | --- | --- |
| 文字 LLM | CCR `Codex API/gpt-6-luna`，Responses | 分类/摘要/标签/问答；图片不走 CCR |
| VLM / 画面文字 | OpenRouter `qwen/qwen3.7-flash` | Chat Completions；输入 $0.03/M、输出 $0.13/M，单次 <32K |
| ASR | OpenRouter `qwen/qwen3-asr-0.6b` | audio/transcriptions；$0.00000333/秒；字幕命中跳过 |
| 文本 embedding | `perplexity/pplx-embed-v1-0.6b`，计划 1024 维 | $0.004/M tokens；按实际响应验证维度，不沿用 Qwen query prefix |
| 视觉 embedding | `google/gemini-embedding-2`，计划 768 维 | 图像 $0.45/M tokens，约 $0.00012/张；同 encoder 编码问题 |
| cue padding / 初次 / 密集采样 | ±5 秒 / 1 秒 / 0.5 秒 | 重叠合并；不明确窗口局部补采，每轮最多 20 张 |
| 覆盖采样 | 约 2 秒，长视频按全局预算降密度 | 3 分钟约 90 帧；和 cue/scene 共享额度 |
| 候选帧 / 持久证据 | 1000 / 100 | 本地筛选候选；不为每候选付费，不大量存重复字卡 |
| VLM 请求 / 发送图像 | 各最多 100/视频 | 多图逐张计数，失败/裁剪/重试都累计 |
| VLM 批量 / 并发 | 约 30 秒、最多 15 图 / 1 | 输入目标 24K，32K 前继续拆分；max output 初始 512/批 |
| 首轮图片 | 最长边 1024 | 原图保留，一次高清/裁剪复核；不是固定 image tokens 保证 |
| Gemini 代表帧 | 3 分钟目标 12，最多 24 次图像输入 | 证据与向量数量分开；上限含重试，超额记视觉索引 partial |
| OpenRouter 金额 | 3 分钟目标 $0.01；每视频停止阈值 $0.03 | ASR/VLM/两类 embedding 合计；CCR/迁移/查询另记 |
| 视觉累计预算 | 600 秒 | 新增视觉下载、scene/frame、VLM、图像 embedding；非全任务 SLA |
| cue / 覆盖配额 | 80% / 20% | 可互借，不越过全局上限 |
| 可读性判断 | unreadable/uncertain/conflict + 邻帧一致 | 不使用旧 OCR 0.90 置信阈值；未知不补猜 |
| cache / outbound TTL | 30 天 / 24 小时 | 已引用证据不删除 |
| 磁盘 / 回答图片 | ≥5GB / ≤2 张 | 低盘暂停回填；工具强制两图 |
| temporal chunk | 20–45 秒目标，60 秒/1800 字符硬限 | 无细边界旧长段使用 text fallback |

单个 3 分钟视频预算：90 帧 VLM，输入按每图 1000 tokens + 3000 提示词/转录、输出 1800（含计费推理），文本 embedding 5000 tokens，音频 180 秒。12 张代表帧方案约 **$0.0050834**，预算 **$0.01**；全部 90 张向量约 **$0.0144434**，预算 **$0.02–$0.03**。这些不是实测固定费用；图片 token 化、推理、重试/供应商会改变账单。价格日期 2026-10-01，来源与配置模板见 devplan.md 第 5–7 节。

请求发出前持久化请求/图像/预计费用预留；响应后以 usage.cost 对账。账单未知时保留预留，不计零、不自动无限重发。美元阈值是停止新增调度的阈值，上游未确定计费不能保证绝不越界；限制每批输入/输出、留安全余量、报告 overrun。恢复不清零，暂停/停机等待不计视觉工作时间。全库重嵌入与后续查询另有 job/ledger，不挤进单视频预算。

### 2.5 每个 MVP 的完成定义

每阶段都要同时具备：可复现功能、自动化测试、明确失败路径、与旧行为的兼容说明、实际运行记录、可关闭/恢复策略。仅“代码能运行”或“模型返回过一次内容”不构成阶段完成。

日志必须记录 commit/patch、配置指纹、模型/prompt/schema 版本、测试命令、报告位置和未解决问题。测试未执行写 not_run；失败写 failed；不能以文档中的预期结果代替真实证据。

## 3. Phase 1：transcript 时间基础与 cooking 准入

### 3.1 阶段目标与 MVP

**MVP：输入 Telegram 视频链接/附件，获得结构化 transcript；CCR/GPT 给出可验证的内容分类；只有 cooking eligible 的视频能到达视觉入口，其他输入继续正常归档。** 本阶段使用视觉 spy/fake 验证准入，真实 VLM 提取在 Phase 2 完成；ASR 与文本 embedding 在独立测试代际可用，不提前切换生产索引。

用户可以看到/维护者可以查询：分类结果、跳过原因和分析版本；其他内容依旧得到原有转录与摘要。默认关闭时不产生图片处理。

前置条件：第 0.1 节 Linux 开工准备完成；记录该服务器上的核心/ingestion 测试基线，准备 cooking/other/unknown transcript fixture，明确配置和结构化对象位置。无需 GPU，也不需要下载模型权重。

### 3.2 开发步骤

#### P1-01：冻结兼容边界与测试基线

- [ ] 记录 Linux 仓库 commit/branch、计划版本、开发环境和测试数据库；按同步后的实际源码复核本文落点。
- [ ] 记录字幕、当前Whisper/计划OpenRouter ASR、URL、附件输入路径及 cleanup 时机。
- [ ] 记录现有 Markdown、Enrichment、JobState、去重和通知契约。
- [ ] 核对 `_stage` metadata 写入，避免新分类字段被后续阶段整体覆盖。
- [ ] 保存旧流程的典型 fixture：普通文本、字幕视频、无字幕视频、纯音频、重复视频。
- [ ] 使用现有测试命令建立真实基线，已存在失败单独登记。

目标：后续新增逻辑有稳定插入点，不改旧字符串 API、不丢任务 metadata。产物：兼容清单、fixture、基线日志。验证：原测试通过或明确列出先前失败。

#### P1-02：结构化 transcript 类型和兼容 wrapper

- [ ] 定义 TranscriptWord、TranscriptSegment、TranscriptResult。
- [ ] 新结构化方法保留浮点秒、language、timing_precision 和原始 segment 顺序。
- [ ] 现有调用方仍能通过旧方法得到原 Markdown 字符串。
- [ ] 生成稳定 segment ID；同一 transcript 的分类/cue/manifest 使用同一套 ID。
- [ ] 校验 NaN、Infinity、负值、反向区间；缺时间不能变成 0 秒的伪精确区间。
- [ ] 提供结构化结果与 Markdown 的对应关系，避免兼容 wrapper 再次丢毫秒。

目标：时间不再只存在于被截断的标题中。产物：数据类型、转换器、兼容测试。验证：原方法返回类型与旧调用方不变，新方法保留精度。

#### P1-03：字幕解析与 OpenRouter ASR 时间适配

- [ ] VTT/SRT 保留毫秒/小时，修复全局去重，保留远处重复指令。
- [ ] 字幕成功不调用 ASR；无字幕音轨调用 Qwen3 ASR 0.6B 的 transcription endpoint。
- [ ] 兼容 JSON/base64 或 multipart、实际大小/超时限制；只上传所需音频，不默认整视频。
- [ ] 校验实际供应商 segment/word timestamps、语言、结果；不由模型宣传推断接口一定返回词级时间。
- [ ] 无细时间时 FFmpeg 约 30 秒音频分片，返回文本绑定片段 coarse 范围；切分重叠/去重策略固定，offset 与音视频起始对齐。
- [ ] 无可靠 offset 标 approximate；空音轨、静音、坏响应与服务失败有明确状态，不假装已转录。
- [ ] 用音频 hash/model/语言/分片时间配置缓存，记录秒数、请求 ID、usage.cost/预留和失败重试。
- [ ] 保留旧 Faster-Whisper 字符串 wrapper 兼容，新云配置不自动切回本地模型；暂停/取消生效。

目标：字幕/ASR 共用时间契约，词级与粗分片精度诚实表达。验证：协议 fixture、offset、静音、只返回 text、429/timeout；实际时间能力 smoke 后报告。

#### P1-04：CCR 文本 adapter 与可靠输出校验

- [ ] 使用独立文本配置 OPENAI_API_STYLE/BASE_URL/MODEL/API_KEY。
- [ ] 显式使用 `Codex API/gpt-6-luna` 路由，避免 CCR 默认 provider 造成静默替换。
- [ ] 首选 Responses adapter，保留已有非 CCR Chat 配置兼容。
- [ ] 分类请求只含 transcript 文本与来源元信息，不含图片。
- [ ] 输出进入 Pydantic 校验，不能因为发送 JSON Schema 就相信结果一定有效。
- [ ] schema 参数不兼容时，仅在同 provider/model 下改为 JSON-only prompt + 本地校验；不静默换模型。
- [ ] 客户端 API key 通过配置注入，日志不输出凭据；不读取 CCR management token。
- [ ] 设置有界文本长度/超时，明确解析失败和服务不可用 fallback。

目标：文本路由可验证且结果可控。验证：fake Responses/Chat、错误 JSON、未知字段、缺字段、超时均有确定结果。真实 CCR 兼容 smoke 单独记录，普通 CI 不调用付费模型。

#### P1-05：TranscriptAnalyzer 分类与基础 enrichment

- [ ] 返回 category、decision、evidence_segment_ids、reason、覆盖记录及基础摘要/标签。
- [ ] cooking 阳性要求有实际做饭/食谱制作内容；食物关键词或 collection 名称不足以准入。
- [ ] 检查所有 evidence IDs 属于输入 transcript；虚构 ID 导致不准入。
- [ ] 长 transcript 按有界顺序批次分析，保留完整批次覆盖，不只看标题/开头。
- [ ] 聚合规则固定：有效、足够明确的 cooking 制作依据可准入；未完整分析且没有明确阳性依据为 unknown；冲突无法解决为 uncertain。
- [ ] 基础 enrichment 尽量复用，非 cooking 不再做一轮重复摘要调用。
- [ ] 分类/摘要部分失败分开处理：坏摘要不能变成无依据 cooking，坏分类不能丢转录。

目标：分类可解释、可缓存、不会因一处“酱汁”误触发。验证：跨 collection、后半段 cooking、混合内容和不完整覆盖 fixture。

#### P1-06：视觉 gate 与零调用保证

- [ ] gate 同时检查总开关、category 允许列表、decision 和视频轨。
- [ ] gate 位于任何视觉专用下载、scene、frame、VLM、visual embedding provider 调用之前。
- [ ] other/unknown/分类不可用/audio-only/disabled 均直接走文本保存路径。
- [ ] 在 Phase 1 使用 fake 视觉入口和调用计数 spy，避免“只是没拿到结果，实际上已下载/抽帧”。
- [ ] 只支持 cooking profile；other 的屏幕指向词不能绕过 gate。
- [ ] queued/backfill 将来复用相同 gate，不另写更宽松的判断。

目标：非 cooking 的额外视觉成本为零。验证：负例各视觉依赖调用次数都等于 0，而不是只检查最终 status。

#### P1-07：分类缓存、metadata 与失败归档

- [ ] 分类 cache key 包含 transcript checksum、classifier model/prompt/schema 及允许类别指纹。
- [ ] 只有输入完全匹配的缓存能用于服务失败降级；修改 transcript 或策略即失效。
- [ ] 分类写入 job.metadata 与 `video_classification` frontmatter，不改变原 transcript_checksum。
- [ ] 保存 category/decision/依据/覆盖/版本和跳过原因。
- [ ] 分类不可用时使用有效缓存或 unknown；基础摘要失败时使用确定性文本 fallback。
- [ ] 保证成功转录继续 staging、目标选择、持久化、indexing 和现有通知。
- [ ] 暂停/取消异常保持传播，普通 API 故障捕获不吞控制信号。

目标：CCR 出错时视频仍可归档，且不会因为 classifier 失败而上图片模型。验证：故障注入和 checksum/metadata 断言。

#### P1-08：OpenRouter 文本 embedding、配置与状态

- [ ] 核心 EmbeddingProvider factory 覆盖 ingest/query/status/smoke 和 legacy adapter，新增 OpenRouter text client。
- [ ] 支持 backend、URL、model、dimensions、api_key_env 与有界 batch/timeout；凭据只来自 secret/environment。
- [ ] 校验 OpenAI-compatible embedding data/index/count、维度、有限数值；不沿用 Ollama response shape 或 Qwen 专用 prefix。
- [ ] 新云管线在独立 PostgreSQL 测试 schema/索引代际验证，不混写/混查旧 Ollama 空间；生产切换留给 Phase 3。
- [ ] 新增总视觉开关、允许类别、各角色 model/endpoint、预算与云就绪状态；默认关闭视觉，旧配置可启动。
- [ ] 缺 key/不可用模型输出就绪错误，不下载本地权重、不自动切模型；文本 embedding 失败保持归档并标 index_pending。
- [ ] API 模型/维度/预处理组成 space ID；明确新模型与旧模型同为 1024 维也不兼容。
- [ ] status 脱敏报告 CCR 与 OpenRouter 各角色、active space、分类/跳过、预计/实际费用；配置变更按原生命周期重启。

目标：Phase 1 具备云 ASR/文本 embedding 与准入基础，后续 benchmark 可用 OpenRouter 索引。验证：fake HTTP、旧配置、错维度/坏向量/计费缺失及新旧空间隔离。

### 3.3 修改落点

现有：`ingestion/backend/transcriber.py`、`video_processor.py`；`ingestion/backend/ingestion/media.py`、`service.py`、`models.py`、`enrichment.py`、`markdown.py`、`config.py`、`repository.py`。建议新增 `temporal_models.py`、`content_classifier.py`、OpenRouter ASR/usage adapter；核心修改 `embedding.py`、`config.py`、`rag.py` 的 provider factory，具体包位置按现有 import/打包边界固定。以上新增文件是实施建议，目前未创建。

### 3.4 必需测试

| ID | 输入/触发 | 必须验证的结果 |
| --- | --- | --- |
| T1-01 | SRT/VTT 01:02:03.456 | 浮点秒和毫秒保留，wrapper 可继续使用 |
| T1-02 | 相邻滚动字幕 + 五分钟后重复同句 | 只去近邻重复，后一次指令仍存在 |
| T1-03 | ASR word/segment/text-only、音频分片 offset | 真实时间与 coarse 精度区分；不伪造词级时间 |
| T1-04 | NaN/Infinity/负时间/反向区间 | 拒绝非法结构；正常未知时间不伪造 |
| T1-05 | 明确食谱制作 | cooking eligible，依据 IDs 存在 |
| T1-06 | 探店、营养讲解、仅提到食物 | 不准入；五类视觉操作调用都为 0 |
| T1-07 | cooking collection 中非 cooking | 不因 collection 强行准入 |
| T1-08 | 其他 collection 中真正食谱 | 可以准入，保存目标不自动改变 |
| T1-09 | 长 transcript 后部食谱；某批次失败 | 前者可发现阳性；无阳性且覆盖不全为 unknown |
| T1-10 | 模型引用不存在 ID / 冲突 category | 拒绝无效准入，不开启视觉 |
| T1-11 | CCR 断连、超时、坏 JSON | 有效同版本缓存可复用；否则 unknown，转录仍归档 |
| T1-12 | transcript/model/prompt/类别策略改变 | 旧分类缓存失效，不能沿用旧 eligible |
| T1-13 | 总开关关闭、unknown、纯音频 | 视觉下载、scene、抽帧、VLM、视觉embedding 全为 0 |
| T1-14 | other 中出现“屏幕上的配方” | cue 不绕过类别 gate |
| T1-15 | `_stage` 和后续 metadata 更新 | 新分类字段仍存在，原 job 控制字段仍存在 |
| T1-16 | 非 cooking 正常导入 | 基础 enrichment 复用；不重复做同内容文本分析 |
| T1-17 | 暂停/取消发生在分类前后 | 控制生效，不被 API fallback 捕获 |
| T1-18 | 原普通文档与所有旧输入 fixture | Markdown、目标选择、索引、通知回归通过 |
| T1-19 | OpenRouter ASR endpoint、大小/超时/静音/429 | 有界请求，字幕命中零 ASR，秒数与费用可追溯 |
| T1-20 | text embedding 的 data/index/count/维度/NaN | 合法响应正确映射，错维/非有限值拒绝 |
| T1-21 | 同维 Ollama/Perplexity、query/doc 策略 | 不混查混写，旧 Qwen prefix 不无条件复用 |
| T1-22 | 缺 key/模型不可用/embedding failure | 就绪脱敏，归档 index_pending，不自动本地 fallback |

### 3.5 验收条件与交付证据

- [ ] T1-01–T1-22 与受影响旧测试通过；文本 embedding 在隔离代际可用。
- [ ] 输出一个 cooking 阳性和一个 other 阴性的完整 job/Markdown 样例。
- [ ] 输出负例调用计数报告，所有视觉项为 0。
- [ ] 分类依据、版本、覆盖、跳过原因能从 status/metadata 追溯。
- [ ] CCR 文本失败时仍能归档，不丢成功转录。
- [ ] 旧配置、关闭开关和无 GPU 环境可工作。
- [ ] 日志清楚写明真实 VLM 提取和视觉检索尚未由本阶段验证。

退出策略：关闭新增视觉开关；保留兼容 wrapper 和分类元数据。Phase 1 无新增 RAG 表结构，可继续旧检索。

## 4. Phase 2：OpenRouter 视觉提取、代表帧向量与旧视频回填

### 4.1 阶段目标与 MVP

**MVP：对已准入的 cooking 视频，识别画面中未口述的食谱文字/比例，写入可检索的 Visual Timeline，保存对应截图，并安全更新旧 cooking 文档。** 本阶段继续使用现有字符 chunk，不能提前宣称检索结果已拥有可靠结构化时间字段。

最低演示：博主只说“加入屏幕上的料汁”，系统的 Markdown 包含画面原文“生抽 2 勺、老抽 1 勺、醋 1 勺”和实际截图时刻；现有 rag_search 能找到这段原文。此时完整的 Telegram 取图能力留给 Phase 4。

前置条件：Phase 1 verified；结构化时间和 gate 可用；OpenRouter VLM/视觉 embedding 的独立配置与预算准备好；合成视频 fixture、fake provider 和真实基准素材目录可用。素材尚未提供时可以开发离线测试，但阶段最终质量验收保持 not_run，不能据此进入下一阶段。

### 4.2 开发步骤

#### P2-01：云 provider、媒体依赖与协议边界

- [ ] 新增 Fake/OpenRouter Vision 与 VisualEmbedding provider，明确各自 endpoint/request/response schema。
- [ ] 使用 `qwen/qwen3.7-flash` 与 `google/gemini-embedding-2`，key 从环境读取，不走 CCR 图片转换器。
- [ ] 保留 FFmpeg、可选 scene/image processing 的 lock；无 GPU 核心可安装，optional lazy import。
- [ ] 模型就绪只读目录/配置；真实推理另设显式 smoke 入口和费用报告，不在启动时自动付费。
- [ ] VLM Chat、embedding、ASR 参数按接口实际支持适配；ASR 不盲用 Chat 的 provider sort/order。
- [ ] 缺 key、模型不可用、网络断开报告状态，不下载本地模型、不自动换更贵模型或 encoder。

目标：云 API 可替换且协议边界可验证，媒体工具不变成本地模型依赖。验证：fake HTTP/参数/错误、核心 package import/安装。

#### P2-02：只对准入视频准备媒体源

- [ ] `prepare_visual_media` 只能在 Phase 1 gate 之后调用。
- [ ] URL 有字幕时，先分类，再按需要补取视频；保留原平台认证、session 恢复和限速。
- [ ] 无字幕 URL 沿用音频转录，再根据分类取得视频；能合法复用已有源时复用。
- [ ] 附件复用 cleanup 前尚存在的 source，不重复下载。
- [ ] 继承当前视频时长/文件大小、公共 URL 和受控媒体路径限制；不新建更宽松下载器。
- [ ] 下载受视觉剩余 deadline 控制；视频轨不存在时标 audio_only。
- [ ] 计算实际取得视频的 SHA256；URL 字符串不能作为内容不变的证明。
- [ ] 受保护 CDN/signed URL 仅临时使用，不写入公开检索 metadata 或日志。

目标：仅在需要时增加画面下载，不增加非 cooking 成本。验证：有字幕 cooking/other、无字幕、附件、失效认证、超大小/时长、无视频轨。

#### P2-03：cue 规则、窗口与低频兜底

- [ ] 首期 cooking profile 覆盖“屏幕上、如图、这个比例、打上屏幕、on screen/as shown”等指向词。
- [ ] 加/倒/混合等动作须与料汁/酱汁/配比等组合；单个“加”不产生高优先级。
- [ ] cue 保存原始 segment ID、规则和上下文；优先 word 时间，否则整段时间。
- [ ] 生成前后各 5 秒窗口，裁到视频边界；排序并合并重叠窗口。
- [ ] 初次每秒取帧；提示未读出配方/不可读/数字冲突才局部加密到半秒。
- [ ] 长 segment 的窗口连续分段处理，不把几十秒讲话错误当作一个瞬间。
- [ ] AdaptiveDetector 生成场景代表候选；初始最短场景 0.5 秒。
- [ ] 无切换时整视频为一个场景；detector 异常只降级，不丢 cue 路径。
- [ ] 只对准入 cooking 的未覆盖区间使用 `max(2 秒, 时长/90)` 覆盖采样，和 cue/scene 共享发送图像预算。
- [ ] 可选文本语义 cue 默认关闭；启用后仅返回已有 segment ID，与确定性规则共享预算。

目标：优先把成本放在相关窗口，同时知道哪些区间没有充分看过。验证：中文/英文 cue、重叠、头尾、长段、无 cue、scene 失败。

#### P2-04：准确抽帧与时间校准

- [ ] 使用解码路径获得目标画面，可取 P/B 帧，不只选择 I-frame。
- [ ] 从帧 PTS/time_base/起始偏移取得实际 observed_at；另存 target_time。
- [ ] 支持 VFR、非零 source start time、音视频流起始偏移；统一到 transcript 所用视频时间线。
- [ ] 明确记录时间精度/偏移来源。偏移无法可靠确认时标 approximate，不输出毫秒级确定声明。
- [ ] 不用 `frame_number / fps` 或文件名作为原始时刻。
- [ ] 分组解码/抽帧，避免每张图片独立重启长视频完整解码。
- [ ] 候选生成有上限、有背压；超时/取消可停止 FFmpeg/scene 子进程。
- [ ] 合成 fixture 包含 timecode 或已知字卡出现区间，比较观测时间与真实区间。

目标：保存的是“这张图何时出现”，而非计划采样时间。验证：确定性 fixture 中画面与已知源帧匹配；误差不超过可证明的帧时间边界，不能用宽阈值掩盖偏移错误。

#### P2-05：候选筛选、分组和证据状态

- [ ] 完全相同像素/hash 预去重；pHash 只分组，不删除可能含 2→3 数字变化的近似图。
- [ ] 本地清晰度、cue 相关性、时间/scene 覆盖做发送前优先级，候选最多 1000，不逐候选付费。
- [ ] 3 分钟约 90 帧覆盖，与 cue/scene 共享最多 100 张发送图像；超预算保留 partial/剩余窗口。
- [ ] 每约 30 秒分组、初始最多 15 图；按输入估算目标 24K 拆分，避免达到 32K 更高价档。
- [ ] 读取后按新增文字/数量/单位、完整性、动作证据与时间覆盖挑选持久截图，保留不同状态。
- [ ] 只规范化排版供比较，保留中文原文/数字/单位；模型自报 confidence 不作为概率。
- [ ] 精选约 12 张代表帧用于 Gemini embedding，不因向量配额删掉持久文字证据；默认最多 24 次图像输入。

目标：付费输入可控且重要数字/动作不被相似去重抹掉。验证：候选/发送/向量三个计数、短显、数字变化、时间覆盖和批次拆分。

#### P2-06：OpenRouter VLM 动作/文字提取与一次复核

- [ ] 同一次请求返回每个 frame ID 的动作/物体关系、visible_text、readability、conflicts；不要求字框/置信度。
- [ ] 验证 ID 来自输入且一一对应，缺失/重复/越界/坏 JSON 分项失败；模型返回时间无效。
- [ ] prompt 禁止由 transcript 或常见配方补造用量；描述只引用实际观察画面，稀疏帧不能证明动作全过程。
- [ ] 模糊数字/冲突只在剩余额度内做一次高清/裁剪复核，保留原图和裁剪坐标。
- [ ] 429/5xx 有界退避、控制重试与最大输出；含推理的输出 tokens 都记账，响应丢失保留未知支出。
- [ ] 不发送图片给 CCR，不切本地模型，不自动升级高价 VLM；仍失败保存 partial/transcript-only。
- [ ] 可靠视觉事实给 CCR 文本 enrichment；未知数字不进入确定摘要，冲突不因供应商名称被覆盖。

目标：低价云 VLM 同时完成动作与画面文字，未知诚实保留。验证：多图 response、原文/未知/冲突、费用/重试、CCR 图片调用恒为 0。

#### P2-07：云请求、金额、图像与时间调度

- [ ] 初始云并发 1，按角色控制限流/timeout；本地只运行媒体工具，不调度模型 GPU 加载。
- [ ] 分别统计请求≤100、VLM 图像≤100、候选≤1000、持久证据≤100、Gemini 图像输入≤24。
- [ ] 同视频 ledger 覆盖 OpenRouter ASR/VLM/两类 embedding；目标 $0.01、默认新增调度停止阈值 $0.03。
- [ ] 发出前持久化预留，返回 actual usage.cost 后对账；max input/output、安全余量和单价 revision 参与估算。
- [ ] 不明请求按已发送/费用未知处理；恢复不清零，不盲重发，不宣称美元阈值是上游绝对硬限。
- [ ] 视觉累计 600 秒包含视频补下载/scene/frame/VLM/图像 embedding；单请求 timeout 不超剩余工作时间。
- [ ] 每批与子进程边界检查 pause/cancel；达到任一限额保存 partial/coverage，终止新增请求和长媒体子进程。
- [ ] API 重试、一次裁剪复核计额度；暂停/停机等待不计工作时间；终止清理开销另报。
- [ ] 保留平台 auth/deferred/resource 恢复体系；全库迁移/后续查询用独立 ledger，不设每日视频数量上限。

目标：成本优先能在调度中执行，崩溃后不会重新获得完整额度。验证：fake clock/usage、32K 门槛、多图计数、响应丢失/overrun、checkpoint 恢复和取消。

#### P2-08：分层缓存、coverage 与 manifest

- [ ] 分类缓存继续使用 Phase 1 key；所有视觉缓存复用仍先执行当前 gate。
- [ ] 场景 cache key 使用 video hash + detector/decoder/config，不绑 transcript。
- [ ] cue/采样 cache key 使用 transcript timing/cue/sampling 指纹。
- [ ] ASR cache key 包含音频 hash/model/语言/分片/offset/时间能力；VLM key 包含有序图片/裁剪 hash、IDs、provider/model revision、context/prompt/schema。
- [ ] 文本/视觉 embedding cache key 包含各自 encoder space/model/revision/dimensions/preprocess/input hash，不跨空间复用。
- [ ] 代表帧 artifact 保存一图一向量、evidence/time/image hash 与 space ID，供 Phase 3 入库；费用入 ledger。
- [ ] 合并 revision 包含上层 revision、分类准入、云路由、代表帧策略与合并版本。
- [ ] manifest 保存源信息、时间/精度、逐帧原文/动作/状态、证据、模型/空间版本、预算与实际费用、已处理/未处理窗口。
- [ ] coverage 分开记录 transcript 已分类范围、cue 扫描范围、实际视觉采样范围、窗口完成度和跳过原因；不把稀疏点采样记成每一帧都分析过。
- [ ] 修改 prompt 只使相关 VLM/合并层失效，不让所有 scene/已命中图片分析重跑。
- [ ] cache 校验图片存在/hash 正确；缓存损坏能失效重建，不返回失配证据。

目标：结果可复用、可审计，partial 可解释。验证：逐层 key 改变矩阵、缺图、损坏、命中零推理、gate 关闭后有旧缓存也不调用视觉。

#### P2-09：新视频 Markdown、持久资产和统一索引

- [ ] 在确定目标 collection 前，分析结果和资产保存在不被 job cleanup 误删的受控 staging/cache。
- [ ] 目标确定后，写入 `Assets/video/<document-id>/<analysis-revision>/` 的 manifest 与证据图。
- [ ] 图片与 manifest 完整验证后，Markdown 才引用；跨目标保留各 collection 自有相对副本。
- [ ] `Visual Timeline` 放在 Transcript 前，含时间范围、截图时刻、画面原文、状态、方法和相对证据引用。
- [ ] 管理块写入 start/end marker 与生成块 hash，为将来安全更新留边界。
- [ ] frontmatter 保存 video_classification、video_visual、revision、manifest、coverage、usage/复核/失败信息。
- [ ] 新 cooking 视频最终文本 enrichment 加入可靠视觉事实；uncertain/conflict 用量不进入确定摘要。
- [ ] 新视频摘要失败使用确定性 fallback；非 cooking 使用 Phase 1 基础 enrichment。
- [ ] 保留完整 transcript 和原 transcript_checksum；全文 hash 随新增视觉内容更新。
- [ ] 复用核心 ingest_file；使条目足够紧凑，以现有 1800 字符 chunk 尽量保持原文/证据引用同块。
- [ ] 没有视觉结果不生成虚假时间线；视觉失败仍完成文本归档和索引。
- [ ] 清理原视频/非证据候选时，不删除已引用资产、跨 collection 副本或当前恢复 checkpoint。

目标：截图文字进入已有检索系统，图片不因 cleanup 失联。验证：新录入、目标后选、跨目标、全文 hash、摘要范围、原文件 cleanup。

#### P2-10：重复视频与更新入口

- [ ] 正常重复 source 可复用有效同 revision 分析，保持新任务原有交付方式。
- [ ] 旧 Markdown 没视觉或 revision 过期时，安排显式更新，不能永久被“复用旧 Markdown”挡住。
- [ ] 已有目标文件的视觉更新，不走普通 persist 的“丢弃新 staging”分支。
- [ ] 文档 ID、标题归档路径和 source_path 保持稳定，不能新增第二份菜谱。
- [ ] 当前 document 仍关联原来源 job，另记录 backfill/latest visual job；不要假定原 upsert 会改 job_id。
- [ ] 明确 normal reuse、visual upgrade、conflict 三种处理结果。

目标：去重不会阻挡功能升级，也不会产生重复知识文档。验证：同 source、旧 revision、当前 revision、已有目标、两个目标分别更新。

#### P2-11：回填发现、准入、排队与控制

- [ ] 增量扫描可写 collections 中具有视频来源证据的旧文档；普通笔记不作为视频。
- [ ] 优先已有 job/document 登记，再兼容 frontmatter；读取已有 transcript 分类。
- [ ] 只排入 cooking eligible；other/unknown 记录分类但不进入画面队列。
- [ ] 开始处理前重新检查 transcript checksum、允许类别和开关；排队后策略变化也必须遵守。
- [ ] 同 source document + destination + desired revision 的 enqueue 用事务/锁保证幂等。
- [ ] 每个空闲轮次最多排入一个回填；扫描/分类有界分批，不能长时间阻塞新任务。
- [ ] 当前回填视频完成后，新任务与作者批次优先；无每日数量上限，不添加并行 worker。
- [ ] 旧 URL 通过原下载/认证路径重取；已删除附件无源时 source_unavailable，本轮停止自动重试。
- [ ] 资源低于 5GB 暂停；认证/平台限速沿用已有恢复机制。
- [ ] 增加 `visual-backfill status/pause/resume/retry` 维护能力和 `/video_backfill` Telegram 对应入口，命令属待实现。
- [ ] 批次汇总报告成功、partial、缺源、冲突、终止失败；不逐视频刷通知。

目标：可控地升级旧食谱，分类失败/缺源不会占用无限循环。验证：priority、pause/resume、唯一队列、configuration changed、无源和报告。

#### P2-12：保留人工编辑与可恢复文件/数据库更新

- [ ] 保存原文完整 hash、目标路径、生成块 hash、原索引 revision 与私有备份。
- [ ] 只维护程序拥有的视觉块和 video_visual/video_classification 相关元数据，保留摘要/正文/备注及其他 YAML。
- [ ] 原文件无管理块时，在唯一 Transcript 标题前插入；位置不明时采用明确追加策略。
- [ ] 同名非管理视觉块、坏 YAML、无效 marker 或人工修改生成块 → 候选 sidecar + merge_conflict。
- [ ] 先准备/验证新 manifest、资产与 embeddings，再进入替换/写库阶段。
- [ ] 替换前再比较当前文件 hash；出现第三方编辑立即冲突，不能覆盖。
- [ ] 文件替换与数据库事务通过 journal/checkpoint 协调：记录 old/new hash、prepared assets、索引 revision 与当前阶段。
- [ ] 在替换前、替换后写库前、写库后完成标记前分别注入崩溃；恢复时识别旧/新/第三种文件。
- [ ] 第三种内容保留人工文件并报告；只对可证明属于旧/新版本的内容完成或回退。
- [ ] 旧视觉结果失败时保持旧文档/索引，不能用 failed 清空成功证据。
- [ ] 同 revision 成功不再更新；失败自动重试沿用现有最多 2 次上限并累计预算，显式 retry 另有记录。

目标：回填与人工编辑共存，断电不产生半套证据或静默丢字。验证：文件/DB fault injection、hash compare、idempotence、摘要逐字保留。

#### P2-13：阶段内真实检索基准

- [ ] 建立 `--corpus-dir` / `VIDEO_BENCHMARK_CORPUS_DIR` 的可复现素材入口，最终 benchmark 命令在实现时固定。
- [ ] 至少 10 个 cooking 真实视频、60 张人工标注图，另加非 cooking 负例。
- [ ] 标注画面原文、数字/单位、可读/不可读、字卡区间、截图位置与检索问题。
- [ ] 对比 A transcript-only、B VLM 文字/动作+文本检索、C B+12代表帧视觉检索、D B+90帧视觉检索；Phase 2 完成 A/B 与向量 artifact，C/D 召回在 Phase 3–5 完成。
- [ ] 使用当前检索而非直接读生成文件计算 Top 3 正确证据命中。
- [ ] 至少 9/10 视频问题的 Top 3 包含正确画面证据；可明确回答的数量/单位逐项匹配人工标注。
- [ ] 标注基准中错误确认数量为 0；同时报告 unknown/漏检，不能用全部拒答换“零错误”。
- [ ] 记录分类、coverage、各请求/图像/向量数、actual/estimate 费用、复核/失败原因、耗时与缓存，不伪称最终硬件吞吐。
- [ ] 未达门槛先调整选帧/VLM/兼容；调整后固定配置重跑同一基准，登记修改。

目标：Phase 2 证明屏幕内容确实增加检索价值。未达到这一门槛，不进入 Phase 3。

### 4.3 修改落点

建议新增 cooking cue、scene/frame selector、OpenRouter vision/visual embedding adapter、usage ledger、visual enricher、cache/manifest、backfill merge/recovery 模块；集成到 `ingestion/backend/ingestion/service.py`、`media.py`、`markdown.py`、`repository.py`、`cli.py`、`config.py`。

配置/探测落在 `src/rag_favorite/ingestion.py`；媒体 optional dependencies 与现有 ingestion requirements 协调；systemd 资源设置只在实测后显式调整。回填准备 embeddings/事务能力可对核心 ingest_file 做内部职责提取，保持原公开接口。

### 4.4 必需测试

| ID | 输入/触发 | 必须验证的结果 |
| --- | --- | --- |
| T2-01 | 字幕 URL cooking / other | 仅 cooking 调用视觉视频下载，other 为 0 |
| T2-02 | 附件/音频、超大小/时长、失效认证 | 附件复用；audio 跳过；安全/平台限制沿用 |
| T2-03 | cue 位于开头/结尾、重叠/长 segment | 窗口裁切/合并正确，不超过预算 |
| T2-04 | 无 cue cooking、无切换、scene 异常 | 低频兜底可用；只降级，不阻断转录 |
| T2-05 | VFR、非零 PTS、流偏移、P/B 字卡 | 图片/实际时间正确；不依赖编号/fps |
| T2-06 | 完全相同图和近似 2→3 图 | 前者可去重，后者不同状态均保留 |
| T2-07 | 底部配方与普通字幕重复 | 不屏蔽底部配方，字幕可降权 |
| T2-08 | 中文数字、中英单位、排版变化 | 保留原文，不合并不同用量/单位 |
| T2-09 | 模糊/遮挡、0.5 秒字卡、数字冲突 | 有界加密/复核；仍不清楚为 unknown/conflict |
| T2-10 | VLM 同时读取动作和清晰文字 | 逐 frame ID 原文/描述正确；不重复另发 OCR 请求 |
| T2-11 | 多图、缺项/重复/越界 ID、非法 JSON | 校验与分项失败正确，程序绑定时间 |
| T2-12 | 缺 key/模型不可用/429/5xx/timeout | 有界重试或 partial，不下载本地模型、不自动加价换模型 |
| T2-13 | VLM 不可用 / 视觉向量关闭 | 前者保留文本；后者仍提取画面文字；CCR 图片始终为 0 |
| T2-14 | 上下文给常见配方，图中读不清 | 不用上下文补造数量；矛盾保持 conflict |
| T2-15 | 1000候选/100图/100请求/100证据/24向量边界 | 独立计数；一批多图不能绕过图像上限 |
| T2-16 | 请求发出后崩溃、暂停/恢复 | 预留/未知支出保留，累计请求/图像/美元不清零 |
| T2-17 | 600秒/美元阈值耗尽、FFmpeg/HTTP卡死 | 停新增调度/媒体子进程，partial/coverage/overrun保存 |
| T2-18 | ASR/VLM/embedding 阶段与控制 | role协议正确、无需加载本地权重、取消异常不吞 |
| T2-19 | 同图命中、prompt/space/context改变 | 命中零调用，只失效相关层；向量不跨空间复用 |
| T2-20 | cache 损坏/缺图、有缓存但不准入 | 失效修复/降级；缓存不绕过 gate |
| T2-21 | 目标后选、多 collection、cleanup | 相对引用正确，各自资产存在，证据不被删除 |
| T2-22 | 新摘要/旧摘要、uncertain 用量 | 新摘要纳入可靠画面事实；旧摘要保持；未知不确认 |
| T2-23 | 重复 source、旧 revision、已有目标 | 更新/复用正确，无第二份文档，不丢 staging 更新 |
| T2-24 | 回填排队重复、后来配置/文本改变 | 幂等；开工前重新 gate |
| T2-25 | 当前回填运行期间新任务到达 | 当前完成后新任务优先，不启动第二 worker |
| T2-26 | 缺源附件、磁盘不足、限速/认证 | 缺源终止本轮自动尝试；资源暂停；沿用恢复 |
| T2-27 | 人工正文/摘要/备注/其他 YAML | 更新后逐项保持；transcript_checksum 不变 |
| T2-28 | 人工改管理块、坏 YAML、同名块 | sidecar + conflict，原文件不覆盖 |
| T2-29 | 替换前并发编辑 | hash 比较发现冲突，保留新人工内容 |
| T2-30 | 替换/DB提交/checkpoint 各崩溃点 | 能恢复旧/新一致版本；第三种内容不覆盖 |
| T2-31 | 重复恢复、失败重试、旧成果存在 | 幂等；预算持续；旧成功证据不清空 |
| T2-32 | 10 视频/60 图真实基准 | Top 3 ≥9/10，已确认量/单位准确，未知/漏检单列 |
| T2-33 | 非 cooking 真实负例 | 分类后所有视觉操作为 0 |
| T2-34 | 无GPU/本地模型的核心安装及原录入 | portable core、普通文本、旧 API 回归通过 |
| T2-35 | 接近32K输入、批量15图、reasoning usage | 拆分到低价档，所有计费输出记账，不按请求当帧数 |
| T2-36 | Gemini多图embedding/错维/聚合结果 | 一图一向量、count/index/768维验证；聚合不冒充逐图 |
| T2-37 | usage缺失/响应丢失/重试/价格变化 | actual与estimate分开、保留未知支出，不声称绝对硬费用上限 |

### 4.5 验收条件与交付证据

- [ ] 所有受影响的 T2 测试通过，真实素材质量结果单独记录。
- [ ] 一个新 URL、一个新附件、一个旧 URL cooking 回填完成；不可用旧附件正确报告缺源。
- [ ] Visual Timeline、manifest 和图片可互相验证，cleanup 后仍可找到证据。
- [ ] 所有图片仅走 OpenRouter，CCR 图片调用为 0；请求/图像/向量/美元预算、未知费用与一次复核可追溯。
- [ ] Gemini 代表帧 artifact 一图一向量，可供 Phase 3 入独立视觉表；Phase 2 A/B 检索使用 OpenRouter 文本测试代际。
- [ ] 崩溃恢复和人工修改保护有实际 fault-injection 日志。
- [ ] 现有字符检索的 10 视频屏显问题 Top 3 达到至少 9/10；明确用量逐项正确。
- [ ] 未出现“全部 unknown 但宣称准确率 100%”；报告可读样本数量、正确读取数量和漏检。
- [ ] 新录入、旧文本、作者批次、平台恢复及原目标选择回归通过。
- [ ] stage 报告注明：结构化检索时间和 Telegram 截图交付仍待 Phase 3–4。

退出策略：关闭视觉/回填，已完成的 Markdown 和证据保留；不删人工文件或有效资产。失败新任务继续文本保存，失败回填保持旧文档/索引。Phase 2 仍无新增 RAG 表列。

## 5. Phase 3：时间分块、云文本空间迁移与视觉检索

### 5.1 阶段目标与 MVP

**MVP：同一段 transcript、屏幕配方和截图 ID 以可靠时间关联存入核心 chunks；新旧数据库可升级；普通文本保留分块行为，并在更换 encoder 时完整重嵌入到新空间。** 本阶段完成检索数据准备和核心查询返回，不依赖新增 Telegram UI。

最低演示：检索“这道菜的料汁比例”后，数据库中的匹配 chunk 可追溯到对应 transcript 段、可见原文、实际证据时刻和 modality；非 cooking 有可靠 transcript 时间时可以时间分块，但不会因此抽帧。

前置条件：Phase 2 verified、manifest/space schema 固定；准备真实 DB fixture、0001 checksum、全库文档/hash/token 清单与迁移费用估算。

### 5.2 开发步骤

#### P3-01：ContentChunk 与视频文档识别

- [ ] 定义 content、modality、可空 start/end、metadata；modality 为 text/transcript/visual/multimodal。
- [ ] 使用可信 frontmatter/manifest/job 来源识别视频文档，不仅靠文件名或随意出现的时间字符串。
- [ ] 普通 Markdown 保持 chunk_text；有来源和合法时间的旧视频 transcript 可兼容解析。
- [ ] 缺少/坏 manifest 不阻断全文索引；根据可验证 transcript 或 text fallback 建块。
- [ ] 不读取不受信任 frontmatter 中任意绝对路径；manifest 路径限定 collection 内。
- [ ] 明确 timing_precision 可表达 word/frame、segment/coarse、approximate/unknown，沿用已固定 schema。
- [ ] 保留文档级标题、摘要、来源说明等无时间 text chunks。

目标：数据类型表达四种信息来源，普通笔记不会误变成视频。验证：真实 manifest、伪造关联、旧视频、普通含时间笔记和坏 metadata。

#### P3-02：时间边界与 chunk 生成

- [ ] 以真实 transcript/word 边界及 scene/cue 上下文组织，目标 20–45 秒。
- [ ] 对有可验证拆分边界的 temporal chunk 执行 ≤60 秒、≤1800 字符。
- [ ] 很短相邻区间可以合并，但不能让多个不同字卡状态丢失 evidence 时刻。
- [ ] 超字符数在真实语句/文字边界拆分；无 word 时，拆出的文字仍标原 segment 的粗范围，不按字符比例计算秒数。
- [ ] **无法满足 60 秒且没有真实细分时间的旧长 segment，不编造子时间：退回 text chunks，在 metadata 记录原段粗上下文范围与 fallback 原因，chunk start/end 留空。** 独立截图若有可靠时刻仍可作为 visual chunk。
- [ ] visual 事实按 observed_at 归属，scene 起止只是上下文；不能把相邻步骤的图挂到当前配方。
- [ ] 跨边界处理使用固定归属规则，例如 `[start,end)`、末尾闭合；同一 evidence 不因边界误归属两次。
- [ ] 很长单个视觉条目按文字上限拆分，重复保留证据关联/方法/状态，不能截掉用量或只保留时间标题。
- [ ] 长间隔、空 transcript 与有视觉无字幕均可生成合法 chunks，不用连续填空伪造内容。
- [ ] chunk 文本包含必要标题/步骤，但不得为上下文重复过多而挤掉主要配方。

目标：每段知识的时间与证据可解释，不能靠精确外观掩盖粗数据。验证：60 秒/1800 字符边界、旧长段、末帧和混合/单模态。

#### P3-03：chunk metadata 与引用稳定性

- [ ] metadata 保存 evidence_ids、source_url、scene/cue/segment 关联、timing_precision、visual_status 和相关 revision。
- [ ] 外部可返回字段与内部调试字段分开，不能把缓存绝对路径/临时 signed URL 放入检索输出。
- [ ] visual/multimodal 关联只指向当前有效文档/manifest 的证据；旧 revision 的结果要么仍有明确可访问版本，要么报告 stale/missing。
- [ ] 数字 uncertain/conflict 仍可检索，但 content 与 metadata 都明确不确定，不能生成确定配方文本。
- [ ] text fallback 不用 start=0/end=0 假装有时间。
- [ ] 相同输入和配置生成稳定 chunk 顺序与可重建关联，方便测试与恢复。

目标：Phase 4 可以安全消费数据，不需要重新猜时间或搜图片文件名。验证：错 revision、dangling evidence、内部路径泄露和不确定状态。

#### P3-04：时间/视觉空间迁移与初始化统一

- [ ] 增加核心 `0002_video_temporal_chunks.sql`，在 `migrations/core` 和 package resources 同步维护。
- [ ] `rag_chunks` 新增 modality（默认 text）、start_seconds/end_seconds（nullable double precision）、metadata（默认空 JSONB）。
- [ ] 数据库/应用都校验合法 modality、时间成对为空或成对有值、有限非负及 end ≥ start；覆盖 NaN/Infinity。
- [ ] 旧 rows 自动保持 text、空时间、空 metadata，原向量内容/维度不变。
- [ ] 不修改已应用 `0001_document_rag.sql`，保持文件 checksum 基线。
- [ ] MigrationRunner 仅初始迁移替换 vector dimensions；ALTER-only 迁移不再要求 vector(1024) 标记。
- [ ] 保留迁移 checksum、事务、advisory lock 和重复执行幂等。
- [ ] 统一现有 initialize_schema、CLI init/setup 和 MigrationRunner；已存在但历史登记路径不同的库也有明确升级测试，不能伪造已应用记录绕过校验。
- [ ] package wheel 中包含新 SQL；源码与 packaged SQL 内容一致。
- [ ] 核心 0002 与 `ingestion/migrations/0002_multi_destination_documents.sql` 是不同迁移序列，不能写错目录或复用旧文件。

- [ ] 新增不可变 `0003_video_visual_embeddings.sql`：独立视觉向量表/space与active generation登记结构，package SQL同步；Gemini默认768维，不修改旧文本vector列。
- [ ] 新OpenRouter文本索引在同一PostgreSQL的shadow schema/代际建立，保留稳定document IDs/source paths；不另建向量数据库。
- [ ] 新迁移名称以当前序列核对后固定，已应用0001不改；每条视觉向量绑定collection/document/evidence/analysis revision/observed_at/image hash/encoder space。

目标：fresh install/旧库升级一致，旧索引保留，新文本与视觉空间可隔离。验证：真实 PostgreSQL 临时数据库的 fresh/upgrade/rollback-on-failure/concurrent apply。

#### P3-05：embedding 空间、全量重建与 current 判断

- [ ] 普通文本 chunking_version=1/index_version=3；视频 temporal chunking_version=2/media index_version=4，另有 encoder space/generation。
- [ ] current 同时检查 checksum、文档类型、model/revision、维度、预处理与 active generation。
- [ ] 单纯时间升级只重建视频；本次 Ollama→Perplexity 必须全部文本重嵌入，普通文档也包含在内但不做视觉。
- [ ] 已有visual Markdown/manifest可重建temporal chunks，不默认重新取视频或重新调用VLM。
- [ ] 全库token/价格估算与迁移ledger独立于3分钟视频预算；按有界批次、缓存、checkpoint执行，不每日限量。
- [ ] shadow保存完整文档/chunk快照与stable IDs，读取期间原索引可用；变更文档hash检测与增量追平固定。
- [ ] 全量完成、差异追平、关联核对后短暂阻止索引写入，原子切active generation/encoder；失败不切换半成品。
- [ ] 回退成对切旧索引与旧encoder，明确freshness与回退配置，不把新查询向量发旧空间、不自动本地fallback。

目标：更换模型不会因同为1024维而静默错检，迁移可恢复/可核对。验证：同维异模型隔离、完整/未完整代际、并发文档编辑、cutover与rollback。

#### P3-06：索引事务、重建任务与恢复

- [ ] 内部提取“准备 chunks/embeddings”与“写入文档/chunks”两部分，公开 ingest_file 行为兼容。
- [ ] 完成全部准备后，用同一事务更新文档并替换 chunks。
- [ ] embedding 部分失败或 DB 异常时旧 chunks 保留，不暴露一半新时间块。
- [ ] 重建逐文档/有界批次，支持暂停与重试；不启动未经租约设计的并行 worker。
- [ ] 保持 source_path/document ID，记录需要升级、已升级、失败、fallback 数量。
- [ ] 与 Phase 2 journal 协调新文件、manifest revision 和索引 revision，恢复不产生错图关联。
- [ ] 同版本再次重建为幂等/no-op，文档真正变更时正常重建。

目标：时间索引上线不会短暂毁掉旧检索。验证：embedding/事务故障、停机恢复、幂等、混合版本查询。

#### P3-07：代表帧视觉索引、RRF 与兼容查询

- [ ] 将 Phase 2 Gemini artifact 写入独立pgvector视觉表，事务校验count、space/维度、evidence与图片hash。
- [ ] 视觉query使用同一Gemini encoder/model/revision/768维，禁止Perplexity或旧ImageBind向量互查。
- [ ] 文本query只使用active文本space；各空间内部cosine召回，之后RRF/rank fusion合并，不直接相加cosine。
- [ ] 合并后映射稳定document/chunk/evidence与时间；保留原collection/filter/limit/score字段语义，额外融合信息放可选metadata。
- [ ] 普通纯文本collection不调用Gemini；含视觉索引时的云query预算/usage单独记账，文字向量和视觉向量可分别缓存。
- [ ] Gemini不可用或视觉索引未就绪时仅使用同active文本代际，报告未执行视觉召回；文本模型失败不假装查询成功。
- [ ] 视觉召回可独立关闭，证据取图不生成新向量/分析；legacy adapter透传合法metadata，不建第二套schema。

目标：画面语义可召回而不破坏文本主契约，费用与降级可追溯。验证：图文检索、跨空间拒绝、RRF、同文档多帧去重、服务故障与纯文本零Gemini调用。

### 5.3 修改落点

`src/rag_favorite/rag.py`、`embedding.py`、`config.py`、`migrations.py`、`database.py`、`resources/migrations` 与新增视觉索引/space registry；根目录 `migrations/core`；`services/rag-app/rag.py` 兼容转发与 `services/rag-mcp/retrieval_adapters.py` 的兼容；`tests/test_rag_core.py`、`test_migrations.py` 及新增时间分块测试。

### 5.4 必需测试

| ID | 输入/触发 | 必须验证的结果 |
| --- | --- | --- |
| T3-01 | multimodal、只有 transcript、只有 visual | modality 正确，内容/证据不丢 |
| T3-02 | 20/45/60 秒、1800 字符边界 | 可拆 temporal 不超硬限；无凭空时间 |
| T3-03 | >60 秒且无 word 的旧长段 | text fallback、时间列空、粗范围说明保留 |
| T3-04 | 超长视觉事实/字幕段 | 用量与 evidence 绑定；不能只有标题无正文 |
| T3-05 | 证据恰在边界、跨 scene、末帧 | 固定归属正确，observed_at 与上下文分开 |
| T3-06 | 普通含时间笔记、坏/缺 manifest | 普通笔记不误判；视频可 fallback；不读越界路径 |
| T3-07 | fresh DB 和只有旧 schema 的 DB | 新字段默认正确；旧 rows/向量/维度保持 |
| T3-08 | 无 vector 标记的 ALTER 迁移 | 原样可执行；0001 checksum 不变 |
| T3-09 | 并发/重复迁移、checksum 篡改、故障 | advisory lock/幂等；篡改拒绝；事务回退 |
| T3-10 | 未登记旧初始化路径、CLI init/setup | 有受测试升级路径，所有入口 schema 一致 |
| T3-11 | 非法 modality、半空/负/NaN/Infinity 时间 | app 和 DB 层拒绝非法值 |
| T3-12 | 同encoder的text3/video4、换encoder的新代际 | 时间升级仅视频；换encoder全部文本重嵌入但普通文档零视觉 |
| T3-13 | 非 cooking 的 transcript 时间分块 | 可索引时间，但所有图片操作为 0 |
| T3-14 | embedding 失败/DB 提交失败 | 旧 chunks 完整保留，无半新索引 |
| T3-15 | 重索引暂停/恢复、same revision | 可恢复/幂等；document ID/source_path 不变 |
| T3-16 | stale evidence、conflict、绝对缓存路径 | stale/uncertain 可表达；不泄露内部路径 |
| T3-17 | SQL 源码/package wheel | 内容一致，wheel 中可读取新迁移 |
| T3-18 | 原文本检索、legacy adapter | score语义/过滤/主结构兼容，原回归通过 |
| T3-19 | 同维不同encoder、错维视觉/query | 拒绝跨space查询，不由维度相同推断兼容 |
| T3-20 | 全库shadow不完整/并发编辑/差异追平 | 未完成不切换；新变更覆盖；stable IDs保留 |
| T3-21 | atomic cutover/故障/rollback | encoder/index成对切换，旧数据保留与freshness明确 |
| T3-22 | Gemini一图一向量、evidence与hash | 独立768维索引正确，旧文本列不混写 |
| T3-23 | 文本+视觉召回、RRF、普通collection | 不直接相加cosine，纯文本零Gemini，无重复/错图结果 |
| T3-24 | Gemini失败/关闭/查询usage | 同active文本降级，query费用单列，取图零推理 |

### 5.5 验收条件与交付证据

- [ ] T3-01–T3-24 通过，迁移测试包含真实 PostgreSQL。
- [ ] 提交 0001 前后 checksum 对比、fresh/upgrade schema 和迁移日志。
- [ ] 提交一个多状态食谱的 chunk/manifest 时间归属对照表。
- [ ] 提交“仅时间升级不重嵌入普通文档”与“换encoder全库文本重建”的独立报告；迁移费用、shadow追平/切换/回退有证据。
- [ ] Gemini索引、同space查询、RRF与云故障文本降级有真实基准，普通文档没有图片处理。
- [ ] DB 故障时旧索引可用，重建恢复结果一致。
- [ ] 时间精度与粗段 fallback 明确，未用文字比例产生伪精确时间。
- [ ] 核心查询结果可供 Phase 4 消费；对外 MCP 主契约尚未改变。

退出策略：停用视频新分块/视觉召回或成对回退encoder与旧索引代际；旧Markdown保留，明确回退代际freshness。新增列/表/旧数据保留，不自动删列或就地改旧vector维度。软件回退必须实测旧代码可忽略新增列，不能只凭“ALTER 是增加列”推断兼容。

## 6. Phase 4：时间检索、证据工具与 Telegram 截图回答

### 6.1 阶段目标与 MVP

**MVP：用户在 Telegram 问料汁比例，系统通过 rag_search 找到对应段落，引用视频出处与时间，再通过 rag_evidence_get 交付最多两张真正相关的截图。** 工具不会根据文件名到处搜图，答案不会把未知用量变成确定结论。

前置条件：Phase 3 verified，证据 manifest/时间元数据稳定；Telegram/OpenClaw 测试环境和工具 allowlist 可测试。

### 6.2 开发步骤

#### P4-01：rag_search 时间 metadata 与主契约兼容

- [ ] 保留原 search 参数、limit 上限、collection 选择、excerpt、scores语义与document ID等主字段；视觉召回以RRF融合，可选metadata标明retrieval modalities/fused rank。
- [ ] 视频新信息只放已验证 metadata：modality/start/end/source_url/evidence_ids/timing_precision/visual_status。
- [ ] section 可显示上下文时间范围，截图 actual time 单独表达。
- [ ] 普通文本 metadata 继续兼容空对象/已有值，不能要求时间必填。
- [ ] metadata 做字段白名单和类型/数量验证，不直接透传 manifest 全对象。
- [ ] 防止内网缓存绝对路径、API key、临时签名 URL 出现在面向用户/模型的结果。
- [ ] 旧 retrieval adapter 与 MCP validator 不丢合法时间/evidence 信息。

目标：用户可引用时间，旧搜索客户端仍可运行。验证：原 contract fixture、新视频、旧文本、非法 metadata 和 adapter round-trip。

#### P4-02：rag_evidence_get 契约与 ID 解析

- [ ] 增加 `rag_evidence_get(document_id, knowledge_base, evidence_ids)`；IDs 数量最多 2。
- [ ] 查已配置 collection、对应 document 与有效 manifest 的明确关联。
- [ ] 仅接受该文档中登记的 evidence ID，不接受输入图片路径/任意 basename。
- [ ] 去重重复 ID；跨文档/跨 collection ID 不能混用。
- [ ] 返回 source、observed_at、原文/识别状态、相对引用、missing IDs 和 delivery failures。
- [ ] 文档/证据/manifest 丢失有结构化结果；不能返回错误图片或凭文件名猜测。
- [ ] stale search result 指向旧 revision 时重新校验，必要时报告缺失，不能静默改成不相关新图。
- [ ] 工具描述准确说明 references 是只读知识读取，openclaw 模式另写临时交付缓存。

目标：图片可追溯、可受控读取。验证：同一张图不同 ID/文档、missing/stale、0/1/2/3 个请求。

#### P4-03：安全证据路径与 managed outbound

- [ ] 从现有 recipe helper 提取可复用 staging 职责到 packaged src，不 import 未打包的旧 cooking MCP server。
- [ ] 路径从 manifest 的相对引用得到，resolve 后再次检查 collection 边界；拒绝越界 symlink、`..`、绝对路径。
- [ ] 不沿用全盘 basename fallback；不通过模型拼接主机路径。
- [ ] managed outbound 使用受控目录，目录 0700、文件 0600、内容 hash 命名、原子写入。
- [ ] staging 失败不破坏可靠文字返回；分图记录 delivery failures。
- [ ] concurrent/same hash staging 幂等，不留下半张文件。
- [ ] 临时交付 TTL 24 小时，清理不触碰持久证据；取图不重新调用VLM/embedding。

目标：交付路径稳定、私有且不会越界。验证：路径/权限/损坏/并发/TTL/无 outbound 写权限。

#### P4-04：配置、注册、doctor 与打包

- [ ] app config 增加 evidence delivery_mode=references/openclaw、outbound_dir、max_images=2、ttl_hours=24。
- [ ] portable core 默认 references；计划中的 Linux Telegram 部署显式启用 openclaw，实际目录与服务用户在服务器确认。
- [ ] OpenClaw 默认 outbound 示例 `~/.openclaw/media/outbound/rag-favorite`，实际用户/服务权限由部署确认。
- [ ] MCP_TOOL_NAMES 从两个更新为三个：rag_search、rag_status、rag_evidence_get。
- [ ] 同步注册、stdio smoke、doctor、OpenClaw toolFilter、安装/探测和相关 fixture。
- [ ] wheel 中包含 helper 和注册代码，无源码仓库依赖也能启动。
- [ ] 原rag_status兼容，新增分类/视觉/回填/交付、各角色API就绪、active space与模型费用通过可选字段表达。

目标：新工具真的可调用，不只存在 Python 函数。验证：stdio list/call、doctor、allowlist 和安装后 smoke。

#### P4-05：OpenClaw 技能说明与回答约束

- [ ] 更新 private-rag/cooking 相关说明：先 search，依据当前命中 ID 取图，不凭空构造 path/evidence ID。
- [ ] 只选最相关的 1–2 个证据，配方多状态时解释每张图属于哪个步骤/时间。
- [ ] 回答包含原文配方、普通来源链接、上下文范围与截图时刻，避免把两类时间混为一谈。
- [ ] OpenClaw 原样消费工具提供的 MEDIA 指令；最终人类文本不展示主机绝对路径。
- [ ] uncertain/conflict 明确看不清/矛盾，不能拼成完整确定配方；缺图说明缺图但保留文字。
- [ ] 普通文本问题无需调用证据工具；同问题多文档命中仍遵循两图上限。
- [ ] 无需新增平台时间跳转 URL 规则；来源链接先使用普通链接。

目标：证据真正帮助答案，而不是添两张无关图片。验证：工具调用轨迹、source/time 对照、缺图与冲突回答。

#### P4-06：故障、资源与交付清理

- [ ] references 模式返回引用信息，不偷偷写 OpenClaw outbound。
- [ ] outbound 不存在/无权限/磁盘不足时返回结构化交付失败，检索/文本答案仍可用。
- [ ] 文件被删/损坏时报告 missing，不改知识正文、不自动重下载整段视频。
- [ ] cleanup 与同时取图有明确顺序/原子策略，不能读到半文件。
- [ ] 记录证据请求、实际交付图片数、失败原因，不记录图片 API/媒体凭据。
- [ ] 证据工具不触发新视觉处理、回填、重索引或写人工知识文件。

目标：取图是可预测的小操作，不能隐式扩大为昂贵媒体分析。验证：所有这些副作用调用为 0。

#### P4-07：真实 Telegram 全链路验收

- [ ] 用 Phase 2 真实问题集，经 Telegram 发问 → core search → evidence get → OpenClaw delivery。
- [ ] 对照人工标注，确认返回文字来自正确画面，截图属于同视频/同步骤。
- [ ] 验证“截图时刻”和“上下文范围”准确，不因显示格式丢小时。
- [ ] 覆盖缺源、缺图、低置信、多状态和普通文本问题。
- [ ] 记录工具清单、调用轨迹、用户实际收到的图片和答案，不用 mock delivery 替代真实 E2E。

目标：用户可见的核心场景完成；Phase 4 至少保留 Phase 2 ≥9/10 的正确证据检索门槛。

### 6.3 修改落点

`src/rag_favorite/mcp_contracts.py`、`mcp_server.py`、`mcp_support.py`、`config.py`、`openclaw.py`；新增 packaged evidence helper；`services/rag-mcp/retrieval_contracts.py`、`retrieval_adapters.py`、smoke；OpenClaw example config、`openclaw/skills` 与 ingestion plugin 测试。具体技能内容只在本阶段实施时修改，本次文档未修改技能文件。

### 6.4 必需测试

| ID | 输入/触发 | 必须验证的结果 |
| --- | --- | --- |
| T4-01 | 原 search 参数/普通文本结果 | 主契约与原 scores/limit/collection 行为兼容 |
| T4-02 | 视频 metadata 经过 validator/adapter | 时间、modality、IDs 不丢；非法字段被拒绝/清理 |
| T4-03 | 0/1/2/3 IDs、重复 IDs、非法输入 | 上限受控，重复不发两次，不接受路径代替 ID |
| T4-04 | 跨文档/collection、未知 ID、stale revision | 不给错图；missing/stale 结构化报告 |
| T4-05 | `..`、绝对路径、越界 symlink、basename 诱导 | 所有越界被拒绝，不全盘搜索 |
| T4-06 | 无权限、坏图、图片缺失、manifest 损坏 | 单项失败不会破坏其他可靠文本/图 |
| T4-07 | 并发同图 staging、写入中断 | hash 命名幂等，原子写，无半图 |
| T4-08 | Linux outbound 权限与 TTL | 0700/0600；清理临时图，不删持久证据 |
| T4-09 | references/openclaw 两种模式 | 前者不 staging；后者准确报告临时缓存副作用 |
| T4-10 | MCP list、stdio call、doctor、toolFilter | 三个工具真正注册/允许调用，旧工具保留 |
| T4-11 | 安装 wheel 后运行 | helper/工具可加载，无旧服务源码依赖 |
| T4-12 | MEDIA 指令与最终文本 | 原样交付；用户文本不显示主机路径 |
| T4-13 | conflict/uncertain、多字卡步骤、缺图 | 不确认未知用量；图/步骤/时间明确对应 |
| T4-14 | 普通文本问题与 evidence tool | 不额外图片调用；无VLM/embedding/回填副作用 |
| T4-15 | 真实 Telegram cooking 问题 | 正确文字、来源、时间、1–2 张相关图实际到达 |
| T4-16 | 故障交付与真实 non-cooking 问题 | 保留文本可用性，行为与准入边界一致 |

### 6.5 验收条件与交付证据

- [ ] T4-01–T4-16 与旧 MCP/OpenClaw/Node 测试通过。
- [ ] 三工具 list/call、allowlist、doctor、安装 smoke 有日志。
- [ ] 真实 Telegram 至少包含标准配方、多状态、unknown、缺图和普通文本演示。
- [ ] 每个答案的图片能沿 evidence ID → manifest → document/source 回溯。
- [ ] 每个回答最多两图，时间不伪造，用量不从常识补造。
- [ ] 路径越界/权限/副作用测试通过，missing 图不打断可靠文字。
- [ ] 未导致 Phase 2 基准下降到 9/10 以下；若下降，先修检索/映射再进入 Phase 5。

退出策略：切换 references 或停用新增交付配置，保留 rag_search 和已存知识。回退旧软件时同步旧 toolFilter/tool list；不删除证据，不自动执行 schema downgrade。

## 7. Phase 5：Linux 实机质量、部署与运行保障

### 7.1 阶段目标与 MVP

**MVP：在此前开展开发的 Linux 服务器上，用最终版本复验云模型管线，达到质量门槛，证明费用/限流/回填/恢复可控，并交付配置与手册。** 目标机器为 32GB RAM、RTX 4060 Laptop；CPU 精确型号/系统/服务限制以开工时及最终复验记录为准，不用本地 GPU 吞吐推算 API 速度。Phase 5 不代表此前阶段在 macOS 实施，也不承担首次建立开发环境的工作。

前置条件：Phase 4 verified；服务器访问、测试collection、CCR文本与OpenRouter各角色配置、active索引及真实corpus可用。本次不表示已部署或已获得服务器登录/secret。

### 7.2 开发步骤

#### P5-01：Linux、API与索引环境指纹

- [ ] 复核第 0.1 节/P1-01 的 Linux 环境基线，记录最终 Linux/CPU/RAM/Python/FFmpeg/service 用户/资源限制/磁盘及变化；GPU 仅环境信息，无本地模型必需条件。
- [ ] 探测Linux实际CCR地址/协议/文本model；OpenRouter出口网络、各角色model/provider/endpoint、价格版本与可用性。
- [ ] 配置/目录就绪不发送付费推理；真实CCR文本、ASR/VLM/embedding smoke使用独立小预算和脱敏日志。
- [ ] 记录active text generation、视觉space、维度、预处理/模型revision与旧索引备份/freshness。
- [ ] 不改共享Ollama/GPU进程，不在日志展示凭据。

目标：实测环境和计费可追溯。验证：版本化环境/就绪/space报告。

#### P5-02：可重复安装、云配置与重启

- [ ] 固定代码/依赖lock，构建core/ingestion，确认SQL、provider helper、配置模板/systemd资源打包。
- [ ] 只准备媒体工具与HTTP依赖，不安装OCR/VLM/ASR/embedding权重。
- [ ] 配置CCR文本与OpenRouter各角色及费用/限流参数；key通过secret/environment注入。
- [ ] 验证无GPU安装、restart、缺key/模型不可用/无网络就绪与已有知识读取。
- [ ] 单独小样本验证各API响应、时间/向量维度/usage字段；计费缺失以unknown/estimate报告。
- [ ] 如供应商模型无法固定权重revision，记录model ID/provider/时间/response信息并明确不可完全复现的范围。

目标：服务器按文档重建，模型不依赖本地残留。验证：clean package/restart与角色协议smoke。

#### P5-03：真实 corpus、标注与评测隔离

- [ ] 固定至少 10 cooking 视频、60 标注图，以及非 cooking 负例；本次新增工程建议负例至少 10 条。
- [ ] 为每个视频记录 source/hash、transcript 来源、长度、语言、类别和代表问题。
- [ ] 人工标注画面原文、数字/单位、可读性、字卡区间、正确证据位置和冲突情况。
- [ ] 问题与答案不能从系统输出反向生成；题目要包含 transcript-only 无法解决的画面信息。
- [ ] 至少覆盖：未口述比例、同镜头变数字、重复 cue、无 cue、小字/模糊、英文单位、动作与纯 food mention 负例。
- [ ] 所有 A/B 使用同一固定主评测集和标注版本；不能删掉难例后仍报原门槛。
- [ ] 采样/阈值/模型调优记录与最终评测分开；另备留出样本更好。若复用同一小样本调优，报告必须注明，不能宣称泛化准确率。
- [ ] 原图/原文保留；评测字符串规范化规则固定，单位变化不能被“相似文本”掩盖。

目标：质量报告衡量真实任务，避免自证正确。验证：dataset/annotation manifest、人工审核和可复现固定 seed/顺序。

#### P5-04：四组质量/费用基准

- [ ] A transcript-only；B OpenRouter VLM动作/画面原文+文本检索；C B+12代表帧视觉索引；D B+90帧视觉索引。
- [ ] D显式提高视觉输入上限/预算，在独立实验配置执行；不静默扩大生产支出。
- [ ] 相同query/标注/Top3/最终回答，记录原文、动作证据、截图归属、unknown/漏检与RRF收益。
- [ ] 区分首次处理、缓存命中、复核/重试；实际model/provider/计费和配置指纹齐全。
- [ ] 比较12和90向量的视觉检索粒度/收益；没有足够收益时保持12代表帧。
- [ ] Qwen3-Embedding-8B或更强VLM仅显式对照；换文本encoder另建空间，不在同索引混测。

目标：成本优先有实际质量与费用依据。验证：固定A/B/C/D结果与actual usage ledger。

#### P5-05：资源、API延迟与费用测量

- [ ] 记录字幕/ASR、CCR分类、补下载、scene/frame、OpenRouter VLM、两类embedding、索引、CCR摘要和交付耗时。
- [ ] 报告整体/视觉阶段p50/p95，首次与缓存命中分开；不由网页tps/延迟推算整视频时间。
- [ ] 连续至少10个代表任务，记录本地RAM/磁盘/媒体进程、API限流/timeout/重试/partial/费用，确认无本地模型GPU加载。
- [ ] 逐视频actual vs estimate美元成本、按role token/秒/图与unknown账单，CCR LLM和充值/存储单列。
- [ ] 至少一个3分钟样本核对约$0.0051估算；偏差说明分辨率、推理、复核/重试或供应商来源，不把预算当实测。
- [ ] 记录全库重嵌入总tokens/费用与query embedding费用，独立于单视频入库账单。
- [ ] 600秒只是累计视觉工作预算；美元阈值仅停止新增调度，已发请求/不确定计费可能overrun并须报告。
- [ ] 依据实测调整并发/批量/图像尺寸/service limits，配置变更后复验受影响项。

目标：Linux作为媒体/API客户端可持续运行，成本与速度有真实数据。验证：连续任务资源曲线与逐请求费用/延迟报告。

#### P5-06：故障与恢复演练

- [ ] 任务运行时重启 worker，覆盖下载、VLM、embedding、文件替换、DB 提交等 checkpoint。
- [ ] 模拟OpenRouter断连/429/5xx/计费响应丢失、CCR文本不可用、embedding错维和数据库不可用。
- [ ] 模拟磁盘不足/图片缺失/权限错误；保留旧文档和已持久证据。
- [ ] 回填时修改旧 Markdown，验证冲突 sidecar 和摘要保留。
- [ ] 有新任务时执行回填 pause/resume，确认优先级和预算连续性。
- [ ] 检查 restart 不重复创建文档/无限 enqueue、不重置远端请求限额。
- [ ] 演练关闭视觉embedding：VLM画面文字/文本检索仍可用；关闭总视觉开关时图片请求为0，CCR文本保持。
- [ ] 演练关闭视觉：停止新视觉/回填，已存文字和图片可检索/取图。

目标：真实运行环境的恢复符合前面离线设计。验证：每个注入点的前后 hash/revision/status/request count。

#### P5-07：完整项目回归与发布工件

- [ ] 执行当前 Makefile 的 `make check`：lint、core/ingestion/compat/Node/macOS、compile、build。
- [ ] 补上新 ingestion 模块的 lint 范围；当前默认 ruff 范围未覆盖整个 ingestion，不能只跑默认 lint 就宣称新增模块已检查。
- [ ] 迁移测试在临时 DB、核心 MCP 在安装包环境、Telegram 在真实测试环境分别执行。
- [ ] 检查 `.env.example`、app config 示例、systemd 源码/打包资源和 README 一致。
- [ ] 原 author batch、平台 session/auth、目标选择、普通检索和 legacy compatibility 不能因新功能退化。
- [ ] 生成版本化 release report：代码版本、依赖/model 指纹、测试结果、基准、已知限制、启停方式。
- [ ] baseline 中已有失败与新回归分别报告；不将未执行平台测试写成 pass。

目标：交付可安装、可维护，而不只是局部测试通过。验证：完整 check/build 日志、wheel 安装 smoke 与回归差异。

#### P5-08：小样本上线、观察与回退

- [ ] 安装时默认关闭视觉；用测试 collection/小样本 cooking 验证后再显式启用。
- [ ] 开启视觉准入后，确认非 cooking 操作仍为 0，文本/图片路由仍独立。
- [ ] 先完成新录入样本，再恢复自动 cooking 回填，避免一次扫描造成不可控更新。
- [ ] 最终配置明确OpenRouter各角色、代表帧配额、美元停止阈值/未知支出策略、active索引、磁盘/TTL和outbound；CCR仅文字。
- [ ] 本次新增建议：连续观察至少 24 小时的运行日志；若环境空闲，主动补足代表任务，不把“没任务没出错”当稳定证据。
- [ ] 遇到人工内容覆盖、证据错配、超预算、非 cooking 抽帧或无限重试，立即停用视觉/回填并保留诊断记录。
- [ ] 软件回退演练保留新增数据库列/证据；旧版能读取 Markdown、普通索引继续可用。
- [ ] 准备运行手册：status、backfill pause/resume/retry、缺key、API限流/未知账单、索引回退、缺源/冲突/缺图及日志定位。

目标：有清晰的运行入口、停止条件和恢复路径。验证：启停/回退实操日志及观察报告。

#### P5-09：最终验收与交付签收记录

- [ ] 将 P1–P5 任务、T1–T5 测试和质量指标汇总为 verified/not_run/failed。
- [ ] 每个未解决问题写用户影响、可复现条件、当前 fallback 和后续范围。
- [ ] 确认没有把按需视频片段/新类别/UI 重设计混入当前完成声明。
- [ ] 提交部署配置模板（不含 secret）、依赖 lock、模型指纹、数据库迁移结果、基准/真实 E2E 和运行手册。
- [ ] 明确最终模型/代表帧策略由实测决定；没有质量收益时保留低价模型和12代表帧，切encoder须另建空间。
- [ ] 只有所有必需门槛通过才标五阶段 verified；缺服务器/corpus 时相关结果保持 not_run，不伪造结论。

目标：开发、质量与运维证据齐全，结果可以复核。

### 7.3 最终指标与验收方法

| 指标 | 计算/记录方法 | 门槛或使用方式 |
| --- | --- | --- |
| cooking 分类 | 人工类别 vs category/decision；单列 unknown 与覆盖不完整 | 固定负例不得触发视觉；正例漏准入计入漏检 |
| 非 cooking 视觉次数 | 下载/scene/frame/VLM/视觉 embedding 计数 | 验收负例全部为 0 |
| Top 3 正确证据命中 | 每视频固定主问题，Top 3 是否有正确视频/配方证据；至少 10 视频 | 至少 9/10，样本更多时报告分母与比例 |
| 数量/单位正确性 | 对每个明确回答的用量逐项核对人工原文；单位不做无依据换算 | 基准中错误确认数量为 0；不是生产零错误保证 |
| 可读字卡覆盖 | 实际正确读取的可读项 / 全部人工可读项；unknown 与漏采单列 | 必须报告，不能只用接受答案精度隐藏漏检 |
| 画面文字字符错误率 | 固定原文与规范化规则计算 | A/B 比较与错误分析，不先编造硬阈值 |
| 截图对应性 | source/document/evidence ID/时间与人工标注相符 | 实际交付图不得错文档/错步骤；缺图明确报告 |
| CCR/OpenRouter路由 | 各role的endpoint/model/provider/请求序列 | CCR仅文字；其他模型仅OpenRouter，无自动本地/高价模型fallback |
| 请求/图像/金额预算 | checkpoint、usage/预留、failed/retry、实际stage时间 | 请求/发送图像各≤100、候选≤1000、证据≤100、向量图像≤24；视觉600秒；$0.03停新增调度且overrun另报 |
| deadline 退出 | 到时停止新增工作/进程，另记终止与清理开销 | 不能用未记录清理时间宣称“全任务 600 秒” |
| RAM/媒体进程/API稳定性 | 本地峰值、超时/限流/连续任务曲线 | 不加载本地模型，无持续泄漏；降级有记录 |
| 模型费用 | actual vs estimate、未知账单、role/token/秒/图、价格revision | 3分钟估算与真实账单对照，CCR/迁移/query/充值单列 |
| 向量空间/代际 | 全库hash清单、encoder/dimensions、cutover/rollback | 不混查同维异encoder；Gemini空间独立，一图一向量 |
| 持久一致性 | file/manifest/images/index revision 与 fault injection | 无人工覆盖、无半新索引、恢复幂等 |
| 交付数量 | tool output 与 Telegram 实际图片 | 每次答案最多 2 图 |
| 安装与兼容 | make check、临时 DB、clean wheel、真实 Telegram | 必需检查通过，not_run 与已有失败明确区分 |

数量比较允许预先固定的可核对表达等价，例如“二”与“2”，但保留源文和归一化过程；不允许把“勺”未经来源确认换成毫升，也不允许将“适量”变成数字。

### 7.4 必需测试与演练

| ID | 输入/触发 | 必须验证的结果 |
| --- | --- | --- |
| T5-01 | Linux 环境与 service 用户 | 硬件/版本/地址/权限报告真实，不沿用开发机猜测 |
| T5-02 | clean install、restart、缺key/无网络 | 可重复部署；就绪错误清晰；不安装本地模型 |
| T5-03 | CCR文本/OpenRouter各角色小预算smoke | 各endpoint/schema/usage可用，图片不经CCR；无secret日志 |
| T5-04 | VLM多图/ASR粗时间/两类向量响应 | ID/offset/维度正确，实际provider/model/usage齐全 |
| T5-05 | 至少 10 cooking /60 图/负例主集 | 固定标注与题目，质量达到门槛，困难项不删 |
| T5-06 | A/B/C/D同集执行 | 12与90代表帧成本/质量对照，D用独立上限/预算 |
| T5-07 | 连续至少10个代表任务 | RAM/媒体/API稳定，真实p50/p95/partial/限流/成本报告 |
| T5-08 | API限流/磁盘或内存不足 | 有界退避/暂停，不调用本地模型、不杀其他服务 |
| T5-09 | worker 在多 checkpoint restart | 预算不清零，文件/DB/证据可恢复，无重复文档 |
| T5-10 | CCR/OpenRouter/DB不可用 | 已得transcript/旧知识保留，index_pending/失败不误标success |
| T5-11 | 回填期间人工编辑/磁盘不足 | conflict/资源暂停，原文和证据保留 |
| T5-12 | backfill priority/pause/resume | 新任务在当前回填结束后优先，单 worker |
| T5-13 | 视觉embedding off / 总visual off | 前者保留VLM文字，后者零图片；CCR文本独立，旧证据可查 |
| T5-14 | 完整 make check + 新 ingestion lint | core/ingestion/compat/Node/macOS/compile/build 齐全 |
| T5-15 | wheel MCP、DB fresh/upgrade、真实 Telegram | 安装包与运行契约一致，全链路文字/图正确 |
| T5-16 | TTL/低磁盘/交付权限演练 | 不清已引用证据，missing/交付失败可解释 |
| T5-17 | 上线观察/软件回退 | stop/rollback 有证据，旧文本读取正常，不删列/截图 |
| T5-18 | 最终任务与测试矩阵审阅 | 无虚假pass；限制/未完成事项与交付齐全 |
| T5-19 | 3分钟样本actual/estimate/usage丢失 | 各role账单/unknown齐全，偏差有来源，未计费项不写零 |
| T5-20 | 全库云索引cutover与成对rollback | active encoder/space一致，旧数据保留，迁移/query费用单列 |

### 7.5 验收条件与交付证据

- [ ] T5-01–T5-20和全部必要前阶段回归通过。
- [ ] 最终固定基准符合 ≥9/10 Top 3、明确用量正确、负例视觉 0 和两图上限。
- [ ] Linux媒体/API连续任务与每role实际费用/延迟有数据；模型不依赖本地权重/GPU加载。
- [ ] 真正演练缺源/缺图、限流/账单未知、API故障、空间错配、索引回退、人工编辑和checkpoint恢复。
- [ ] make check、额外 ingestion lint、clean package smoke 与 DB migration 证据齐全。
- [ ] 最终配置、lock/hash、部署步骤、运行手册、回退与已知限制已保存。
- [ ] 观察期结果明确；未实测部分写 not_run，不因文档完成而标 verified。

退出策略：停止新视觉/回填，必要时关闭视觉召回或切换references，成对回退encoder/index；保留普通转录、已有知识/证据。任何破坏人工内容、证据错配或预算失控都必须先修复，再恢复回填。

## 8. 贯穿五阶段的端到端验收剧本

以下为测试规格。示例用量是合成 fixture 内容，不能把它当作真实素材或用户配方。

### E2E-01：有明确 cue 的屏显配方

1. 视频 transcript 在 83–85 秒说“把屏幕上的料汁倒进去”，未说具体用量。
2. 字卡在 82–87 秒显示“生抽 2 勺、老抽 1 勺、醋 1 勺”。
3. 分类 cooking eligible；窗口按词/段前后 5 秒生成并裁切。
4. OpenRouter VLM按frame ID读出字卡与动作，数字清晰/无冲突；不另调用OCR或CCR图片。
5. manifest 记录实际截图时刻，Markdown 写原文和引用。
6. 新摘要可包含可靠比例；全文与 transcript checksum 各自语义正确。
7. temporal chunk 绑定画面与步骤，search Top 3 命中正确证据。
8. Telegram 回答原文比例、来源、上下文时间与截图时刻，附 1–2 图。

断言：不靠transcript补量、CCR仅文字/其他模型OpenRouter、frame ID与图片对应、cleanup后证据存在；usage可追溯。

### E2E-02：非 cooking 视频也提到“屏幕上”

1. 上传探店视频或演讲，transcript 有“屏幕上这份菜单/图表”。
2. 分类为 other 或 uncertain；即使选 cooking collection 也不强行准入。
3. 保留文本 enrichment、Markdown 和索引。
4. 检查视觉下载、scene、frame、OpenRouter VLM/视觉 embedding request 全为 0。
5. 用户仍能查询 transcript；有可靠时间可做 transcript temporal chunk。

断言：类别 gate 在所有视觉入口之前；时间索引不触发画面分析。

### E2E-03：同镜头两套数字

1. 同一镜头字卡先“2 勺”，后改“3 勺”，pHash 非常相近。
2. 不在VLM读取前相似去重删除；两种数量状态分别保存。
3. 各 evidence 绑定实际观察时间和对应步骤。
4. 查询具体步骤返回正确状态；若来源不能解释更改，答案说明出现不同用量而不擅自选一个。

断言：数字变化不会消失；检索范围不把两张图错误拼成一套配方。

### E2E-04：云模型失败、账单未知与视觉降级

1. OpenRouter VLM遇到429/timeout或坏JSON，只在剩余请求/图像/时间/金额内退避重试。
2. 模糊字卡最多一次裁剪复核，仍不清楚为unknown；图片不改送CCR/本地模型。
3. 已发请求响应丢失，保留pending/未知支出，重启不清零或当零费用重发。
4. Gemini embedding不可用，可靠原文/动作仍通过Perplexity文本索引；不混查旧向量空间。
5. 关闭视觉embedding只停新视觉向量/召回；关闭总visual则新图片处理为0，CCR文字独立。

断言：role路由、费用预留、partial/index_pending与已有文本/截图可用性符合规格。

### E2E-05：旧文档回填、人工修改和断电

1. 旧 cooking transcript 分类通过，排入一次回填，保留原 document ID。
2. 人工摘要/备注存在；只有程序管理视觉块允许替换。
3. 文件替换前并发修改 → conflict + sidecar，原文不覆盖。
4. 无并发时在替换后/DB 前终止 worker，重启从 journal 恢复。
5. 最终 file、manifest、图片和 chunks 属于一致 revision；原 transcript checksum 不变。
6. 再执行同 revision 没有第二份文档、额外无谓模型请求或预算清零。

断言：人工内容逐项保持、恢复幂等、版本一致。

### E2E-06：缺源、缺图与普通文档共存

1. 旧附件已经删除 → source_unavailable，不无限重试，旧文本可查。
2. 某 evidence 图丢失 → evidence_get 返回 missing，可靠原文仍能回答。
3. 同encoder的时间升级不重嵌入普通笔记；本次切换Perplexity的全库迁移必须重嵌入普通文本，但不抽帧。
4. 关闭视觉后，已有 visual Markdown/截图继续查询与交付。

断言：缺媒体不破坏基础知识；时间升级和encoder全库迁移范围/费用分开，切换模型不能省略普通文档重建。

### E2E-07：长视频预算耗尽与恢复

1. 很多cue/慢API/重试导致600秒、图像/请求/向量限额或美元停止阈值达到。
2. 保存已完成证据、未处理窗口和 partial，不继续后台无限处理。
3. 暂停/重启后累计请求/图像/金额预留与处理时间不清零。
4. 搜索仍可使用已存部分成果，回答不声称已完整阅读视频。

断言：限额是实际实现的，不只是配置说明；coverage 与答案一致。

## 9. 测试工程、运行方式与报告要求

### 9.1 测试分层

| 层次 | 解决的问题 | 数据/依赖 | 执行时机 |
| --- | --- | --- | --- |
| 单元/属性边界测试 | 时间解析、gate、窗口、去重、budget、key、metadata | 纯 fixture、fake clock/provider | 每次相关修改 |
| 组件集成 | FFmpeg帧时间、OpenRouter HTTP契约、manifest、文件恢复 | 合成视频、受控子进程、临时目录 | Phase 2 起 |
| DB 集成 | migration、constraints、事务、mixed versions | 独立临时 PostgreSQL/pgvector | Phase 3 起；不能用 mock 代替全部迁移 |
| 契约/协议 | MCP list/call、search/evidence、OpenClaw filter | stdio server、Node fixture、安装包 | Phase 4 起 |
| 云模型smoke | ASR/VLM/两类embedding协议、时间/维度/usage | 显式小预算API与少量素材 | 组件就绪/供应商变化 |
| 真实语料基准 | 未口述配方、漏检、数字、Top 3 | 人工标注真实 corpus | Phase 2 首次；Phase 4–5 复验 |
| 真实服务 E2E | 用户实际收到答案和图片 | Telegram/OpenClaw、测试 collection | Phase 4–5 |
| 故障与部署演练 | stop/restart、限流/未知账单、低盘、空间回退 | 受控测试环境、版本化备份 | Phase 2 恢复；Phase 5 实机 |

CI/普通自动化测试不调用付费模型，不依赖公网下载。OpenRouter smoke/真实CCR/Telegram 另设显式测试入口和日志，绝不能以 fake 的准确率当真实模型质量。

测试应验证外部行为与重要不变量，不为每个私有方法写镜像测试。budget/gate/人工保护/时间归属/迁移/协议属于必须自动化的高价值行为。

### 9.2 建议测试文件与覆盖

以下是待实施文件分组，不表示已经存在；可以根据模块最终职责合并。

| 建议文件 | 主要覆盖 |
| --- | --- |
| ingestion/tests/test_transcript_temporal.py | T1 时间解析、旧 wrapper、重复字幕、word |
| ingestion/tests/test_video_classification.py | category、依据、长文本、cache、unknown |
| ingestion/tests/test_visual_gate.py | 非准入所有视觉调用为 0 |
| ingestion/tests/test_video_cues.py | cue、窗口、合并、无 cue 兜底 |
| ingestion/tests/test_frame_selection.py | PTS/VFR、数字状态、清晰度、sampling |
| ingestion/tests/test_openrouter_asr.py | audio endpoint、分片offset、实际时间能力、费用 |
| ingestion/tests/test_openrouter_vision.py | 多图ID/JSON、原文/单位/动作、重试与32K门槛 |
| tests/test_openrouter_embedding.py | data/index/count/维度/有限值、space隔离/预处理 |
| ingestion/tests/test_vision_routing.py | OpenRouter各role、CCR仅文字、开关/JSON/unknown |
| ingestion/tests/test_visual_budget_cache.py | 累计预算、checkpoint、层级失效、coverage |
| ingestion/tests/test_visual_persistence.py | Markdown/资产/checksum/目标后选/cleanup |
| ingestion/tests/test_visual_backfill.py | discovery、gate、priority、幂等/缺源 |
| ingestion/tests/test_visual_merge_recovery.py | 人工保护、并发编辑、file/DB 崩溃点 |
| tests/test_video_temporal_chunks.py | chunk modality/归属/硬限/粗段 fallback |
| tests/test_migrations.py（扩展） | fresh/upgrade/ALTER/checksum/锁/package |
| tests/test_rag_core.py（扩展） | current/version/事务/检索兼容 |
| tests/test_embedding_generation.py | 全库shadow/追平/cutover/rollback、同维异space |
| tests/test_visual_embedding_retrieval.py | 一图一向量、Gemini query、RRF、文本降级 |
| tests/test_rag_evidence.py | ID/path/revision/权限/staging/TTL |
| tests/test_mcp_server.py 等（扩展） | 三工具、元数据、协议、doctor/安装 |
| ingestion/openclaw-plugin/*.test.js（扩展） | 工具允许、两图、MEDIA/错误交付 |
| ingestion/benchmark 视觉评测入口 | corpus、标注、A/B/C/D、metrics、机器指纹 |

### 9.3 现有可用检查命令

下面来自当前 Makefile，**本次计划阶段未运行这些业务检查**。后续在 Linux 服务器的仓库根目录、项目开发环境中运行并保存结果，不将当前工作区的结果当作服务器基线。

```bash
make test-core
make test-ingestion
make test-compat
make test-node
make test-macos
make compile
make build
make check
```

当前 `make test-macos` 仅执行 macOS 安装/卸载脚本的 `bash -n` 语法检查，可纳入 Linux 的 `make check`；它不表示已在 macOS 运行安装器或完成平台实测。未来若增加依赖 macOS 实机的项目，应在相应环境单列结果，Linux 未执行的项目保持 `not_run`。

后续新增 ingestion 代码还需要纳入 ruff。建议在 Linux 实施后使用既有测试目录并补充例如：

```bash
python3 -m ruff check ingestion/backend/ingestion ingestion/tests
```

若该范围存在历史 lint 错误，先记录基线和本轮增量，并把新模块全部覆盖；不能以历史问题为理由不检查本轮文件。未来的 `visual-backfill`、`--with-visual`、`--corpus-dir` 等命令是规格入口，尚未实现，具体可执行 CLI 必须在对应 phase 交付时固定，不把占位命令记作已运行。

### 9.4 必需报告内容

每次阶段报告至少包含：代码版本、执行时间/机器、配置指纹、model/provider/space/price revision、测试列表和实际 pass/fail/not_run、样本/标注版本、预算/coverage、结果样例、已知限制与下一阶段阻塞。

测试失败要写可复现输入和预期/实际差异。修复后仅对受影响测试和必要回归重跑；没有新问题时不无意义重复扩大检查。

## 10. 开发日志与阶段完成记录

### 10.1 计划修订记录（当前尚无新增功能实施日志）

| 日期 | 已实际完成的文档/研究工作 | 产物/证据 | 状态 |
| --- | --- | --- | --- |
| 2026-10-01 | 初始整理 transcript 引导选帧、本地 OCR/VLM、CCR 路由、时间检索方案 | devplan.md 初始方案 | 历史记录；本地模型路线已由最新决策替代 |
| 2026-10-01 | 修订 cooking-only 与文本/图片独立路由 | devplan.md 的初始准入/provider/回填方案 | 历史记录；准入边界沿用，模型路线现已更新 |
| 2026-10-01 | 核对当前 Makefile、migration renderer、索引版本和工具清单等落点 | 当前仓库只读检查 | 已核对，未改业务代码 |
| 2026-10-01 | 将实施拆成五阶段并逐项定义MVP/验收/恢复 | 本文件，初始路线为本地模型（已被最新决策替代） | 历史文档记录；各Phase planned |
| 2026-10-01 | 按用户最新决策改为CCR文字+OpenRouter其他模型，加入90/12帧费用与全库空间迁移 | devplan.md、本文件、模型调研 | 仅文档完成；未实施、未新增/运行测试、未付费调用 |
| 2026-10-01 | 明确当前计划阶段，后续 Phase 1 起在 Linux 开发；加入服务器开工清单与环境边界 | devplan.md、本文件 | 仅文档修订；未在 Linux 执行环境准备、新功能开发或测试 |

### 10.2 阶段状态面板

| 阶段 | 状态 | 功能完成 | 离线测试 | 真实测试/验收 | 可以进入下一阶段吗 |
| --- | --- | --- | --- | --- | --- |
| Phase 1 | planned | 未实施 | not_run | 分类/零视觉调用待验证 | 否 |
| Phase 2 | planned | 未实施 | not_run | ≥10 视频/60 图待验证 | 否 |
| Phase 3 | planned | 未实施 | not_run | 真实 DB migration/重建待验证 | 否 |
| Phase 4 | planned | 未实施 | not_run | 真实 Telegram 待验证 | 否 |
| Phase 5 | planned | 未实施 | not_run | Linux 资源/最终基准/发布待验证 | 否 |

实施时更新这张表，不将本文待办清单的存在等同于完成。

### 10.3 后续 Linux 开发步骤的日志模板

```markdown
### YYYY-MM-DD — Pn-xx — 步骤名称

- 状态：planned / in_progress / blocked / implemented / verified
- 代码版本或 patch：
- Linux 主机标识、系统与开发用户：
- Linux 仓库根目录、开发环境与测试数据库：
- 对应计划版本/需求变更：
- 实际完成：
- 修改文件：
- 用户可见行为/达到的目标：
- 关键配置、schema/prompt/model revision：
- 测试 IDs 与实际命令：
- 结果：pass / fail / not_run；报告位置：
- 故障/回归/资源数据：
- 人工内容、证据、预算等不变量验证：
- 新假设或偏离规格及原因：
- 剩余事项/阻塞：
- 下一步与退出/恢复方式：
```

### 10.4 每阶段验收模板

```markdown
### Phase n — MVP 验收 — YYYY-MM-DD

- MVP 功能演示：
- 所有 Pn 任务状态：
- Tn 与受影响回归结果：
- 真实语料/DB/Telegram/服务器证据：
- 固定基准与质量门槛：
- provider 调用、预算、coverage：
- 失败隔离/取消/恢复/回退验证：
- 兼容与安装结果：
- 未解决限制：
- 结论：verified / failed / not_run
- 是否满足进入下一阶段条件：是 / 否；原因：
```

### 10.5 变更与问题记录

每条新问题至少记录 issue ID、关联 P/T/E2E、发现版本、复现条件、影响、严重程度、临时 fallback、修复版本和回归证据。以下情况属于阶段阻断：

1. 非 cooking 自动进入视觉阶段。
2. 已确认数字/单位错误或图片属于错误文档/步骤。
3. 人工内容被覆盖、文件/DB 恢复不一致。
4. 图片开关/预算被绕过、图片误发CCR、自动切本地或高价模型、同维异space混查。
5. 重试/恢复清零预算或无限回填。
6. 仅时间schema升级导致普通文档无必要重嵌入，或encoder切换遗漏全库重建/切到半成品；旧检索/安装失效。
7. 证据路径越界或清理删除有效引用图片。

吞吐较慢、某些图保持 unknown、缺源附件无法回填不自动等同于 bug；它们必须如实报告，并与预算/质量验收区分。

## 11. 需求与阶段追踪矩阵

| 用户需求/核心不变量 | 开发任务 | 主要测试/演示 | 最终可见结果 |
| --- | --- | --- | --- |
| 不给所有视频抽帧，先 transcript 分类 | P1-05/06/07、P2-11 | T1-05–14、T2-24/33、E2E-02 | 非 cooking 仅文本处理 |
| 文本优先 CCR/GPT | P1-04/05、P2-09、P5-01 | T1-11/16、T5-03/13 | 分类/摘要走CCR，图片不经过CCR |
| CCR仅文字，其他模型OpenRouter | P1-03/08、P2-01/05/06/07 | T1-19–22、T2-10–18/35–37、E2E-04 | 云协议/usage/一次复核可追溯，无本地模型加载 |
| 成本优先与代表帧索引 | P2-05/07/08、P3-05/07、P5-04/05 | T2-15–17/35–37、T3-19–24、T5-19/20 | 90帧分析+12向量预算、费用与质量实测 |
| 屏幕上的酱汁触发截图 | P2-03/04/05 | T2-03–09、E2E-01 | 命中窗口里的原始配方 |
| 前后 3–5 秒、1秒/半秒采样 | P2-03、budget/config | T2-03/09/15 | 初始按 ±5 秒，必要时局部 0.5 秒 |
| 数字变化不能去重漏掉 | P2-05、P3-02/03 | T2-06/08、T3-05、E2E-03 | 多状态证据分别绑定时间 |
| Linux 32GB / 4060 8GB 可运行 | P2-01/07、P5-01/02/05 | T2-18、T5-01/04/07/08 | 媒体/API资源、模型价格与延迟报告 |
| 旧 cooking 自动回填 | P2-10/11/12 | T2-23–31、E2E-05/06 | 幂等升级，无无限缺源重试 |
| 保留人工编辑 | P2-12 | T2-27–31、T5-11 | 保留正文/摘要，冲突 sidecar |
| 不留完整原视频，留证据 | P2-08/09、P4-03/06 | T2-21、T4-08、E2E-06 | 截图与 manifest 长期可用 |
| 时间定位和检索 | P1-02/03、P2-04、P3 全部 | T1-01/02、T3-01–18 | 时间精度、区间与 actual time 可解释 |
| 最多两张截图直接回答 | P4-02–07 | T4-03/12/15、E2E-01 | Telegram 真正收到相关图 |
| 看不清不猜 | P2-05/06、P3-03、P4-05 | T2-09/14、T4-13 | unknown/conflict 明确表达 |
| 100 次/10 分钟与暂停恢复 | P2-07/08/12 | T2-15–18/30/31、E2E-07 | 累计预算可追溯，partial 不隐藏 |
| 不换架构、不破坏普通文本 | P1-01、P3-04–07、P5-07 | T1-18、T3-07–18、T5-14 | 原数据库/主契约延续；encoder切换全量shadow重建且可回退 |
| 真实质量，不靠模型宣传 | P2-13、P4-07、P5-03–05 | T2-32/33、T4-15、T5-05–07 | 可复现基准与实机报告 |

## 12. 假设、未提供信息与明确的后续范围

### 12.1 最新模型与部署假设

1. cooking依内容分类，unknown跳过视觉，collection不代替classifier。
2. CCR `Codex API/gpt-6-luna`承担所有文字LLM；Qwen3.7 Flash/Qwen3 ASR0.6B/Perplexity0.6B/Gemini2承担其他模型，不部署本地OCR/模型。
3. 3分钟90帧分析、12代表帧向量是成本起点；数字/动作/检索质量尚未实测。
4. 候选1000、VLM请求/图像各100、证据100、向量图像24、视觉600秒；金额目标$0.01、停止新调度$0.03，不冒充绝对上游费用硬限。
5. cue±5秒，初次1秒、必要时0.5秒；覆盖约2秒，长视频降低密度并记录partial。
6. 每批最多15图/约30秒，目标输入24K，32K前拆分；输出含计费推理并记账。
7. ASR无实际细时间时按音频分片标coarse，未知offset标approximate，不编造词级时间。
8. 文本模型计划1024维/Gemini计划768维，按响应验证；同维换encoder也全库重建，query使用匹配空间。
9. shadow代际在同PostgreSQL，追平后成对切encoder/index；旧索引保留，RRF融合独立视觉与文本召回。
10. 缓存30天/outbound24小时，引用证据长期保留；5GB磁盘门槛、人工保护/单worker/回填优先级沿用。
11. 3分钟费用估算不含CCR、全库迁移、query、充值/税/存储；每项actual/estimate/unknown分别报告。
12. 后续 Phase 1 起在 Linux 开发；Linux/CCR/OpenRouter 实际可用性、corpus 与原索引规模在开工准备中确认，发布前复核。当前只修订计划。

### 12.2 本次拆分新增的工程假设

1. **Phase 5 用于实机发布保障**，不把原预留按需视频模型入口变成默认必做功能。
2. **保留原 Phase 1 的质量关口**：新 Phase 2 内就做 ≥10 视频/60 图初步基准，后期再复验。
3. **无法可靠细分的超长旧 transcript 退回 text**，粗上下文保存在 metadata；优先满足不编造时间，不能用假的 60 秒子区间满足硬限。
4. **预算请求发送前预留**，崩溃时不明请求按消耗计数；恢复不重新得到完整额度。
5. **600 秒累计实际视觉工作**，暂停/停机等待不计，终止/清理额外开销单独报告；不是全任务十分钟承诺。
6. **补充至少 10 条 non-cooking 真实负例**，作为最终基准建议，目的是验证准入成本为零。
7. **至少连续 10 个代表任务与建议 24 小时观察**，用来发现资源增长/恢复问题；没有任务的空闲时间不能单独证明稳定。
8. **回填发现也有界分批**，不因扫描/分类长时间挡住新任务；仍保持单 worker、不设每日上限。
9. **测试与日志中的 pass 必须有证据**；本次全部 phase 保持 planned，真实测试 not_run。

这些假设不需要阻塞当前文档；实施时如发现冲突，应记录原因、具体影响和新验收方式。改模型/配额/类别/合并语义属于规格变更，不能静默修改后沿用旧的 verified 结论。

### 12.3 尚未提供的信息

- Linux访问/service用户/目录权限、CCR实际地址与文字路由。
- OpenRouter key注入方式、账号可用供应商/模型与实际时间/usage能力；无需在计划中保存secret。
- cooking corpus/旧附件位置、可取来源范围。
- 全库collection/document/token规模、现有索引维度/代际和重建费用估算。
- 测试collection、outbound目录、最终批量/限流/支出参数。

当前保留计划状态。后续在 Linux 开工时准备这些信息，并按第 0.1 节进入 P1-01；功能开关计划默认关闭，启用后按 OpenRouter 路线处理图片/音频。尚未执行的开发、部署、质量与费用验收不写完成。

### 12.4 五阶段之后的可选增强

1. 原生视频片段VLM/embedding与时序检索，验证实际OpenRouter接入后再加入。
2. 其他类别profile与准入规则；平台时间跳转/更多证据交互。
3. 更强VLM、ASR或Qwen8B文本embedding的独立成本/质量对照；换encoder另建空间。

上述不阻断默认五阶段，不要求永久保留原视频。

## 13. 最终交付清单

- [ ] 五个阶段的功能代码与明确版本/patch。
- [ ] 新增CCR/OpenRouter配置模板、媒体/HTTP依赖lock、model/provider/space/price revision。
- [ ] transcript/分类/cue/视觉/manifest/chunk/MCP schema 与版本说明。
- [ ] 核心0002时间/0003视觉与代际迁移、全库shadow重建/追平/切换/回退、0001 checksum证据。
- [ ] T1–T5 自动测试与真实演练报告，所有 not_run/fail 单独列出。
- [ ] ≥10 cooking 视频/60 图与负例的标注、A/B/C/D、Top 3/数量/coverage 质量报告。
- [ ] Linux32GB/4060 Laptop的媒体RAM、API延迟/限流/连续任务、逐role actual/estimate费用与降级报告。
- [ ] 新导入与旧回填样例、人工保护/冲突/断电恢复日志。
- [ ] 真实 Telegram 的正确配方、出处/时间、最多两图与缺图/未知样例。
- [ ] make check、补充 lint、clean package、MCP/allowlist/doctor、平台兼容结果。
- [ ] 启停、pause/resume/retry、资源不足、限流/未知账单、缺key/缺源/冲突/缺图、encoder/index成对回退手册。
- [ ] 更新本文件的状态面板与真实开发日志，保留新假设、限制和后续范围。

五阶段最终完成意味着：用户的 cooking 视频能以 transcript 为入口获得可靠的画面知识、可追溯时间和截图证据，同时非 cooking 的图片处理成本为零；质量、资源、失败恢复和旧项目兼容均有真实测试支撑。
