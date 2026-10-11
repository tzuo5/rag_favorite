# VideoRAG 接入核查记录

核查日期：2026-10-03，America/Chicago。项目目录：`/home/tzuo5/rag_favorite`。本文记录已核查的事实；实施方案见 [VideoRAG、本地 embedding 与远程 MCP 修改计划](../videorag-local-mcp-plan.md)。没有在本次核查中安装模型或部署服务。

**这是初始核查快照。** 后续已在用户目录准备真实文本模型/数据库并验收新 Phase 1；最新运行事实见 [实施记录](../implementation/videorag-phase-1.md)。下文保留核查时的原始状态。

## 1. 本地项目与配套远程仓库

用户确认的配套仓库是 [tzuo5/rag_favorite](https://github.com/tzuo5/rag_favorite)，用户确认的视频项目是 [HKUDS/VideoRAG](https://github.com/HKUDS/VideoRAG)。

| 核查项 | 结果 |
| --- | --- |
| Git 远程 | 仅发现 origin，地址为 https://github.com/tzuo5/rag_favorite.git |
| 本地 HEAD / 远程 HEAD | 均为 `ab03f923b26447ea9dd87bf455c33d28b7d576fb`，远程通过 git ls-remote 核对 |
| 文档中的 VideoRAG | 已有 devplan.md、Development MVP.md、docs/research/video-rag-deployment.md 引用或调研 |
| 运行代码与依赖 | 未发现 VideoRAG 集成、vendored 源码或 Git submodule；根虚拟环境未安装 VideoRAG、ImageBind、LightRAG 或 RAGAnything |
| 原有备份 | 已检查的备份代码与依赖中也没有发现 VideoRAG 接入；备份不是当前运行配置 |
| 工作区修改 | 已有未提交的 Phase 1 开发修改，需要保留；远程 HEAD 相同不表示这些修改已经上传 |

结论：**曾经研究过 VideoRAG，尚未配置或接入。** 本结论覆盖当前工作区、已核查备份和上述远程默认版本；不代表已遍历所有历史提交和无关远程分支。

## 2. 当前机器和运行状态

| 项目 | 核查结果 | 对实施的影响 |
| --- | --- | --- |
| GPU | NVIDIA GeForce RTX 4060 Laptop GPU，8188 MiB 显存 | 必须实测 ImageBind 的内存和延迟，不能直接套用 24 GB 参考配置 |
| 内存 | 系统可见约 30 GiB RAM，8 GiB swap | 模型生命周期、图加载和缓存都需要限额 |
| Python | 系统 3.14.4；项目根 .venv 为 3.12.14 | 老版视频模型依赖宜先在独立环境验证 |
| FFmpeg / ffprobe | 未发现可执行程序 | 视频实测前需安装 |
| Ollama / Docker / gh | 未发现可执行程序 | 本地文本编码服务、数据库部署方式尚需准备；gh 不影响实现本身 |
| 模型依赖 | 根 .venv 未发现 torch、transformers、sentence-transformers | 没有本地 embedding 运行验收证据 |
| MCP SDK | 根 .venv 为 mcp 1.30.0，提供 Streamable HTTP 相关 API | 现有应用仍只启动 stdio，SDK 能力不等于服务已部署 |
| Telegram / OpenClaw | 未发现 Telegram 凭据、OpenClaw 配置或相关运行服务 | 当前 Telegram 链路没有可用性证明，不能报告为可用 |
| 应用配置 / 数据库 | 用户配置文件、ingestion .env 与数据库监听未发现 | 真实数据库和模型 API 验收尚未完成 |

[Linux Phase 1 实施记录](../implementation/linux-phase1.md) 中的测试是历史离线结果；mock 模型测试与跳过的数据库测试不能当作真实视频、Telegram 或本地 embedding 验收。本次没有重新运行这些测试。

## 3. 上游版本与关键源码事实

本次只读核对 HKUDS/VideoRAG 的 `VideoRAG-algorithm`，固定提交为 `c412a093a820ef7a0e0dda31076ed871136198b3`，提交时间为 2026-03-18。未安装该代码，未下载模型权重，未引入 Vimo 桌面客户端。

| 源码事实 | 修改含义 | 来源 |
| --- | --- | --- |
| 默认逻辑片段为 30 秒，入库描述采样 5 帧，查询细化采样 15 帧 | 短暂出现的中文配方需要额外的入库抽帧与事实提取 | [videorag.py](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/videorag.py) |
| 查询通过 video_path_db 重新打开原视频，生成与问题有关的细化描述 | 原视频删掉后，原版查询路径不能直接使用；应改为读取入库时持久化的事实与证据 | [caption.py](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/_videoutil/caption.py)、[_op.py](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/_op.py) |
| 查询包含文本、图谱和视频召回，也直接调用最终回答模型；only_need_context 字段未在该查询函数中实现返回分支 | rag_search 需要真正抽离结构化检索函数，不能仅设置一个参数就声称返回纯上下文 | [_op.py](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/_op.py)、[base.py](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/base.py) |
| 视频向量与检索问题由 ImageBind 对应的视觉、文本编码器生成；上游写入和查询会重新加载模型并调用 .cuda() | 与普通文本 embedding 分开存储；增加共享模型实例、设备选择和批量控制 | [向量存储](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/_storage/vdb_nanovectordb.py)、[feature.py](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/_videoutil/feature.py) |
| 使用 JSON KV、nano-vectordb 与 NetworkX；部分代码直接读取存储 ._data | 生产环境继续 PostgreSQL/pgvector，需要存储适配和调用点修改，不能仅替换类名 | [videorag.py](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/videorag.py) |
| 视频标识来自文件名；切片逻辑有整数时长转换；部分区间处理使用 eval | 使用内容/来源标识、真实时间戳和类型校验，避免同名碰撞与时间损失 | [videorag.py](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/videorag.py)、[split.py](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/_videoutil/split.py)、[_op.py](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/_op.py) |

上游 README 给出的参考环境包含 Python 3.11、旧版模型依赖与 24 GB GPU，并说明主要在英文环境测试。这是参考配置，不能推导出 8 GB 必然失败或必然能流畅运行。[固定版本 README](https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/README.md)

## 4. 模型、协议和来源限制

- 本地文本编码候选为 Qwen3-Embedding-0.6B，默认输出 1024 维；查询指令与文档输入应按模型要求分别处理。[Qwen 模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)、[Ollama embed API](https://docs.ollama.com/api/embed)
- 本地视频编码基线为 ImageBind；其官方示例支持指定设备，模型卡提示文本训练以英文为主。中文视频问题的视觉召回必须单独验证。[ImageBind](https://github.com/facebookresearch/ImageBind)、[模型卡](https://github.com/facebookresearch/ImageBind/blob/main/model_card.md)
- ImageBind 官方许可证为 CC-BY-NC 4.0。应分别记录 VideoRAG 代码、模型和依赖许可证，不能将整套系统笼统标为 MIT；上游 VideoRAG 对模型许可证的文字摘要不能覆盖模型自己的许可文件。[ImageBind LICENSE](https://github.com/facebookresearch/ImageBind/blob/main/LICENSE)
- 远程 MCP 计划采用 Streamable HTTP；需按实际调用端与所用 SDK 验证协议兼容。MCP 提供工具调用入口，Telegram 的接收消息和回复仍需 Bot 适配器。[MCP 架构](https://modelcontextprotocol.io/docs/learn/architecture)、[传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- Telegram 云端 Bot API 的 getFile 下载存在 20 MB 限制；大附件需另外规划 Local Bot API Server 或受控上传入口，不能默认任何转发视频均能下载。[getFile](https://core.telegram.org/bots/api#getfile)、[Local Bot API Server](https://core.telegram.org/bots/api#using-a-local-bot-api-server)

以上外部项目能力与约束来自官方文档或固定版本源码。CPU/GPU 调度、数据库结构、删除策略、工作量和验收方案是本项目的工程设计，尚待实施验证。
