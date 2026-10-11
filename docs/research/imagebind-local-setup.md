# ImageBind 本地配置

更新：2026-10-04。当前目录 `/home/tzuo5/gpt projects/rag_favorite`。此前尚未安装的核查快照保留在 [历史 JSON](imagebind-local-review-results.json)；当前状态以 [持续进度](../implementation/autonomous-progress.md) 与 [实施记录](../implementation/videorag-phases-0-6.md) 为准。

## 当前已安装

| 功能 | 本机实际配置 |
| --- | --- |
| 文本 embedding | LM Studio/llmster，Qwen3 Embedding 0.6B，1024 维，loopback 1234 |
| 视频/视觉 query embedding | ImageBind huge，独立 Python 3.11/PyTorch 2.1.2 + CUDA 12.1，loopback 9123 |
| ASR | 本地 faster-whisper small，CPU int8，保留词时间 |
| 分类/视觉知识抽取 | 现有 CCR Responses provider；不是 embedding 服务 |
| 检索持久化 | 既有 PG/pgvector，文本/视频/图分开排名，融合后交给 Codex |

文本模型不变。ImageBind 是独立的 PyTorch 编码器，LM Studio 的文本 `/v1/embeddings` 继续负责 Qwen。主应用没有新增 torch 必需依赖；重依赖留在模型环境。

ImageBind 上游为 `53680b02d7e37b19b124fa37bae4b6c98c38f5be`，官方权重 4803584173 字节，SHA-256 `d6f6c22bedcc90708448d5d2fbb7b2db9c73f505dc89bd0b2e09b23af1b62157`。这是 VideoRAG 思路的适配，不能声称原版逐项复现。[官方加载源码](https://github.com/facebookresearch/ImageBind/blob/53680b02d7e37b19b124fa37bae4b6c98c38f5be/imagebind/models/imagebind_model.py)

代码/权重保留 [CC-BY-NC 4.0 来源](https://github.com/facebookresearch/ImageBind)。独立模型环境、固定 revision 与依赖快照已真实加载通过。

## 配置和命令

文本配置：`.runtime/phase1/config.toml`。视频配置：`.runtime/videorag/video.toml`，字段示例见 [实际支持的 TOML](../../examples/video-local.toml.example)。凭据分别存私有 0600 文件，不放进 Git 或聊天。

```bash
cd '/home/tzuo5/gpt projects/rag_favorite'
.venv/bin/python examples/prepare_video_models.py
bash examples/video_runtime.sh status
bash examples/video_runtime.sh cli import-url 'https://xhslink.cn/o/SHARE_ID' --collection cooking
bash examples/video_runtime.sh cli search '问题' --collection cooking
```

新机器准备模型：用户目录安装 uv 后运行 `examples/prepare_video_models.py --prepare`；它使用 [已验证依赖快照](../../examples/imagebind-requirements.lock.txt)、固定 ImageBind checkpoint 与 Whisper revision。首次下载之后，服务关闭 HF/Transformers 自动联网；缺失权重/错误 digest/错误向量空间会失败，不转云 embedding。

## 采样、资源和空间

保留官方每片段 5 clips × 3 crops，共 15 视图；逐视图推理，FP32 mean 与 L2。GPU profile 显式 FP16，CPU FP32，空间身份区分 dtype。初始小视频实测约 1.99 GB GPU 峰值、0.78 秒编码；CPU 整批与逐视图接近一致。见 [初始 benchmark](../implementation/imagebind-benchmark-results.json)。这不代表所有原始分辨率都能直接解码。

真实 4K/高帧率视频曾使整段解码 RAM 达到约 27 GB 并被 OOM 杀死。临时编码片段现限制最长边 512、8 FPS；原片用于必要的高清文字复核。该预处理进入 space_id，旧视觉向量不会被混用。模型接口限制单次片段最长 120 秒，并对未限制的直接输入转码。模型只有一次加载，GPU OOM 会明确报错，不悄悄改变 dtype 或改用云端。

默认逻辑片段 30 秒；超过 180 秒的视频可采用配置的 60 秒片段，以降低远程抽取请求数。短暂数字仍依赖额外按口述时间抽帧；该粒度变化需要真实质量评估，不能只凭资源改善判定质量通过。

Qwen 与 ImageBind 都是 1024 维也不能混用。视觉问题由 ImageBind TEXT 分支编码，和其 VISION 向量比较；中文原问题给 Qwen，额外短英文 visual_query 给 ImageBind。文本上限 77 token 包括两个特殊 token，即 75 个内容 BPE token，超限会报错。[官方模型卡](https://github.com/facebookresearch/ImageBind/blob/53680b02d7e37b19b124fa37bae4b6c98c38f5be/model_card.md)

space_id 固定权重 SHA、源码、解码/clip/crop/224 参数、聚合/归一化、dtype。更换视频编码空间后，已删除的源不能从 transcript 重建视频向量；已得知识可保留并用对应文本空间检索。

## 当前边界

实际 Codex 已通过旧两工具与新五工具 MCP 调用；Telegram 取消。同机 stdio 已接通，公开远程 HTTP/OAuth 尚未部署。模型硬件和合成闭环已实测，真实样本处理进行中；中文 ASR、数字/单位和 10 视频/60 人工问题的完整质量仍未验收。

无额外 Agent 框架前置条件。服务器保存派生知识、向量、图关系和必要截图；完整发布并校验后清理托管原视频，后续查询不下载或重开原媒体。

更新的 bounded 实测见 [结果](../implementation/imagebind-bounded-benchmark-results.json)：15 视图，GPU FP16 0.607 秒/约 1.99 GB 峰值，CPU FP32 11.417 秒/约 10.11 GB 进程峰值，CPU/GPU cosine 0.999998629。使用独立 test-fixtures，不进入用户检索。重跑命令：

```bash
PYTHONPATH=src RAG_VIDEO_CONFIG=.runtime/videorag/video.toml OMP_NUM_THREADS=4 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .runtime/video-encoder/venv/bin/python examples/imagebind_benchmark.py
```
