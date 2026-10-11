# 关注博主采集：使用与验收

## 使用

```bash
# 导入所有关注博主；继续已有历史游标，日常同时刷新已扫描作者首页
bash examples/video_runtime.sh cli import-following

# 仅运行两页以检查链路；未完成时退出码为 2
bash examples/video_runtime.sh cli import-following --max-pages 2 --seconds 120

# 仅续跑，不在已经完成时新建扫描
bash examples/video_runtime.sh cli import-following --continue-only

# 状态、暂停、恢复采集；不会停止正在处理的媒体任务
bash examples/video_runtime.sh following-status
bash examples/video_runtime.sh cli following-pause
bash examples/video_runtime.sh cli following-resume

# 安装默认每日同步和每半小时续跑
bash examples/video_runtime.sh following-schedule

# 调整同步时间；分钟固定为 15，续跑固定为每半小时
bash examples/video_runtime.sh following-schedule --hour 7 18 --timezone America/Chicago

# 移除关注调度，保留数据库、Cookie、知识和视频 worker
bash examples/video_runtime.sh following-schedule --uninstall
```

部署其他机器时，安装项目 `video-sources` 和 `image-notes` 可选依赖，并运行既有
数据库迁移入口；定时器安装也会先校验和应用缺失迁移。当前机器已安装本地 OCR 依赖，
不需要管理员安装 Tesseract。首次登录仍使用项目既有 owner 登录入口。

`--restart` 建立新的扫描周期，保留已有任务与知识，不允许更换账号混入原库。
正常完成退出码 0；blocked 为 1；限页、时长、背压、暂停或已有扫描占锁为 2。
定时服务把 2 当作可续跑结果，把 1 作为实际失败。

## 状态的意义

- `following_complete`：本次关注名单与精确计数相符。
- `discovery_complete`：当前扫描范围已经结束；检查 `enumeration_scope` 区分历史全量与近期增量。
- `last_full_scan_at`：最近一次历史或周期全量扫描结束时间，尚未全量完成时为 null。
- `processing_complete`：发现扫描完成、关联媒体任务均完成且没有已知动态媒体缺口。
- `jobs`：实际队列状态；包含可获取的 Live Photo 子视频。
- `discovered`：来源关联中的唯一笔记数；`submitted`/`duplicates` 是已完成页检查点的累计提交／重复次数，部分页重放可增加重复次数。
- `author_errors`、`media_gaps`：具体作者采集失败和动态媒体缺口。
- `worker_paused`、`worker_refresh`：媒体队列暂停及部署刷新进度，与采集暂停分开显示。

首次回填允许跨多次运行。定时器不需要 Codex 窗口运行，但电脑关机时不能处理；
`Persistent=true` 会在用户服务管理器启动后补跑。登录与验证阻塞保留进度，
不会假装自动完成认证，也不会自动重新提交已过期的失败媒体。

逐图成功检查点持久化后可复用。图片部分失败仍会索引已成功部分，并将原任务标记
未完成。完成过的旧正文图文第一次从关注入口出现时升级同一任务；新版纯正文完成
记录带版本标记，不会被每次同步重新排队。

## 调度与部署检查

```bash
systemctl --user status rag-favorite-following.timer rag-favorite-following-continue.timer
journalctl --user -u rag-favorite-following.service -u rag-favorite-following-continue.service -n 40 --no-pager
```

加载新 worker 时使用临时队列暂停，等待当前模型任务结束后重启并恢复。
`examples/refresh_video_worker.py` 不打断当前模型请求；若等待期间用户改变队列控制，
脚本保留用户的新设置。实际刷新状态写入私有 `following/worker-refresh.json`。

## 验证与限制

调研与全部假设见 [研究记录](../research/xhs-following-ingestion.md)。机器可读的
实际结果见 `xhs-following-results.json`。测试覆盖分页恢复、账号隔离、循环游标、
背压、日常首页与历史游标共存、跨入口去重、图文升级、逐图 OCR/视觉检查点、
Live Photo 子视频以及真实 PostgreSQL 状态。

真实账号已验证 40 位关注博主，以及多页作者的连续作品响应。首次限页导入
62 条内容，状态为 partial；这不是所有历史内容处理完成的证明。平台的响应变化、
不精确计数、限制和无法访问内容仍可能阻止全量扫描。图片视觉检索目前通过 OCR/VLM
生成文字进入统一检索，不提供独立 ImageBind 图片相似查询。

本次部署时另观察到现有共享视频队列有两条非关注来源任务在 `embed_video` 阶段
以 `LOCAL_ENCODER_HTTP_ERROR` 阻塞，具体 HTTP 根因尚未确定。关注采集和调度已启动，
但这不代表共享视频处理服务已经通过全部内容的实机验收；图片 VLM 新路径也尚未完成
实机验收。真实 OCR、分页和数据库检查已经通过。
