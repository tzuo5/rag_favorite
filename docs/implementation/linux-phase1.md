# Linux Phase 1 实施记录

日期：2026-10-03（America/Chicago）。状态：**in_progress，尚未达到 Phase 1 的完整验收条件**。

## 来源与恢复方式

- 计划提交：[ab03f923](https://github.com/tzuo5/rag_favorite/commit/ab03f923b26447ea9dd87bf455c33d28b7d576fb)，包含 `devplan.md`、`Development MVP.md` 与模型调研文档。
- 用户确认原副本为仓库根目录中的 `ubuntu_home_backup_20260815.tar.gz`。原包保持不变。
- 工作目录：`/home/tzuo5/rag_favorite`；开发分支：`upgrade/video-rag-phase1`；修改基于上述产品化提交，尚未提交或推送。
- 旧包是 Ubuntu home 服务布局，没有最新计划要求的 `src/rag_favorite` 核心。恢复原代码到 `.backup-original/services`、`.backup-original/AI-Video-Transcriber`；恢复原知识库到 `.backup-original/知识库`，原配置保留为私有文件。它们被 Git 排除，不作为当前运行配置。
- 当前工作代码使用产品化目录 `src/rag_favorite` 与 `ingestion`。`AI-Video-Transcriber` 是指向 `ingestion` 的兼容链接；修改代码时两条路径指向同一份文件。
- 本次未恢复整个 home、数据库 dump、历史运行缓存或模型权重；原包保留这些内容以供后续按需恢复。原数据库没有被启动、迁移或覆盖。

## 真实环境

Ubuntu 26.04.1 LTS，x86_64，服务用户 `tzuo5`，i7-13700H，20 个逻辑 CPU，约 30 GiB 可见 RAM。系统 Python 为 3.14.4；开发环境 `.venv` 使用应用随附 Python 3.12.14，并安装产品的 MCP/ingestion/dev extras。没有安装本地模型。

当前 PATH 未发现 FFmpeg、ffprobe、psql、Docker 或 Node。Node 回归使用应用随附运行时。用户确认 CCR 还没有安装；旧 `.env` 只有 Whisper 配置，未发现 OPENAI/OpenRouter 配置项。尚未提供 OpenRouter 凭据、真实视频目录及开发数据库连接。

## 已实施的基础修改

1. `temporal_models.py` 定义结构化 transcript、segment/word、稳定 ID 和有限非负时间校验。无时间时保留 unknown；支持 MM:SS、HH:MM:SS、75:30 与毫秒。字幕只合并相邻重叠滚动内容，保留远处重复指令。Whisper 保留旧字符串入口，模型依赖延迟加载，浮点时间不再被截成整数秒。
2. `content_classifier.py` 提供 CCR Responses/兼容 Chat 文本 adapter、本地 Pydantic 校验、顺序有界批次、引用 ID 校验与分类缓存。schema 参数不兼容时只在同一模型上尝试 JSON prompt。分类与基础 enrichment 复用同次调用，坏摘要与坏分类分开处理；覆盖不完整且没有阳性证据时为 unknown。
3. 视觉入口检查开关、cooking 准入与视频轨；默认关闭。非准入输入不调用新增视觉管线；已提供可注入 fake 入口验证调用计数。**真实视觉 provider 尚未实现**，正例只记录 `visual_provider_not_implemented`，不伪报识别成功。旧平台 cover 行为继续遵循已有契约。
4. `_stage` 保存分类版本、覆盖、跳过原因与结构化 transcript 到 job metadata，并在 Markdown frontmatter 保存独立分类字段。保留已有 metadata 与原 transcript checksum 语义；分类/API 故障继续生成确定性 Markdown；控制异常传播。
5. `openrouter_asr.py` 使用独立转录 endpoint，只发送有界音频分片。字幕命中零 ASR；供应商时间加真实分片 offset，文本-only 使用 coarse 分片范围，无可靠起点标 approximate。无自动 Whisper fallback。
6. ASR 按音频 hash、模型、语言与时间配置缓存；SQLite ledger 在发出请求前持久化费用预留，实际 `usage.cost` 单独对账。未知费用阻止自动重发与后续同视频请求。估价使用计划中的 2026-10-01 单价及余量，尚未验证真实账单。
7. 产品核心使用统一 embedding factory，支持 OpenRouter 的 data/index/count、维度和有限数值校验；查询不沿用 Qwen prefix。云空间 ID 包含 endpoint/model/revision/dimensions/preprocessing；旧 Ollama 数据保持原标识，新云空间使用独立标识。
8. 入库/查询发现混合空间会拒绝；写入时加事务锁并重新校验，查询持共享锁校验。本阶段仅准备独立空测试库，**未实现 Phase 3 的完整 shadow generation/切换机制**。旧 recipe 专用表拒绝云 encoder；当前 cooking 导入使用统一产品核心。
9. 工厂创建的云文本 client 持久化请求与费用状态，不保存文本或密钥；响应丢失不自动重放。embedding 失败保留已归档 Markdown，job metadata 标 `index_pending`，不发送“向量已完成”通知。云 status 为配置就绪检查，不声称模型实际可用。

## 测试事实

原产品化基线：核心 67 passed；ingestion 182 passed、32 skipped。

新增覆盖包括字幕毫秒/远处重复、非法时间、实际 ASR word/segment/text-only offset、纯音频与禁用开关、虚构分类 ID、后半段 cooking、不完整覆盖、缓存失效、坏摘要、控制传播、Responses/Chat、同路由 schema fallback、归档与 metadata 保留、字幕零 ASR、429/失败费用恢复及错向量。全部模型调用为 fake/mocked transport，未调用付费 API。

当前回归命令（仓库根目录）：

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests services/rag-app/tests
(cd ingestion && PYTHONPATH=../src ../.venv/bin/python -m pytest -q tests)
PYTHONPATH=services/rag-mcp:src .venv/bin/python -m unittest discover -s services/rag-mcp/tests
PYTHONPATH=services/cooking-rag:src .venv/bin/python -m unittest discover -s services/cooking-rag/tests
.venv/bin/python -m ruff check src/rag_favorite tests scripts
.venv/bin/python -m compileall -q src ingestion/backend services scripts
```

具体测试数字与原始日志见同目录的 `phase1-test-results.txt`。数据库依赖测试按原配置跳过；这些 skipped 不能作为数据库验收通过。

## 配置与下一步关口

连接模板：`examples/video-cloud.env.example`；隔离数据库模板：`examples/video-cloud.toml`。二者不自动启用，也不含凭据。复制环境模板为私有 `.env` 后设为 0600；设置真实 CCR 地址和客户端 key、OpenRouter key。不能直接采用尚未确认的 3456 示例端口。

按顺序完成后续工作：

- 确认 CCR 的实际项目、安装方式和 Codex provider 配置；安装/启动并验证 Responses 与 `Codex API/gpt-6-luna`，避免猜测路由或管理 token。
- 安装 FFmpeg/ffprobe，准备独立 PostgreSQL/pgvector 测试数据库，重新跑跳过的 DB 测试和同维异空间隔离/并发验证。
- 配置 OpenRouter，核对实时模型目录、维度、ASR 参数与价格；运行小样本真实 smoke，对照 usage 和原始时间验证结果。
- 在预算与故障场景下验证中途暂停/取消、分片恢复、账单对账。ASR 与文本 embedding 当前各有记录机制；Phase 2 前还需接入统一每视频 role-aware 总预算，不能把当前两份记录称为完整统一 ledger。
- 提供至少 10 个真实做饭视频与人工标注。Phase 2 的 VLM/选帧/持久证据/回填及基准完成后，才能继续 Phase 3–5。

**本次交付是可审阅的 Phase 1 基础改动与离线验证，不是视频视觉检索升级全部完成，也不是已部署运行的系统。**
