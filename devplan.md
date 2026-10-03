# 视频视觉知识与时间检索开发计划

更新日期：2026-10-01（America/Chicago）。当前为计划阶段；后续在 Linux 服务器开发，沿用 OpenRouter 成本优先方案。

**当前状态：planning / planned（计划阶段）。** 已整理需求、模型调研、费用估算和实施规格；本文描述的新增功能尚未开始开发，模型质量、接口兼容、费用与性能均待实际验证。当前只编辑开发文档，未安装依赖或模型、调用付费 API、迁移数据库或运行测试。

**后续实施环境：用户的 Linux 服务器。** Phase 1 起的代码开发、依赖安装、测试、数据库实验和 API 联调均在 Linux 工作目录开展；当前 macOS 工作区用于方案整理，不作为服务器环境基线。服务器路径、系统版本、服务用户、CCR 地址和素材位置在开工时记录。

## 1. 目标与已经确认的选择

核心场景：博主说“加入屏幕上的料汁”，画面显示“生抽 2 勺、老抽 1 勺、醋 1 勺”。系统应检索出画面的原始配方，给出视频出处、时间和相关截图，不依赖博主把配方念出来。

本次详细设计 Phase 1–3，逐阶段验收；Phase 4 的按需视频片段分析仅保留后续入口。实施保留 PostgreSQL/pgvector、现有 collections、Markdown 知识文件、采集生命周期及 MCP 主检索契约。文本 embedding 改为 OpenRouter，并在同一 PostgreSQL 中重建独立索引代际；使用 FFmpeg、现有字幕/下载能力和 Telegram/OpenClaw 集成。旧 Ollama 索引仅保留为迁移前的兼容与回退数据。

已确认：

- Telegram 链接与附件先获取 transcript 并进行内容分类；仅判定为 cooking 做饭/食谱的视频进入视觉增强，不改旧 Web 转录页面。
- 视觉功能默认关闭；启用后仍须通过 cooking 分类门槛。其他类别、分类不确定与纯音频不进行视觉专用下载、场景检测、抽帧、VLM 或视觉 embedding 调用。
- 文字 LLM（分类、摘要、标签、问答）通过 CCR 调用 `Codex API/gpt-6-luna`；VLM、画面文字提取、文本 embedding、视觉 embedding、需要模型的语音识别均走 OpenRouter。模型与协议配置分开。
- OpenRouter 接收抽出的图像与所需音轨，不默认上传整段视频。屏幕文字由 VLM 同次提取，不部署独立本地 OCR；FFmpeg、场景检测和清晰度/hash 筛选在本地运行。
- 旧视频先分析已有 transcript，仅 cooking 内容自动回填；可以扫描所有 collection 发现这些视频，但不对所有类别抽帧。不设每日视频数量上限；单 worker 空闲时运行，新任务在当前回填视频完成后优先处理。
- 回填尽量保留人工编辑并合并；新视频摘要纳入清晰视觉事实，旧文档保留原摘要与人工正文。
- 原视频处理后清理，证据关键帧长期保存；回答直接附最多两张相关截图。
- 数字看不清时允许一次高清图或局部裁剪复核；仍不清楚就标为未知，不补猜。
- 保留每视频最多 100 次 VLM 请求和 600 秒累计视觉工作预算；另加最多 100 张发送图像（含裁剪/重试）、视觉向量数量与美元支出控制。3 分钟默认约 90 帧分析、12 张代表帧做视觉 embedding；第 4–5 节区分目标预算和停止阈值。
- Phase 1 须通过至少 10 个真实视频的屏显配方检索基准：正确证据 Top 3 命中率至少 90%，回答中的数量与单位符合人工标注。

使用现有 PostgreSQL/pgvector 增加稀疏代表帧视觉索引；不引入新向量数据库、LangGraph、知识图谱、多代理视频系统或 UI 重设计，不整体移植 VideoRAG。图像向量检索不等同于原生视频时序编码。

### 1.1 文档用途与 Linux 开工入口

- 本文保存设计、模型/预算与架构约束；[Development MVP.md](<Development MVP.md>) 保存五阶段待办、验收规格和后续实施记录，以该文件的五阶段编号推进。
- 当前所有新增功能阶段保持 `planned`，未执行的测试保持 `not_run`；计划中的配置、接口、迁移和命令不表示已实现。
- 后续进入 Linux 开发时，先完成 Development MVP.md 第 0.1 节的开工准备，再开始 P1-01。基础环境不等到 Phase 5 才建立；Phase 5 对最终版本复验和发布。
- 本次编辑时，两份计划与调研目录尚未纳入 Git。后续同步到 Linux 时应将它们一并纳入版本管理或复制，并记录代码与文档版本，不能只克隆代码后遗漏计划。

## 2. 当前项目事实与实施落点

本节为当前工作区源码的只读核对结果，不是 Linux 服务器的运行状态；在 Linux 同步代码后，P1-01 按实际版本重新核对。

| 当前事实 | 修改含义 |
| --- | --- |
| src/rag_favorite 是产品核心；services/rag-app/rag.py 是兼容转发。CookingIndexerAdapter 也调用核心 ingest_file。 | 时间分块和检索修改集中在产品核心，不另建 cooking 视频检索系统。 |
| URL 优先抓字幕，成功后不下载视频；无字幕时下载音频。 | transcript 分类为 cooking 且视觉启用后，才通过 prepare_visual_media 取得画面；其他类别保留字幕/音频路径。 |
| Telegram 附件原文件会复制到 job 目录，写 staging Markdown 后被清理。 | 在清理前完成视觉处理；证据与可复用缓存移出临时目录。 |
| 当前 Faster-Whisper 返回浮点 segment 时间，但输出截成整秒；VTT/SRT 也丢掉毫秒。 | 字幕保留毫秒；新增 OpenRouter ASR adapter，统一结构化结果与兼容 Markdown wrapper。时间能力依实际供应商响应校验。 |
| 核心 request_embeddings/embed_documents/search 直接创建 OllamaEmbeddingClient，默认 Qwen 0.6B、1024 维。 | 接入 EmbeddingProvider factory 与 OpenRouter client；切换模型必须完整重嵌入文本，不能把同维新旧向量混用。 |
| 字幕解析使用全局 seen_texts。 | 改为相邻、重叠滚动字幕去重，不能删除相隔很久再次出现的“加入酱汁”。 |
| builder 的 checksum 是 transcript body 校验和；staging/索引另有全文 SHA256。 | 保留 transcript_checksum 语义；视觉内容用独立 revision 和完整文档 hash。 |
| 重复任务直接复用旧 Markdown，目标文件存在时当前 persist 会丢掉新 staging。 | 必须增加显式视觉更新路径，不能仅在原去重后追加代码。 |
| jobs.metadata 已有 JSONB；JobState 与数据库 CHECK 相互约束。 | Phase 1 用 metadata 存视觉状态，不添加视觉专用 JobState 或 RAG 表字段。 |
| 文本 enrichment 只看 transcript；真实 API 失败目前会抛错。 | 新视频加入有界视觉事实；文本/视觉服务失败均有确定性转录归档 fallback。 |
| chunk_text 按 1800 字符、250 重叠切分，可能把时间标题和画面描述拆开。 | Phase 1 验证画面文字可检索；可靠时间归属由 Phase 2–3 完成。 |
| migration renderer 要求每份 SQL 含 vector(1024)。 | 新增 ALTER 迁移前，改成只渲染初始迁移的维度，不改已应用 0001 内容。 |
| MCP 白名单忽略未知顶层字段，但保留 metadata；工具清单目前固定两个。 | 时间和证据放 metadata；新增取图工具时同步工具清单、smoke、OpenClaw filter。 |

