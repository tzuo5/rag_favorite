# Dev Doc VideoRAG 本地双 Embedding 与 Codex MCP

更新日期：2026-10-04，America/Chicago。当前项目：`/home/tzuo5/gpt projects/rag_favorite`；配套仓库：[tzuo5/rag_favorite](https://github.com/tzuo5/rag_favorite)。

本文按 [完整修改计划](videorag-local-mcp-plan.md) 维护各 Phase 的独立 MVP、前置条件、任务、验收和退出方式。按最新要求，**Codex 成为主要入口，取消 Phase 5 Telegram，新增 Phase 1A Codex 文本接入**；保留旧编号以便追踪。视频 Embedding 的源码核查与配置步骤见 [ImageBind 本地配置](research/imagebind-local-setup.md)。

**当前状态：Phase 1、1A 已 verified；Phase 0 编码/资源已实测，真实样例 PoC 正在验证；Phase 2–4 的核心实现和合成闭环已运行，真实样本处理与质量关口正在推进；Phase 6 用户服务、备份恢复已运行，回填/故障演练已通过，生产迁移与最终质量仍未验收；Phase 5 cancelled。** LM Studio、PG/pgvector、ImageBind、本地 ASR、CCR 和单 worker 当前均已部署。Codex 已实际调用五个扩展工具。11 条原始小红书输入已记录，扫码登录已验证，真实视频开始入库并清理托管媒体。不要把合成客户端闭环当作中文质量全部通过。见 [持续进度](implementation/autonomous-progress.md)、[实施记录](implementation/videorag-phases-0-6.md) 与 [真实样本状态](implementation/real-sample-ingestion-results.json)。

后续 provider 调整已验收：**本地文本模型默认由 LM Studio / llmster 提供**，替代 Ollama；旧代际保留供显式回退。[LM Studio 实施记录](implementation/lmstudio-provider.md)

这里的“独立 MVP”指：前置 Phase 完成后，本阶段可以用自己的入口运行、展示和验收，不必等后续扩展工具或最终部署完成。各阶段共用一个系统与知识库，不分别复制项目。本文 Phase 0–6 对应完整计划 V0–V6，Phase 1A 对应 V1A；与旧 [Development MVP.md](<../Development MVP.md>) 的五阶段编号无关。


当前分类策略已按用户最新要求修订：**取消强制类别、取消 cooking 视觉准入，默认检索全部知识。** 旧 collection key 仅保留作存储兼容；现行实现见 [统一知识库修改记录](implementation/unified-library-update.md)。原 cooking 10 视频 / 60 问题是早期质量基准，现行总体验收使用至少 10 个不同来源、60 个人工核实问题，并覆盖不同主题；已完成旧样本不会自动重新下载或补做视觉。

最新视觉策略已实现：**无语音视频每 0.5 秒一帧；有语音转录或提供字幕文件的视频每 1 秒一帧；超过 600 秒完全跳过视觉处理，600 秒本身仍处理。** 保存分批小结和逐帧分析 Markdown，模型批次可恢复。累计时间预算已取消；详细规则、工作流和真实模型链路验证见 [密集抽帧修改记录](implementation/dense-sampling-update.md)。

## 0. 最终目标与全局约束

最终使用流程：定时读取收藏或在 Codex 提交受控视频资产/链接，不要求指定类别 → MCP 异步 import → 提取并保存 RAG 知识 → 清理托管原视频 → Codex 调用检索和证据工具，组织带出处、时间和截图的回答。同机先使用 stdio；跨机器的受保护远程 MCP 作为后续扩展。大文件通过受控资产入口上传，不塞进 MCP JSON。

所有阶段都必须遵守以下规则：

1. **本地双 embedding。** 普通文本使用本地文本模型；视频片段及其查询使用匹配的本地视频/文本编码器。维数相同也不能混用空间，不能静默回落到云 embedding。
2. **模型供应商可配置。** 不依赖 OpenAI 或 OpenClaw 才能启动。文字 LLM、VLM、必要 ASR 可以使用现有 CCR/OpenRouter 或其他适配器；本次不要求它们全部本地运行。
3. **字幕优先，无强制分类。** 先读随视频提供的字幕文件；没有提供字幕时使用本地 Faster Whisper Medium（CPU INT8，beam_size=5、VAD、词级时间戳），后续收藏夹、链接和托管文件导入共用该配置。画面里的烧录字幕由 VLM 读取，不属于字幕文件。确认无语音时按 0.5 秒抽帧，有转录/字幕时按 1 秒抽帧，超过 600 秒完全跳过视觉。视觉默认配置关闭，所有者显式启用后适用于所有主题的视频，不受 cooking 或 collection 名称限制；当前所有者运行配置已启用。新导入进入统一逻辑知识库，默认全库检索。2026-10-04 已在同一源视频上实测 Medium，并核对运行中任务的 ASR 检查点使用 Medium；已完成的旧内容不会自动重写。
4. **知识持久化后删源。** 完整 import 成功前，所需知识/向量/证据已保存，原视频副本、音轨、临时切片及缓存已清理。查询不读取或重新下载原视频。
5. **保留可审查证据。** 保存转录、知识点、caption、向量、必要图关系、来源/时间和少量截图；不长期保存可播放的视频。回答最多附两张相关截图。
6. **数字不猜。** 模糊数字只做一次高清/局部裁剪复核，仍不明确则 unknown；字幕与画面冲突要保留出处和冲突。
7. **复用现有系统。** 保留 PostgreSQL/pgvector、collections、Markdown、任务生命周期、普通文本 RAG 和旧 stdio 两工具契约；图关系存同一 PostgreSQL。
8. **回填保护人工内容。** 保留旧摘要和人工正文，冲突写 sidecar；单 worker 空闲回填，新任务在当前视频完成后优先，无每日数量上限。
9. **真实验收与 mock 分开。** 离线测试证明接口和异常处理；模型质量、数据库和实际 Codex/MCP 客户端须另有运行证据。

上游版本、模型参考和查询删源限制的依据见 [核查记录](research/videorag-integration-audit.md)。模型候选沿用 Qwen3-Embedding-0.6B 与 ImageBind；Phase 0 验证本机可行性，模型权重版本和预处理确定后写入 space_id。

## 1. Phase 总览与 MVP

| Phase | 独立 MVP：结束时能演示什么 | 依赖 | 原计划 | 估计 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 0 本机可行性 | 一个隔离 PoC：ImageBind 视频/query 本地编码、CPU/GPU 实测、离线与删源后召回 | 已有文本基线；样例、模型下载与必要 provider 配置 | V0 | 原估 2–3 天 | in_progress：资源已实测，真实 PoC 推进中 |
| 1 本地文本 RAG | 用 CLI 导入并检索普通 Markdown；文本编码本地完成，已验证代际可回退 | Phase 0 的文本/数据库基线 | V1 | 2–3 天 | verified（隔离 MVP） |
| 1A Codex 文本接入 | 恢复新目录下文本运行，真实 Codex 通过 stdio 搜索指定库 | Phase 1 | V1A | 已实施 | verified：实际 Codex 两工具 |
| 2 视频入库与删源 | 用 CLI/任务入口导入各主题视频，导出可读知识与截图；完成后托管目录无原视频 | Phase 0、1 | V2 | 原估 5–8 天 | implemented：合成通过，真实任务处理中 |
| 3 持久知识检索与 QA | 视频删除后输出正确事实、出处、时间和截图，Codex 基于证据回答 | Phase 2、1A | V3 | 原估 3–5 天 | implemented：真实删源检索已运行，10/60 未验收 |
| 4 Codex 扩展 MCP | 实际 Codex 提交异步 import、读取任务与证据，旧 stdio profile 兼容 | Phase 3 | V4 | 核心已实施；远程另计 | implemented：实际 Codex 五工具，真实五工具与截图已验证；质量另验 |
| 5 Telegram | 从当前范围移除，不要求 Bot 或大附件转发能力 | 不再依赖 | V5 | 不计入 | cancelled |
| 6 迁移与 Linux 交付 | 旧库受控回填，重启后恢复，Codex/MCP、代际切换与回滚通过 | Phase 0、1、1A、2–4 | V6 | 原估 3–5 天 | implemented 部分：服务/备份恢复已运行，回填/故障演练通过，生产与质量待验收 |

原计划尚未实施时估计为 15–24 个工作日。核心闭环目前已完成较多，剩余时间主要取决于真实样本质量、人工标注、历史库规模与恢复排障。后续收藏入口已获得更多不同主题候选，但最终 10 视频/60 个人工标注问题尚未完成质量验收；不能给出无等待条件的完成日期。跨机器 HTTP/身份权限仍为单独扩展。

~~~mermaid
flowchart LR
    P1[Phase 1 已验收文本 RAG] --> PA[Phase 1A Codex 文本接入]
    P0[Phase 0 ImageBind 本机 PoC] --> P2[Phase 2 视频入库与删源]
    PA --> P2
    P2 --> P3[Phase 3 检索与 QA]
    P3 --> P4[Phase 4 Codex 扩展 MCP]
    P4 --> P6[Phase 6 迁移与部署]
~~~

Phase 1A 先验收真实 Codex 的现有文本工具，不等待视频能力。Phase 4 新增独立扩展 profile，复用共享服务；只有跨机器调用需要远程 HTTP。最终回答优先由 Codex 当前模型生成，服务端 rag_ask 为可选功能，不作为首个交付前提。

## 2. 状态、交付与验收约定

任务状态采用 planned → in_progress → implemented → verified；取消范围记 cancelled。implemented 不表示真实验收通过。VR1-01–06 与 VR1A 文本接入已验收，VR5 cancelled；其他逐项证据见最新实施记录。下方保留原验收清单，完整真实/质量/迁移关口未通过时不勾选。

每个 Phase 要交付代码/配置、可重复演示入口、结果与证据、资源/费用记录、限制和恢复说明。阶段记录保存在 `docs/implementation/videorag-phase-N.md`，真实测试结果单独留档；Phase 1 记录已创建，其余随实施建立。修改过的配置/命令只有真正实现并验证后才能写入运行说明。

阶段验收只运行与行为有关的检查和既有兼容回归。通过后进入下一阶段；失败就保留 implemented/in_progress 并修复，不以完成了演示来跳过必需的恢复/权限检查。

## 3. Phase 0：本机可行性与独立 PoC

**目标：** 在 RTX 4060 Laptop / 8 GB 显存这台机器上，确定本地双编码与适配 VideoRAG 的方向可行，并提早暴露中文证据、依赖和资源问题。

### 3.1 独立 MVP

在隔离样例目录运行一个 PoC：输入至少 3 条涵盖不同主题的视频/转录；生成本地文本向量，各主题的有时间轴视频另生成本地视频向量与带时间的派生知识/截图；删除 PoC 托管视频副本后，重启脚本，通过两个正确的向量空间召回样例证据。

此阶段使用隔离存储与脚本，不接生产库，也不要求完整图谱或最终问答服务。PoC 保存的事实必须来自实际样例；手填标注只能作为评测真值，不能冒充模型提取结果。

### 3.2 前置条件与落点

需要模型下载权限、至少 3 条样例和可用的分类/抽取 provider；缺少外部凭据时可以先验证本地编码，但整个 Phase 保持未验收。准备 FFmpeg/ffprobe、隔离 PostgreSQL/pgvector，并核对现有工作区修改，保持原有代码和备份。

落点：隔离模型依赖锁、拟新增 local_encoder/videorag_adapter PoC、examples、环境诊断脚本和阶段记录。核心应用继续 Python 3.12；模型依赖先验证独立 3.11 环境，通过本地接口连接。

### 3.3 开发任务

| ID | 开发内容 | 验证/产物 |
| --- | --- | --- |
| VR0-01 | 记录系统、GPU、Python、FFmpeg、数据库、网络与已有配置；建立隔离测试目录 | 可复查基线；不将缺失配置或工具写成已安装 |
| VR0-02 | 固定 VideoRAG 源码与模型权重 revision/digest，记录许可证和所需补丁；锁定隔离依赖 | 可重复安装/加载；不会自动加载无用 MiniCPM 或 Whisper |
| VR0-03 | 保留 Qwen 基线；验证 ImageBind 视频/query、CPU 后 GPU，batch 1；保留 15 视图采样并实测 microbatch | 正确维度/归一化/有限数值；逐视图与整批差异、RAM/VRAM、耗时、加载次数 |
| VR0-04 | 做最小分段知识持久化和删源后召回；验证中文问题对应的视觉检索改写 | 原媒体不存在仍返回正确样例证据；不同主题均可进入已启用的视觉阶段 |
| VR0-05 | 固定首个调用端为 Codex，同机 stdio 优先；核对资产提交、图片资源和明确库范围 | 记录本地连接与受控资产方案；远程身份仅在跨机器需求中扩展 |

### 3.4 MVP 演示与完成标准

演示顺序：运行诊断 → 输入样例 → 输出编码/派生记录 → 移除托管媒体 → 重启 PoC → 运行两个空间的检索。日志区分真实模型调用和测试替身。

- [ ] 两种 embedding 本地可运行；权重下载完成后，阻断编码器外部网络仍能编码。
- [ ] CPU/GPU 数据已测量；不重复加载 ImageBind，不出现无人处理的 OOM。
- [ ] 至少 3 条不同主题样例记录完整，并验证视觉不受主题限制。
- [ ] 原视频不存在时仍可读取已保存事实/时间/截图并召回；查询不下载媒体。
- [ ] 中文效果、8 GB 限制与依赖选择有事实记录，未达标部分不记 verified。

**退出/回退：** 若 GPU 不可行，先验证 CPU。仍不可行则调整本地编码器并重新做本阶段，不能默认切到云 embedding。清理仅限 PoC 拥有的副本，不删除用户提供的原始资料或生产数据。

## 4. Phase 1：本地文本 RAG 与索引空间

**目标：** 先把普通文档和视频派生文本的基础检索切到可靠的本地文本编码，为视频后端提供稳定接口。

### 4.1 独立 MVP

用 CLI 与可重复演示入口，导入两组不同 collection 的 Markdown，用中文问题检索到正确文档。阻断演示客户端的非 loopback TCP 后继续编码检索，模型服务关闭云功能；只在请求的库内召回，可以切回已验证的旧 generation。测试范围见实施记录，未修改系统防火墙。

此 MVP 能作为普通文本知识库独立使用；视频编码器与 MCP HTTP 不属于本阶段前置条件。

### 4.2 前置条件与落点

依赖 Phase 0 的文本模型与数据库基线；这部分已在本次准备，完整视频 PoC 尚未完成。旧数据先只读盘点。落点：`src/rag_favorite/embedding.py`、`config.py`、`setup.py`、`rag.py`、`indexes.py`、`migrations.py`、核心迁移与打包资源、相关既有测试。

### 4.3 开发任务

| ID | 开发内容 | 验证/产物 | 状态 |
| --- | --- | --- | --- |
| VR1-01 | 将本地文本 provider 纳入配置与启动检查；保留已有云适配为显式兼容选项 | 新目标默认/指定 local，不因请求失败自动调用云服务 | verified |
| VR1-02 | 分离 embed_documents 与 embed_query 的预处理；query 指令正确，文档不误套前缀 | 按模型要求调用，批量与单条行为一致 | verified |
| VR1-03 | space_id 绑定权重 digest、维度、归一化、预处理/指令版本；generation 绑定 schema/encoder 快照 | 同维不同模型/指令索引被拒绝；NaN/维度错误被拒绝 | verified |
| VR1-04 | 追加核心迁移与 shadow generation；修正 renderer 仅对初始 schema 渲染向量维度 | 无 vector 标记的新迁移可应用；旧 0001 checksum 不变 | verified |
| VR1-05 | 重建样例库文本向量，保持普通 Markdown/collections 与 CLI、旧 stdio 行为兼容 | 数据库检索命中、库隔离与兼容回归通过 | verified |
| VR1-06 | 增加明确的代际切换/回退入口与运行说明 | encoder 与 active index 同步切换；失败不留下混合空间 | verified |

### 4.4 MVP 演示与完成标准

演示顺序：配置本地模型 → 创建 shadow generation → 导入两库样例 → 搜索 → 请求错空间/错库 → 切回旧 generation。已有真实数据全量切换留到 Phase 6。

- [x] 真实 PostgreSQL/pgvector 入库和中文搜索成功，不只验证 mock SQL。
- [x] 文档与 query 正确预处理，文本编码无外部 embedding 请求。
- [x] 错空间、同维不同模型、错误维数和非有限向量返回明确错误。
- [x] 单库搜索不会隐式跨库；现有普通文档和 stdio 两工具契约回归通过。
- [x] 新 migration 可从空库及现有 schema 追加，shadow 切换/回退有演练证据。

证据：[实施记录](implementation/videorag-phase-1.md)、[真实 MVP](implementation/phase1-text-mvp-results.json)、[回归日志](implementation/videorag-phase1-test-results.txt)、[stdio 客户端](implementation/phase1-stdio-mcp-results.json)、[重启恢复](implementation/phase1-runtime-results.json)。生产知识库尚未切换。

**退出/回退：** 保留旧索引，用 index activate 切回已验证 generation 的匹配 encoder 快照。无版本身份的 legacy 向量保留但不能安全激活，应重建或与明确的旧应用/配置配对；不覆盖旧向量，也不删除 Markdown。

## 4A. Phase 1A Codex 文本接入

**独立 MVP：** 保留 LM Studio 与既有索引，在新项目目录恢复运行，真实 Codex 列出 rag_search/rag_status 并搜索指定样例库；返回事实引用，由 Codex 组织回答。

| ID | 开发内容 | 验证/产物 | 状态 |
| --- | --- | --- | --- |
| VR1A-01 | 修复移动目录后的 editable 安装、CLI shebang、私有路径和含空格启动参数 | 新目录下 doctor/检索成功，旧样例索引未覆盖 | verified |
| VR1A-02 | 恢复 LM Studio 与隔离 PG，固定模型 digest/active generation，重跑必要真实检查 | 本轮在线与检索证据；不重建已正确的文本模型配置 | verified |
| VR1A-03 | 为实际 Codex 注册现有 stdio profile，凭据从私有环境读取 | Codex 真正握手并调用两工具；不能只以 SDK 测试代替 | verified |
| VR1A-04 | 验证明确库范围、无结果/服务停机提示与重启恢复，保存连接说明 | 普通文档体验可用；不默认跨库、错误不泄漏凭据 | verified |

完成标准：实际 Codex 调用记录、指定库正确命中和重启恢复均通过。退出方式：禁用该 MCP 条目，保留知识库与文本索引。此阶段不提供视频 import 或声称视频模型已配置。

## 5. Phase 2：视频知识入库、证据持久化与删源

**目标：** 把 VideoRAG 的视频处理接到现有 ingestion 生命周期，完成“视频变知识”的可靠过程。

### 5.1 独立 MVP

通过 CLI/队列入口提交至少 3 条涵盖不同主题的真实视频：导出 transcript、分段 caption、结构化配方/知识点、图关系、文本/视频向量与截图。任务完成后，托管目录没有原视频、音轨、物理切片或下载副本；另一进程能从持久数据重新读取知识和证据。

此阶段通过“知识导出/检查”验证价值，不等待完整检索融合或最终回答。图关系必须带支持事实，不能仅展示无出处实体列表。

### 5.2 前置条件与落点

依赖 Phase 0、1，本地模型和 VLM/必要 ASR 已可用。视觉在测试配置中显式启用，生产默认仍关闭。落点：`ingestion/backend/ingestion` 下 media/service/models/repository/temporal_models/markdown/enrichment、分类与 ledger，新增 VideoRAG/存储适配、核心与 ingestion 新迁移、cleanup。

### 5.3 开发任务

| ID | 开发内容 | 验证/产物 |
| --- | --- | --- |
| VR2-01 | 建立共享 actor/channel/origin/幂等键；Telegram 路由字段只在 Telegram 渠道必需 | CLI/MCP 入库不伪造 Telegram ID；重复来源同请求不重复建任务 |
| VR2-02 | 字幕优先、结构化 transcript 和无强制分类流程 | 有字幕不额外 ASR；所有主题均可使用显式启用的视觉处理，无语音可分析画面 |
| VR2-03 | 替换原生 insert_video 编排；稳定内容/来源 ID、约 30 秒逻辑片段、小数秒/PTS 对齐 | 同名附件不碰撞；VFR、短尾段、跨段字幕与未知时间正确处理 |
| VR2-04 | 实现真实 VLM caption/知识点/屏显文字提取；无语音 0.5 秒、有转录/字幕 1 秒密集抽帧，分批恢复与一次数字复核 | ≤600 秒覆盖全部计划帧；>600 秒视觉调用为零；模糊数字 unknown，冲突保留 |
| VR2-05 | 本地 ImageBind 片段编码与同空间 query 接口；单实例、batch/设备/队列调度 | 视频仍存在时完成编码；向量产物可重放，模型加载次数可观察 |
| VR2-06 | 接入 VideoRAG 实体/关系构建；PG KV/节点/边/向量适配，移除 ._data/eval 依赖 | 持久图可恢复；每条关系能回溯支持事实，collection/generation 隔离 |
| VR2-07 | 写入 facts/evidence/manifest 和 Markdown；保留人工区与旧摘要，冲突 sidecar | hash 与引用完整；新进程可读，人工内容无损 |
| VR2-08 | 实现可恢复发布检查点及完整/待索引/待清理/失败状态；同步数据库 CHECK 与 presenter | 文件发布和 DB 提交之间中断不丢证据、不误报 completed |
| VR2-09 | 在满足持久化条件后清理全部托管媒体；失败 TTL、活跃任务锁和清理重试 | 成功无媒体残留；不删除活跃媒体或用户外部路径；清理失败单独报告 |
| VR2-10 | 复用预算与 ledger，区分本地编码资源和远程模型费用 | 达预算停止新请求，结果标不完整；无价格记录标 unknown |

### 5.4 清理和状态必须同时实现

内部检查点按完整计划映射为 media_processing → derived_persisted → index_ready → cleanup_pending → completed；状态代码、SQL、通知与恢复逻辑一致。

| 结果 | 允许的清理/通知行为 |
| --- | --- |
| completed | 所需知识与索引发布且媒体清理成功后才发送完整完成通知 |
| index_pending | 依赖视频的事实/向量产物全部持久化，后续仅写索引时可删媒体；通知未完成索引 |
| 媒体依赖步骤失败 | 短期保留受管副本恢复，默认 TTL 72 小时可配置；到期清理并报告失败，保留可用 transcript |
| cleanup_pending | 知识已可读但删除失败，单独提示并重试；不重新运行模型，不声称清理成功 |
| text_only / visual skipped | 正常文本模式可完成，但清楚报告跳过原因；不能冒充完成视频 embedding |

### 5.5 MVP 演示与完成标准

演示顺序：提交真实视频 → 查看阶段状态 → 导出知识与截图 → 检查托管文件清单 → 重启读取知识 → 重放一次已提交/清理任务 → 注入一次发布与删除失败。

- [ ] 至少 3 条不同主题的真实样例产生可读知识与时间/截图；不把模拟 provider 算视觉实现。
- [ ] 默认关闭和跨主题视觉启用通过；数字复核、unknown/冲突及时间精度有证据。
- [ ] 本地视频向量与文本向量分开存，图关系有出处，持久派生记录可独立恢复。
- [ ] 完成通知前托管原视频/音轨/切片/下载缓存已清理，必要截图正常读取。
- [ ] 同名、重复导入、跨进程重启、发布/索引/删除中断和 TTL 行为正确。
- [ ] 人工内容保护、预算停止与费用未知状态通过；不能假装完整入库。

**退出/回退：** 保留已发布知识与原有普通文本管线，暂停新视觉任务。失败临时媒体按 TTL 清理；不要为回滚重新下载已删视频或把缺失数据填成成功。

## 6. Phase 3：持久知识检索与 QA

**目标：** 把已保存的视频知识真正用于回答，完成不用原视频的文本/图/视频融合检索。

### 6.1 独立 MVP

先确认 Phase 2 样例的原媒体已删除，再关闭源站下载访问。通过 CLI 输入“这个视频里料汁怎么配”等中文问题，得到正确配方、出处、时间和最多两张截图；问一个未提取的视觉细节时得到证据不足。

同一服务返回结构化上下文给调用者，普通 Markdown 检索继续可用。Codex 基于上下文回答，跨机器远程 MCP 不属于本阶段前置条件。

### 6.2 前置条件与落点

依赖 Phase 2 的真实持久知识、本地两空间与 Phase 1A 的 Codex 调用端；独立回答 LLM 为可选配置。落点：`src/rag_favorite/rag.py`、拟新增 retrieval/QA/application_services、VideoRAG 查询适配、证据读取、配置与检索测试。

### 6.3 开发任务

| ID | 开发内容 | 验证/产物 |
| --- | --- | --- |
| VR3-01 | 定义 RetrievalBundle，补齐 fact/evidence ID、来源、时间精度、空间/代际与可靠性 | 返回结果可追溯，unknown、冲突、source_deleted 状态不丢失 |
| VR3-02 | 实现普通文本与视频 query 编码路由；中文视觉检索改写保留数字/单位/专名 | query 进入匹配空间，改写失败明确降级，无云 embedding |
| VR3-03 | 接入 VideoRAG 文本/图/视频召回与排名融合；提前做库/代际过滤、限制图扩展 | 初始用 RRF，不直接比较不同空间 cosine；多库与图关系不泄漏 |
| VR3-04 | 真正抽离 retrieve_context 与最终 answer；移除原视频 fine-caption 和重下载路径 | 搜索不运行最终回答；上下文全来自持久知识，不解码原媒体 |
| VR3-05 | 优先让 Codex 按 RetrievalBundle 回答，验证冲突/未知与最多两张截图；独立 QA adapter 可选 | 配方有事实支持；未保存细节不猜；不将相似度当数字证据 |
| VR3-06 | 加入有界上下文、query/入库编码调度、调用次数与资源/费用记录 | 查询不重复加载模型；最终回答仅一次，排队和降级可观察 |
| VR3-07 | 建立真实中文评测集、文本/视频召回对照及兼容回归 | ≥10 个不同来源视频、≥60 条人工标注，覆盖不同主题；建议 ≥10 条负例；质量达标报告 |

### 6.4 RetrievalBundle 最小字段

返回 query 原文与必要改写、明确请求的 collections、各索引 generation；证据项含 document/video/segment/fact/evidence ID、transcript/caption/知识点、图关系的支持事实、出处/标题/作者、start/end 与时间精度、各通道排名/融合排名、可靠性与 unknown/冲突、原媒体状态。

结构化搜索不能只有一段模型生成的总结。截图以服务端登记的 evidence ID 获取；持久知识没有支持时允许返回空证据和明确原因。

### 6.5 MVP 演示与完成标准

演示顺序：检查原视频不存在 → 禁止下载访问 → 搜索上下文 → 问答 → 读取引用截图 → 问未知细节 → 重启后复测 → 运行真实质量基准。

- [ ] 10 个不同来源视频 / 60 条人工标注证据，覆盖不同主题，正确证据 Top 3 命中率 ≥90%。
- [ ] 可读配方的数量/单位与标注一致；无法确定时 unknown，不能靠常识补数字。
- [ ] 无视觉权限时调用为零；普通资料与默认全库/显式分区搜索兼容，图扩展不越界。
- [ ] 原视频删除后重启仍能回答；查询期间无源视频读取、解码或下载。
- [ ] retrieve_context 不调用最终回答，QA 只做一次最终回答；改写/筛选调用另行记录。
- [ ] 回答提供正确来源、时间与最多两张相关截图；未保存细节明确证据不足。

**退出/回退：** 保留已保存数据和空间，可关闭图/视频融合退回明确标记的文本检索。质量不达标先修抽帧/事实提取或召回，不能通过查询重下载掩盖入库缺漏。

## 7. Phase 4 Codex 扩展 MCP

**目标：** 将视频 import、状态和证据服务接入实际 Codex，保留旧本地两工具 stdio 契约。同机使用独立 extended stdio profile；受保护远程 HTTP 是跨机器调用的可选扩展。

### 7.1 独立 MVP

用实际 Codex 完成初始化、列工具与搜索；提交 video_import，获得 job_id，查询处理状态，完成后读取证据并回答。重试同一请求不重复 import；库/任务隔离有效。若启用远程 HTTP，再以实际远程客户端验收认证、并发身份和重连。

此 MVP 可供实际 Codex 和其他 Agent 使用。调用者取 rag_search 的证据，用当前模型回答；若启用可选 rag_ask，也可获得服务端答案。

### 7.2 前置条件与落点

依赖 Phase 3 和 Phase 2 的非 Telegram 身份/任务字段；需要一个实际客户端及确定的受保护访问路径。落点：`mcp_server.py`、`mcp_contracts.py`、`mcp_support.py`、拟新增 remote profile/共享服务/授权模块、配置、示例与 MCP 测试。

### 7.3 开发任务

| ID | 开发内容 | 验证/产物 |
| --- | --- | --- |
| VR4-01 | 新增 Codex extended stdio profile；跨机器需要时增加 HTTP，保留旧两工具 schema | 旧客户端通过，实际 Codex 握手通过；remote 可选项单独验收 |
| VR4-02 | 实现 principal → actor/collections/scopes 映射及受保护连接配置 | 服务端确认身份；客户端不能自行伪造 actor、库权限或 Telegram 用户 |
| VR4-03 | 注册 rag_search / rag_status，返回可供 Codex 回答的证据；rag_ask 可选 | 上下文与 CLI 一致；状态只显示授权范围，不泄漏凭据或他人任务 |
| VR4-04 | 注册异步 video_import / ingestion_status；校验 URL/asset、目标库、幂等键与任务所有者 | 工具快速入队；无任意路径或内网下载；任务状态/重复请求正确 |
| VR4-05 | 注册 evidence_get，采用实际客户端支持的受控图片资源返回方式 | 图片可读取；类型/大小/路径校验与 collection 权限均有效 |
| VR4-06 | 验证协议版本、会话、并发身份隔离、超时/取消和标准错误；防止工具层重复回答 | MCP 真正可调用；中断 HTTP 不导致已入队任务重复执行 |
| VR4-07 | 给出 Agent 连接示例与完整调用报告；回归旧 MCP/OpenClaw 兼容入口 | 一个真实外部客户端通过；若只验证测试客户端则明确未验收目标 Agent |

### 7.4 MVP 工具契约

| 工具 | 最小输入 | 最小结果 | 权限 |
| --- | --- | --- | --- |
| rag_search | query、knowledge_base、limit；额外库需显式传入 | RetrievalBundle、证据引用 | read |
| rag_status | 授权范围内的库/服务选择 | 编码/索引/任务可用性 | read |
| rag_ask（可选） | query、明确库范围 | 服务端答案、引用、最多两项截图 ID | read + answer |
| video_import | 公共来源 URL 或受控 asset ID、目标库、幂等键 | job_id、accepted/duplicate | ingest |
| ingestion_status | job_id | 处理/索引/清理结果和失败/降级原因 | 所有者/管理员 |
| evidence_get | evidence_id | 受控图片内容或资源引用 | 对应库 read |

首个 Codex 部署使用同机 stdio 与受控本机权限；跨机器调用时再配置内网/VPN 或已有受保护入口，并验证身份/TLS。只有目标平台要求公开 OAuth 时才扩展该方案；该工作不默认包含在普通 token 兼容测试内。MCP 不用于传送巨大原视频或读取任意服务器路径；上传采用受控资产入口，成功处理后同样清理。

### 7.5 MVP 演示与完成标准

- [ ] 实际 Codex 完成握手、五项必需工具调用与基于证据的回答；rag_ask 和远程 HTTP 若启用则单独验收。
- [ ] 搜索/回答/证据与 Phase 3 相同，不再调用原视频。
- [ ] import 异步执行，同幂等键不会重复任务；错误/取消/重连结果明确。
- [ ] read 身份不能入库，越权 collection/job/evidence 均被拒绝；图、缓存、会话无泄漏。
- [ ] 图片返回方式被实际调用端识别；旧 stdio 仍只暴露原两个工具。
- [ ] 访问配置与示例不含真实 token，错误输出不含秘密或服务器内部路径。

**退出/回退：** 禁用扩展 profile，保留本地 CLI、worker 或已持久化知识；回退旧 stdio profile。若已启用远程入口则关闭监听。不能通过取消鉴权来绕过客户端连接问题。

## 8. Phase 5 Telegram 已取消

2026-10-04 按用户最新要求，将 Codex 作为主要入口，VR5-01–07 全部记 cancelled，不计入剩余工作量。Bot token、聊天授权、polling、update offset、附件转发和 Local Bot API 不再是开发或上线前置条件。

既有 Telegram/OpenClaw 兼容代码保留，当前不开发新 Bot。视频入口由 Phase 4 的扩展 MCP 与受控资产提交承接；任务状态主动查询，最终回答由 Codex 当前模型组织。Phase 6 直接依赖已验收的 Phase 0、1、1A、2–4。

## 9. Phase 6：旧库迁移、运行保障与 Linux 交付

**目标：** 将各阶段 MVP 组合为实际长期运行的服务，并验证已有数据的迁移与恢复。

### 9.1 独立 MVP

在包含人工编辑、旧摘要、缺失原视频、可用原视频的迁移样本库运行盘点与受控回填。生成 shadow generation，验证后切换并演练回退；用 systemd 重启/启动服务，Codex 经 MCP 仍可查到已导入知识，队列继续处理，托管目录没有已完成任务的原媒体残留。

该 MVP 可以先在小范围迁移库验收，不要求所有历史视频当天跑完。最终上线需通过全部适用任务和质量/恢复关口，不能因迁移样本成功就宣布全量回填已完成。

### 9.2 前置条件与落点

依赖 Phase 0、1、1A、2–4 的代码和实际验收，Phase 5 不再是前置条件。需要实际数据库/知识目录、可用旧视频位置和 Codex 配置。盘点确认当前可访问数据，未提供的历史部署不能默认存在。

落点：回填/迁移 CLI、核心/ingestion 迁移与恢复脚本、`src/rag_favorite/resources/systemd`、cleanup、诊断/健康检查、examples、部署说明和最终实施报告。

### 9.3 开发任务

| ID | 开发内容 | 验证/产物 |
| --- | --- | --- |
| VR6-01 | 盘点历史库、人工内容、transcript、证据、旧空间和原视频可用性 | 按 collection 输出迁移清单与缺源标记，先 dry-run 不改数据 |
| VR6-02 | 实现安全回填、人工 checksum/受管区/sidecar、旧摘要保护和单 worker 调度 | 不同主题均可做视觉；新任务在当前回填结束后优先；无每日计数上限 |
| VR6-03 | 建立完整 shadow generation 与切换流程；处理切换期间新任务/重放队列 | 文本/视频/图/encoder 一起切换；无漏写、重复或空间混杂 |
| VR6-04 | 演练索引/应用回滚，区分缺源视频与可重建文本，记录模型更换限制 | 旧索引可用，新增知识保留；不声称恢复已删除原视频 |
| VR6-05 | 配置 systemd、模型环境、分项健康检查、启动依赖和私有凭据路径 | 服务重启后恢复；数据库/编码/模型/MCP/Codex 分项状态真实 |
| VR6-06 | 实现媒体/证据孤儿与清理监测、备份策略、恢复演练和资源/费用报告 | 备份排除临时原媒体，保存知识/证据/索引配置；残留与积压可追踪 |
| VR6-07 | 用最终版本重跑真实中文质量、Codex/MCP、并发/权限及崩溃恢复剧本 | 满足统一验收矩阵；实际 Codex 和受控资产能力没有未说明缺口 |
| VR6-08 | 写部署/升级/迁移/回滚/用户使用说明与最终限制，更新任务状态 | 配置/命令真实可运行；未完成项明确列出，交付证据与版本对应 |

### 9.4 缺源记录的处理

普通 Markdown 和已保存派生文本可以本地重新编码。历史原视频不存在时，保留已得知识/旧证据并标记 source_missing/text_only；不能从 transcript 伪造视频向量。旧空间可保留，补齐或更换视频编码器需用户重新提供媒体。

回填和查询不自动重新下载已删视频。明确重新提供的来源才建立新的受管 import，完成后再次删源。历史用户备份不属于自动删除范围；新应用备份只保留知识、证据、索引及必要运行配置。

### 9.5 MVP 演示与完成标准

- [ ] 含四类历史状态的迁移样本通过，人工正文和旧摘要无损，冲突写 sidecar。
- [ ] shadow 切换/回滚通过，切换期间新任务没有漏写；缺源状态准确且不自动下载。
- [ ] systemd 启动/重启、模型队列、任务继续处理与 Codex MCP 恢复通过；远程入口若启用则单独复验。
- [ ] 备份恢复后能查询知识/截图，媒体清理重试有效，无 completed 任务视频残留。
- [ ] 最终 10 视频/60 标注质量关口和所有适用的全链路/权限/恢复剧本通过。
- [ ] 实际附件规模与目标 Agent 验收通过，未通过项不写全范围完成。
- [ ] 部署、资源/费用、迁移、回滚与限制报告齐全，任务/真实测试状态有证据。

**退出/回退：** 优先切回旧索引/encoder/图组合与兼容应用，保留新增知识表；停止新任务后再评估迁移恢复。已删除原视频不可通过应用回滚恢复，勿自动执行会丢知识的 down migration。

## 10. 跨 Phase 规格与统一验收矩阵

### 10.1 必须延续的配置与预算

实现入口的名称/命令在各 Phase 验证后记录，本文不提供假定已经存在的启动命令。所有配置例子必须标明实施版本。

- 视觉默认关闭；所有者启用后，各主题 ≤600 秒的视频均可执行视觉，不再要求 cooking gate；无语音 0.5 秒、有转录/字幕 1 秒抽帧。
- worker concurrency=1；本地视频编码初始 batch=1；设备/队列按 Phase 0 实测选择，不静默云 fallback。
- 失败媒体默认 TTL 72 小时，可配置；完整成功即清理，不等待 TTL。
- 远程请求/图像额度按计划批次数和帧数设置，预留有限重试及旧账本用量，覆盖 600 秒无语音视频的 1200 帧；旧固定 100 图限制已被替换。达到额度则停止并保留不完整状态；价格未知时不声称已执行美元停止阈值。
- 本地 embedding 不计 API 费用，但记录耗时、RAM/VRAM、段数和磁盘；ASR、文字 LLM、VLM 与入库/问答/回填分别统计。缺价格信息标 unknown。
- 活跃 generation 必须包含匹配的文本/视频/图与 encoder 配置；空间身份不仅是维度。
- 模型服务绑定 loopback，远程仅经受保护的 MCP 入口；MCP token、模型 key 和授权信息存私有配置。

按用户最新要求，累计视觉处理时间预算已取消，`visual_budget_seconds=0` 表示无限制，默认也为 0。网络请求仍保留断线超时；耗时与请求用量继续记录，失败和不完整结果仍不能伪装成成功。

### 10.2 验收矩阵

| 验收主题 | 首次验证 | 最终复验 | 通过条件 |
| --- | --- | --- | --- |
| 本地编码/资源 | Phase 0 | Phase 6 | 权重下载后离线编码有效；设备/耗时/峰值有记录，无云 fallback |
| 文本空间与兼容 | Phase 1 | Phase 6 | query/document 预处理正确，错空间拒绝，普通文本/旧 stdio 可用 |
| 视觉权限与字幕优先 | Phase 2 | Phase 6 | 有字幕不额外 ASR；无语音可分析画面；不按主题阻断视觉 |
| 密集采样与时长阈值 | Phase 2 | Phase 6 | 无语音 0.5 秒、有转录/字幕 1 秒；600 秒包含，>600 秒所有视觉调用为零；逐帧记录完整且批次中断可恢复 |
| 短暂屏显和数字 | Phase 2 小样本 | Phase 3、6 | 一次复核/unknown/冲突准确；可读数字/单位符合真值 |
| 质量基准 | Phase 3 | Phase 6 | ≥10 个不同来源视频、≥60 标注，覆盖不同主题，正确证据 Top 3 ≥90%；负例通过 |
| 删源与无重下载 | Phase 2 持久化；Phase 3 查询 | Phase 6 | 完成后清理全部托管原媒体；重启后回答仍有出处/时间/截图 |
| 发布/索引/删除恢复 | Phase 2 | Phase 6 | 中断可重放；不丢证据、不误报完成、不删活跃文件 |
| 库/身份隔离 | Phase 3、4 | Phase 6 | 文本/图/视频/缓存/证据/任务均不能越权 |
| 检索/回答分离 | Phase 3 | Phase 4、6 | rag_search 无最终回答调用，QA 一次最终生成，证据可追溯 |
| 实际 Codex | Phase 1A、4 | Phase 6 | 握手、五项必需工具、证据回答、幂等、图片和重启通过；remote 可选 |
| 受控大文件资产 | Phase 2、4 | Phase 6 | 文件提交、任务绑定和托管副本清理；不通过 MCP JSON 传视频内容 |
| 旧库与人工编辑 | Phase 2 局部 | Phase 6 | 保留人工正文/旧摘要，sidecar、缺源、优先级和回滚正确 |
| Linux 运行保障 | Phase 6 | 发布前 | 分项健康、重启/备份恢复、媒体残留和费用/资源记录完整 |

### 10.3 七个端到端验收剧本

| 剧本 | 操作顺序 | 预期结果 |
| --- | --- | --- |
| E2E-01 文本库 | 本地导入两库 Markdown → 搜索指定库 → 错空间 → 回退 | 正确命中，库隔离/错空间拒绝，回退可用 |
| E2E-02 cooking 删源问答 | 导入含未口述配方视频 → 持久化/清理 → 重启/禁止源下载 → 提问 | 正确配方、时间/出处/两图；未知细节不猜，无原媒体 |
| E2E-03 跨主题与无语音 | 非 cooking / 无语音视频 → 统一入库 | 不依赖类别，视频可抽帧；无语音不编造转录；证据不足明确保留 |
| E2E-04 处理恢复 | 发布/DB/索引/删除处中断 → 重启/重试 → 检查状态/文件 | 无假 completed，无活跃媒体误删；证据完整，清理幂等 |
| E2E-05 远程调用与隔离 | Agent A 搜索/入库/证据 → 重复入库 → Agent B 越权 | 正常调用有效，无重复 job，越权工具/库/任务/图片拒绝 |
| E2E-06 Codex 全流程 | Codex 提交资产/链接 → 明确库 → 查任务 → 删除托管媒体 → 搜索/读证据/回答 → 重启 | 实际客户端闭环，无重复入库，出处/时间/最多两图正确；不读原视频 |
| E2E-07 迁移与回滚 | 含人工/缺源样本回填 → 新任务优先 → shadow 切换 → 回退/备份恢复 | 人工内容保护，缺源不造向量，代际一致，知识/截图恢复 |

## 11. 输入准备与开始顺序

Phase 0 开始前核对本机依赖和样例；真实验收需要的凭据通过私有配置提供，编写接口、离线测试和模板不必等待所有凭据。

| 输入 | 首次需要 | 缺失时可继续做什么 |
| --- | --- | --- |
| 模型下载与本地权重 | Phase 0 | 适配接口、空间契约和离线校验；真实本地编码保持 not_run |
| 2 cooking + 1 非 cooking 样例，随后扩展 10/60 标注 | Phase 0；Phase 3 完整基准 | 用合成样例验证时间/恢复，但不宣称真实质量达标 |
| 分类/抽取/回答 LLM、VLM、必要 ASR 配置 | Phase 0/2/3 | 本地编码和协议测试；真实抽取/QA 保持 not_run |
| 实际 Codex 运行环境与连接方式 | Phase 1A；Phase 4 扩展验收 | 同机 stdio 优先；跨机器需要时再配置远程身份；无 Telegram 凭据要求 |
| 实际数据库/知识目录与旧视频位置 | Phase 6 | 隔离迁移样本与 dry-run；实际旧库切换保持 not_run |

Phase 1A 已完成。ImageBind 已部署并实测，Phase 2–4 核心实现已运行，下一步依次完成真实样本、数字/单位复核、故障恢复和迁移样例，再完成 10/60 标注关口。实际配置与入口见 [本地配置文档](research/imagebind-local-setup.md) 与 [实施记录](implementation/videorag-phases-0-6.md)。

## 12. 开发进度记录模板

阶段记录采用以下结构；任务表中的 ID 与本文一一对应，方便后续继续开发：

~~~text
Phase：N / 名称
日期、应用版本、配置/模型/索引空间版本：
任务进度：VRN-xx — planned / in_progress / implemented / verified / blocked
本阶段 MVP：入口、样例、演示步骤、结果
验证：离线测试 / 数据库 / 本地模型 / 真实 API / Codex / 远程 Agent（若启用）
每类验证状态：not_run / passed / failed；附结果位置
恢复/回退：执行步骤和结果
媒体清理：托管原媒体清单、证据读取检查、残留结果
质量与资源：命中/数字标注，耗时、RAM/VRAM、费用和未知项
限制或缺失输入：具体原因、影响范围、下一项可继续任务
下一阶段前置条件：已满足 / 未满足
~~~

## 13. 当前检索流程：总结入库，按需读取原文

当前统一知识库采用：视频/笔记 → `knowledge.md` 原始知识记录稿 → CCR 生成并校验 `summary.md` → 总结文本向量入库。普通检索只查询总结，不再混入原始分段、作者正文、关键词、图谱或 ImageBind 排名；旧数据保留，但没有成功发布总结的条目暂不参与检索。

- `rag_search(query, knowledge_base="all", limit=5)` 返回最多五篇不同文档及完整总结；按余弦相似度降序排列。同一总结的多个分块只占一个名额，以最高分块相似度作为文档分数。`limit` 为 1–5；旧参数 `visual_query` 在此流程中拒绝使用。
- 回答 LLM 先查看这五篇总结，选择本次问题需要阅读的文档，再调用 `document_read(document_id, version=metadata.version)`。返回完整总结和 `original_document`，原文指知识记录稿。若 `complete=false`，使用 `next_offset` 和相同 `version` 继续读取，直到完整；版本发生变化时重新检索。
- 校验要求数字和引用能追溯到原始证据，不确定及冲突参数明确标记；不使用常识补齐。仅总结进入 embedding，按本地模型实际 UTF-8 字节限制分块。生成与索引有独立检查点，索引失败不会重新调用总结模型，也不会替换已发布版本。
- 历史补总结复用保存的初稿和证据，不重新下载、转录或视觉分析。独立 `rag-favorite-summary-worker.service` 串行处理持久队列，服务重启恢复未完成任务；失败条目保留错误状态，可明确重试。GUI 的全局暂停也阻止新的补总结任务开始。

在项目目录运行：

```bash
bash examples/video_runtime.sh cli summary-backfill
bash examples/video_runtime.sh cli summary-status
bash examples/video_runtime.sh cli search '寿司醋怎么调，寿司米怎么做？'
# 仅在需要重试已失败的补总结任务时使用：
bash examples/video_runtime.sh cli summary-backfill --retry-failed
journalctl --user -u rag-favorite-summary-worker.service -n 30 --no-pager
```

MCP 服务通过 `RAG_VIDEO_CONFIG` 选择统一总结流程；标准 MCP 入口设置此变量后也使用同一流程。已有 stdio 会话需要重新连接后才能加载新工具与逻辑。查询只返回检索证据，由调用工具的回答 LLM 负责选文档、读取原文和组织回答。上述排序不代表经人工标注验证的回答质量。

总结内部引用采用确定性证据编号，模型选择编号后由系统解析原始引用，避免重抄原文时改变繁简体、标点或换行。`evaluate` 现在统计 `top5_document_recall`；已有分段标注按视频 ID 映射到对应文档，标注仍须人工确认。
