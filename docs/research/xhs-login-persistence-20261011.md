# 小红书登录持久化方案调研

后续针对远程使用约束、当前服务状态与下载/收藏日志的深入核对，见 [详细推荐与实施边界](xhs-session-recommendation-20261011.md)。后续报告中的新运行证据优先于本文早期快照。

日期：2026-10-11。状态：仅完成网络资料调研；没有实现、安装、更改登录、重启服务或验证账号的远端登录状态。以下以官方文档、项目维护者 README 和公开源码为证据，`main` 分支与在线文档可能继续变化。

## 结论与适用方案

**可以减少因本地状态丢失、Cookie 快照落后或会话互相顶替造成的重复登录；没有足够证据承诺网页版账号永久免登录。** 对现有采集程序，优先复用同一个持久浏览器 profile，统一登录工具与采集器使用的 Cookie 路径，在明确的认证失败时从该 profile 重新导出并只重试一次。若浏览器本身已被平台要求验证，仍需用户完成验证。此建议是由下列实现推导的工程方案，尚未在本项目实施或验收。

## 一手资料确认了什么

### 1. MediaCrawler：保存 profile、复用已有浏览器

普通模式在 `SAVE_LOGIN_STATE` 开启时调用 `launch_persistent_context`，目录为工作目录下 `browser_data/xhs_user_data_dir`。因此重新运行程序无需主动丢弃原有浏览器状态；更换工作目录也可能改变实际使用的状态目录。[小红书启动源码](https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/core.py)