主要集成点：ingestion/backend/ingestion 的 media、service、markdown、config、repository；ingestion/backend 的 transcriber、video_processor；产品核心 rag、migrations、mcp_server/mcp_contracts。新模块按职责拆成 temporal_models、content_classifier、cue_detector、scene_detector、frame_selector、openrouter_asr、openrouter_vision、visual_enricher、model_usage_ledger；产品核心增加 embedding provider factory 与视觉索引 adapter；这些是内部接口，不另起一套采集服务。

### 2.1 内容分类与视觉准入

这里的 cooking 是根据 transcript 判断的内容类别，不能只用保存到哪个 collection 来决定。存在综合库中的实际做饭视频可以通过；放在 cooking 库里的餐厅探店、营养讨论或闲聊，也不能仅凭目录自动抽帧。分类不自动移动知识库，目标选择沿用当前流程。

TranscriptAnalyzer 使用已有 CCR/GPT 文本接口分析带 segment ID 的 transcript，返回 ContentClassification(category, decision, evidence_segment_ids, reason) 与可复用的基础摘要/标签。category 初始 cooking/other/unknown，decision 为 eligible/not_eligible/uncertain。实际制作食谱、配料用量、步骤演示等支持 cooking 判定；仅提到食物或单个“酱汁”词不够。

程序验证引用的 segment ID 确实存在，并保留分类模型、prompt 版本、transcript checksum 和已分析覆盖。长 transcript 分批、按原时间顺序分析，避免只看标题/开头；覆盖不完整且没有明确阳性证据时返回 unknown。文本批次使用现有 LLM 超时/长度限制，分类请求只含文本，不附图。

只有视觉开关开启、decision=eligible、category 在允许列表 cooking 内且输入确有视频轨时，才进入画面阶段。CCR 分类失败时可复用相同 transcript checksum 的有效分类缓存；否则记录 classification_unavailable/unknown，保留转录并跳过视觉。未知结果不能靠用户选择的 collection 强行变为 cooking。

分类结果保存在 job.metadata 与 Markdown 的独立 video_classification 元数据，视觉跳过状态区分 not_eligible_category/category_uncertain/classification_unavailable。非 cooking 的“屏幕上”“如图”等 cue 不越过该门槛；场景检测和无 cue 补采样也只在通过门槛后执行。

## 3. 研究结论：什么才是“关键帧”

### 3.1 编码关键帧与知识证据帧不同

