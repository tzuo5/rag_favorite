# rag-favorite 1.2.0 for macOS (arm64)

This archive installs the same rag-favorite CLI, read-only MCP server, web UI,
media ingestion pipeline, Telegram adapter, and guarded OpenClaw integration as
the source release. It includes a reviewed uv arm64 bootstrap binary so
the installer can provision an isolated Python 3.12 runtime without modifying
Apple's system Python.

The bundled `uv` executable is redistributed under its upstream Apache-2.0 or
MIT terms; both license texts are included in `payload/`.

The local MCP endpoint uses stdio. Its reviewed runtime dependency set omits the
SDK's unused HTTP OAuth crypto extra, while retaining FastMCP, protocol clients,
both read-only tools, and OpenClaw compatibility.

## 快速开始（macOS / zsh）

1. 解压安装包。
2. 右键 `install.command`，选择 **打开**。该版本未经过 Apple notarize，Finder
   可能要求一次确认。
3. 安装器会显示当前阶段；Python 3.12、Python packages 和 Chromium 只在首次
   setup 时下载。中断后直接重跑即可，不需要先卸载。
4. 如果 zsh 的 `PATH` 尚未包含 `~/.local/bin`，安装器会询问是否幂等写入
   `~/.zshrc`。不希望修改 shell 时使用 `--no-shell-config`。

```bash
./install.command
source ~/.zshrc  # 仅当安装器提示 PATH 已更新时执行
rag-favorite --version
rag-favorite config validate
rag-favorite setup status
rag-favorite-web
```

`rag-favorite --version` 成功表示 **CLI 安装完成**。`setup status` 中 database
或 embedding 显示 `unavailable`，表示 PostgreSQL/pgvector 或 Ollama 尚未准备，
不是 Python 或 zsh 安装失败。

Install FFmpeg separately for audio/video processing:

```bash
brew install ffmpeg
```

## Complete RAG setup

PostgreSQL + pgvector、Ollama、Telegram credentials 和 OpenClaw 都是用户控制的
外部服务。使用 ingest/search 前执行：

```bash
# PostgreSQL + pgvector 和 Ollama 已经运行时
rag-favorite setup plan
rag-favorite setup apply
ollama pull qwen3-embedding:0.6b
rag-favorite doctor
```

如果希望由 rag-favorite 的 Docker Compose 启动隔离服务，并且本机 5432/11434
端口没有被其他程序占用：

```bash
rag-favorite setup apply --start-services --pull-model
rag-favorite setup status
rag-favorite doctor
```

常见状态：

- `role "rag_admin" does not exist`：当前 PostgreSQL 不是 rag-favorite 创建的实例，
  需要创建配置文件中的 user/database，或改用 `--start-services` 的隔离实例。
- `connection refused` / embedding unavailable：启动 Ollama，再执行
  `ollama pull qwen3-embedding:0.6b`。
- `pgvector extension is unavailable`：为当前 PostgreSQL **相同主版本**安装 pgvector，
  再执行 `rag-favorite init`。不要混用不同 PostgreSQL 主版本的 extension。
- `rag-favorite doctor` 只打印 `Error:` 和修复命令，不应再显示 Python traceback。

编辑安装器打印的 private `.env` 后，再 preview/enable launchd workers：

```bash
rag-favorite ingestion plan --render-launchd --enable-services --with-openclaw
rag-favorite ingestion install --render-launchd --enable-services --with-openclaw
```

OpenClaw registration is optional and never installs or restarts OpenClaw.

## Verify the download

From the directory containing the release archive and `.sha256` file:

```bash
shasum -a 256 -c rag-favorite-1.2.0-macos-arm64.tar.gz.sha256
```

The installer verifies every bundled payload file again before making changes.

## Uninstall

Double-click or run `uninstall.command`. The default preserves configuration,
credentials, sessions, media, knowledge files, and database data:

```bash
./uninstall.command
```

Permanent local application-data deletion requires the explicit
`--purge-data` option. External PostgreSQL data is never deleted.
