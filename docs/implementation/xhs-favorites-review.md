# 收藏导入审查与每晚自动同步

审查日期：2026-10-04。范围为本次新增的收藏分页入口、CLI/MCP 调用链、直接关联的数据库入队和下载路径、登录浏览器共用方式，以及夜间调度。采用逐文件代码审查、安全回归测试和真实 PostgreSQL/小红书调用验证；不是一次覆盖全部历史代码的正式仓库安全扫描。

## 已确认的问题与修复

| 原问题 | 影响 | 已实施修复与证据 |
| --- | --- | --- |
| MCP 后台进程仅传文本配置，视频配置依赖环境 | 从其他客户端启动可能找不到视频配置或使用错误目录 | VideoConfig 记录配置来源，后台子进程明确传递两份配置；新增 MCP 参数回归测试 |
| 手动 URL 与收藏记录的去重口径不同，使用 LIKE 可能误命中 | 同条笔记重复下载，或误认为已导入 | 直接 URL 自动解析笔记 ID，校验传入 ID 与 URL 一致；以完整规范路径和 ID 精确匹配，保留短分享链接的历史 source.json 兼容 |
| 自动同步沿用手动重新提交策略，会忽略媒体已过期的失败任务 | 每晚可能创建新的同源失败任务，反复下载及消耗模型预算 | 自动同步显式 allow_expired=false；失败任务保持原状态，只有用户主动重新提交才允许重新下载；真实数据库验证通过 |
| 排队任务保留第一次扫描时的访问 token | 长队列中的链接可能在真正处理前变旧 | 再扫描时仅更新 queued 任务访问 URL；running 与 complete 任务不改动；真实数据库测试验证 |
| 完成或 restart 后丢弃账号绑定 | Cookie 被替换后可能把另一个账号的收藏混入原库 | 私有 owner.json 固定绑定，完成、续扫、重新扫描均校验账号；回归测试覆盖更换账号 |
| 游标循环记录仅存在内存 | 中断恢复后可能重复循环并误报完成 | 已见游标持久化；空的非终止页、缺失游标、超长 token 和异常结构明确停止；回归测试覆盖跨恢复循环 |
| 状态/日志使用普通文件写入，未验证软链接，状态文件未 fsync | 文件权限和中断后的状态可靠性不足 | 目录 0700、文件 0600、O_NOFOLLOW、文件所有者/硬链接检查；唯一临时文件、fsync 和原子发布；软链接测试验证外部文件不被读写 |
| HTTP 默认跟随跳转后才检查目的地 | 不可信重定向可能已发出请求 | API、分享链接和视频 CDN 各有 HTTPS 域名/端口白名单，在下一跳请求发出前阻止外部地址和降级；回归测试验证 |
| 扫描返回 blocked/partial 时 CLI 仍以 0 退出 | 定时服务可能把失败记为成功 | 明确返回非零退出码；新增调度器错误状态测试 |
| 登录程序与下载 worker 共用 Chrome profile，缺少统一协调 | 登录和下载可能争用浏览器目录 | 共用私有浏览器文件锁；SESSION_BUSY 任务回到队列并延迟重试，避免直接丢成永久失败；互斥锁测试通过 |
| 新生成的来源链接含临时查询参数，图文生成目录没有软链接检查 | 来源引用可能携带临时 token，生成路径保护不一致 | 新生成的知识文档及视频来源标签使用无查询参数的规范链接；图文目录/文件拒绝软链接 |
| worker 默认 SIGTERM 不利于 Python finally 执行 | 加载修复时可能丢失正在记录的失败请求时长 | 用户服务改用 SIGINT；已重启加载新代码，已有 ASR/切片检查点保留，worker 活跃 |
| 原解析器要求小红书标题非空 | 没有独立标题、仅有正文/视频的四条笔记被误报登录失效或不可用 | 接受有效正文/视频，缺少标题时生成显示标题；四条真实来源修复后全部可读取 |
| SECURITY.md 仍把所有 MCP 工具描述为只读 | 安全边界描述不符合视频导入接口 | 明确区分旧只读 MCP 与可写的视频扩展入口及授权标记 |

## 流程核对