其 CDP 模式可以连接已有浏览器并复用已有 context；自行启动时，持久目录带 `cdp_` 前缀。普通模式和 CDP 自启动模式的目录不同，切换模式不等于自动继承原登录。配置提供 `ENABLE_CDP_MODE`、`CDP_CONNECT_EXISTING` 和 `SAVE_LOGIN_STATE`，但这些能力本身不构成平台永久续期保证。[CDP 管理源码](https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/cdp_browser.py)、[配置源码](https://github.com/NanmiCoder/MediaCrawler/blob/main/config/base_config.py)

若选用命令行远程调试，Chrome 官方说明从 136 起，调试开关不能用于默认数据目录，需指定非默认 `--user-data-dir`；应先核对所用浏览器版本与连接方式。[Chrome 官方说明](https://developer.chrome.com/blog/remote-debugging-port?hl=zh-cn)

### 2. xpzouying/xiaohongshu-mcp：登录后保存 Cookie，启动时读取

当前源码在扫码成功后导出浏览器 Cookie；创建浏览器时再从文件读取并注入。它解决的是程序重启后的状态复用，没有将本地 Cookie 保存解释为平台无限延期。[登录保存源码](https://github.com/xpzouying/xiaohongshu-mcp/blob/main/service.go)、[浏览器加载源码](https://github.com/xpzouying/xiaohongshu-mcp/blob/main/browser/browser.go)

Cookie 路径有版本差异：本次读取的 `main` 优先使用显式 `COOKIES_PATH`，否则使用本地目录 `cookies.json`，并兼容旧 `/tmp/cookies.json`；某些 Release 说明则写 `~/.xiaohongshu/cookies.json`。不要据网上示例假定两个进程读写同一文件，应核对实际版本并明确指定路径。[路径源码](https://github.com/xpzouying/xiaohongshu-mcp/blob/main/cookies/cookies.go)、[Release 说明](https://github.com/xpzouying/xiaohongshu-mcp/releases)

维护者 README 提醒同一账号在另一个网页端登录可能踢出当前 MCP 会话，且作者自己的长期运行仍遇到 Cookie 过期。这是维护者经验，未找到与此对应的官方网页端并发登录规则，不能推成所有账号、所有版本的确定行为。[维护者 README](https://github.com/xpzouying/xiaohongshu-mcp/blob/main/README.md)

### 3. jackwener/xiaohongshu-cli：自动从浏览器同步，不是制造新登录

维护者 README 说明支持已保存 Cookie、从本地浏览器提取 Cookie，以及二维码登录；认证失效时可从浏览器重新提取并重试一次。它可以减少手动复制 Cookie 的次数，前提是来源浏览器仍有可用会话，且本机提取方式能够读取该 profile。[认证说明](https://github.com/jackwener/xiaohongshu-cli/blob/main/README.md)

`cookies.py` 的 `COOKIE_TTL_DAYS = 7` 按本地 `saved_at` 计算，超过七天就尝试读取浏览器；失败时保留原 Cookie 并警告。**七天是工具自己的刷新策略，不能据此断言小红书的 `web_session` 七天过期，也不能从延长该常量推导平台会话永久有效。** [TTL 与回退源码](https://github.com/jackwener/xiaohongshu-cli/blob/main/xhs_cli/cookies.py)

### 4. 官方确有 OAuth Token 刷新，但目前不能替代这里的采集登录

小红书账号开放平台公开 `POST /api/sns/v1/oauth2/refresh_token`，使用应用 `app_id` 和 `refresh_token` 换取 `access_token`；这是应用授权体系，不是给网页版 `web_session` Cookie 续期的接口。官方 FAQ 说明刷新不会延长 refresh token 自首次签发起 180 天的期限，到期仍需重新授权。[API 参考](https://openaccount.xiaohongshu.com/docs/api-reference)、[官方 FAQ](https://openaccount.xiaohongshu.com/docs/faq)

接入需要提交应用主体、名称、平台及权限等信息并通过审核。当前公开 Scope 文档只开放 `basic_info`；读取笔记 `read_notes`、读取关注/粉丝列表 `read_followers` 为规划中，未列出可用的收藏列表读取权限。因此不能把官方 OAuth 自动刷新直接接到本项目现有收藏、关注和笔记私有网页接口上，也不能承诺提交应用后必定获得这些权限。[快速接入](https://openaccount.xiaohongshu.com/docs/quick-start)、[权限范围](https://openaccount.xiaohongshu.com/docs/scope)

本次检索未找到官方公开的普通网页 `web_session` refresh token API。这个结论只描述本次公开资料的检索结果，不证明平台内部或受限合作能力不存在。

### 5. 心跳／定时访问：尚不足以承诺永久免登录

在上述三个项目的相关 README、登录与 Cookie 源码，以及账号开放平台资料中，没有找到带有效期测量或明确平台规则的证据，证明“每隔几小时访问一次”能让普通网页版登录无限延期。保存 Cookie、重新读取浏览器 Cookie、刷新 OAuth access token 是三种不同机制，不能把其中任何一种直接叫作网页版永久保活。

小程序官方确实说明使用频率会影响 `session_key` 的有效期，但该密钥来自小程序 `code2Session` 登录体系；不能把此规则套用于网页版 `web_session`。[小程序会话有效性说明](https://miniapp.xiaohongshu.com/doc/DC591932)、[小程序登录态管理](https://miniapp.xiaohongshu.com/doc/DC473950)

## 对现有项目的建议边界

| 建议 | 预期作用 | 仍无法保证的部分 |
| --- | --- | --- |
| 固定唯一持久 profile，并统一登录、导出、采集路径 | 减少状态目录或 Cookie 文件错位 | 平台可能撤销有效会话 |
| 使用仍登录的 profile 按需重新导出 | 减少手动复制和使用旧快照 | 来源浏览器失效时不能无交互恢复 |
| 认证失败后重新加载会话、同步签名器并重试一次 | 验证是否仅为本地快照落后 | 风控、验证码、IP 风险与认证过期应分别处理 |
| 保留真实错误、导出时间与健康状态 | 判断问题发生在哪一层 | 本地 Cookie 到期日不代表服务端接受期限 |

以上是需要结合现有实现接通、核对的工程建议；部分基础能力已存在，具体证据如下。本报告不等于这些建议已在生产链路实施或验收。

## 本地只读检查补充

本次只读检查源码及文件元数据，没有打开用户浏览器、请求小红书、运行登录或导出命令、修改程序、启停服务或操作队列。

- **已保存固定浏览器环境。** [当前登录脚本](../../examples/xhs_login.py) 使用 `.runtime/videorag/xhs-session/profile` 启动持久 context；验证成功后导出 `cookies.txt` 随即退出。因此无需重新设计一套持久 profile，但该脚本本身不是持续同步进程。
- **采集端仍依赖导出的快照。** [签名请求客户端](../../src/rag_favorite/xhs_client.py) 在初始化时读取 `cookies.txt`，签名使用当时建立的 Cookie 字典。HTTP CookieJar 可以接收响应 Cookie，但这个类没有将更新保存到磁盘，也没有重新构建签名字典。[笔记下载入口](../../src/rag_favorite/video_sources.py) 同样读取该文件；[关注列表浏览器读取](../../src/rag_favorite/xhs_following_client.py) 复用 profile，却没有重新导出 Cookie。由此可推导同步改进点，不能据静态代码证明本次反复登录就是它造成的。
- **仓库已有会话管理器，可优先复用。** [会话管理器](../../ingestion/backend/xhs_session/cli.py) 能在浏览器仍已登录时自动导出；[timer 模板](../../ingestion/deploy/xhs-session-check.timer) 是每六小时触发，带十五分钟随机延迟。不过它的默认会话目录、配置项及状态文件结构与上述 RAG 登录入口不同，需核对并统一。[配置来源](../../ingestion/backend/ingestion/config.py) 的默认目录为 `PRODUCT_DATA/sessions/xiaohongshu`。本次 `systemctl --user show` 因沙箱无法连接用户总线，未确认 timer 已安装、启用或正在运行。
- **当前本地文件没有到期。** 检查时 `cookies.txt` 和 `status.json` 的修改时间为 2026-10-11 06:28:07 UTC，状态字段为 `cookies_exported`；`a1` 的本地到期日为 2027-10-04，`web_session` 为 2027-10-11，均未本地过期。只输出名称、时间和布尔状态，没有记录 Cookie 值。本地一年期时间戳及导出标记不证明服务端当前接受它，更不保证一年内不会要求验证。
- **近期存在风控证据，不能统一归因为过期。** [最近的恢复记录](../implementation/xhs-download-recovery-20261011.md) 记录了 `300012` 和页面 IP 风险提示。这是先前记录，早于上述最新 Cookie 导出；本次未验证远端限制是否已解除。当前签名客户端把 `-100`、`-101`、`300012`、`300013` 合并为登录或验证错误；后续应保留具体类别，风控时停止并冷却，只有确认浏览器认证失效才提示重新扫码。

优先方案是复用现有管理器，统一 profile、Cookie 路径、锁与状态结构；在低频检查或明确认证失败时验证浏览器会话，仍有效则原子导出、重新加载 Cookie 与签名器并重试一次。检查频率只是工程参数，不是已经证实的小红书保活规则。真实过期、短信/滑块验证或 IP 风控仍可能需要用户处理。