视频 codec 的 I-frame/keyframe 为解码服务，不能作为配方重要性的判断标准。P/B 帧也可能恰好显示完整比例。FFmpeg 分别提供编码帧类型、时间与 scene 等选择条件。[FFmpeg select 文档](https://ffmpeg.org/ffmpeg-filters.html#select_002c-aselect)

本项目把“关键帧”定义为：能补充转录缺失信息、文字/数字足够清晰、与当前步骤相关且具有独立证据价值的画面。

PySceneDetect 的 ContentDetector/AdaptiveDetector 主要依据颜色/画面变化检测切镜头，AdaptiveDetector 用相邻变化的滚动统计减少运动误报。由算法可以推断：同镜头里只更新一小块配方数字，不一定产生 scene cut。场景检测适合作为候选来源，不能单独保证抓到料汁比例。[PySceneDetect detectors](https://www.scenedetect.com/docs/latest/api/detectors.html)

VideoTree 的研究采用与问题相关的自适应视频表示，可以借鉴“把更多分析预算用于相关区域”的方向；它并未验证本项目的中文 transcript 规则或前后五秒参数。[VideoTree，CVPR 2025](https://openaccess.thecvf.com/content/CVPR2025/papers/Wang_VideoTree_Adaptive_Tree-based_Video_Representation_for_LLM_Reasoning_on_Long_CVPR_2025_paper.pdf)

### 3.2 先用 transcript 找值得看的时间

确定性 cue 规则是首期默认：

- 视觉指向词：屏幕上、画面上、如图、看这里、这个比例、按这个配方、打在/打到/打上屏幕、on screen、as shown、this ratio。
- 烹饪组合：放/加/倒/混合与酱汁/料汁/调料/配比等共现；不能把每个“加”都触发成高优先级。
- 首期只启用 cooking profile；幻灯片、代码、图表等类别不触发视觉。后续新增类别必须显式加入准入列表并定义自己的 cue/profile。
- 保存命中的原始 segment ID、规则和上下文；时间由程序读取原始 segment/word，不由 LLM 编造。
- 可选语义 cue 扩展只做有界文本批处理，返回现有 segment ID；默认关闭，异常时仍使用确定性规则。

字幕优先，已有可用字幕不再付费 ASR。无字幕时经 OpenRouter `audio/transcriptions` 调用 Qwen3 ASR 0.6B；统一保存供应商实际返回的 segment/word 时间及音频分片 offset。接口支持不等于每个供应商都会返回词级时间，实施时核对实际响应。若只返回文本，按本地 FFmpeg 切出的约 30 秒音频分片保存粗区间，标 `coarse`，不按字符比例生成词级时间；无可靠音视频 offset 则标 `approximate`。旧 Faster-Whisper API 保留兼容，但新部署不自动回退到本地模型。[OpenRouter 转录接口](https://openrouter.ai/blog/tutorials/transcription-on-openrouter/)

新字幕保留原始毫秒；旧 Markdown 只能使用已有段级时间，不能恢复原本丢失的词级精度。兼容 MM:SS、HH:MM:SS、小时折算成分钟的 75:30，以及已有粗粒度段落。

## 4. 选帧与识别算法

### 4.1 主流程

~~~text
字幕 / OpenRouter Qwen ASR 结构化 transcript
        ↓
CCR/GPT 文本分类：是否 cooking 做饭/食谱？
        ├─ 非 cooking / unknown → 常规文本归档 + OpenRouter 文本 embedding
        ↓ 是，且视觉开关开启
cue 局部窗口 + 场景候选 + 每 2 秒覆盖采样
        ↓
本地清晰度 / 完全重复筛选；为每帧绑定真实时间和 frame ID
        ↓
OpenRouter Qwen3.7 Flash：分组分析动作 + 画面原文
        ↓
数字不清 / 冲突 → 剩余预算内最多一次高清或裁剪复核
        ↓
视觉时间线 + manifest + 长期证据截图
        ├─ 全部可靠文字 → Perplexity 文本 embedding → 文本检索
        └─ 约 12 张代表帧 → Gemini 图像 embedding → 稀疏视觉检索
~~~

文字与图像都由本次 VLM 提取。模型只返回 frame ID、可见事实与状态，程序绑定时刻；无依据的数量不写入确定摘要。复核使用同一 VLM，不用 CCR 接收图片，不自动切换更贵模型。

### 4.2 窗口与采样

1. 优先采用命中词组的时间；没有词级结果则用整段 start/end。
2. 窗口为 [cue.start - 5 秒, cue.end + 5 秒]，裁到 [0, duration]，合并重叠窗口。
3. 初次每 1 秒取候选画面。长段级窗口按连续子窗口处理，共享全视频预算，避免把一大段讲话视为单个瞬间。
4. 指向屏幕却没有读到配方、VLM 标 unreadable/uncertain 或相邻数字冲突时，只对该窗口补到每 0.5 秒；每轮最多补 20 张。
5. 每个 cue window 优先保留 VLM 读取后文字最完整的 1–3 张，以及文字/数字确实发生变化的不同状态；不为满足配额重复保存相同字卡。
6. 并行生成 PySceneDetect AdaptiveDetector 的场景候选；初始最短场景 0.5 秒，保留短叠字的独立候选机会。
7. 已通过 cooking 分类但无 cue 或有未覆盖区间时，取场景代表帧，并以 max(2 秒, 视频时长/90) 做低成本覆盖。非 cooking 不执行该兜底。采样间隔内更短的闪现可能漏检，coverage 中明确记录，不承诺发现所有瞬时文字。
8. 3 分钟默认覆盖候选约 90 帧；cue/场景候选共享发送图像上限，优先保留新增字卡与动作，不在覆盖 90 帧之外无限追加。长视频/快剪达到预算时标记 partial。每约 30 秒分一组，初始最多 15 帧/请求，输入估算逼近 32K 时进一步拆分。

场景检测若无切换，将整视频作为一个场景；检测超时/异常可只保留 cue 与低频覆盖，不导致转录失败。[PySceneDetect SceneManager](https://www.scenedetect.com/docs/latest/api/scene_manager.html)

### 4.3 关键与非关键的判断

先完全相同像素/内容 hash 去重。pHash 仅用于候选分组，不能直接删除相似画面：2 勺变 3 勺可能只改几个像素。

VLM 读取后按以下顺序排序，而不是给尚未校准的加权分数假装概率：

1. 含新的数量、单位、比例或完整配方文字。
2. 与 cue/步骤直接相关，文字区域完整、遮挡少。
3. VLM 可读状态、文字完整性与相邻帧一致性较高。
4. 画面清晰，距离 cue 较近。
5. 在不同时间段和不同文字状态之间保留覆盖。

数量或单位 token 不同的帧必须进入不同状态，不能被文字去重合并。仅有片头、黑屏、转场、严重模糊、相同背景/相同字卡的重复帧降级。与 transcript 完全重复的烧录字幕降低优先级，但不按屏幕位置直接屏蔽底部区域，以免误删配方字卡。

画面文字仅规范化空白和排版用于比较；保留原始中文数字、单位与标点。VLM 自报置信度不作为校准概率，不使用原本地 OCR 的 0.90 阈值；可读性与邻帧一致只是启发式。数字不一致、缺字或与转录矛盾时保存原始证据并标记 uncertain/conflict。

### 4.4 预算与抽帧时间

| 限额 | 默认 / 规则 |
| --- | --- |
| 本地候选帧 | 1000/视频，仅解码、清晰度与 hash，不跑本地模型 |
| VLM 发送图像 | 100/视频；多图请求逐图计数，包含高清、裁剪、失败与重试 |
| VLM API 请求 | 100/视频；独立于图像数量，不把一批 15 图算成 1 张 |
| 首轮分组 | 约 30 秒、最多 15 帧/请求；输入估算接近 32K 时继续拆分 |
| 持久证据 / 视觉条目 | 100/视频；证据数量不等于 embedding 数量 |
| 视觉 embedding | 3 分钟目标 12 张代表帧；默认最多 24 张，包含重试计数 |
| OpenRouter 支出 | 3 分钟推荐目标 $0.01；每视频默认调度停止阈值 $0.03，可显式调整 |
| 视觉 deadline | 累计 600 秒，含新增视觉下载、检测、抽帧、VLM 与图像 embedding |
| cue / 覆盖预算比例 | 80% / 20%，可互借 |
| 云请求并发 | 初始 1；与限流、重试、暂停及剩余额度统一调度 |
| 首轮图像 | 最长边 1024；原图保存，数字不清时一次高清/裁剪复核 |

上限是工程初始值。美元阈值覆盖此次视频的 OpenRouter ASR/VLM/两类 embedding，不含 CCR LLM、充值手续费、税和存储；全库 embedding 迁移与后续查询单独记账。字幕命中、缓存命中按实际零调用记账。发送前预留请求、图像和预计费用，返回后以 `usage.cost` 对账；响应丢失按未知支出保留预留额，不盲目重发。

费用预留使用版本化单价、输入估算安全余量、最大输出 tokens 和已知音频时长。图像 token 化与上游账单未完全确定时，$0.03 是停止新增工作的阈值，不能声称上游计费绝对不越界；已发请求无法撤销费用。超额或账单缺失单独报告。恢复不清零额度，暂停/停机等待不计处理时间。

代表帧按时间覆盖、不同文字状态和可见动作选择；12 是目标，不因配额合并不同配方。默认上限 24 达到后保持文本证据，记录视觉索引 partial。90 帧全部 embedding 可显式设更高上限并改费用预算。

抽帧使用解码后的目标画面，允许 P/B 帧；记录实际 source PTS、time_base、视频起始偏移、target_time_seconds、observed_at_seconds、timestamp_precision，不以图片编号/fps 假装真实时刻。只有目标时间时标 approximate。[FFmpeg seek](https://ffmpeg.org/ffmpeg.html#Main-options)、[FFmpeg fps](https://ffmpeg.org/ffmpeg-filters.html#fps)

## 5. OpenRouter 模型、协议与费用

### 5.1 默认选型

| 角色 | provider / model | 接口与价格基线（2026-10-01） |
| --- | --- | --- |
| 文字 LLM | CCR `Codex API/gpt-6-luna` | Responses；费用不计入下表 |
| VLM + 画面文字 | `qwen/qwen3.7-flash` | `/chat/completions`；输入 $0.03/M、输出 $0.13/M，单次输入 <32K |
| ASR | `qwen/qwen3-asr-0.6b` | `/audio/transcriptions`；$0.00000333/音频秒 |
| 文本 embedding | `perplexity/pplx-embed-v1-0.6b` | `/embeddings`；$0.004/M 文本 tokens；计划 1024 维，按实际响应验证 |
| 视觉 embedding | `google/gemini-embedding-2` | `/embeddings` 的图像输入；$0.45/M 图像 tokens，约 $0.00012/张 |

OpenRouter base URL 为 `https://openrouter.ai/api/v1`；上述路径相对于此地址。API key 从环境/secret 注入，不放配置明文或日志。Qwen 0.6B 文本 embedding 和 ImageBind 当前未列在可用目录；需要替换调用与重建索引。Qwen3-Embedding-8B 可显式作为 $0.01/M 的文本备选，但切换时必须重建，不在请求失败时自动换模型。

价格与目录依据：[VLM](https://openrouter.ai/qwen/qwen3.7-flash)、[阶梯价格](https://openrouter.ai/api/v1/models/qwen/qwen3.7-flash/endpoints)、[ASR 目录](https://openrouter.ai/api/v1/models?output_modalities=transcription)、[Perplexity](https://openrouter.ai/perplexity/pplx-embed-v1-0.6b)、[Gemini](https://openrouter.ai/google/gemini-embedding-2)、[Google 每图折算](https://ai.google.dev/gemini-api/docs/pricing?hl=en)。详见 [完整调研与算式](docs/research/video-rag-deployment.md)。

### 5.2 一个 3 分钟视频的预算

估算条件：180 秒音频、90 张 VLM 帧；每图暂按 1000 输入 tokens，提示词/转录另加 3000，输出合计 1800（含被计费推理）；文本 embedding 5000 tokens。VLM 分成 6 组，单次输入保持低于 32K；实际 token 化尚未测量。

| 环节 | 推荐 12 张代表帧 embedding | 全部 90 张 embedding |
| --- | ---: | ---: |
| VLM 动作与画面文字 | $0.003024 | $0.003024 |
| ASR | $0.0005994 | $0.0005994 |
| 文本 embedding | $0.000020 | $0.000020 |
| 图像 embedding | $0.001440 | $0.010800 |
| 模型合计 | **约 $0.0051/视频** | **约 $0.0144/视频** |
| 建议预留 | **$0.01/视频** | **$0.02–$0.03/视频** |

仅为一次入库预算，不含 CCR LLM、全库迁移、后续查询和重分析。图像按实际 tokens/供应商结算，不能把每图折算当固定保证。Qwen3.7 Flash 单次输入达到 32K 后为 $0.10/M 输入、$0.40/M 输出，达到 256K 后为 $0.20/M、$0.80/M；先拆分请求，保留实际 usage 与价格版本。代表帧减少会降低视觉检索细粒度，需在真实基准中评估。

### 5.3 Linux 开发、测试与部署环境

后续在 Linux 服务器开发和运行，目标机器为 32GB RAM / RTX 4060 Laptop；服务器承担媒体处理、存储与云 API 客户端工作。新方案不加载本地 OCR、VLM、ASR 或 embedding 权重。保留 FFmpeg、可选 PySceneDetect/图像处理、异步 HTTP client 的依赖 lock。portable core 无 GPU 可安装；新增接口通过可选 provider 配置启用，不自动下载模型。

Phase 1 前在 Linux 同步代码、两份计划和调研记录，建立项目独立开发环境、开发用 PostgreSQL/pgvector 与测试 collection，记录系统/依赖/目录版本。开发数据库和素材目录与已有数据分开，shadow 索引和迁移先在测试环境验证。文件中的相对路径均相对于 Linux 仓库根目录，当前 `/Users/...` 工作区路径不作为服务器配置。

CCR 与 OpenRouter 在 Linux 按实际可达地址配置。示例中的 `127.0.0.1` 表示执行进程所在的 Linux 主机；只有 CCR 在同一主机提供服务时才可照此使用。计划中的价格是 2026-10-01 的研究基线，开始 API 联调前重新核对模型可用性与单价，保留新价格版本。

就绪检查只读配置及模型目录；真实推理 smoke 另有显式入口与费用记录。OpenRouter 各角色按支持的协议适配：ASR 不套用聊天路由控制，embedding 不套用 Ollama 的 Qwen query prefix，视觉索引问题使用同一个 Gemini encoder/维度。未确认的整段视频 embedding 路径不进入 MVP。

## 6. 内部数据和 provider 接口

~~~python
TranscriptWord(start_seconds, end_seconds, text, probability=None)
TranscriptSegment(segment_id, start_seconds, end_seconds, text, words=None)
TranscriptResult(markdown, segments, language, timing_precision, provider_revision)
ContentClassification(category, decision, evidence_segment_ids, reason)
TranscriptAnalysis(classification, base_enrichment, analyzed_segment_ids)
TranscriptAnalyzer.analyze(transcript, source_metadata) -> TranscriptAnalysis
ASRProvider.transcribe(audio_path, offset_seconds=0) -> TranscriptResult

VisualCue(segment_ids, start_seconds, end_seconds, reason, context)
Scene(scene_id, start_seconds, end_seconds)
CandidateFrame(frame_id, scene_id, target_time_seconds,
               observed_at_seconds, image_path, timestamp_precision)
FrameDescription(frame_id, description, visible_text, readability, conflicts)
VisionProvider.describe_frames(frames, context=None) -> list[FrameDescription]
VisualDescription(scene_id, start_seconds, end_seconds, observed_at_seconds,
                  description, visible_text, evidence_id, verification_status)

EmbeddingProvider.embed_documents(texts) -> vectors
EmbeddingProvider.embed_query(text) -> vector
VisualEmbeddingProvider.embed_frames(frames) -> vectors
VisualEmbeddingProvider.embed_query(text) -> vector
ModelUsage(role, request_id, model, provider, input_tokens, output_tokens,
           audio_seconds, image_count, cost_usd, estimated_cost_usd, status)
~~~

这是方法/字段草图，Pydantic 固定 schema；provider 方法在 ingestion 异步边界使用异步 HTTP，核心既有同步接口通过合适 adapter 保持兼容。新增 Fake/OpenRouter ASR、Vision、TextEmbedding、VisualEmbedding providers。所有时间由字幕/ASR 实际响应或 CandidateFrame 绑定，禁止模型虚构时间；批量结果必须一一对应合法 frame IDs，缺项/重复/越界只影响相应帧。

VLM `visible_text` 保存原文；不要求它提供本地 OCR 的字框与校准置信度。manifest 保存 schema、来源 hash、transcript/配置指纹、scenes/cues、逐帧结果、evidence、coverage、usage ledger、provider/model/prompt 与 embedding space revision。证据时刻与场景上下文范围分开。外部 metadata 不暴露缓存绝对路径。

## 7. 配置、CCR、OpenRouter 与缓存

### 7.1 配置示例

以下是**待实现**的配置草图，不代表当前代码已经识别这些字段。视觉默认关闭；文字 LLM、云模型和 embedding 分开配置：

~~~dotenv
# 示例地址：仅适用于 CCR 与开发进程运行在同一 Linux 主机的情况。
# 服务器开工时按实际服务地址/端口填写，不沿用当前 macOS 的环境值。
OPENAI_API_STYLE=responses
OPENAI_BASE_URL=http://127.0.0.1:3456/v1
OPENAI_MODEL=Codex API/gpt-6-luna
OPENAI_API_KEY=<CCR client API key>

OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_API_KEY=<OpenRouter API key>
VIDEO_ASR_BACKEND=openrouter
VIDEO_ASR_MODEL=qwen/qwen3-asr-0.6b
VIDEO_ASR_COARSE_CHUNK_SECONDS=30
VIDEO_VISUAL_ENABLED=true
VIDEO_VISUAL_ALLOWED_CATEGORIES=cooking
VIDEO_VISUAL_DESCRIPTION_BACKEND=openrouter
VIDEO_VISUAL_MODEL=qwen/qwen3.7-flash
VIDEO_VISUAL_CUE_PAD_SECONDS=5
VIDEO_VISUAL_SAMPLE_SECONDS=1
VIDEO_VISUAL_DENSE_SAMPLE_SECONDS=0.5
VIDEO_VISUAL_COVERAGE_SAMPLE_SECONDS=2
VIDEO_VISUAL_BATCH_MAX_FRAMES=15
VIDEO_VISUAL_BATCH_TARGET_INPUT_TOKENS=24000
VIDEO_VISUAL_BATCH_MAX_OUTPUT_TOKENS=512
VIDEO_VISUAL_MAX_CANDIDATES=1000
VIDEO_VISUAL_MAX_EVIDENCE_FRAMES=100
VIDEO_VISUAL_MAX_VLM_IMAGES=100
VIDEO_VISUAL_MAX_VLM_REQUESTS=100
VIDEO_VISUAL_TIMEOUT_SECONDS=600
VIDEO_VISUAL_BACKFILL_ENABLED=true
VIDEO_VISUAL_SEMANTIC_CUES_ENABLED=false
VIDEO_VISUAL_EMBEDDING_ENABLED=true
VIDEO_VISUAL_EMBEDDING_MODEL=google/gemini-embedding-2
VIDEO_VISUAL_EMBEDDING_DIMENSIONS=768
VIDEO_VISUAL_EMBEDDING_TARGET_FRAMES=12
VIDEO_VISUAL_EMBEDDING_MAX_IMAGES=24
VIDEO_OPENROUTER_TARGET_USD=0.01
VIDEO_OPENROUTER_STOP_USD=0.03
VIDEO_OPENROUTER_CONCURRENCY=1
~~~

核心 app config 的待实现 embedding 配置：

~~~toml
[embedding]
backend = "openrouter"
url = "https://openrouter.ai/api/v1/embeddings"
api_key_env = "OPENROUTER_API_KEY"
model = "perplexity/pplx-embed-v1-0.6b"
dimensions = 1024
batch_size = 4
timeout_seconds = 120
~~~

视觉向量使用独立 768 维空间，实际输出必须校验；不能与 1024 维文本索引互查。新云部署不配置本地 OCR/视觉备选，CCR 仅收文字。关闭 `VIDEO_VISUAL_ENABLED` 停止新图片处理；关闭 `VIDEO_VISUAL_EMBEDDING_ENABLED` 仅停视觉向量，保留 VLM 文字提取与文本检索。已存证据取图不重新付费分析。

status 分开报告 CCR 文本、OpenRouter ASR/VLM/两类 embedding 就绪、索引代际、实际调用与费用、partial/unknown 和回填状态。凭据脱敏。缺 key/不支持模型不自动下载本地模型、不静默换向量空间；旧配置可启动，迁移前旧索引与新 OpenRouter 配置通过显式 active generation 区分。

CCR 继续首选 Responses 与本地 Pydantic 校验；服务商格式参数不兼容时可在同模型下使用 JSON-only prompt，不能静默换服务商。3456 仅为配置示例，Linux 开发环境须记录真实地址并联调。OpenRouter VLM 使用 Chat Completions，ASR/embedding 使用各自 endpoint，不经过 CCR 转换器。按角色支持情况配置最低价供应商，禁止自动换成高价模型；ASR 不假设支持聊天接口的 `sort/order` 路由参数。429/5xx 有界退避，重试仍计图像/请求/美元预算，响应丢失不当作零费用。

### 7.2 缓存与证据存放

缓存放 AppPaths.cache_dir/video-visual，不能放被 cleanup 的 job_dir。持久证据放所属 collection 的 Assets/video/<source-document-id>/<analysis-revision>/，含 manifest.json 和截图；Markdown 使用相对引用。

分层缓存：

- 分类：transcript checksum + 文本 classifier model/prompt/schema 版本 + 类别准入配置；过期/不同 transcript 的判定不能复用。
- 场景：video SHA256 + detector/decoder 版本 + scene 配置，不包含 transcript 或 VLM prompt，避免改台词后重跑镜头检测。
- cue/选帧：scene revision + transcript timing/cue 指纹 + sampling 配置；单张抽帧另以 video hash + 实际时间/PTS + extraction 版本复用。
- ASR：音频 SHA256 + model/provider revision + 语言/分片/offset/时间能力配置。
- VLM：有序图片/裁剪 SHA256 + frame IDs + provider/model revision + context hash + prompt/schema；画面文字和动作同层复用。
- 文本 embedding：规范化输入 hash + encoder/model/revision + dimensions + query/document 处理策略 + embedding space ID。
- 视觉 embedding：图片 hash + 同一 Gemini encoder/model/revision + dimensions + preprocessing + space ID；代表帧集合另记 sampling revision。
- 合并结果：上述 revision + transcript checksum + 分类准入、云模型路由配置指纹 + 合并版本。

所有视觉缓存的复用也先通过当前 cooking 准入，不能以缓存存在绕过分类。模型、prompt、上下文或采样改变只重算受影响层。源视频 hash 相同且缓存完整时不重跑场景/抽帧。原视频已删除时，不能仅凭 URL 断言内容 hash 一定没变；显式刷新需重新取源验证，正常同版本任务可以复用已有分析记录。

证据长期保存；临时原视频与非证据候选按现有 cleanup 清理。缓存 TTL 初始 30 天，临时 outbound 24 小时；有当前/备份文档引用的证据不因 TTL 删除。引用图片缺失则降级报告，不让取图失败破坏文本回答。沿用至少 5GB 可用磁盘检查，资源不足暂停回填，不删已引用证据腾空间。

## 8. Phase 1：视觉增强与真实检索证明

### 8.1 新录入路径

1. 字幕优先，无字幕使用 OpenRouter ASR 获取结构化 transcript，保留现有字符串 wrapper；没有供应商细时间时使用明确的音频分片粗区间。
2. 先用 CCR/GPT 的 TranscriptAnalyzer 分类并生成可复用基础摘要/标签。只有 cooking eligible 且视觉开关开启才进入下列画面步骤；其他类别、unknown、无视频轨保留常规文本路径，不启动视觉专用下载/检测/抽帧/VLM/视觉 embedding。
3. 对准入视频检查 OpenRouter VLM/视觉 embedding 配置与预算；URL 才准备受现有时长、文件大小、公共 URL/平台会话约束的视觉视频，附件复用现存 source。
4. 本地提取 cue、场景、清晰度/hash；OpenRouter VLM 分组读取动作/原文并做有界复核，写 usage/cache/manifest。选择代表帧进行 Gemini embedding；失败保留转录和可靠部分，不自动换本地或 CCR 图片模型。
5. 新 cooking 视频将清晰视觉事实加入文本 enrichment，继续优先 CCR/GPT；非准入视频直接复用第 2 步基础结果，避免重复文本调用。不确定数字不进入摘要。
6. builder 在 Transcript 前追加受程序管理的 Visual Timeline；保留完整转录。
7. 验证并持久化证据，使用 OpenRouter 文本 embedding 写入已准备的独立测试索引代际；沿用目标选择、Markdown 保存和现有通知。代表帧向量先保存 artifact，生产代际切换与视觉向量正式入库在下一阶段完成。

Markdown 例子仅为合成验收样例：

~~~markdown
## Visual Timeline

### 00:01:23–00:01:34
来源：示例料理视频；步骤：加入料汁。
画面原文：生抽 2 勺，老抽 1 勺，醋 1 勺。
截图时刻：00:01:27.500
识别方式：OpenRouter VLM；状态：readable
证据：![料汁配方](Assets/video/example/revision/frame.jpg)

## Transcript
...
~~~

同一视觉条目包含标题/步骤、时间、原文和证据引用，控制篇幅以适应现有字符 chunk。这有助于检索，但 Phase 1 不宣称 chunk 时间字段已可靠。

frontmatter 的 video_classification 记录内容类别、decision、分类依据和版本；video_visual 子对象记录 status、reason、role/provider/model、usage 与失败/复核原因、analysis_revision、manifest 相对路径与 coverage；job.metadata 存同信息及预算。status 为 success/partial/failed/skipped，reason 区分 disabled/not_eligible_category/category_uncertain/classification_unavailable/no_provider/no_api_key/model_unavailable/audio_only/source_unavailable/timeout/budget/parse_error 等。实际没有视觉结果时不输出伪造时间线。

### 8.2 失败隔离

- CCR 文本分类失败时保留有效缓存判定或返回 unknown，先跳过画面阶段；不阻断已经成功的字幕/ASR。
- OpenRouter VLM/scene/download 失败不使已取得的转录丢失。模型请求有界重试；无图不发伪造图片，仍失败保存 transcript-only/partial。图像 embedding 失败仅停视觉索引，可靠文字继续文本索引；文本 embedding 失败保存归档并标 index_pending，不谎报可检索、不发送新空间查询给旧索引。
- 文本 enrichment API/JSON 错误同样返回确定性 fallback，保证 CCR 不可用时仍可归档。
- 显式暂停/取消与任务控制异常继续传播，不能被普通视觉异常捕获吞掉。
- 每个窗口、模型请求与长子进程之间检查控制；超时终止子进程，停止 scene manager。
- 新视频即使视觉失败仍继续 indexing；旧文档视觉失败则保持原文件/旧索引。
- provider 全部失败与部分成功分别记录；无 provider 不误标 failed。
- 光学文字缺失不等于“画面没有文字”；区分 unreadable、未采样与真的无内容。

### 8.3 自动回填与人工编辑合并

扫描所有配置的可写 collection，先读取有视频来源证据的旧文档 transcript 并执行/复用分类；只将 cooking eligible 排入视觉回填，不把普通笔记、其他类别或 unknown 排入画面任务。collection 名称只用于来源发现/保存，不代替内容分类。先读现有 job/document 登记，再兼容 frontmatter 来源。通过分类后旧 URL 才重取；无源附件记录 source_unavailable，等待未来重新提供，不能无限重试。

启用视觉后，单 worker 每个空闲轮次最多排入一个通过 cooking 准入的回填任务，不设每日限额。开始画面操作前再次校验允许类别/当前 transcript 的分类，配置变更不继续执行已不符合条件的排队任务。正常新任务与作者批次优先；当前旧视频完成后再让路。保留既有平台限速、认证恢复和 5GB 磁盘门槛。回填使用现有 input_kind、JobState 与 metadata.work_kind=visual_backfill，不新增 worker 或复制整个采集状态机。

同 (source document, destination, desired visual revision) 的回填 enqueue 通过数据库事务/锁幂等。metadata 链接原 job、目标路径、预期 hash 和各 checkpoint；控制记录可暂停/恢复整批回填。补充维护 CLI visual-backfill status/pause/resume/retry，并提供 Telegram /video_backfill 对应控制。回填不逐视频刷通知，完成、暂停资源或需要处理的冲突才汇总。

合并策略：

1. 读取并保存当前全文 hash；固定原 document ID、文件路径和标题归档路径。
2. 只维护带 rag-favorite:visual:start/end 标记且有生成块 hash 的 Visual Timeline，以及程序拥有的 video_visual 元数据；正文、人工摘要、备注保留。
3. 原文件没有该块时，在唯一 Transcript 标题前插入；无明确位置则追加。已有不受程序管理的同名视觉区块、无效 YAML 或人工改过生成块时，写候选 sidecar 并记录 merge_conflict，不覆盖人工内容。
4. 合并后先验证 Markdown/manifest/图片；再准备 embedding 和文件更新。替换前再次比较当前 hash，防止回填期间的新编辑被覆盖。
5. 以私有旧文件备份、staging、job checkpoint 和数据库事务完成可恢复更新；文件系统与数据库不能假定天然原子。断电恢复按旧/新 hash 与索引 revision 完成或回退，不覆盖未知第三种文件内容。
6. 保留 transcript_checksum；更新全文 checksum 与对应索引代际的文本向量；代表帧向量单独版本化。关联文档原 job_id 保留来源关系，latest visual 状态明确更新到原记录 metadata/当前 manifest，另记录 backfill_job_id，不能假设现有 upsert 会改关联。
7. 目标路径不变，沿用核心按 source_path 更新文档；不生成第二份菜谱。跨目标复用分析时，各 collection 拥有自己的相对证据副本。
8. 已完成同 revision 不再回填；失败重试至多沿用现有 2 次上限，保持累计预算，之后列入报告，除非显式 retry/配置 revision 改变。

原文件存在时不再走“丢弃 staging”的普通重复处理分支。正常重复视频应复用当前视觉 revision；旧 revision 缺失时安排更新，不让去重永久挡住升级。

Phase 1 完成条件：新任务/回填/失败路径通过测试，真实屏显配方基准达标，普通文本与旧 ingestion 回归通过；再进入 Phase 2。

## 9. Phase 2：时间分块与数据库迁移

### 9.1 数据模型和分块

新增 ContentChunk(content, modality, start_seconds=None, end_seconds=None, metadata=None)，modality 初始 text/transcript/visual/multimodal。

视频识别优先使用可信 manifest/frontmatter 关联；普通 Markdown/文本继续原 chunk_text。有时间的旧转录解析为 TranscriptSegment，时间不完整的旧文档仍使用 text chunk，不伪造时间。时间分块本身不触发视觉分析；非 cooking 可仅利用已经取得的 transcript 时间，绝不为了分块去抽帧或调用图片模型。

时间分块采用 scene/cue 边界及 transcript 语义段落，目标约 20–45 秒，硬上限 60 秒、1800 字符；短相邻区间可合并，多个字卡状态仍带各自 evidence 时刻。长段超过字符限额时在真实 transcript/word 边界拆分；没有精细时间时保留原段粗范围。旧段超过 60 秒且无可靠子边界时退回 text chunks，时间列留空，metadata 保存粗上下文范围与 fallback 原因，不按文字长度编造精确时刻。

chunk 合并：标题/步骤上下文 + 原时间段 transcript + 此区间的视觉事实。visual 事实按 observed_at 归属区间，scene 起止只作上下文。只有视觉或只有 transcript 时使用相应 modality；摘要/来源说明等无时间文本仍是 text chunk。任意分割保持证据与原文绑定，不让时间标题落在另一个 chunk。

保存浮点秒数；显示统一 HH:MM:SS，证据时刻可带毫秒。只有真正取得细时间时标 precise，否则 segment/approximate。无时间字段始终可空。

### 9.2 迁移与索引版本

新增不可变迁移 0002_video_temporal_chunks.sql，同步 migrations/core 与 package resources：

~~~sql
ALTER TABLE rag_chunks
  ADD COLUMN modality text NOT NULL DEFAULT 'text',
  ADD COLUMN start_seconds double precision,
  ADD COLUMN end_seconds double precision,
  ADD COLUMN metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
~~~

验证合法 modality、有限非负时间和 end >= start；时间允许全空，不能半空。不存图片二进制、不重写 0001。时间迁移不就地改变旧文本 vector 列维度；OpenRouter 新文本索引在独立代际构建，视觉索引在独立空间建表。

MigrationRunner 只对初始建表迁移替换 vector 维度；ALTER-only 迁移原样执行，仍保留 checksum、事务和 advisory lock。统一 initialize_schema 与迁移入口，确保 CLI init/setup 和已有库都获得新增列。

普通文档仍用 chunking_version=1；视频 temporal chunking_version=2、media index version=4。current 判断同时检查文档类型、checksum 和 embedding space/generation。仅时间分块升级时只重建视频；本次从 Ollama 切换到 OpenRouter 文本 encoder 时，全部可检索文本（包括普通文档）须一次性重嵌入。两种重建原因独立记账，普通文档仍不跑视觉。

复用 ingest_file 的文档 upsert 与同事务 chunks 替换，内部提取“准备 chunks/embeddings”和“写索引”职责，供安全回填复用。整个文档的 chunk metadata 更新一起提交，不能暴露一半新时间块。

### 9.3 文本空间切换与代表帧视觉索引

核心增加 EmbeddingProvider factory，覆盖 ingest、query、smoke/status 和所有 legacy adapter，移除主路径直接创建 Ollama client 的耦合。不把 Ollama 专用 query instruction 无条件加给 Perplexity；document/query 预处理遵循模型规范并进入 space ID。

在**同一 PostgreSQL** 建立 OpenRouter 文本检索的 shadow schema/索引代际，迁移序列沿用项目 runner，保留稳定 document IDs/source paths 与原索引。新模型即使也输出 1024 维，仍必须全量重建；全量完成、增量差异追平并核对后，短暂阻止索引写入并原子切换 active generation 与查询 encoder。任一 collection 未完整重建不得混查；准备阶段用独立测试 collection/代际做 Phase 1 基准。每代记录模型、provider revision、维度、预处理、文档 hash 清单与费用。回退切换旧索引与旧 encoder 必须成对，明确旧代际的 freshness，不能静默把云查询向量发给本地旧索引。

新增不可变迁移 `0003_video_visual_embeddings.sql`，同步 package resources；包含视觉向量表与 active generation 登记结构。每条视觉向量绑定 collection/document/evidence ID、analysis revision、observed_at、encoder space、dimension（初始 768）和图片 hash。Gemini 同一 encoder 编码检索问题与代表帧，不使用 Perplexity 文本向量查询该表。批量 embedding 每张图产生独立向量，不能把多图聚合成一个向量后假装逐帧结果。

按 Development MVP.md 的五阶段交付编号：Phase 2 生成代表帧向量 artifact；Phase 3 写入独立 pgvector 表、实现视觉召回与 RRF；Phase 4 将结果用于 MCP/Telegram。文字召回和视觉召回只在各自空间比较，之后以 RRF/rank fusion 合并到统一文档/时间/证据；保留已有 score 字段语义，不直接加两个空间的 cosine。视觉服务不可用时降级到同一 active generation 的文本召回，标明未执行视觉召回；纯文本 collection 不调用 Gemini。视觉召回可独立关闭，取图不触发推理。

这是稀疏帧级图像检索，不声称具备 ImageBind 的原生片段时序语义；原生视频 embedding 留在后续范围。

## 10. Phase 3：时间检索与截图交付

### 10.1 保持 rag_search 主契约

保留现有参数、collection 选择、excerpt 和各 score 字段。视频结果把 modality/start_seconds/end_seconds/source_url/evidence_ids/timing_precision/visual_status 放 metadata；section 可显示时间范围。文字结果继续 metadata={} 或既有兼容值，不要求时间字段。

示例仅表示新字段位置：

~~~json
{
  "document_id": "document:123",
  "title": "示例料理视频",
  "knowledge_base": "cooking",
  "section": "00:01:23–00:01:34",
  "excerpt": "画面原文：生抽 2 勺，老抽 1 勺，醋 1 勺。",
  "metadata": {
    "modality": "multimodal",
    "start_seconds": 83.0,
    "end_seconds": 94.0,
    "evidence_ids": ["frame:example"],
    "timing_precision": "segment",
    "visual_status": "success"
  }
}
~~~

明确验证 metadata 字段，不能直接透传内部缓存路径或签名 URL。legacy retrieval adapter 保留兼容透传；旧 isolated cooking 服务不新增第二套 temporal schema。

### 10.2 新增证据读取工具

新增 rag_evidence_get(document_id, knowledge_base, evidence_ids)，最多 2 个 evidence ID。查配置 collection + document + 当前有效 manifest 的关联，只接受已登记 ID，不接受用户输入图片路径。

返回来源、observed_at、文字/识别状态、相对引用、missing_evidence_ids、image_delivery_failures。Telegram 交付沿用项目已有的 managed outbound + 原样 MEDIA 指令方式：

- 将安全图片 staging/helper 提取到 packaged src 模块，避免 import 未打包的 cooking MCP server。
- 按 manifest 的确定相对路径解析，resolve 后再次验证 collection 边界；拒绝越界 symlink 和 basename 全盘搜索。
- outbound 目录/文件沿用 0700/0600，按内容 hash 命名、原子写入。
- app config 新增可选 evidence 配置：delivery_mode=references/openclaw，outbound_dir 默认 ~/.openclaw/media/outbound/rag-favorite，max_images=2，ttl_hours=24。普通核心默认 references；当前 Telegram 配置启用 openclaw。
- MEDIA 指令是内部交付能力，不向人展示主机绝对路径；OpenClaw 按当前工具返回原样消费，不凭空拼路径。
- 取图不修改知识库；openclaw 模式会写临时交付缓存，在工具描述中如实说明。
- 默认工具数由 2 增至 3，同步 MCP_TOOL_NAMES、doctor、stdio smoke、OpenClaw toolFilter、安装/探测测试和说明。
- 更新 private-rag/cooking 使用说明：先 search，再只用当前结果 ID 取相关图片，最多两张；答案引用出处与时间，缺图时仍可引用文字。

数量 uncertain/conflict 时，回答明确看不清或原文矛盾，不能把残缺用量拼成确定配方。同时提供“上下文范围”与“截图时刻”，避免误解。来源链接保留普通链接，本次不把各平台时间跳转链接作为新增必需能力。

## 11. 验证、实施顺序与交付门槛

### 11.1 后续验证规格

本次只改计划，不新增或运行测试。后续在 Linux 开发环境使用 fake/stub HTTP 与固化响应验证：

- 字幕/ASR 毫秒、分片 offset、供应商无词级时间、粗旧转录、重复指令、VFR/非零 PTS。
- cooking 准入、other/unknown/纯音频视觉零调用、长 transcript 分类覆盖。
- cue/scene/覆盖采样、数字 2→3 不被相似去重、短显文字、动作与 frame ID 绑定。
- OpenRouter VLM 批量响应、缺失/重复 ID、JSON、429/5xx、一次复核、没有本地模型或 CCR 图片调用。
- 请求/图像/向量/美元/时间分别计数，32K 拆分、usage 缺失、响应丢失、崩溃恢复不清零、取消传播。
- ASR/VLM/embedding cache revision；代表帧向量一图一向量；跨空间查询拒绝。
- 回填人工保护、file/DB journal、缺源附件、保留有效截图。
- 时间迁移与新空间 shadow 重建、全量文本一次重嵌入、增量追平、atomic cutover/rollback、不混用同维向量。
- 独立视觉索引、RRF、不直接相加 cosine、视觉不可用文本降级、MCP 主契约/两图/路径边界。

### 11.2 真实素材、费用与服务器基准

至少 10 cooking 视频、60 标注图与 non-cooking 负例；参数化 corpus 目录。对照 A：transcript-only；B：OpenRouter VLM 画面文字/动作 + 文本检索；C：B + 12 代表帧视觉索引；D：B + 90 帧视觉索引（单独提高上限/预算）。比较 Top 3、数字/单位、漏检/unknown、动作证据、截图归属、coverage、ASR 时间精度、分阶段 p50/p95、实际 usage/cost、重试和缓存。

VLM 请求带价格版本和输入/输出计费；报告每个 3 分钟任务与整个 corpus 的 OpenRouter 成本，CCR LLM 单列，不把未计费项写零。$0.0051 是推荐方案估算，不是质量或实测账单保证。

Phase 1 关口保留：10 个视频屏显问题 Top 3 至少 9/10，明确数量/单位逐项正确、基准错误确认 0，unknown/漏检另报。Phase 2 关口增加：新旧库、全量新空间重建、时间归属与视觉索引兼容。Phase 3 关口：真实 Telegram 文字、出处、时间和两图，以及视觉关闭/缺图/冲突降级。

### 11.3 实施顺序

开工准备：先在 Linux 同步计划与代码，确认版本、独立依赖环境、开发数据库/测试 collection、媒体目录和 API 配置，并在 Linux 记录原测试基线。具体清单见 Development MVP.md 第 0.1 节。

1. 结构化 transcript、OpenRouter ASR、CCR 分类、gate、统一 embedding factory/文本 client；先在独立测试索引验证，不混写旧空间。
2. 本地 FFmpeg/scene/清晰度/hash、OpenRouter VLM 批量动作/原文、usage ledger、预算与缓存。
3. 代表帧 Gemini embedding artifact、Markdown/manifest/截图、自动回填与人工保护；在 OpenRouter 文本测试代际完成原 Phase 1 基准。
4. 时间 migration、视觉索引 migration、全库文本 shadow 重建/追平/切换、视觉召回与 RRF。
5. MCP metadata/证据工具/Telegram 交付，真实素材、API 费用与 Linux 连续任务验收。

具体五阶段待办与稳定任务/测试 ID 见 [Development MVP.md](<Development MVP.md>)。各 phase 仍为 planned，业务代码/数据库/API 实测均未执行。上线默认关闭视觉，先小样本再启用回填；回退保存 Markdown、截图及旧索引代际，查询 encoder 必须随索引一起切换。

## 12. 最新工程假设与待提供信息

1. 用户已选择 CCR 文字 LLM + OpenRouter 其他模型；本地 OCR/VLM/Whisper/GPU 串行及 CCR 图片备选方案由本次修订替代。
2. 初始模型为 Qwen3.7 Flash、Qwen3 ASR 0.6B、Perplexity Embed V1 0.6B、Gemini Embedding 2；这些是成本优先候选，中文数字、动作与检索质量待实测。
3. 3 分钟约 90 帧分析、12 帧视觉向量；默认发送图像 100、向量图像 24、候选 1000、视觉累计 600 秒、美元停止阈值 $0.03。上限和质量需求发生冲突时保存 partial，不自动扩大消费。
4. cue ±5 秒、初始 1 秒/必要时 0.5 秒；无 cue 覆盖约 2 秒，长视频按预算降低密度并记录覆盖。
5. 不使用模型自报 confidence 当概率；数字不清一次复核，仍不清楚 unknown/conflict，不补猜。
6. 字幕优先，ASR 实际无细时间时采用粗音频分片，不声称 OpenRouter 每供应商都提供 word timestamps。
7. 文本与视觉 encoder 空间分开；文本换模型须全部重嵌入，即使同为 1024 维；视觉默认 768 维按实际响应校验。
8. 新索引先 shadow 构建并保留旧数据，切换/回退同时切 encoder；文字与视觉召回用 rank fusion，不直接相加向量相似度。
9. 缓存 30 天、outbound 24 小时，已引用证据长期保留；5GB 磁盘门槛、人工保护、单 worker 回填/新任务优先均沿用。
10. Linux 访问、开发与服务用户、仓库/数据路径、CCR 地址、OpenRouter key/供应商可用性、corpus/旧附件、现有全库规模在开工准备时确认；发布阶段复核。当前计划阶段不索取或读取 secret。
11. 全库迁移、后续查询、CCR LLM 与充值/存储费用不计入单个视频入库预算，分别记录。当前没有实际质量、速度或费用验收结果。

后续可选：原生视频片段 VLM/embedding、其他类别 profile、更强 VLM 对照、平台时间跳转；不进入本次默认 MVP。
