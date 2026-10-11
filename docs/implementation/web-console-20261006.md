# 私有网页控制台实施与验收

日期：2026-10-06。当前状态：**网页、本机服务及私有 HTTPS 已部署并实测；跨设备最终验收和管理后台设置待完成。**

## 已实现和部署

- FastAPI / Uvicorn 可选 web 入口，本地前端资源，响应式手机页面。
- 复用现有 QueueMonitor：统计、模型调用、筛选搜索、分页、任务详情、结构化日志，
  全局与单任务 Start / Stop；没有新增另一套队列或更改 worker 调度语义。
- 每 3 秒后台读取，快照失败保留旧数据、标为过时并禁用前端操作；恢复自动刷新。
- 本机代理来源、HTTPS 地址、所有者身份、设备 IP 白名单四重检查，拒绝 Funnel；
  单用户 scrypt 密码、8 小时设备绑定会话、Cookie 安全属性、限速、Origin / CSRF 校验。
- 仅公开快照白名单字段，验证错误不回显输入；前端用文本节点显示任务标题，
  不加载外部 CDN，访问日志关闭，配置与初始密码权限 600，目录权限 700。
- 用户服务 `rag-favorite-web.service` 已启用，监听 `127.0.0.1:8765`。
  当前用户 `Linger=yes`，允许用户服务在无人登录时持续运行；开机恢复未进行重启实测。

## 验证证据

- 40 项相关测试通过：18 项网页访问与操作测试，以及队列锁、GUI 响应和路由状态回归。
- 8 项真实 PostgreSQL 隔离知识库集成测试通过，涵盖网页和桌面共享任务状态、全局与
  单任务暂停恢复、失败重试、跨知识库拒绝；测试任务结束后删除。
- 生产服务只读快照实际返回 694 项任务，生产队列未执行暂停或重试操作。
- Chrome 实测 390×844 手机和 1440×1000 电脑布局：无横向溢出，搜索、分页显示、
  退出隐藏数据、网络断开禁用操作、恢复更新通过，无 JavaScript 页面错误。
- HTTP 本机实测拒绝直接访问、其他所有者、非白名单设备、未登录快照及无 CSRF 操作。
- Ruff、JavaScript 语法和改动空白检查通过。测试环境的 Starlette 产生 httpx
  将来迁移至 httpx2 的弃用提示，不影响本次测试结果。

第一轮本机浏览器验证使用测试桥接注入 Serve 头访问真实本机服务。
机器可读证据与预览保存在 `.runtime/web/`，
包括 `local-verification.json`、`mobile-preview.png`、`desktop-preview.png`。

## 私有 HTTPS 实测（当地时间 2026-10-06 19:04）

所有者在手机完成 Serve 启用后，已建立持久映射：

`https://tzuo5-linux.tail9064aa.ts.net/` → `http://127.0.0.1:8765`

- Serve 配置仅含 TCP 443 HTTPS 和上述代理，不含 `AllowFunnel` 公网授权。
  `tailscale funnel status` 也会显示同一 Serve 配置，不能把其非空输出误当成已发布公网。
- Linux 通过真实 HTTPS 访问首页返回 200，curl 证书验证结果为 0；未使用跳过证书校验。
- 实际登录与实时快照返回 200，当前 694 项任务；会话 Cookie 安全属性正确。
- 未登录和退出后快照返回 401，无 CSRF 或错误 Origin 的操作返回 403，未执行生产队列操作。
- 伪造身份、来源地址和 Funnel 头的 HTTPS 请求被 Serve 覆盖，无法改写后端识别的真实来源。
- Chrome 通过真实 HTTPS 完成登录、任务显示及退出，确认安全上下文，手机和电脑尺寸
  无横向溢出，无页面 JavaScript 错误。
- 证据：`.runtime/web/https-verification.json`、`mobile-https-preview.png`、`desktop-https-preview.png`。

本轮使用 Linux 本机到自身 Serve 的真实 HTTPS 连接，**仍不能代替手机蜂窝网络及
Mac 外部网络访问，也不能代替其他账号或未批准设备的实际拒绝测试。**

## 尚需完成的外部设置及验收

- 在管理后台确认 Personal 免费套餐、开启多因素认证和新设备审批。
- 合并设备级 TCP 443 限制并收紧覆盖该端口的宽授权，保留既有 SSH 权限。
  本机生成的 `.runtime/web/access-policy-fragment.json` 为候选片段，未提交后台。
- 完成 iPhone 蜂窝网络、Mac 外部网络访问；Linux 本机 HTTPS 已通过。
- 实测公网、未批准设备、其他账号拒绝访问。当前只有应用层模拟、本机拒绝和真实 Serve 头覆盖证据。

操作、改密、接入、停用和卸载方式见 [网页控制台文档](../web-console.md)。
