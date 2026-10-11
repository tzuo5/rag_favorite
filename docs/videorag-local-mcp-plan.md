# VideoRAG 本地 Embedding 与 Codex MCP 修改计划

更新：2026-10-04，America/Chicago。当前项目：`/home/tzuo5/gpt projects/rag_favorite`；配套远程仓库：[tzuo5/rag_favorite](https://github.com/tzuo5/rag_favorite)。

**结论：方案可行，改动属于中等到较大的后端改造。** 保留现有文档库、PostgreSQL/pgvector、任务队列与采集能力，采用 HKUDS/VideoRAG 的视频知识组织和多路检索，以 Codex 为主要 MCP 调用端；同机 stdio 优先，跨机器远程 MCP 为扩展；取消 Telegram Bot。文本 embedding、视频 embedding 均在这台 Linux 机器运行；大模型供应商可以配置，不要求使用 OpenAI 或 OpenClaw。

**原视频只用于 import。成功完成后删除服务器上的原视频副本、音轨和临时切片，知识库保存转录、知识点、向量、关系、出处、时间与必要证据截图。后续查询不读取或重新下载原视频。** 这要求改造 VideoRAG 原版的查询细化步骤，交付的是适配本项目的 VideoRAG 后端，不能将原版查询逻辑原封不动接上。

本文是新目标的主计划，取代 [devplan.md](../devplan.md) 和 [Development MVP.md](<../Development MVP.md>) 中冲突的技术选择。旧文档保留设计历史。新阶段统一使用 V0–V6；**V1 历史 verified（隔离文本 MVP），V0、V1A、V2–V4、V6 planned，V5 cancelled**。生产库尚未切换。本轮 LM Studio/隔离数据库未监听，项目目录移动后的运行路径需要先修复；历史验收不等于当前在线。结果见 [新 Phase 1 实施记录](implementation/videorag-phase-1.md)；初始事实见 [接入核查记录](research/videorag-integration-audit.md)，此前云模型基础工作见 [旧 Linux Phase 1](implementation/linux-phase1.md)。

**分阶段开发入口：** [Dev Doc](videorag-dev.md) 保留 Phase 0–6 编号，新增 Phase 1A Codex 文本接入，取消 Phase 5，包含每阶段的演示、前置条件、验收和回退。Phase 0–6 与本文 V0–V6 一一对应；架构/范围见本文，开发任务与验收进度在 Dev Doc 和阶段实施记录中维护。

**后续范围更新：** 已取消强制类别、cooking 视觉 gate 与累计处理时间预算。当前所有主题采用无语音 0.5 秒、有转录/字幕 1 秒抽帧；超过 600 秒完全跳过视觉，600 秒本身处理。密集抽帧、分批检查点和 Markdown 输出已实现并完成真实模型合成链路验证，见 [最新修改记录](implementation/dense-sampling-update.md)。本计划中的初始环境和阶段状态属于历史快照，当前状态以 Dev Doc 为准。

## 1. 已确认的范围与继承要求

| 项目 | 本次确定的行为 | 与此前要求的关系 |
| --- | --- | --- |
| 主要入口 | Codex 提交受控视频资产/链接、查询任务、检索知识与读取证据 | 取消 Telegram；同机 stdio 优先 |
| Agent 调用 | 首先验收实际 Codex；跨机器需要时增加受保护远程 MCP | 保留旧两工具 stdio，新视频工具放独立扩展 profile |
| 视频后端 | 接入 HKUDS/VideoRAG 的分段知识、视频向量和图关联检索 | 替代此前“不整体接入 VideoRAG”的选择 |
| 文本 embedding | 本地模型，独立文本向量空间 | 替代此前默认 OpenRouter 文本 embedding |
| 视频 embedding | 本地视频编码器及与之匹配的文本查询编码器 | 替代此前云端代表帧图像 embedding；两者不等同 |
| 大模型 | 分类、抽取、摘要和回答的供应商可配置；CCR 可作为适配选项 | 不把 OpenAI 模型或 OpenClaw 作为启动前提 |
| VLM / ASR | 字幕优先；需要时调用可配置的 VLM / ASR | 本次没有要求全部模型本地化；现有云适配可以复用 |
| 视觉准入 | 默认关闭；所有者启用后所有主题适用，无语音 0.5 秒、有转录/字幕 1 秒抽帧，超过 600 秒跳过全部视觉 | 覆盖旧 cooking gate；纯音频与无可用视频媒体仍不能做视频视觉分析 |
| 数字与单位 | 看不清时一次高清/裁剪复核，仍不清楚则标记未知 | 继续有效，不以模型常识补猜配方 |
| 原视频保留 | import 成功后清理，不进入长期知识库或新备份 | 本次再次明确；查询按持久化知识回答 |
| 证据 | 保存出处、时间和必要截图；Codex 回答最多附两张相关截图 | 继续有效，截图属于 RAG 证据，不保存可播放的视频片段 |
| 普通资料 | 现有 Markdown、collections 和文本 RAG 继续使用 | 无需全部改成图谱或重做前端 |
| 旧视频回填 | 空闲单 worker 执行，新任务在当前视频完成后优先；不设每日数量上限 | 保留旧摘要和人工编辑；冲突写 sidecar |

用户已明确取消类别限制；默认在统一逻辑知识库检索，不再依据 cooking 判定决定视觉处理。

## 2. 目前已有的能力与缺口

初始核查时，本地与远程 HEAD 均为 `ab03f923b26447ea9dd87bf455c33d28b7d576fb`，工作区有未提交修改；机器为 RTX 4060 Laptop、8188 MiB 显存、约 30 GiB RAM。VideoRAG 仍只有调研，没有运行集成。[初始核查记录](research/videorag-integration-audit.md)

随后按用户要求完成 Phase 1：在用户目录准备 PostgreSQL 18.6/pgvector 0.8.1、Ollama 0.32.0 和固定 digest 的 Qwen3-Embedding-0.6B，真实验证本地文本检索与索引代际。当时 FFmpeg、视频模型仍未补齐；没有部署视频链路。Telegram 凭据现已从前置条件移除。[实施记录](implementation/videorag-phase-1.md)

本地 ImageBind 的源码、权重大小、设备和采样策略已重新核查，尚未安装/实测，见 [本地配置文档](research/imagebind-local-setup.md)。下一步先恢复文本运行并接入 Codex，再补齐视频 PoC。

最新 provider 要求已实施：本地文本服务改为 **LM Studio / llmster**，保留 Qwen 权重并重建独立索引代际。默认配置与真实验收见 [LM Studio 记录](implementation/lmstudio-provider.md)。

| 当前落点 | 可以复用 | 必须补齐 |
| --- | --- | --- |
| src/rag_favorite/rag.py | 文档入库、collection 过滤、pgvector 检索 | 时间与证据元数据、多路视频检索、问答编排 |
| src/rag_favorite/embedding.py、indexes.py | 本地编码、完整文本空间、query/document 预处理、代际切换/回退 | 视频编码器、后续视频/图组合版本 |
| src/rag_favorite/mcp_server.py | FastMCP、输入校验、rag_search / rag_status | Codex 注册/实际验收、扩展工具；跨机器时增加远程身份 |
| ingestion/backend/ingestion | 队列、下载/字幕、结构化 transcript、分类、回填、通知与清理 | 真正的视觉实现、VideoRAG 适配、可靠发布与删源检查点 |
| ingestion/openclaw-plugin | 已有来源、任务和 collection 语义 | 提取共享业务接口供 Codex MCP 使用 |
| ingestion/backend/ingestion/notifier.py | 既有兼容通知能力 | Codex 通过 ingestion_status 查任务，不要求 Bot 通知 |
| 数据库迁移 | 核心文档表与 ingestion 任务表 | 双向量空间、视频知识/图关系、非 Telegram 调用身份 |

现有离线测试通过记录不代表真实视觉质量已达标；当前视觉分析还有 provider_not_implemented 路径。新 Phase 1 的 verified 仅覆盖真实文本 MVP，不能用它代替后续视频质量验收。

## 3. 目标架构与轻量部署原则

~~~mermaid
flowchart TD
    C[Codex 对话与最终回答] --> M[本机 stdio / 独立扩展 MCP]
    A[其他机器 Agent 可选] --> H[受保护远程 MCP]
    H --> S[共享服务 身份 collection 任务 检索]
    M --> S
    S --> W[单 ingestion worker / VideoRAG 适配]
    S --> R[持久知识检索与证据]
    W --> Q[LM Studio Qwen 文本编码 CPU]
    R --> Q
    W --> I[ImageBind 视频与匹配 query 本地编码]
    R --> I
    W --> P[PostgreSQL pgvector 图关系]
    R --> P
    W --> K[Markdown 事实 时间 截图]
    R --> K
    W --> L[可配置分类 LLM VLM 必要 ASR]
    W --> D[持久化成功后清理托管媒体]
~~~

Codex 通过 MCP 使用共享知识库与单 worker，基于检索证据组织最终回答。服务端最终 QA 和 rag_ask 为可选功能；分类、视觉事实抽取及必要 ASR 仍须实际 provider。Codex 采用云端模型时，返回的证据会参与其云端回答；双 embedding 继续在本地完成。

部署优先采用现有 Python 项目与 systemd，保留 PostgreSQL/pgvector；不增加 Neo4j、新向量数据库、Electron 客户端或多 Agent 编排。图关系属于视频后端的必要知识结构，存到 PostgreSQL，由 NetworkX 在受控范围计算。图谱数量、加载内存和重建时间纳入实测。

核心应用继续使用项目 Python 3.12 环境。视频模型依赖先用独立 Python 3.11 环境验证，通过本地编码服务或受控 worker 接口连接，避免把上游旧版 torch/transformers 强塞进全部服务。具体版本由 V0 的兼容测试确定，不直接照抄上游依赖锁。

## 4. VideoRAG 的接入方式

固定上游版本 `c412a093a820ef7a0e0dda31076ed871136198b3`，优先接入 `VideoRAG-algorithm`。在独立适配模块中保留来源、补丁和版本，复用其分段知识构建、实体/关系抽取及文本—图谱—视频召回逻辑。对媒体流程、存储、模型调用和查询细化进行明确修改；不把一个普通图片向量接口包装后称为完整 VideoRAG。

| 上游行为 | 本项目的修改 |
| --- | --- |
| 直接使用原生 insert_video 编排下载后处理 | 拆出分段、描述、知识构建步骤，接入已有字幕优先、分类 gate、worker 和预算控制 |
| 默认本地 Whisper / MiniCPM | 接入已有 transcript 和可配置 VLM/ASR；不导入就自动加载无用模型 |
| 文件名作为视频身份 | 使用稳定 source ID 与内容 hash；同名附件不能覆盖 |
| 默认 30 秒逻辑片段 | 作为初始基线；时间使用真实小数秒/PTS，短尾段、VFR 和字幕跨段需正确对齐 |
| JSON / nano-vectordb / NetworkX 持久化 | 在原生隔离 PoC 后接入 PostgreSQL KV、向量、节点/边存储适配 |
| 存储内部 ._data、eval 时间字符串 | 改为明确接口和数值类型校验，不依赖可执行字符串 |
| 查询读取原视频做细化 caption | 移到 import 时做充分事实提取；查询只读已保存的知识和证据 |
| 检索和最终回答混在 videorag_query | 抽离 retrieve_context，产出结构化证据；回答单独调用模型 |
| 每次写入/查询重新加载 ImageBind 并直接 .cuda() | 模型实例按进程集中管理，指定设备，batch 从 1 开始 |

上游查询确实会重新打开原视频；其 only_need_context 字段也不能直接当作已实现的纯检索接口。以上不是简单配置开关能够完成的替换。[上游查询实现](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/_op.py)、[查询细化描述](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/_videoutil/caption.py)

## 5. 本地 Text Embedding 与 Video Embedding

### 5.1 两个独立空间

| 作用 | 初始模型候选 | 输入/输出 | 运行安排 |
| --- | --- | --- | --- |
| 文档、转录、知识点和实体文本检索 | Qwen3-Embedding-0.6B | 文档文本与带检索指令的 query，默认 1024 维 | LM Studio / llmster，优先 CPU；文本 MVP 已验收 |
| 视频片段向量 | ImageBind huge，作为 VideoRAG 基线 | import 时的临时视频片段，1024 维 | CPU 验证后测试 GPU，单实例、batch 1 起步 |
| 视频空间的 query 向量 | 同一版本 ImageBind 的文本编码器 | 视觉检索问题，1024 维 | 与视频编码使用相同权重和预处理空间 |

**同为 1024 维不意味着能互相比较。** Qwen 向量不能拿去查询 ImageBind 视频索引；ImageBind query 也不能用于普通文本索引。每个索引记录 model ID、权重 revision/digest、维度、归一化、预处理版本、query 指令版本与 generation。检索时核对完整空间身份，不能只比维数。

Qwen 查询与文档输入需分别处理；VideoRAG 原版通用 embedding_func 不能不加区分地给两类输入套同一前缀。LM Studio 空间绑定本地 GGUF SHA-256、provider 和预处理版本。[Qwen 模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)、[实施记录](implementation/lmstudio-provider.md)

ImageBind 文本侧主要面向英文，中文问题的视觉检索先由 Codex 或配置好的改写适配器生成一份简短英文视觉检索表述，保留原问题并严格保护数字、单位和专名；随后由本地 ImageBind 编码。改写是可审计的文本模型调用，embedding 本身仍在本地。改写失败时返回可用的文本证据和明确降级状态，不能捏造视觉结果。[ImageBind 模型卡](https://github.com/facebookresearch/ImageBind/blob/main/model_card.md)

### 5.2 8 GB 显存的运行约束

1. 先记录 CPU 推理的正确性、峰值 RAM 和单条耗时，再测试 GPU 峰值与批量；不提前承诺每分钟视频的处理速度。
2. 本地不默认常驻 VLM、ASR 或回答 LLM，先把显存留给视频 embedding；文本编码优先 CPU。若以后增加本地大模型，重新测量资源争用。
3. 保留默认 5 个时间 clip × 3 个 crop 的 15 视图采样，优先实测逐视图 microbatch，再评估 FP16；精度/采样改变纳入空间身份。GPU 上的视频写入与视频 query 编码通过同一队列/锁调度，不重复载入 ImageBind。查询优先级与入库分段检查点结合，避免一个长入库阻塞所有查询。
4. 超出显存时先缩小批量并评估 CPU 执行，记录降级；不能自动改用云 embedding 违反本地要求。若本地基线仍不可用，报告可测量的阻碍，再选择本地替代编码器并重建视频索引。
5. 模型只在初始化时下载固定版本；验收阶段阻断 embedding 对外请求，证明离线编码可工作。分类/抽取/回答的外部调用单独统计。

原版参考 24 GB GPU，并在写入和查询中硬编码 CUDA；这不是本机 8 GB 的验收证据。[上游 README](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/README.md)、[向量实现](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/_storage/vdb_nanovectordb.py)

## 6. Import 流程与成功后删源

### 6.1 处理顺序

1. **接收和建任务。** 记录服务端确认的调用身份、来源渠道、collection、幂等键和可用出处。Codex 提交的受控资产进入 job 临时目录；MCP 接收 URL 或受控上传 asset ID，不接收任意服务器文件路径。
2. **优先获取 transcript。** 有字幕直接复用，无字幕才执行 ASR；保留时间精度、原始文字、来源语言、校验和与稳定 segment ID。没有可靠时间时标记 unknown/coarse，不制造精确区间。
3. **按时长和转录确定模态。** 不进行强制主题分类。无语音视频每 0.5 秒抽帧，有转录/字幕每 1 秒抽帧；超过 600 秒不进行视觉解码、VLM 图片分析或视频 embedding，仅保留可用文字知识。
4. **取得视觉媒体并切分。** 时长符合策略时使用托管原视频，按约 30 秒生成逻辑区间，较长视频按 60 秒切片；短视频、尾段和跨区间字幕保持正确归属。必要的物理切片只存在于临时目录。
5. **在 import 期间提取足够的视觉事实。** 保留 VideoRAG 分段描述，密集采样全部计划帧并分批分析，覆盖屏显文字、动作、顺序和知识点。每批最多 8 帧，逐批保存检查点；Markdown 包含分批小结及逐帧记录。缺少帧描述时不得发布完整任务。
6. **处理不确定事实。** 关键数字看不清时做一次高清或局部裁剪复核；仍不明确保存 unknown 与原因。将事实和证据帧 ID、真实时间、transcript 区间、抽取版本绑定。字幕内容与画面冲突时保留冲突，不能静默合并。
7. **本地视频和文本编码。** 原视频仍存在时完成需要视频的 ImageBind 编码；对 transcript、caption、知识点和图实体执行本地文本编码。保存完整空间身份、向量校验结果和可重放的派生记录。
8. **构建视频知识和图关系。** 复用 VideoRAG 的实体/关系构建，在指定 collection 与 generation 内持久化；每条关系能回溯到支持它的片段和事实。生成/更新 Markdown，不覆盖人工正文。
9. **发布派生知识。** 暂存文件按 manifest 验证 hash、证据可读性与引用完整性，再进入持久目录；数据库提交引用与索引状态。使用可恢复的发布检查点处理文件系统与数据库不能共享事务的问题。
10. **清理所有临时媒体。** 校验没有运行中的媒体读取者后删除原视频副本、音轨、物理切片、抽帧候选、下载缓存和重试副本；复核托管目录无残留，记录 source_deleted_at，再发送 import 完成通知。

VideoRAG 的 30 秒区间用于视频知识组织；必要的事实区间和证据时间可以更细。3 分钟视频约有 6 个逻辑片段，但该数量不能推导处理时长，也不能替代短暂屏显文字的抽帧。[上游分段与采样参数](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/videorag.py)

### 6.2 保存和删除清单

| 内容 | import 成功后 | 用途 |
| --- | --- | --- |
| 下载/转发得到的原视频副本 | 删除 | 只用于处理期间解码 |
| 音轨、物理视频切片、下载缓存、未选中候选帧 | 删除 | 临时处理资料 |
| 完整 transcript 与时间信息 | 保存 | 原话证据与文本检索 |
| 分段 caption、知识点、动作/配方结构化事实 | 保存 | 删除原视频后的知识上下文 |
| 文本/视频向量、空间版本、索引代际 | 保存 | 多路召回与可重建索引 |
| 实体/关系与支持它的事实 ID | 保存 | 跨片段关联；不是无出处的结论 |
| 原链接、标题、作者、导入时间、hash | 保存 | 引用和去重；不缓存签名下载 URL 或 Bot token |
| 少量证据截图及必要裁剪 | 保存 | 屏显数字复核、最多两张相关截图回复 |
| Markdown 与历史人工编辑 | 保存 | 可读的知识档案 |

删除范围是应用拥有的下载/上传副本及临时缓存。后台清理保护用户其他目录中的文件、历史备份或上游网站内容；未来部署的长期备份排除 job 临时媒体。受控资产入口的上传缓存也纳入清理，完成状态要覆盖 worker 与上传入口的全部托管副本。

### 6.3 状态、失败与恢复

引入或映射可恢复的内部检查点：`media_processing → derived_persisted → index_ready → cleanup_pending → completed`。任务枚举、数据库 CHECK、presenter 与客户端状态同步修改，不能仅在代码中增加字符串。

- **completed**：配置要求的派生知识与索引均已发布，证据引用有效，原视频和临时媒体已清理。纯文本模式本身可以正常完成；结果标明 visual_status=skipped 及原因。
- **index_pending**：所有依赖视频的事实与向量产物已可靠保存，仅数据库索引写入待重试时，可以清理媒体并用持久派生记录重试；不报告为完整可检索完成。向量产物属于 RAG 数据，重试结束后清理重复 staging 产物。
- **媒体依赖步骤失败**：若视频编码或必要事实提取尚未成功，先保留短期受管临时副本供恢复，默认 TTL 72 小时、可配置；不能提前删掉又假装完整入库。TTL 到期清理并标记失败/缺失能力，保留已得到的 transcript，重做需用户重新提供视频。
- **cleanup_pending**：派生知识已可查询，但删除失败；通知“知识已入库，临时视频清理待重试”，不要发送已清理成功的完成提示。清理 worker 幂等重试，不重新做模型调用。
- 重启时先按检查点验证已发布 manifest；不得删除仍被运行任务读取的媒体，也不得把文件已发布但 DB 未提交的孤儿直接当作成功。

删除前的必要条件是“所需视频知识已经提取并持久化”，不要求原视频永久保留到每次查询。清理失败与处理失败应可观察，不能让临时目录无期限增长。

### 6.4 删除原视频带来的能力边界

向量是召回索引，不是可还原的视频。删源后可以查询已经提取的知识，不能保证回答 import 时未保存的视觉细节，也不能用旧向量无损恢复画面。对此返回“现有证据不足/未知”。

更换视频编码器权重或预处理通常需要原视频重新编码；原视频已删除时，旧视频保留旧空间或标为不可重建，需用户主动再次提供来源。系统不得在查询或回填时偷偷重新下载。必要截图可用于其已记录事实的核对，但不能冒充完整视频时序信息。

## 7. 删除原视频后的检索与问答

### 7.1 结构化检索接口

新增共享 `retrieve_context` 服务，返回 RetrievalBundle，至少包含：

- query 原文、视觉检索改写、明确请求的 collections、各空间 generation；
- document / video / segment / fact / evidence ID；
- transcript 摘录、保存的 caption、知识点与图关系的支持事实；
- source URL 或附件来源、标题/作者、时间区间、时间精度；
- 各召回通道排名、融合排名、可靠性与冲突/unknown 标记；
- 可读取的 evidence ID，及 source_media_state=deleted / unavailable 等状态。

普通资料沿用文本召回；视频资料结合文本、实体/关系和 ImageBind 视频召回。先按 collection 和 generation 过滤，再做扩展、融合和 rerank；不要全局图遍历后才过滤结果。以 RRF/排名融合为初始方案，不直接比较两个向量空间的 cosine 值。

默认只搜用户指定 collection；跨库查询必须明确请求并通过调用身份的权限检查。图节点、边、缓存和证据读取也遵守同一范围，不能通过共享实体名称串入其他库。

### 7.2 证据与回答

`rag_search` 返回结构化上下文，不执行最终回答；允许必要的检索改写/筛选，但各调用可统计。Codex 基于结果组织最终回答；可选 `rag_ask` 复用同一上下文，仅运行一次服务端最终生成。两条路径按调用者需求选择。

完全取消原版 `retrieved_segment_caption` 对原视频的读取：选择已保存的事实与证据，组装上下文后回答。测试时原视频应先被删除，甚至关闭源站访问，仍能返回出处、时间和截图。查询默认也不再启动视觉模型从截图生成新事实；证据不足时明确说明。

配方数量和单位只能来自可引用 transcript 或经过验证的屏显事实；场景相似度、图关系和 LLM 常识不能成为精确数字证据。回答中的事实关联 evidence/fact ID，最多选择两张覆盖答案的截图，避免只显示高相似但无关的画面。

## 8. 数据结构与迁移

### 8.1 建议的新增结构

表名为设计建议，实施时结合已有 schema 命名；所有新增视频结构含 collection、generation、schema_version 和稳定 ID。

| 数据 | 主要字段/关系 |
| --- | --- |
| video_assets | source metadata、content hash、duration、actor、import 状态、media_state、source_deleted_at |
| video_segments | video_id、真实 start/end、时间精度、transcript 引用、caption、处理版本 |
| video_facts | segment_id、事实文本、类型、材料/数量/单位、可靠性、unknown/冲突、支持证据 |
| video_evidence | 托管相对路径、mime、hash、frame PTS、crop 来源、可读性；不存原视频路径 |
| text / video embeddings | 向量与对应对象 ID、完整 space_id；分表/独立索引，不混写 |
| graph_nodes / graph_edges | 节点/关系类型、支持事实 ID、所属库与代际；必要的 JSONB 属性 |
| artifact_manifest | 产物 hash、发布检查点、清理清单、引用完整性与恢复状态 |
| index_generations | 文本/视频/图版本组合、encoder 配置、active 状态、回滚位置 |

知识文件保留人可读 Markdown；结构化 transcript、facts 和 manifest 独立保存，以免依靠重新解析人工修改的 Markdown 恢复精确证据。证据文件原子发布，持久路径不嵌入短期下载 URL。

### 8.2 与旧 schema 的兼容

现有 ingestion 任务含非空 Telegram user/chat/message 字段，不能给 MCP 调用伪造 Telegram ID。增加 channel、actor_id、origin reference、幂等键，Telegram 路由字段仅 Telegram 渠道必需。旧记录映射到原 Telegram 身份；外部 principal 来自服务端鉴权，不信任客户端自行填写用户身份。

已有 ingestion 迁移到 0007，后续从新迁移开始；核心迁移继续追加，不修改已应用 0001 的 checksum。Phase 1 已修复 renderer 仅对初始 schema 渲染维度，并追加/验证核心 0002 代际迁移。后续视频表仍需新迁移，同步维护打包资源与迁移清单。

Phase 1 隔离样例库已建立匹配本地 Qwen/LM Studio 的独立代际；生产旧向量仍需明确身份和迁移。Phase 6 建立完整 shadow generation，重新编码普通 Markdown 和派生文本，验证后将匹配的文本/视频索引、图与 encoder 配置组合切换；保留旧 generation 供回滚。维数相同不代表空间可混用。

## 9. Codex MCP 与可选远程服务

### 9.1 新入口与兼容策略

保留当前 stdio 模式的 `rag_search`、`rag_status` 两工具契约和 smoke test。新增独立的远程/扩展 profile 与启动入口；把协议外的逻辑移入共享 service，避免两个服务器复制实现。不能仅改 transport 后就公开现有工具，更不能直接在原只读 profile 加写入工具导致旧校验失败。

同机 Codex 使用 stdio，先接通旧两工具 profile，再加入独立扩展 profile。跨机器场景采用 Streamable HTTP 的 `/mcp` endpoint；SDK 提供相关 API，客户端协议版本、初始化、会话、超时、取消和错误返回仍需实际测试。视频文件通过受控资产通道提交。[MCP 传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

### 9.2 建议工具

| 工具 | 输入摘要 | 输出摘要 | 权限 |
| --- | --- | --- | --- |
| rag_search | query、knowledge_base、可选额外库、limit | RetrievalBundle 与证据引用 | read |
| rag_status | 允许查询的库/服务范围 | 模型空间、索引、任务可用性；无凭据 | read |
| rag_ask（可选） | query、明确库范围、回答选项 | 答案、引用、最多两项截图 evidence ID | read + answer |
| video_import | 公共来源 URL 或受控 asset ID、目标库、幂等键 | job_id、accepted/duplicate、后续状态查询入口 | ingest |
| ingestion_status | job_id | 处理/索引/清理状态、失败或降级原因 | 任务所有者或管理员 |
| evidence_get | evidence_id | 受权限检查的图片资源/短期读取引用 | 对应 collection 的 read |

video_import 快速入队，不让 MCP 工具调用等待视频处理完成。evidence_get 只读取服务端登记的证据，限制类型与大小，不接受路径穿越；图片返回方式与目标 Agent 支持的 MCP 资源能力一起验收。工具输入沿用长度、limit 和 collection 校验，敏感数据不出现在错误堆栈中。

### 9.3 部署和访问边界

首个部署使用同机 stdio 和受控本机身份，固定 collection 与 read/ingest 权限；跨机器时再使用内网/VPN 或已有受保护入口，配置 TLS 与服务端身份校验。服务端 token 映射到 actor 和可访问 collections；新增 URL 继续走现有公共 URL/重定向校验，不能下载任意内网地址。

若需要接入要求 OAuth 的公开 Agent 平台，再按该平台与 MCP 授权规范实现；不能假定一个固定 Bearer token 能连接所有客户端。V1A 先验收实际 Codex 的文本调用，V4 做扩展工具和证据调用验收。此方案不依赖 MCP sampling；Codex 组织最终回答，若实现 rag_ask 则使用项目配置的适配器。

## 10. Telegram 范围已取消

按 2026-10-04 的最新目标，V5/Phase 5 取消，不新增 Bot、polling、offset、聊天授权或 Local Bot API 服务，不需要 Telegram 凭据。既有兼容代码保留。视频资产入口、任务状态和证据由 Codex 扩展 MCP 承接，大文件通过受控资产通道提交。

## 11. 预算、配置与运行记录

按用户最新要求取消累计视觉处理时间预算，运行配置和新配置默认均为 `visual_budget_seconds=0`。之前累计的耗时仅用于记录，不再阻断后续切片；网络请求保留断线超时。

固定 100 张图的旧上限无法覆盖用户指定的密集采样，已改为按计划帧数和批次数设置有限请求/图像额度，预留有限重试和旧账本用量。累计时间不再阻断任务，失败请求仍记录，不无限自动重试。当前 CCR 不提供价格数据，费用标记 unknown；旧计划 0.03 美元阈值没有可核验的计价基础，不能声称已实施。600 秒静音视频计划 1200 帧，按每批最多 8 帧进行分析。

此前“约 90 帧分析 + 12 张云图像 embedding”的分配被本地视频分段编码取代；不能直接沿用云图像计费公式。文本/视频 embedding 的 API 费用为零，仍记录本地耗时、RAM/VRAM、段数、采样数和磁盘；ASR、文字 LLM、VLM 的费用单独归属。入库、问答、回填分开统计；已有 ledger 扩展复用。

下面是完整目标的拟定 YAML 结构，**尚未整体实现**。已实现的 Phase 1 使用 TOML，实际配置字段与命令见 [实施记录](implementation/videorag-phase-1.md)；不要把此 YAML 当作当前可运行配置：

~~~yaml
ingestion:
  worker_concurrency: 1
  visual_enabled: false
  visual_categories: [cooking]
  delete_source_after_import: true
  query_redownload_source: false
  failed_media_ttl_hours: 72
embedding:
  text_provider: local_lmstudio
  text_model: text-embedding-qwen3-embedding-0.6b
  text_dimensions: 1024
  video_provider: local_imagebind
  video_device: cpu  # V0 通过 GPU 实测后可改；不得自动回落到云
  video_batch_size: 1
models:
  llm_provider: configured_adapter
  vlm_provider: configured_adapter
  asr_provider: configured_adapter
mcp:
  profile: codex_extended
  transport: stdio  # 同机优先；跨机器另配置受保护 streamable-http
  server_answer_enabled: false  # 最终回答由 Codex 组织
~~~

凭据使用本地环境/权限受控配置，不写进 Git。启动前验证 provider、空间版本、collection、服务依赖与凭据缺失；不只输出一个笼统 healthy。

建议的运行单元是 Codex 启动的 MCP、单 ingestion worker、数据库，以及 LM Studio/本地视频编码服务；远程 MCP 仅按需增加；是否合并本地编码进程由内存测量决定，不声称系统只有两个进程。模型接口绑定 loopback，远程只开放受保护的 MCP 入口。沿用已有 cleanup 服务，增加证据孤儿检查与删除重试。监测重点为队列积压、媒体残留、索引待写、模型负载与问答失败。

## 12. 分阶段实施、落点与退出关口

估计按一名工程人员、配置和样例及时到位、单机开发计算；不包含等待凭据/网络或大规模旧库全量跑完的时间。模型速度、中文质量与依赖兼容会影响估计。

| 阶段 | 任务与主要改动 | 必须交付的验收结果 | 估计 |
| --- | --- | --- | --- |
| V0 视频可行性 | 固定 ImageBind/权重，独立依赖、CPU/GPU microbatch、FFmpeg 与中文样例 | 2 cooking + 1 非 cooking、离线编码、删源召回；无 Telegram 依赖 | 2–3 天 |
| V1 本地文本空间（隔离 MVP 已验收） | 修改 embedding/config/migrations；query/document 输入分离；完整 space_id；shadow 文本 generation | 真实本地编码与检索；错维度/NaN/错空间拒绝；普通资料兼容；已验证 generation 回退 | 2–3 天 |
| V1A Codex 文本接入 | 修复移动目录后的环境，恢复模型/DB，注册既有 stdio | 实际 Codex 调用与指定库命中、重启恢复；不重做文本空间 | 0.5–1 天 |
| V2 视频入库与持久化 | 新 VideoRAG 适配模块、PG 存储适配；改 media/service/temporal_models；字幕与 gate、事实/证据、图/视频向量、manifest/删源 | 同名不碰撞；时间与数字证据正确；完整成功才报告完成；真实视频知识可落库，媒体清理/崩溃恢复通过 | 5–8 天 |
| V3 持久知识检索与 QA | 改 rag.py；抽离 retrieve_context；文本/图/视频融合、空间检查、来源时间、问答适配 | 原视频不存在时正确检索/回答；不重下载；至少 10 个真实视频质量关口通过 | 3–5 天 |
| V4 Codex 扩展 MCP | 独立 extended stdio profile、异步 import/状态/证据；远程按需增加 | 实际 Codex 五项必需工具、证据回答、去重/隔离/重启；旧 profile 兼容 | 1–2 天 |
| V5 Telegram 已取消 | 保留编号和旧兼容代码 | 无新 Bot 验收要求 | 不计入 |
| V6 迁移与单机交付 | 旧库扫描/回填、人工编辑保护、代际切换/回滚、systemd、资源/成本/恢复复验 | 最终质量报告、部署说明、可观察状态、回滚与媒体残留检查；历史缺源记录明确降级 | 3–5 天 |

**剩余完整范围粗估 15–24 个工作日**，已完成文本阶段不重算，Telegram 取消；跨机器 HTTP/身份权限需要时另加 2–3 天。样例/provider 及时到位，且不含等待、依赖/OOM 排障和全部历史视频处理时间。Phase 0 测量后修订。

V0 先确认资源和中文证据可行性；V2 先用至少 3 条不同主题真实视频检查短暂屏显信息。V3 使用至少 10 个不同来源与不少于 60 个人工核实问题验收，保留烹饪配方作为数字准确性的子集。任一关口失败，不把合成链路通过当作最终质量通过。

### 12.1 预计涉及的文件与新增模块

| 位置 | 修改内容 |
| --- | --- |
| src/rag_favorite/embedding.py、config.py、setup.py | 本地 provider、空间身份、配置验证与初始化 |
| src/rag_favorite/rag.py、ingestion.py | 视频感知检索、结构化上下文、普通文档兼容 |
| src/rag_favorite/mcp_server.py、mcp_contracts.py、mcp_support.py | 旧 stdio 保留；新的 remote/extended 工具和契约 |
| src/rag_favorite/migrations.py、resources/migrations、migrations/core | 新迁移、renderer 修正、shadow generation 和部署同步 |
| ingestion/backend/ingestion/service.py、media.py、models.py、repository.py | 新 actor/channel、分段管线、发布/清理状态与恢复 |
| ingestion/backend/ingestion/content_classifier.py、temporal_models.py、model_usage_ledger.py | 保留已有改动，补充 VideoRAG 元数据/预算与 gate 验证 |
| ingestion/backend/ingestion/markdown.py、enrichment.py、notifier.py | 事实/出处/截图输出、人工编辑保护、可查询任务结果与兼容通知 |
| ingestion/migrations 与 rollback | 新身份、状态、视频知识表与可恢复迁移 |
| 拟新增 videorag_adapter / local_encoder / application_services / extended MCP 模块 | 上游补丁隔离、本地模型、入口共享业务与 Codex 工具；名称在实施时定稿 |
| pyproject.toml、模型环境锁、systemd 资源、examples | 可选依赖/启动入口、固定权重来源、服务配置、Agent 调用示例 |
| tests、ingestion/tests、docs/implementation | 有行为意义的回归/真实视频结果与阶段记录 |

现有 OpenClaw 插件和兼容目录不在本计划中删除。先完成接口兼容后再决定是否逐步停用；不覆盖工作区已有未提交开发。

## 13. 验收矩阵

| 场景 | 验收标准 |
| --- | --- |
| 中文屏显配方、博主未口述 | 至少 10 条真实 cooking 视频、60 条标注证据；正确证据 Top 3 命中率 ≥90%；回答中可读数量和单位与标注一致 |
| 模糊数字/字幕冲突 | 只做规定次数复核；仍不清楚输出 unknown；冲突有出处，不猜数字 |
| 不同主题、无语音及长视频 | 各主题均可视觉处理；无语音 ≤600 秒为 0.5 秒间隔，有转录/字幕 ≤600 秒为 1 秒；>600 秒视觉调用为零；纯音频不生成视频向量 |
| 删源后查询 | 完成通知前删除托管原视频/切片；重启后仍能回答、给出时间/截图；查询路径不存在重下载和视频解码 |
| 删除与崩溃恢复 | 发布前/后、DB 提交前/后、清理前/后注入中断；不误报成功、不删除活跃媒体、不丢证据；清理重试幂等 |
| 视频细节未曾保存 | 明确证据不足；不能因为匹配一个相似视频就补写未记录事实 |
| 时间对齐 | VFR、小数秒、短尾段、跨段字幕正确关联；只有粗时间时不伪造精度 |
| 向量空间 | 文本/视频不同空间，同维错模型拒绝；NaN/维度/预处理变化检测；active generation 与模型配置一致 |
| 本地 embedding | 权重已下载后，禁用其外部网络仍可编码和检索；无自动云 fallback；记录 CPU/GPU峰值与耗时 |
| 结构化检索 | rag_search 不调用最终回答模型；rag_ask 只做一次最终回答；每条关键事实有可访问证据 |
| collection 隔离 | 文本、视频、图扩展、缓存、evidence_get 和任务状态均不能越过身份允许范围 |
| MCP 兼容 | 指定 Agent 真正完成初始化/列工具/搜索/异步导入/查询状态；旧 stdio 两工具测试继续通过 |
| Codex 全流程 | 真实客户端明确选库、提交资产/链接、查任务、检索/读证据/回答；重复请求与重启不重复入库 |
| 人工编辑与回填 | 原文 checksum 对比；不覆盖人工改动与旧摘要；冲突 sidecar、新任务优先行为可验证 |
| 旧源不存在 | 明确 source_missing / text_only；不从 transcript 伪造视频向量，不自动重新下载 |
| 最终部署 | 数据库、FFmpeg、编码器、模型 provider、MCP/Codex 分项健康检查；无长期原视频残留；备份/回滚演练通过 |

离线单元测试覆盖契约和恢复分支；真实媒体、真实数据库、真实模型与实际 Codex 作为单独验收。不要将 mock 成功写成模型质量已通过，也不要用上游英文 benchmark 替代本项目中文配方基准。

## 14. 旧库回填、切换与回滚

1. 盘点所有库的 transcript、人工编辑、来源、现有证据与可用原视频；不按主题排除内容，视觉是否执行由所有者配置、真实时长及视频可用性决定。
2. 普通文本与已保存派生文本可以直接重建本地文本空间。完整的视频处理只对明确可用、已授权处理的原视频执行；完成后按新策略清理托管副本。
3. 原视频已不存在的历史记录保留现有知识，标为 text_only/source_missing。可用的旧图像证据保留，但不能冒充新的 ImageBind 视频编码；用户以后重新提供视频才补全。
4. 采用 shadow generation；文本、视频、图与模型配置组合通过质量检查后原子切换。新导入在切换期间进入指定 generation 或可重放队列，避免漏写。
5. 保留旧摘要与人工正文，新事实追加到受管区；版本冲突写 sidecar 并记录合并状态。回填无每日数量限制，但受单 worker、预算和新任务优先约束。
6. 回滚切回上一组索引/encoder/图配置与兼容应用版本，不将新向量写进旧空间。新 schema 的 down migration 只在确认保留数据后执行；正常回滚优先保留新增知识表。

回滚不能恢复已删除的原视频；可恢复的对象是知识、证据、索引和应用配置。若计划将来频繁更换视频模型，需要接受重新提供源视频的成本；本次选择优先满足不长期存视频。

## 15. 后续实施需要用户提供的内容

项目和仓库、VideoRAG 身份、本地 embedding、独立入口及删源要求已确认，不再重复询问。编写适配层、数据结构、离线测试和部署模板可以继续独立开展；真实服务验收需要以下输入：

1. **模型连接配置**：分类 LLM、VLM、必要 ASR 的 endpoint、模型和私有凭据；现有可用配置可复用。最终回答由 Codex 当前模型组织，首个交付不需要额外 QA 模型。
2. **真实样例与标注**：先准备 2 cooking + 1 非 cooking；随后扩展至少 10 条 cooking 和 60 条屏显证据，用于质量验收。
3. **实际数据位置**：生产数据库、人工知识与可用旧视频目录；不存在的历史部署不能默认存在。
4. **Codex 与访问位置**：同机 stdio 为默认方案；若 Codex 和后端在不同机器，再确认受保护远程地址、身份与资产提交方式。不需要 Telegram token。

这些是进入真实验收时的输入，不阻止当前计划形成，也不要求现在重新选一套 Agent 框架。服务器 GPU 已核查；本地模型可用性和处理速度由 V0 测量后给出。

## 16. 完成交付物

完成本计划后应交付：Codex 可用的知识检索/视频 import MCP、按需增加的其他机器 Agent 远程 MCP、适配 VideoRAG 的持久知识后端、本地双 embedding 与独立空间、成功入库后的可靠媒体清理、出处/时间/截图回复、旧库迁移与回滚工具、实际资源/成本/中文质量报告及 Linux 部署说明。

VideoRAG 和模型的来源、固定版本及各自许可证随部署记录；ImageBind 官方为 CC-BY-NC 4.0，不将其误标为 MIT。[官方许可证](https://github.com/facebookresearch/ImageBind/blob/main/LICENSE)