1. 使用已有 owner-only Cookie 调用 /user/me，固定当前账号；不接受任意账号 ID。
2. 每页读取收藏，校验分页结构，间隔 2 秒。每日从第一页重新扫描，避免续扫老游标时漏掉新收藏。
3. 将每条可读取收藏提交 PostgreSQL 持久化队列；数据库去重允许扫描页在崩溃后重放。
4. 扫描进程使用独立文件锁；数据库入队使用现有 advisory lock。同库单 worker 保持模型资源串行，浏览器登录另有共用锁。
5. worker 下载时检查来源，视频优先读字幕，无字幕时做本地 Faster Whisper 转录。取消强制分类和 cooking gate；显式启用视觉后，各主题视频均可抽帧、VLM 分析和本地 ImageBind 编码。确认无语音时使用真实视频时间轴做画面分析，不生成虚构字幕。图文当前只导入作者标题/正文，没有图片 OCR。
6. 已发布知识和证据先持久化，之后删除任务拥有的原视频。失败媒体继续使用现有 TTL，不被每晚同步无条件重新创建。
7. 收藏扫描 complete 仅表示列表已入队；数据库中的 queued/running/blocked/complete 分别表示实际处理状态。

## 验证结果

- 普通测试：158 passed，12 skipped。跳过包含 4 个需要显式私有数据库配置的队列测试。
- 显式真实 PostgreSQL 测试：另行运行这 4 个队列测试，全部通过；测试使用临时 library ID，已清理测试行，不切换索引、不调用大模型。
- 真实夜间服务手动启动验证：21 页，203 次读取均去重，0 个新增任务，扫描 complete；service Result=success / ExecMainStatus=0。
- 实际完成定时器卸载及重新安装，原 Cookie、知识库和后台 worker 保留，定时器最终 active/enabled。
- Ruff、compileall、shell 语法及 git diff --check 通过。Cookie 与账号绑定状态为 owner-only，运行目录被 Git 忽略。
- 没有重启整台机器进行验收，也没有重新登录来测试手机验证码流程。

## 已启用的每日调度

- 原生 systemd 用户定时器：rag-favorite-favorites.timer。
- 当前时间：每天 07:00 和 18:00，America/Chicago，自动遵循芝加哥夏令时。按用户最新要求替换原 22:00 设置。
- 配置命令：`bash examples/video_runtime.sh favorites-schedule --hour 7 18 --timezone America/Chicago`。
- 入口默认 all，不要求选择类别；写入现有默认存储分区（旧 key 为 cooking），检索覆盖全部配置分区。该 key 仅用于数据兼容，不限制主题或视觉处理；不迁移或重建已有索引。
- 不依赖 Codex 桌面窗口或 agent 回合，服务使用保存的固定文本/视频配置。
- Persistent=true；电脑关机期间不能执行，启动用户服务管理器后会补跑一次错过的同步。已验证 Linger=yes。
- 每次从第一页扫描，已有记录去重；不自动重试视觉预算、无语音、已过期媒体等失败任务。
- 单次同步最长 30 分钟。超时/中断会停止，下次定时运行从头安全重扫；部分和失败状态返回非零。

```bash
# 查看定时器和收藏扫描状态
bash examples/video_runtime.sh favorites-nightly-status

# 手动同步/续扫
bash examples/video_runtime.sh cli import-favorites

# 看实际内容处理队列
bash examples/video_runtime.sh cli status

# 夜间同步日志
journalctl --user -u rag-favorite-favorites.service -n 40 --no-pager

# 改为北京时间 22 点
bash examples/video_runtime.sh favorites-schedule --hour 22 --timezone Asia/Shanghai

# 撤销夜间调度，保留数据与已有 worker
bash examples/video_runtime.sh favorites-schedule --uninstall
```

## 保留的实际限制

平台仍可使登录失效；不存在永久 Cookie 保证，需要验证时会明确失败并保留队列，用户重新扫码后再同步。没有存储账号密码。非官方分页接口变化会明确停止，不能把部分扫描误报为全部完成。图文图片内容尚不进入检索。主题不会阻断视觉处理；无语音视觉处理也受现有单视频预算约束。已有待处理和 blocked 任务并未因设置定时器而成为处理成功。新的统一库行为及受阻任务明细见 [统一知识库修改记录](unified-library-update.md)。

机器可读验证记录：xhs-favorites-audit-results.json。
