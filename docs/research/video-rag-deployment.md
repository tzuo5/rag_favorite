# HKUDS/VideoRAG 部署配置与限制

调研日期：2026-10-01。对象为官方仓库 `main` 下的 `VideoRAG-algorithm` 与 `Vimo-desktop`；模型名称为代码中的默认值，不代表当前最优选型。用户目标：Linux、32 GB 系统内存、RTX 4060 Laptop（按标准 8 GB 显存考虑），处理约 3 分钟中文视频中的人物动作、屏幕文字和做饭步骤。本文为源码与文档调研，没有安装模型、运行推理或测量性能。

## 需要自行准备什么

无需训练模型或自行标注训练集；官方使用已有模型权重与模型 API。原版需要本地视频文件、模型权重和模型服务配置。[原版安装及 Quick Start](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/README.md)

| 模型角色 | 原版默认配置 | 自己需要准备的内容 |
|---|---|---|
| 文本 LLM | `gpt-4o` 与 `gpt-4o-mini` 两个角色；Quick Start 使用同一个 `gpt-4o-mini` 服务承担两个角色 | 云 API key，或本地 LLM 服务；两个角色可以共用一个模型 |
| VLM：视频片段描述 | 本地 `MiniCPM-V-2_6-int4` | 下载权重；替换其他 VLM 时修改加载和调用适配 |
| 文本 embedding | `text-embedding-3-small`，1536 维；Ollama 示例使用 `nomic-embed-text`，768 维 | embedding API 或本地模型，匹配向量维度和索引 |
| 视频与文本共同空间的 embedding | 本地 ImageBind huge，视频索引为 1024 维 | ImageBind 权重；它独立于上面的文本 embedding |
| 语音识别 ASR | 本地 `faster-distil-whisper-large-v3` | Whisper 权重；中文视频要调整原版 ASR 配置 |

文本模型与 embedding 默认值、两个 LLM 角色及 Ollama 适配见 [原版 `_llm.py`](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/_llm.py)。VLM 加载路径与视频索引维度见 [原版 `videorag.py`](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/videorag.py)。ASR 权重加载见 [原版 `asr.py`](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/_videoutil/asr.py)。

ImageBind 用视觉编码器编码视频、用自身文本编码器编码检索问题，让两者可以比较相似度。**工程判断：**普通文本 embedding 不能直接替代此功能；替换多模态 embedding 需要适配编码调用、维度并重建对应索引。[原版 `feature.py`](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/_videoutil/feature.py)

还需准备 Python 与视频处理依赖、模型权重目录、原视频和持久工作目录。原版默认使用 JSON、nano-vectordb 和 NetworkX 存储，并不要求另建 PostgreSQL、Neo4j 或托管向量数据库。**工程判断：**若接入现有 PostgreSQL/pgvector 应用，需要实现存储适配或导出处理结果。[原版存储默认类](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/videorag.py)

## 原版与 Vimo 的差别

