# 私有网页任务控制台

网页与本地桌面 GUI 共用 `QueueMonitor`、PostgreSQL 和 `queue-control.json`。
手机与电脑看到的是同一任务队列。关闭浏览器不影响后台 worker。
页面适配手机，包含统计、模型调用、任务搜索与筛选、分页、详情、结构化日志，
以及全局和单任务 Start / Stop。每 3 秒刷新一次；数据过时会明确提示并禁用操作。
任务进度只表示当前阶段，不估算总完成百分比。

## 安装和接入

在项目目录执行：

```bash
.venv/bin/python -m pip install -e '.[web]'
.venv/bin/python -m rag_favorite.web_console --settings .runtime/web/settings.json --init
.venv/bin/python examples/install_web_console.py
tailscale serve --bg http://127.0.0.1:8765
tailscale serve status
```

首次运行 Serve 可能要求在 Tailscale 管理页面开启 HTTPS。只使用 Serve，
不启用公网 Funnel，不做路由器端口转发。免费模式采用 Tailscale Personal，
限个人非商业用途；在管理后台确认套餐，不启用付费试用。
参考 [Serve 官方文档](https://tailscale.com/docs/features/tailscale-serve) 和
[官方价格](https://tailscale.com/pricing)。

手机、电脑安装 Tailscale，登录同一所有者账号并连接私人网络，然后访问 Serve
显示的 `https://设备名.私人网络.ts.net`。Linux 本地浏览器也使用这个 HTTPS 地址；
直接访问 `http://localhost:8765` 会被拒绝。

登录用户名为 `admin`。初始随机密码存放于 `.runtime/web/initial-password.txt`，
仅服务器当前用户可读；不要将密码放入 URL 或发到聊天。可用本地文件管理器打开，
或在自己终端执行 `cat .runtime/web/initial-password.txt`。
初始化不会覆盖已有配置；配置权限必须是 600。

更换密码：

```bash
.venv/bin/python -m rag_favorite.web_console --settings .runtime/web/settings.json --reset-password
systemctl --user restart rag-favorite-web.service
```

输入至少 16 个字符的新密码；输入不回显。更换后删除初始密码文件，重启使旧会话失效。

## 访问限制

应用仅监听 `127.0.0.1:8765`；Uvicorn 不接受自动代理头改写。
每个请求同时检查本机代理来源、配置的网站地址、HTTPS、Serve 的所有者身份、
设备 Tailscale IP 白名单，并拒绝 Funnel 请求。Serve 会覆盖身份和来源地址头，
参考 [对应版本的实现](https://github.com/tailscale/tailscale/blob/v1.102.4/ipn/ipnlocal/serve.go)。
Linux 本机经 Serve 访问可能没有身份头，仅在来源属于配置的本机地址时允许。
持有网站密码仍不能绕过网络设备限制；本机同一 Linux 用户和 root 属于受信任边界。

初始化只批准当前所有者的现有设备。新设备需人工审批后，将其 Tailscale IPv4/IPv6
地址加入私有配置 `allowed_ips` 并重启服务；撤销设备时同时删除配置中的地址。
不要自动允许所有新加入的设备，不将 `owner_login` 改成其他账号。
`settings.json` 中 `self_ips` 仅包含这台 Linux 的 Tailscale 地址。

**管理后台还需要完成以下设置，应用白名单不能代替这些网络设置：**

- 确认 Personal 免费套餐，账号开启多因素认证。
- 开启新设备审批，检查现有 Linux、手机和电脑，移除不认识的设备。
- 检查 Access controls：仅批准设备可访问 Linux 的 TCP 443；保留原有必要 SSH 权限。
  授权规则是叠加关系，添加窄规则不会抵消原有 `*` 到 `*` 的宽规则，
  需要同步收紧覆盖网页端口的已有授权；不要直接替换整个策略导致 SSH 中断。
- 不分享此服务器；禁用或删除允许此服务器使用 Funnel 的授权。

设备审批适用于所有套餐，见 [官方说明](https://tailscale.com/docs/features/access-control/device-management/device-approval)。
部署时生成的 `.runtime/web/access-policy-fragment.json` 只包含网页授权候选片段，
用于人工合并和检查；它不是完整策略，不能直接覆盖管理后台。

网页另外使用独立密码（scrypt 哈希）、8 小时会话、设备绑定、登录限速、
Secure / HttpOnly / SameSite Cookie、Origin 和 CSRF 校验。
服务重启使所有会话失效。密码和 Cookie 不写入访问日志。
前端资源完全本地提供，不加载 CDN；任务标题按文本显示，防止 HTML 注入。

## 操作、接口和错误

- 全局 Start 恢复领取，必要时启动已有 worker；不自动恢复单独暂停的任务。
- 全局 Stop 暂停新任务领取；已领取任务继续完成、发布及清理，不强制杀进程。
- 单任务 Start 恢复暂停或重试失败 / 受阻任务；单任务 Stop 暂停后续领取。
- 禁止对已完成或其他知识库的任务执行操作。重复请求与并发操作会被拒绝。
- 数据库离线时保留上次快照并标为过时；恢复后自动更新。锁超时或后台错误
  不回传原始异常，页面提示结果未能确认，须刷新核对，避免把错误当成成功。

接口：`POST /api/login`、`GET /api/session`、`POST /api/logout`、
`GET /api/snapshot`、`POST /api/action`。写入必须通过登录、来源和 CSRF 校验。
动作请求为 `{action, job_id?, request_id}`，动作只有 `start`、`stop`、
`start_job`、`stop_job`，`request_id` 为每次操作生成的 UUID。
状态接口返回 `{snapshot, stale, age_seconds, error}`；过时返回 503 并保留已有数据。
快照仅公开明确允许的任务、模型与结构化日志字段，不返回 provider 配置及原始载荷。

## 验收、运行和卸载

```bash
systemctl --user status rag-favorite-web.service
journalctl --user -u rag-favorite-web.service -n 30 --no-pager
tailscale serve status --json
tailscale funnel status --json
ss -lnt
.venv/bin/python -m pytest tests/test_web_console.py tests/test_queue_control.py -q
RAG_GUI_TEST_CONFIG="$PWD/.runtime/phase1/config.toml" .venv/bin/python -m pytest tests/test_queue_monitor_integration.py -q
```

数据库集成测试使用随机隔离知识库，结束后清理测试任务，不操作生产队列。
真实手机蜂窝网络、外部电脑网络、未批准设备和其他账号必须分别实测，
HTTP 本机测试和模拟代理头不能代替这些验收。
user systemd 服务随用户管理器启动；在管理器停止时不可用，需根据服务器
已有设置确认开机无人登录运行。服务器停机、休眠或失去网络时无法访问。

停用网站（不影响现有 GUI 和 worker）：

```bash
tailscale serve --https=443 off
.venv/bin/python examples/install_web_console.py --uninstall
```

关闭 443 前检查没有其他服务使用该映射；卸载保留配置与凭据，按需自行删除。
此方案不保证账号、已授权设备或服务器被攻陷后仍安全；遗失设备应立即撤销，
必要时更换网页密码并重启服务。
