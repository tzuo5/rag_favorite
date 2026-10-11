# VideoRAG Phase 1：本地文本 RAG 与索引代际

日期：2026-10-03，America/Chicago。对应 [Dev Doc](../videorag-dev.md) 的 **Phase 1 / VR1-01–06**，与旧 [Linux Phase 1](linux-phase1.md) 的云模型基础工作分别记录。

**provider 已按用户后续要求切为 LM Studio。** 下文保留首次 Ollama 验收事实；当前配置、默认行为、重启与新验收见 [LM Studio 补充记录](lmstudio-provider.md)。

**状态：verified，已通过隔离样例 MVP。** 普通 Markdown 的本地编码、中文检索、collection 隔离、完整编码空间校验、shadow 构建、切换和回退，以及旧 stdio 两工具已真实运行。尚未切换生产知识库；VideoRAG 视频处理、视频 embedding、删源、远程 MCP 和独立 Telegram 属于后续阶段。

用户明确要求直接开始 Phase 1，因此本次先补齐 Phase 0 中所需的文本模型和数据库基线。Phase 0 的双编码、真实视频 PoC 与中文视觉质量仍未验收。

## 1. 开发任务与实现

| ID | 状态 | 本次结果 |
| --- | --- | --- |
| VR1-01 | verified | 默认本地 Ollama，配置与就绪检查要求固定模型 digest；请求失败不自动切到云 embedding |
| VR1-02 | verified | 文档保持原文；query 独立加检索指令；批量编码、单条编码与传输配置一致 |
| VR1-03 | verified | space_id 绑定权重、revision、维度、归一化、预处理与指令；generation 单独绑定 schema 和编码器快照；拒绝同维错空间、非法向量与模型中途变化 |
| VR1-04 | verified | 追加 0002，0001 原文件及 checksum 不变；只有初始 schema 渲染向量维数；新库、既有迁移历史和幂等追加通过 |
| VR1-05 | verified | 真实 Qwen 编码、PostgreSQL/pgvector 查询；两库四文档检索成功；原 stdio 的 rag_search / rag_status 兼容 |
| VR1-06 | verified | 新增 index create/build/activate/list；active 指针与注册编码器一并解析；实测切换、回退、并发写入保护与重启恢复 |

主要落点：[embedding.py](../../src/rag_favorite/embedding.py)、[config.py](../../src/rag_favorite/config.py)、[indexes.py](../../src/rag_favorite/indexes.py)、[rag.py](../../src/rag_favorite/rag.py)、[migrations.py](../../src/rag_favorite/migrations.py)、[0002](../../migrations/core/0002_index_generations.sql)、CLI、setup、MCP 和两处兼容 wrapper。

本地客户端绕过环境代理、拒绝 HTTP 重定向，只接受 loopback 模型接口。每批编码前后核对已安装模型 digest；输入不允许静默截断，返回向量检查数量、维度、有限数值和非零范数。新配置默认 CPU（num_gpu=0），没有调用 OpenAI/OpenRouter 或其他付费模型。

## 2. 索引与切换方式

每个 generation 使用同一 PostgreSQL 中的独立 `rag_gen_*` schema，保存原有文档/切片表。`public.rag_index_generations` 保存完整编码配置、space_id、collection 根目录、构建 manifest 和 ready 状态；`public.rag_index_state` 保存唯一 active 指针。没有增加新数据库服务。

`[index].generation="active"` 时，每次操作从注册表同时取得 schema 与编码器，形成固定快照。TOML 中过时的模型指令不会被拿来查询另一个 active 空间。跨库 stdio 搜索也只解析一次快照；切换中的在途查询可完成在原空间的检索。

创建 generation 使用当前 TOML 的编码配置；build 与 activate 使用该 generation 的注册快照。build 只允许非 active 代际，扫描所有配置的文档源，核对 hash 与完整覆盖后标 ready。切换前检查真实模型、索引完整性和源文件版本；编码期间 active 改变会拒绝该次写入，要求重试。

原 `public` 文档/向量表保留。**无版本身份的 legacy 向量不能在新代码中安全激活**，需要明确的旧应用/配置配对，或重建为固定权重的新 generation。本次回退是在两个已验证 generation 之间完成，不能据此宣称任意历史向量均可复用。更改 collection 根目录也需新建 shadow 代际。

0001 与 HEAD 完全一致，SHA-256：

```text
8948302560843e96107ab513a294126b51bcd26a76edc110a3072076d6946340
```

## 3. 本机运行环境

依赖全部放在 Git 排除的 `.runtime/phase1`，由当前用户运行；没有 sudo、系统包安装、系统服务改动或 Linux 密码输入。

| 项目 | 实际配置 |
| --- | --- |
| 应用 | 项目 .venv，Python 3.12.14 |
| 数据库 | PostgreSQL 18.6、pgvector 0.8.1；隔离库 rag_phase1；loopback 55432 |
| 模型服务 | Ollama 0.32.0；loopback 11435；OLLAMA_NO_CLOUD=1 |
| 文本模型 | qwen3-embedding:0.6b，1024 维；CPU 编码 |
| 模型 digest | ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d |
| 私有配置 | .runtime/phase1/config.toml 与 secrets.env，权限 0600；数据库随机密码不进入 Git |
| 样例 | cooking、tech 两库，共四个短 Markdown |

PostgreSQL 包以 `apt-get download` 获取后用 `dpkg-deb -x` 解包。Ollama 使用官方发行包并核对 GitHub release 的 SHA-256：`56362d7609dfa9e35aaebb7c9cab25605d8f0528ec3d5d585dc83d6642002bab`；模型权重已下载到隔离模型目录。环境没有变成系统安装，FFmpeg 和视频模型依赖仍需后续准备。

