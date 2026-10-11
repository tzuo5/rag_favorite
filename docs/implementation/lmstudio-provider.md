# Phase 1 补充：本地 provider 切换为 LM Studio

日期：2026-10-03，America/Chicago。状态：**verified（隔离文本 MVP）**。用户要求将本地模型 provider 换成 LM Studio；本次完成实际服务、默认配置、文本 embedding 客户端、就绪检查、索引重建、回退与重启验证。此前 [Phase 1 Ollama 记录](videorag-phase-1.md) 保留为原始验收快照。

## 当前运行状态

本地默认 provider 为 `lmstudio`，实际接口是 `http://127.0.0.1:1234/v1/embeddings`。使用 LM Studio 官方无界面服务 **llmster 0.0.25+1**，CLI commit `69d945a`，llama.cpp runtime `2.41.0`。安装在当前用户的 `.lmstudio`，安装器使用 `--no-modify-path`；没有 sudo、Linux 密码输入或系统包修改。llmster 是 LM Studio 的服务器运行方式，不需要桌面 GUI。[官方 headless 文档](https://lmstudio.ai/docs/developer/core/headless)

仍使用 **Qwen3-Embedding-0.6B / Q8_0 / 1024 维**，以 `lms load ... --gpu off --context-length 4096` 加载。复用原来的 639150592 字节 GGUF，通过 hard link 导入 LM Studio 模型目录；没有重新下载另一套 Qwen 权重。[官方模型导入说明](https://lmstudio.ai/docs/cli/local-models/import)

模型标识：`text-embedding-qwen3-embedding-0.6b`。固定的是 GGUF 文件 SHA-256：

```text
06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439
```

Ollama 原来的 digest 是 manifest 身份，两者不能直接互换。新 provider 的 space_id 为 `ecaa29d4d2bf10f4667534e9a6b51b6867a46ff82c9a683278e38182f67a392b`；当前 active 为 **lmstudio-20261003-a**。原 Ollama 服务已停止，模型文件与旧索引保留供显式回退。

私有配置 `.runtime/phase1/config.toml` 已切换 LM Studio，原配置备份为 `.runtime/phase1/ollama.toml`；数据库仍是隔离 `rag_phase1`，端口 55432，样例仍为两库四文档。生产知识库未迁移。

## 实现与身份校验

新增 `LMStudioEmbeddingClient`，调用 LM Studio 的 OpenAI 兼容 `/v1/embeddings`。文档不加 query 指令，检索问题单独加指令；响应按 index 排序，验证数量、维度、有限数值与非零范数。客户端只允许 loopback HTTP，绕过环境代理并拒绝重定向；可从 `LM_API_TOKEN` 读取可选认证 token。接口格式兼容 OpenAI，不涉及 OpenAI 托管服务。[官方 embeddings API](https://lmstudio.ai/docs/developer/openai-compat/embeddings)

每批编码前后结合 `/api/v1/models` 和本机 `lms ps --json` 校验 embedding 类型、唯一 loaded identifier、modelKey、实际 GGUF 路径和 context。读取 LM Studio home pointer 与 settings.downloadsFolder，固定文件内容 SHA；缓存键含 inode、大小、mtime/ctime，文件修改后重新计算。LM Link 的远程设备条目拒绝使用。[官方模型列表 API](https://lmstudio.ai/docs/developer/rest/list)

需要本机 `lms`，默认自动发现，也可在 TOML 设置 `[embedding].lms_path`。此版本支持本地 GGUF embedding；不把未加载模型、其他聊天模型或 MLX 当作可用 encoder。模型要先明确加载，避免请求时自动选模型和设备。替换权重文件后必须 unload/reload，再新建索引代际，不能用文件 hash 冒充已重新加载的内存权重。

为避免长文本静默截断，客户端按 UTF-8 字节数加 16 个 special-token 预留做保守 context 校验，超限明确要求切分。该检查会比实际 Qwen token 数更保守，不能称为精确 tokenizer 计数。LM Studio 的设备设置发生在 load 阶段，不能沿用 Ollama 的请求 options.num_gpu。[官方 load 参数](https://lmstudio.ai/docs/cli/local-models/load)

默认 starter、无配置时的默认值、CLI embedding inspect、setup status 与 doctor 都切到 LM Studio。setup 启动 Docker 时只管理 PostgreSQL；`--pull-model` 保留为显式 Ollama 兼容操作，LM Studio 使用 `lms get/import/load`。旧 provider 的空间 hash 保持不变，未覆盖旧向量。

## 运行与恢复

本机已准备的隔离环境，从仓库根目录运行：

```bash
bash examples/phase1_runtime.sh start
.venv/bin/rag-favorite --config .runtime/phase1/config.toml embedding inspect
.venv/bin/rag-favorite --config .runtime/phase1/config.toml doctor
.venv/bin/rag-favorite --config .runtime/phase1/config.toml setup status --json
.venv/bin/rag-favorite --config .runtime/phase1/config.toml search '凉拌料汁生抽老抽各几勺？' --collection cooking
```

`phase1_runtime.sh` 按当前私有配置转到 `lmstudio_runtime.sh`。新脚本控制已准备的 Qwen 样例服务和隔离 PostgreSQL，不是通用安装器；模型固定为本次样例。重复 start 不再加载第二份模型。stop 关闭 HTTP、卸载该 Qwen 模型并停止隔离数据库，不删除数据。

一般部署可参考 [TOML 模板](../../examples/lmstudio.toml)。先安装 LM Studio/llmster、导入 embedding GGUF，加载模型并启动 loopback server，再运行 embedding inspect，将返回 digest 写入私有配置。新建 generation 后 build/activate；不要直接拿 LM Studio query 向量查询旧 Ollama 索引。已有模型时不需要下载或 API key；启用本机 API 认证时再提供 LM_API_TOKEN。

```bash
lms load text-embedding-qwen3-embedding-0.6b --gpu off --context-length 4096
lms server start --bind 127.0.0.1 --port 1234
rag-favorite embedding inspect
```

本次实际做了 LM Studio A/B 两代切换/回退，也切到原 Ollama `local-text-a` 代并用配套 encoder 成功搜索，再切回 LM Studio A。跨 provider 回退需要显式启动旧 Ollama 服务；当前 LM Studio 运行脚本不会自动拉起它。

## 验收与范围

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 核心、rag-app 与真实集成 | 122 passed，含八项真实数据库/LM Studio 测试 | [日志](lmstudio-test-results.txt) |
| 旧 memory MCP 兼容 | 66 passed | 同上 |
| cooking 兼容 | 14 passed、1 skipped | 同上 |
| 相关 ingestion 回归 | 48 passed | 同上；provider 为测试替身 |
| LM Studio 两库检索与 A/B 回退 | 两个问题各运行四次，八次 Top 1 全部正确 | [MVP JSON](lmstudio-text-mvp-results.json) |
| 跨 provider 回退 | 旧 Ollama 搜索成功，已恢复 LM Studio | [provider JSON](lmstudio-provider-results.json) |
| 服务重启、幂等 start、stdio MCP | 健康检查、检索、两工具握手与调用通过 | [运行 JSON](lmstudio-runtime-results.json) |
| Ruff、shell、wheel 资源 | 通过 | 测试日志与本地 .runtime/lmstudio 构建记录 |

新的离线测试覆盖本地接口与可选认证、批量/排序、query 指令、坏向量、权重中途变化、CLI/API 身份不一致、远程设备、未加载模型、超长输入、本地 URL 约束，以及 setup 不启动 Ollama。

样例客户端的非 loopback Python TCP 被 guard 阻断；CLI 的 IPC 与本地模型库存另行校验，没有声称修改了系统防火墙。本次没有调用付费模型。

**当前完成的是 Phase 1 的本地文本 provider 替换。** 视频 embedding 仍按计划由独立本地视频 encoder 实现，不能假设 LM Studio 的文本 embeddings endpoint 能直接编码视频。VideoRAG 入库/删源、最终 QA、远程 MCP 与 Telegram 的阶段边界不变。