**Vimo 默认不是全本地模型管线。**其 `_llm.py` 的默认文本 LLM 为 `gpt-4o-mini`，文本 embedding 为 `text-embedding-3-small`，VLM 为通过 DashScope 调用的 `qwen-vl-plus-latest`。OpenAI 与 DashScope 分别有 API key、base URL 配置。[Vimo `_llm.py`](https://github.com/HKUDS/VideoRAG/blob/main/Vimo-desktop/python_backend/videorag/_llm.py)

Vimo 后端实际工作进程从用户配置读取 `analysisModel`、`processingModel` 和 `caption_model`，文本 embedding 名称与 1536 维度仍写在代码中；ImageBind 由本地共享服务加载权重。**工程判断：**使用别的 embedding 模型，不能只改聊天模型名称；需要改相应配置与调用。[Vimo `videorag_api.py`](https://github.com/HKUDS/VideoRAG/blob/main/Vimo-desktop/python_backend/videorag_api.py)

Vimo 的 ASR 也通过 DashScope `Recognition` 在线识别，使用配置中的 `asr_model`，代码提供中文、英文、日文提示。音频文件经 API 调用发送；VLM 将抽帧编码为 JPEG 后发送至模型 API。因此默认运行方式包含音频、图像和文本的外部模型调用与费用。[Vimo `asr.py`](https://github.com/HKUDS/VideoRAG/blob/main/Vimo-desktop/python_backend/videorag/_videoutil/asr.py)、[Vimo `caption.py`](https://github.com/HKUDS/VideoRAG/blob/main/Vimo-desktop/python_backend/videorag/_videoutil/caption.py)

Vimo README 说明源码运行已在 macOS M1 验证，打包版本仍为 Beta 规划，Windows/Linux 发布尚列为后续工作。这不能作为 Linux 4060 的兼容性或速度验证。[Vimo README](https://github.com/HKUDS/VideoRAG/blob/main/Vimo-desktop/README.md)

## 对该机器与视频用途的限制

1. **原版没有 8 GB 显存可直接运行的证据。**README 的参考配置是 RTX 3090、24 GB，表述是该容量足够，不是声明最低显存。原版视频向量存储在写入和查询时都重新创建 ImageBind 并直接调用 `.cuda()`。**工程判断：**小显存部署需要处理设备选择、模型加载生命周期、批量大小及 VLM/ASR/embedding 的阶段调度；32 GB 系统内存不等于 32 GB 显存。[原版 README](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/README.md)、[原版 `vdb_nanovectordb.py`](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/_storage/vdb_nanovectordb.py)

2. **默认抽帧较稀疏。**每 30 秒片段在入库时使用 5 帧，查询细看时使用 15 帧。**工程判断：**短暂出现的文字、调料克数、倒入某种材料的瞬间可能不在采样帧内，增加帧数又会增加处理成本。[原版采样配置](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/videorag.py)

3. **默认视觉管线没有独立 OCR。**原版 caption 将帧统一缩放至 1280×720，让 VLM生成英文描述，没有单独保存 OCR 字符、文字区域或验证配方数字的步骤。**工程判断：**对“屏幕上的文字”要求高时，应额外做 OCR、变化检测和证据帧保存；VLM 的一般描述不能保证逐字准确。[原版 `caption.py`](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/_videoutil/caption.py)

4. **中文需要适配。**原版官方只声明英语环境测试，建议多语言场景修改 WhisperModel；默认视觉提示词也要求英文描述。**工程判断：**中文食材名称、口音、字幕数字和检索语义需要单独核对，不能直接沿用英语实验结论。[原版语言说明](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/README.md)、[原版提示词](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/_videoutil/caption.py)

5. **建库和问答都存在模型成本。**入库包含转录、描述、向量化、实体关系提取；检索后还会重新读取原视频并生成针对问题的详细描述。**工程判断：**不能只用最终 LLM 的 tokens/s 推算整条链路；多次提问仍有 VLM 成本，原视频须保持可访问。[入库流程](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/videorag.py)、[查询时重新描述](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/_videoutil/caption.py)

6. **维护与集成需要开发工作。**原版安装文档固定了 PyTorch 2.1.2、Transformers 4.37.1 等版本；本地 VLM 加载与 `.chat()` 调用绑定 MiniCPM。**工程判断：**接入新的 Qwen VLM、Ollama API 或现有应用通常需要适配代码和依赖环境；这不是填一个模型名称即可完成的替换。[依赖版本](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/README.md)、[MiniCPM 调用](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/_videoutil/caption.py)

## 针对该用途的判断

官方定位强调多段长视频的知识抽取与检索问答。[项目说明](https://github.com/HKUDS/VideoRAG)

**工程建议：**约 3 分钟中文做饭视频，优先形成带时间戳的 ASR + OCR + 关键帧动作描述，再存入已有文本检索系统并让 LLM 汇总。这样更容易保留食材名称、数字和动作证据，也更方便按阶段控制 8 GB 显存。如果以后需要跨大量长视频寻找没有字幕的视觉场景，再评估加入 ImageBind 多模态检索与图谱。上述建议不是该机器上的实测结论；目前没有依据给出整套 VideoRAG 处理 3 分钟视频的可靠秒数。

## 补充：ImageBind 全本地与远端部署

**官方事实：**ImageBind 自身支持 CPU，官方示例在没有 CUDA 时选择 `cpu` 并执行 `model.to(device)`。因此“全部本地”可以采用 CPU 与 GPU 分工，不能理解为所有模型同时驻留 8 GB 显存。[ImageBind 官方使用示例](https://github.com/facebookresearch/ImageBind)

`imagebind_huge` 创建完整六模态模型，包含 32 层、1280 宽的视觉编码器以及 24 层、1024 宽的文本编码器。**结构估算：**它是约十亿参数量级的模型，仅 FP32 参数就属于数 GB 量级；这不是下载文件字节数、推理峰值内存或该笔记本的实测值。本次没有获得官方 checkpoint 的精确字节数，不把网上常见的约 4.5 GB 下载描述当作已核实硬件指标。[ImageBind 模型构造源码](https://github.com/facebookresearch/ImageBind/blob/main/imagebind/models/imagebind_model.py)

**工程判断：**32 GB RAM 使 CPU 运行 ImageBind 成为可评估的方案；GPU 可用于需要推理生成的 VLM，其他模型按阶段运行。原版 VideoRAG 写入与查询时都重新加载 ImageBind 并调用 `.cuda()`，需要改设备选择、缓存模型或用独立 embedding 服务，才能实现该调度；不能原样宣称全本地稳定可用。[VideoRAG 的 ImageBind 加载](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/_storage/vdb_nanovectordb.py)

**远端部署的约束：**把视频 embedding 放到另一台机器时，视频与问题必须继续使用匹配的 ImageBind 视觉/文本编码器、权重和预处理，保持共同向量空间。普通云端 VLM 的文字回答，或其他文本 embedding API，不能直接拿来查询原来的 ImageBind 视频索引；若换编码器，需要重新编码对应数据。[VideoRAG 的视觉与查询编码](https://github.com/HKUDS/VideoRAG/blob/main/VideoRAG-algorithm/videorag/_videoutil/feature.py)

官方模型卡认为 ImageBind 文本编码器主要适用于英语。**工程判断：**中文视觉检索应考虑将检索问题转为英文，或另选支持中文的匹配多模态编码器并重建索引；中文文本 RAG 的 embedding 可独立保留。[ImageBind 模型卡](https://github.com/facebookresearch/ImageBind/blob/main/model_card.md)

**部署优先级判断：**若接受 CPU 与 GPU 分阶段运行，全部本地在架构上现实，仍需适配并检查实际峰值。若要求更高并发或速度，ImageBind 适合移到运行同模型的远端服务；若主要问题是动作或小字识别质量，优先将困难帧交给更强的云端 VLM。当前没有该 4060 Laptop 上的吞吐数据，因此没有给出处理 3 分钟视频所需秒数。

## 补充：OpenRouter embedding 可用性与费用

核对日期：2026-10-01，仅查询公开目录、文档与价格，没有发送付费推理请求。当前官方 embedding API 目录返回 33 个模型、无下一页。`qwen/qwen3-embedding-0.6b` 有介绍页面，但当前可用目录未列出，页面也没有价格或供应商列表；预算应选当前目录中的型号。Cohere Embed v4、Qwen3-VL embedding 和 ImageBind 同样未在此目录列出。[实时 embedding 目录](https://openrouter.ai/api/v1/embeddings/models)、[Qwen 0.6B 页面](https://openrouter.ai/qwen/qwen3-embedding-0.6b)

| 文本 embedding | 当前最低标价，美元/百万输入 tokens | 说明 |
|---|---:|---|
| `perplexity/pplx-embed-v1-0.6b` | 0.004 | 目录中最低付费文本价格；32K 上下文 |
| `qwen/qwen3-embedding-8b` | 0.01 | 支持多语言，中文用途的优先候选 |
| `baai/bge-m3` | 0.01 | 多语言，1024 维稠密向量候选 |
| `qwen/qwen3-embedding-4b` | 0.02 | 其托管价格高于 8B，不能按模型大小推断 API 价格 |
| `openai/text-embedding-3-small` | 0.02 | 与 VideoRAG 原默认配置一致 |

价格取自[实时 embedding 目录](https://openrouter.ai/api/v1/embeddings/models)。**选型判断：**成本优先的中文检索可先选 Qwen3-Embedding-8B；若只追求最低付费价格，可评估 Perplexity 0.6B。这里没有将“API 可调用”当作已验证中文配方检索质量。[OpenRouter 官方多语言与低成本指南](https://openrouter.ai/blog/insights/best-embedding-models-2026/)

可用的付费多模态候选包含 `voyageai/voyage-multimodal-3.5`：文本 $0.12/百万 tokens、图像 $0.60/十亿 pixels；以及 `google/gemini-embedding-2`：文本 $0.20/百万 tokens、图像 $0.45/百万图像 tokens。实际预算需使用供应商计费后的像素或 token 数，不能只以视频时长推断。[Voyage 模型与价格](https://openrouter.ai/voyageai/voyage-multimodal-3.5)、[Gemini 模型与价格](https://openrouter.ai/google/gemini-embedding-2)

**已证实的 OpenRouter 接入方式是文本、图像以及两者混合的 embedding。**官方指南验证了 Gemini Embedding 2 与 Voyage Multimodal 3.5 的这些请求，并明确未把视频作为已验证推荐。Gemini 模型页列出视频输入和视频价格，Voyage 描述也提到视频能力，但本次未获得具体视频 embedding 请求的端到端接入证据。可据此实现抽帧的图像检索；不应直接宣称能原样上传 3 分钟视频完成原版 ImageBind 的视频编码。[官方已验证范围](https://openrouter.ai/blog/insights/best-embedding-models-2026/)

**工程判断：**若把 ImageBind 换为上述模型，需修改视觉索引适配、统一检索问题与帧的 encoder 配置，并重新生成整个视觉索引；即使输出同为 1024 维，也不共享原来的向量空间。帧级检索与原版视频片段 embedding 的时序语义也不同。官方指南同样要求跨模型家族更换时重建索引。[向量空间兼容性说明](https://openrouter.ai/blog/insights/best-embedding-models-2026/)

免费候选有 `nvidia/llama-nemotron-embed-vl-1b-v2:free`（文本+图像）以及免费文本 embedding。OpenRouter 官方指南说明其测试账户因免费模型训练隐私设置无法调用免费路由，并建议不要将免费路由作为大批量生产入库的唯一路径。因此此处不把免费模型当作稳定开销为零的保证。[免费路由限制](https://openrouter.ai/blog/insights/best-embedding-models-2026/)

## 补充：四类模型的本地资源与上云顺序

不计算文字 LLM。**工程结论：**单视频离线处理，使用量化小 VLM、CPU/GPU 分工、串行调度，四类模型全部本地运行是现实的部署方向；原版默认模型组合全部同时驻留 8 GB GPU，不适合作为部署方案。这是基于模型与运行时资料的可行性判断，尚非服务器实测。

- **VLM：**MiniCPM-V-2_6-int4 官方给出约 7 GB GPU 内存的使用说明，不能把它理解为多帧视频峰值上限。Ollama 的 Qwen3-VL 2B Q4 与 4B Q4 下载包分别为 1.9 GB、3.3 GB，下载大小也不是推理显存。工程起点可选 2B Q4 独占 GPU；4B Q4 作为需要控制分辨率、帧数与上下文的候选。[MiniCPM 官方模型卡](https://huggingface.co/openbmb/MiniCPM-V-2_6-int4)、[Qwen 2B Q4](https://ollama.com/library/qwen3-vl:2b-instruct-q4_K_M)、[Qwen 4B Q4](https://ollama.com/library/qwen3-vl:4b-instruct-q4_K_M)
- **文本 embedding：**Ollama 的 `qwen3-embedding:0.6b` 为 Q8_0、596M 参数、639 MB 下载包。Qwen 官方说明 0.6B 支持最多 1024 维和 CPU 部署。工程建议继续本地运行，可用 CPU 或在 VLM 卸载后使用 GPU，小批次与有界文本长度能控制资源；不建议为显存问题优先迁到云端。[Ollama 模型元数据](https://ollama.com/library/qwen3-embedding:0.6b)、[Qwen 官方模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- **ASR：**Faster-Whisper 支持 CPU int8 与 GPU int8。官方示例中 small/CPU/int8 使用 1477 MB RAM；large-v2/GPU/int8 使用 2926 MB VRAM，分别是 i7-12700K 与 RTX 3070 Ti 8 GB 上的基准，不能作为该 4060 的测量。工程建议 small 多语言模型运行于 CPU，或更大多语言模型单独使用 GPU。[官方基准与用法](https://github.com/SYSTRAN/faster-whisper)
- **现有代码：**`ingestion/backend/transcriber.py` 默认 `base`、`cpu`、`int8`、2 CPU threads；环境变量可覆盖。这说明当前代码已有 ASR 避开 GPU 的路径，不能据此断言服务器实际环境变量与质量。
- **调度：**Ollama 支持 `keep_alive=0` 卸载模型；并发请求会扩大上下文内存。工程上按整个视频的阶段进行模型切换，减少逐帧反复加载；Ollama 的卸载不会替代独立 PyTorch/CTranslate2 进程的释放。[Ollama 内存与并发说明](https://docs.ollama.com/faq)

**按当前人物动作与屏幕文字用途，若只把一个环节交给云端，优先 VLM：**较强云端模型可用于复杂动作、模糊文字或多个画面的关系识别，并释放本地 GPU；这属于需要素材评估的质量收益判断。Qwen 0.6B 文本 embedding 与 ASR 可保留本地；ImageBind 先尝试 CPU 或独占 GPU 分时。若吞吐瓶颈明确出在 ImageBind，再考虑远端同模型服务；若瓶颈出在转录速度或口音识别，再考虑云端 ASR。云端 VLM 无法补回根本没有抽到的帧，仍须保留 OCR 与抽帧策略。

## 最新方案：除 CCR 文字 LLM 外，模型全部走 OpenRouter

用户最新偏好为成本优先：文字 LLM 通过 CCR 调用 GPT 6 Luna，其他模型使用 OpenRouter。以下为 2026-10-01 公开价格预算，排除 CCR 的文字 LLM 开销、充值手续费、税、存储和网络费用，没有进行付费推理或测量准确率。它估算一次入库的视觉描述、画面文字提取、转录及向量化；以后查询时重新调用 VLM 会另收费。

### 付费低成本组合

| 角色 | OpenRouter model ID | 预算采用的单价 |
|---|---|---|
| VLM，动作描述及画面文字提取 | `qwen/qwen3.7-flash` | 输入 $0.03/百万 tokens，输出 $0.13/百万 tokens，限单次输入低于 32,000 tokens |
| ASR | `qwen/qwen3-asr-0.6b` | $0.00000333/音频秒 |
| 文本 embedding，最低付费预算候选 | `perplexity/pplx-embed-v1-0.6b` | $0.004/百万 tokens |
| 视觉检索，以视频抽帧图像构建索引 | `google/gemini-embedding-2` | 图像 $0.45/百万 tokens；Google 折算约 $0.00012/张图像 |

VLM 单价来自 [Qwen3.7 Flash 模型页](https://openrouter.ai/qwen/qwen3.7-flash)；阶梯来自[实时 endpoint 数据](https://openrouter.ai/api/v1/models/qwen/qwen3.7-flash/endpoints)：单次输入达到 32,000 tokens 后变为输入 $0.10/M、输出 $0.40/M，达到 256,000 后变为 $0.20/M、$0.80/M。因此预算按小片段调用，不能将页面起步价直接套在一次提交整个视频的大请求上。

ASR 页面显示的价格经过舍入，预算使用[实时转录目录](https://openrouter.ai/api/v1/models?output_modalities=transcription)中精度更高的值，并以[模型页](https://openrouter.ai/qwen/qwen3-asr-0.6b)确认单位为秒。文本 embedding 单价来自 [Perplexity 模型页](https://openrouter.ai/perplexity/pplx-embed-v1-0.6b)。如果希望继续用 Qwen，可以选当前可用的 [Qwen3-Embedding-8B](https://openrouter.ai/qwen/qwen3-embedding-8b)，$0.01/M；每视频 5,000 tokens 仅多 $0.00003，中文检索效果仍需素材评估。

Gemini 图像单价来自 [OpenRouter 模型页](https://openrouter.ai/google/gemini-embedding-2)，每图约 $0.00012 的换算来自 [Google 官方价目](https://ai.google.dev/gemini-api/docs/pricing?hl=en)。OpenRouter 按实际图像 tokens 和供应商价格结算，每张图的数值是预算近似；部分供应商为 $0.495/M。此预算使用图像输入，不套用原生视频输入 $12/M 的计价。Google Batch 折扣没有计入，也没有假设该折扣可从 OpenRouter 获得。

### 一个 3 分钟视频的算式

预算假设：180 秒音频；每 2 秒抽 1 帧，共 90 帧；按 6 个 30 秒片段调用 VLM。每帧 VLM 计费输入暂按 1,000 tokens 规划，加上各请求的提示词与转录共 3,000 tokens；VLM 输出合计 1,800 tokens，包含任何被计费的推理 tokens；文本 embedding 共 5,000 tokens。每帧 1,000 tokens 是规划假设，尚未实测该模型的图像 token 化；分辨率、裁剪补读、推理输出、重试和高价供应商路由会改变结果。

| 项目 | 计算 | 美元/视频 |
|---|---|---:|
| VLM | 93,000 × 0.03 / 1,000,000 + 1,800 × 0.13 / 1,000,000 | 0.003024 |
| ASR | 180 × 0.00000333 | 0.0005994 |
| 文本 embedding | 5,000 × 0.004 / 1,000,000 | 0.000020 |
| 90 帧图像 embedding | 90 × 0.00012 | 0.010800 |
| 合计 | 上述之和 | **0.0144434，约 $0.015** |

在同样的 token 假设下，30 帧视觉描述及 30 帧 embedding 约 $0.00544；90 帧约 $0.01444；180 帧约 $0.02794。180 帧模式更容易遇到每请求 32K 的价格门槛，实际 token 数较高时应继续拆分请求。更密的采样也不保证捕获每个短暂画面，重要文字仍需变化检测或裁剪补读。

**成本优先的工程建议：**VLM 仍看 90 帧，视觉索引只编码去重后的约 12 张代表帧，保留其片段和时间戳。这样图像 embedding 为 $0.00144，总模型预算约 **$0.00508/视频**，可先预留 $0.01；若 90 帧全部入视觉索引，总额约 $0.015，可预留 $0.02–$0.03。少存视觉向量会降低视觉检索的细粒度，这只是需要结合素材评估的折中，并非已验证准确率相同。

**接入限制：**上述方案使用抽帧图像 embedding 替换 ImageBind，需要改调用适配并重建视觉索引；视觉检索问题也必须由 Gemini Embedding 2 编码，不能用 Perplexity 或 Qwen 文本向量查询 Gemini 图像索引。文本 RAG 索引继续使用独立文本模型。OpenRouter 官方目前明确验证了 Gemini 的文本/图像请求，本次不把直接上传完整视频的 embedding 作为已验证路径。[官方 embedding 接入范围与向量空间说明](https://openrouter.ai/blog/insights/best-embedding-models-2026/)

屏幕文字由同一次 VLM 请求提取，未单列 OCR 模型费用；细小字体和数字的逐字准确性尚未验证。FFmpeg 抽帧和提取音轨可在 Linux 本地执行，不产生模型 API 费用。实际账单以每次 API 的 `usage.cost` 为准；[OpenRouter 转录接口说明](https://openrouter.ai/blog/tutorials/transcription-on-openrouter/)也提供此成本字段。
