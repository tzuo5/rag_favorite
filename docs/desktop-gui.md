# 桌面网页快捷方式

双击桌面的 **RAG 任务控制台**，在默认浏览器打开已有的私有 HTTPS 网页。
桌面和应用菜单使用同一个入口。关闭浏览器不影响后台处理。

原生 Qt 窗口及 PySide6 依赖已移除；任务监控、暂停、恢复、模型状态和账号操作统一在网页中完成。
网页继续使用原来的登录与 Tailscale 访问限制，快捷方式不包含密码。

在项目目录安装或更新桌面及应用菜单图标：

```bash
.venv/bin/python examples/install_desktop_gui.py
```

安装器沿用旧名称，以便原地替换已有图标。目标地址来自 `.runtime/web/settings.json` 的 `origin`。
修改网站地址后重新运行安装器。需要先部署[网页服务](web-console.md)。

命令行也可打开网页，旧 `rag-favorite-gui` 命令安装更新后仍可使用：

```bash
.venv/bin/python -m rag_favorite.desktop_launcher
.venv/bin/python -m rag_favorite.desktop_launcher --print-url
```

移除快捷方式：

```bash
.venv/bin/python examples/install_desktop_gui.py --uninstall
```

此操作只移除入口。网页服务、队列和知识库继续保留。
