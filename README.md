# rag_favorite

一个从 Telegram 输入到本地 embedding、pgvector 索引，再到 OpenClaw 检索/回答的完整私有 RAG 项目。

## 数据流

```text
Telegram
   │
   ▼
OpenClaw Telegram channel
   │
   ├── 文本/记忆 ───────────────► governed memory API
   │
   └── 视频、音频、作品/作者链接 ► ingestion plugin
                                      │
                                      ▼
                         下载 → 转写 → 清洗 → Markdown
                                      │
                                      ▼
                     Ollama qwen3-embedding:0.6b (1024 维)
                                      │
                                      ▼
                              PostgreSQL + pgvector
                                      │
                                      ▼
                           rag-mcp / cooking-rag MCP
                                      │
                                      ▼
                                  OpenClaw
```

## 目录

- `ingestion/`：Telegram/OpenClaw 入站插件、视频/音频转写、作者批量发现、知识文档写入及 worker。
- `services/ollama/`：本地 embedding 服务。
- `services/rag-postgres/`：PostgreSQL + pgvector。
- `services/rag-app/`：知识文档切块、embedding、索引与混合检索 CLI。
- `services/rag-mcp/`：OpenClaw 使用的统一 RAG MCP 与受治理记忆 API。
- `services/cooking-rag/`：菜谱专用写入、检索与 MCP。
- `openclaw/`：脱敏后的 OpenClaw 配置示例和路由 skills。

## 安全说明

仓库不包含 Telegram Bot Token、API key、数据库密码、Cookie、浏览器登录态、数据库备份、个人知识库正文或 OpenClaw 记忆。请从示例文件复制本地配置，并确保真实配置权限为 `0600`。

## 快速启动

当前实现以 Linux、Docker、Python 3.12 和 OpenClaw 为运行环境。部分路径保留了现网默认布局 `/home/ubuntu/...`；部署到其他位置时，请同步修改配置和 systemd unit 中的路径。

1. 启动 Postgres 和 Ollama：

   ```bash
   cp services/rag-postgres/.env.example services/rag-postgres/.env
   docker compose -f services/rag-postgres/compose.yaml up -d
   docker compose -f services/ollama/compose.yaml up -d
   docker exec ollama ollama pull qwen3-embedding:0.6b
   ```

2. 为 Python 服务创建虚拟环境：

   ```bash
   python3 -m venv services/rag-app/.venv
   services/rag-app/.venv/bin/pip install -r services/rag-app/requirements.txt

   python3 -m venv services/rag-mcp/.venv
   services/rag-mcp/.venv/bin/pip install -r services/rag-mcp/requirements.txt

   python3 -m venv services/cooking-rag/.venv
   services/cooking-rag/.venv/bin/pip install -r services/cooking-rag/requirements.txt

   python3 -m venv ingestion/.venv
   ingestion/.venv/bin/pip install -r ingestion/requirements.txt
   ```

3. 配置本地凭据：

   ```bash
   cp ingestion/.env.example ingestion/.env
   cp openclaw/rag-mcp-runtime.env.example ~/.openclaw/rag-mcp-runtime.env
   chmod 600 ~/.openclaw/rag-mcp-runtime.env
   ```

4. 参考 `openclaw/openclaw.example.json` 配置 Telegram、ingestion plugin 和 MCP，然后重启 OpenClaw gateway。

5. 初始化并索引文档：

   ```bash
   services/rag-app/.venv/bin/python services/rag-app/rag.py init
   services/rag-app/.venv/bin/python services/rag-app/rag.py doctor
   services/rag-app/.venv/bin/python services/rag-app/rag.py ingest \
     "/path/to/knowledge" --knowledge-base tech
   ```

具体入站、批量作者导入和平台登录态运维见 `ingestion/docs/`。

## 测试

```bash
ingestion/.venv/bin/python -m pytest ingestion/tests
services/rag-mcp/.venv/bin/python -m unittest discover -s services/rag-mcp/tests
services/cooking-rag/.venv/bin/python -m unittest discover -s services/cooking-rag/tests
node --test ingestion/openclaw-plugin/*.test.js
```