Ollama `/api/ps` 的本次加载结果为 `size_vram=0`、`size=2371270737`、context 4096。这个 API 数字是加载资源估计，不等于实测峰值 RSS。短文本 warm query 约 0.2 秒；不能外推为视频处理速度或大型知识库质量。

## 4. 运行与复现

以下命令从仓库根目录运行。辅助脚本仅控制本次已准备的用户运行环境；它不会安装依赖。Ollama 用独立 session 启动，退出启动终端后仍可访问；本次已停止并重启两个服务，从另一 shell 验证持久索引和检索恢复。

```bash
bash examples/phase1_runtime.sh start
bash examples/phase1_runtime.sh status
.venv/bin/rag-favorite --config .runtime/phase1/config.toml embedding inspect
.venv/bin/rag-favorite --config .runtime/phase1/config.toml doctor
.venv/bin/rag-favorite --config .runtime/phase1/config.toml index list
.venv/bin/rag-favorite --config .runtime/phase1/config.toml search '凉拌料汁生抽老抽各几勺？' --collection cooking
```

需要新文本版本时选一个尚未使用的代际名称。先修改私有 TOML 的模型 digest/预处理，再依次创建、构建和激活；这里 `local-text-next` 是操作示例，每次新建需不同名称：

```bash
.venv/bin/rag-favorite --config .runtime/phase1/config.toml index create local-text-next
.venv/bin/rag-favorite --config .runtime/phase1/config.toml index build local-text-next
.venv/bin/rag-favorite --config .runtime/phase1/config.toml index activate local-text-next
```

回退时，从 `index list` 选定上一代 ready 名称，运行 `index activate 上一代名称`。源文件已发生修改而需要回到旧快照时，显式加 `--allow-stale`，接受该代未包含最新编辑；此参数仍检查已保存索引的空间和完整性。

新 starter 配置默认 digest 为空：先自行下载所需权重，用 `embedding inspect` 获取 digest，写入 `[embedding].model_digest` 后才能编码。没有自动下载模型或云服务兜底。

可重复运行真实样例脚本，每次创建一对新的代际，最后停留在该对的 A 代；只允许隔离 `rag_phase1*` 数据库，数据库名覆盖与实际连接不一致会被拒绝：

```bash
.venv/bin/python examples/phase1_text_mvp.py \
  --config .runtime/phase1/config.toml \
  --output .runtime/phase1/repeated-mvp.json
RAG_FAVORITE_INTEGRATION_CONFIG=.runtime/phase1/config.toml \
  .venv/bin/python -m pytest -q
bash examples/phase1_runtime.sh stop
```

真实集成测试创建自己命名的临时数据库，结束后只删除该临时库；数据库名环境覆盖必须先移除。普通 `pytest` 未提供真实配置时跳过这八项，不应把 skipped 当作数据库验收通过。停止样例服务不删除模型或数据库文件。systemd 与开机自启属于 Phase 6。

## 5. 验收事实与证据

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 核心回归（含真实模型/数据库） | 102 passed，含八项真实集成测试 | [测试日志](videorag-phase1-test-results.txt)、[命令记录](phase1-checks.json) |
| rag-app collection/CLI 兼容 | 5 passed | 同上 |
| 旧 memory MCP 兼容 | 66 passed | 同上 |
| cooking wrapper 兼容 | 14 passed、1 skipped | 同上；跳过项没有计为验收通过 |
| 相关 ingestion 分类/转录/云接口回归 | 48 passed | 同上；模型调用为测试替身 |
| 实际中文 MVP、代际切换与回退 | 两库四文档，八次查询全部 Top 1 正确，库隔离通过 | [MVP JSON](phase1-text-mvp-results.json) |
| 真实 stdio MCP 客户端 | 初始化、仅两工具、status、单库搜索、显式跨库成功 | [stdio JSON](phase1-stdio-mcp-results.json) |
| 服务停止/启动与持久索引恢复 | PostgreSQL、模型健康检查和真实搜索通过 | [运行记录](phase1-runtime-results.json) |
| Ruff、wheel 与打包迁移 | 通过；wheel 含新 indexes 模块，两个 SQL 与 canonical 逐字节一致 | [命令记录](phase1-checks.json)；本地构建日志 .runtime/phase1/wheel-build.log |

八项真实集成测试包括既有 0001 的追加迁移/幂等、legacy 数据保留、中文检索与库隔离、固定在途快照、同维错空间先拒绝、未完成/active 构建保护、旧快照回退、编码时 active 改变、代际表丢失时禁止 public 回落，以及源文件中途编辑不提交。

MVP 的“八次查询”是两个问题在 A、B、固定 A、回退 A 上各查一次，不是八个不同质量样本。外网验证使用 Python TCP socket guard 阻止非 loopback 连接，并验证一条禁止探针；libpq 连接本机数据库，Ollama 配置禁用云功能。**没有修改系统防火墙，也没有声称整个 OS 的外网被封禁。**

## 6. 范围与下一阶段

本次没有改动原备份、部署生产库或提交/推送 Git；原工作区修改保留。当前能独立演示的是本地普通文本 RAG 和 stdio 工具。

Phase 2 开始前仍需完成 Phase 0 的视频相关条件：FFmpeg/ffprobe、固定 VideoRAG 与视频权重/隔离依赖、真实 cooking/非 cooking 样例，以及分类/VLM/必要 ASR 的实际配置。视频知识持久化、原媒体成功后删除、图/视频融合、最终 QA、远程 MCP 权限和 Telegram 闭环均不在这次验收中。
