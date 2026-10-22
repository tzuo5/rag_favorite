# RAG 控制台双端 UI 实施与验收

公开版本已匿名化本机入口和任务标识；原始验收记录保留在本地。
2026-10-11 更新至现有私有控制台：<https://your-node.your-tailnet.ts.net>。
在开始实施时已有的未提交改动上增量完成，保留现有小红书会话、流水线、桌面 GUI 和队列控制。
没有新增数据库结构或分类体系，没有改动密码、设备白名单或 Tailscale 路由。

## 已交付

- 四个入口：总览、任务、运行、账号。中性色背景、蓝色强调，支持系统 / 浅色 / 深色主题。
- 小于 1024 CSS px 使用底部导航、任务卡片、约 75dvh 的详情弹层；支持展开、关闭、返回关闭、焦点和滚动位置恢复。
- 1024 CSS px 起使用侧栏、紧凑任务列表、右侧详情；详情操作区按可用视口高度调整。
- 总览显示队列状态、展示状态统计、独立的可检索完成数量、四阶段概况和按原因码归并的异常。
  登录和验证提示置顶；未知原因单独标注，不推断为登录问题。全局暂停在各入口顶部提示，等待任务仍计入等待中。
- 全库字面子串搜索、状态 / 原因 / 原有集合筛选、分页、独立详情和可见任务批量更新。
- 每 3 秒前台刷新；更新可见状态而保留列表成员和排序，点击“列表有更新”才重排。
  搜索防抖并取消旧请求；隐藏页面停止前端轮询。断网只保留同一查询的数据，禁用相关操作。
- 全局暂停 / 恢复有影响说明和确认；单任务直接操作，反馈保留在详情中，不被轮询覆盖。
- 小红书二维码、二维码链接、短信和验证画面保留。图片载入后才接受拖动，拖动期间不刷新画面，放大后按实际显示尺寸映射坐标。
- 输入字号至少 16 CSS px、主要触控目标至少 44 CSS px，保留缩放、底部安全区、减少动画和弹层焦点管理。
- 运行页分别展示 HTTP 成功、有效输出、流完成状态；最近调用和日志明确注明保留范围。

## 数据接口

新增 `src/rag_favorite/web_jobs.py`，由 `web_console.py` 提供：

| 接口 | 合约 |
| --- | --- |
| `GET /api/jobs` | `q`、`state`、`reason`、`collection`、`page`、`page_size`；默认 40，上限 100；返回匹配总数、页码、采集时间、`display_state`。 |
| `GET /api/jobs/{id}` | 当前服务配置知识库中的独立详情。 |
| `GET /api/jobs/updates?ids=…` | 最多 100 个 UUID，批量状态和缺失 ID。 |
| `GET /api/snapshot?include_jobs=false` | 去除任务及阶段明细数组，增加全库展示状态数量和原因汇总。 |

默认 `/api/snapshot` 与 `QueueMonitor.snapshot()` 的合约保留。
查询只能访问服务端配置的知识库；标题、任务 ID、任务或阶段错误码使用参数化字面子串搜索。
展示状态在列表、筛选和统计中共用。阶段关联采用单行关联，避免多阶段产生重复任务；
终态任务不关联过期阶段，阶段库必须与任务库一致。队列排名先于筛选和分页计算，排序以 ID 稳定收尾。

查询使用只读、一致性事务及 5 秒 SQL 超时；鉴权、网络来源限制、写操作 Origin / CSRF 和字段白名单保留。
接口不返回原始 payload、凭据或数据库异常原文。

## 自动化结果

最终两组执行共 **98 项通过**，无失败、无跳过。
有一项现有 Starlette TestClient 使用 httpx 的弃用提示，不影响本轮结果。

| 范围 | 结果与证据 |
| --- | --- |
| Web 安全、查询、运行修复、队列控制、桌面 GUI | 60 项通过，7.76 秒。 |
| UI Chromium + WebKit，以及已有小红书合成浏览器测试 | 38 项通过，50.56 秒。 |
| 浏览器版本 | Chromium 154.0.8037.97；WebKit 26.6。均为本机自动化运行时。 |
| 视口和主题 | 402×874、874×402、375×812、1440×800、1280×720；每个视口覆盖深浅主题与四个入口。桌面视口使用桌面浏览器上下文。 |
| 交互 | 详情展开、返回 / 关闭、刷新恢复详情、焦点与滚动恢复；稳定列表、防抖 / 过期响应、分页、原因筛选、空结果、长标题 / 长错误；确认、成功 / 失败反馈、断网恢复、会话失效及重新登录。 |
| 隐藏页面与键盘 | 隐藏状态下无轮询请求；返回立即更新。通过合成 visualViewport 变化验证键盘状态和底部导航处理。 |
| 缩放与动画 | 720×400 CSS 视口覆盖 1440×800 在 200% 缩放时的等效重排空间；减少动画、16px 输入字号、44px 导航触控检查通过。未声称操作了真机浏览器 200% 缩放。 |
| 小红书 | 所有数据和画面均为合成。核对画面实际解码、正常 / 放大拖动坐标、拖动期间不刷新、二维码链接、验证码清空及原有持久化临时配置测试。没有触发真实账号登录。 |
| 全库 SQL | 每次创建 2405 条随机隔离库任务，加外库任务；验证原 2000 条之外的查询、61 页和总数、字面 `%` / `_` / `\`、稳定排序、筛选前排名、阶段错误码、暂停 / 重试等待、原因分组、跨库隔离和脱敏，结束后清理。 |

浏览器请求使用合成数据拦截，保留生产 CSP；只允许同页合成 blob 图片由浏览器本地加载。
WebKit 依赖从 Ubuntu 官方仓库解包到 `/tmp/rag-ui-webkit-deps`，未安装系统包或修改系统库。

执行命令：

```bash
RAG_GUI_TEST_CONFIG="$PWD/.runtime/phase1/config.toml" \
QT_QPA_PLATFORM=offscreen \
timeout 180 .venv/bin/python -m pytest \
  tests/test_web_console.py tests/test_web_jobs.py tests/test_runtime_repairs.py \
  tests/test_queue_control.py tests/test_queue_monitor_integration.py \
  tests/test_desktop_gui_responsiveness.py -q

