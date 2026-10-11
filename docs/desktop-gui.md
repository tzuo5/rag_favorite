# 桌面任务控制台

双击桌面的 **RAG 任务控制台**，即可查看当前视频知识库的真实 PostgreSQL
任务队列。它使用独立桌面窗口，每 3 秒自动刷新；关闭窗口后后台队列继续运行。

## 查看任务与模型

- 顶部显示运行中、等待中、已暂停、已完成、受阻、失败的数量。
- 队列按实际领取顺序排列，支持按状态、存储分类筛选，以及搜索标题、任务 ID、错误码。
- CCR 每次刷新都读取本机 Router 管理接口，依据当前请求别名和匹配的路由规则，
  显示下一次请求使用的模型、思考强度、来源和读取时间；修改 Router 后无需重启窗口。
  接口离线、网关不匹配或动态脚本无法确定时明确显示不可用，
  不会把旧请求别名当成当前模型。其他模型读取 Faster Whisper、文字 Embedding 和 ImageBind 配置。
  “处理中”依据 worker 当前阶段和当前 PID 的真实调用记录。
  ASR、文字 Embedding 和 ImageBind 通常调用较短，任务重试时还会复用已保存的结果；
  卡片保留最近调用时间、成功或失败、耗时、调用次数及缓存复用次数。
  短调用结束后显示“刚完成”20 秒，缓存命中显示“缓存复用”，不会计入模型调用次数。
  调用记录保存在视频数据目录的 `model-activity.json`，从本次更新开始累计，
  不含视频内容、提示词或认证信息。
  “本地端口可达”只说明服务端口在线，
  不代表已经验证模型推理、认证、额度，或监控其他项目的模型调用。
- 帧数与音频片段数是当前阶段的实际进度，不是整个任务的完成百分比。
  没有可计算进度时显示“—”；更新时间是数据库阶段更新时间。
- 底部显示 worker 的结构化任务日志，选中任务可查看完整 ID、错误码、尝试次数与时间。
  不展示原始载荷、认证信息或带访问令牌的来源 URL。

## Start / Stop 的行为

**顶部 Start** 恢复队列领取任务，必要时启动已有用户 worker 服务。
单独暂停的任务仍保持暂停。Start 不会自动重试所有失败任务。

**顶部 Stop** 持久化暂停领取新任务。已经领取的任务完成当前处理、入库和清理；
其余任务等待再次 Start。模型服务继续驻留，收藏同步仍可添加待处理任务。

**Start 选中任务** 恢复单独暂停的任务，或重试失败 / 受阻任务。
全局暂停时，恢复的任务仍需顶部 Start 才能处理。已完成任务不能重复执行；
媒体已过期的任务需重新提交来源。

**Stop 选中任务** 暂停此任务的后续领取。已经领取的任务继续完成；
若它之后进入等待重试，暂停设置阻止再次领取。它不取消或删除任务。

控制保存在视频数据目录的 `queue-control.json`，退出 GUI 或重启 worker 后保留。
worker 与控制台通过同一文件锁协调暂停和任务领取；暂停设置损坏时停止领取，
并报告错误，不会自动恢复执行。

## 安装与运行

在项目目录运行：

```bash
.venv/bin/python -m pip install -e '.[gui]'
.venv/bin/python examples/install_desktop_gui.py
.venv/bin/python -m rag_favorite.desktop_gui
```

依赖使用 Qt for Python 的 `PySide6-Essentials`，只安装到项目虚拟环境。
配置优先级为命令行、`RAG_FAVORITE_CONFIG` / `RAG_VIDEO_CONFIG`、当前项目
`.runtime/phase1/config.toml` / `.runtime/videorag/video.toml`。
安装器将明确的配置路径写入启动器；它不写入密钥。

GUI 需要现有 `rag-favorite-video-worker.service` 以及数据库。首次更新 worker
代码后需加载新版本；界面会核验当前 PID 发布的控制能力，旧 worker 无法显示可用的 Stop。
数据库连接失败时保留上次显示的数据并明确提示可能过时，自动尝试重连。
模型状态同时标为“信息过时”，恢复连接后重新显示实时数据。

表格仅更新变化的数据，列宽可手动拖动；连续刷新保留选中任务。
后台操作进行期间关闭窗口会立即隐藏窗口，并在本次操作结束后退出，
不阻塞桌面；重复打开可重新显示窗口。暂停锁等待最多 3 秒，
超时明确报告本次操作未完成，不会假报暂停成功。

读取 CCR 需要已有 `~/.claude-code-router/service.json`，也可用
`CCR_SERVICE_STATE` 指定位置。只调用本机认证的只读管理接口，
不发起模型推理，不修改 Router 设置，也不展示接口令牌。

只读快照与真实窗口截图：

```bash
.venv/bin/python -m rag_favorite.desktop_gui --snapshot
QT_QPA_PLATFORM=offscreen .venv/bin/python -m rag_favorite.desktop_gui \
  --screenshot .runtime/gui/window.png
```

可指定 `--config PATH --video-config PATH`。卸载桌面和应用菜单图标：

```bash
.venv/bin/python examples/install_desktop_gui.py --uninstall
```

Qt 的后台线程与窗口信号用法参考[官方示例](https://doc.qt.io/qtforpython-6/examples/example_widgets_thread_signals.html)。