RAG_UI_WEBKIT_EXECUTABLE=/tmp/rag-ui-webkit-deps/webkit-with-local-libs.sh \
RAG_BROWSER_TEST=1 RAG_UI_TEST_ENGINES=chromium,webkit \
RAG_UI_ARTIFACTS="$PWD/.runtime/ui-redesign-20261011/screenshots" \
timeout 180 .venv/bin/python -m pytest \
  tests/test_web_console_browser.py tests/test_xhs_phone_browser.py -q
```

标准 Playwright 依赖齐全的机器可省略 `RAG_UI_WEBKIT_EXECUTABLE`；本机临时目录清理后需重新准备测试运行时。
相关 Python 文件 Ruff、两份 JavaScript 语法检查和本轮文件的 `git diff --check` 通过。

## 真实数据与上线

上线前只读查询实际配置的知识库：**2717 条任务，68 页，末页 37 条**。
末项可按 ID 精确搜索、独立读取和批量更新读取。这里指真实数据库查询层验证，未混同为登录后的生产 HTTP 请求。

2026-10-11 21:50 UTC 仅重启 `rag-favorite-web.service`：

| 服务 | 上线前 PID | 上线后 PID |
| --- | ---: | ---: |
| Web | 520956 | 1496602 |
| 视频 worker | 535948 | 535948 |
| 小红书会话 | 535791 | 535791 |
| ImageBind | 2023088 | 2023088 |

这些服务检查时均为 active / running。全局暂停为 false，单独暂停为 0；队列控制内容未改变。
现有私有 HTTPS 首页及三个静态资源返回 200，资源内容哈希与本地完全一致。
`/api/jobs`、独立详情、批量更新、精简快照和 `/api/session` 在未登录时均返回 401；API 文档入口仍为关闭的 404。

验证期间本机绑定回环来源自访被拒绝为 403，默认 Tailscale 路由及已配置本机 Tailscale 地址正常返回 200；
未放宽白名单。没有可用的本地网页登录凭据，因此自动化没有重置密码或模拟生产登录会话。
随后用户在两台真机完成重新登录并确认基础操作正常，记录如下。

## 真机验收

| 设备 | 结果 | 确认方式与范围 |
| --- | --- | --- |
| iPhone 16 Pro Chrome | 通过基础验收 | 用户在本对话确认：重新登录、四项导航、任务搜索和详情均正常。 |
| MacBook Air 2020 Chrome | 通过基础验收 | 用户在本对话确认：重新登录、四项导航、任务搜索和详情均正常。 |

真机反馈不是本机自动化截图；没有把 Chromium 模拟等同于 iOS Chrome。
引擎背景见 [Chrome 官方说明](https://developer.chrome.com/docs/web-platform/blink)。
以下专项未获得逐项真机确认：软键盘和地址栏变化、真机浏览器 200% 缩放、真实小红书扫码 / 短信 / 拖动验证、
切换外部网络后的重连。自动化已覆盖相应的合成场景或等效布局，真实平台交互仍留作专项验收。

## 产物与回滚

运行结果和合成截图保存在被 Git 忽略的 `.runtime/ui-redesign-20261011/`：

- `rollout-results.json`：服务、资源、接口鉴权及用户真机反馈。
- `pre-rollout.json`：上线前只读查询和 PID。
- `screenshots/browser-versions.json`：实际自动化浏览器版本。
- `screenshots/*`：明确使用演示数据的手机 / 电脑深浅主题和详情截图。
- `deployed-files.json`：网页运行文件哈希。
- `rollback-web.py`：仅恢复五个网页运行文件并重启 Web；若上线后这些文件又被修改，则拒绝覆盖。

实施前备份：`.runtime/ui-redesign-backup-20261011T211114Z/`。
备份保存的是本轮开始时的工作区内容，包括原有未提交改动；不是 Git HEAD。

需要回滚网页运行版本时，在仓库目录执行：

```bash
.venv/bin/python .runtime/ui-redesign-20261011/rollback-web.py
```

回滚不会重启 worker 或小红书会话，不修改队列、数据库、登录配置，也不删除本轮报告、测试或新查询模块。
回滚后的旧 Web 入口不再引用新查询模块。回滚脚本已准备，未执行。
