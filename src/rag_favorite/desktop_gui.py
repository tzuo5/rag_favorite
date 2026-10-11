"""Native desktop queue monitor. Install the optional ``gui`` dependencies."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .config import ConfigError, load_config
from .queue_monitor import PIPELINE_STAGES, QueueMonitor
from .video_config import load_video_config

STYLE = """
QWidget { background: #111c27; color: #dbe5ef; font-size: 13px; }
QMainWindow { background: #111c27; }
QFrame#Panel, QFrame#Stat, QFrame#ModelCard {
    background: #1a2937; border: 1px solid #2b4052; border-radius: 9px;
}
QFrame#Stat QLabel, QFrame#ModelCard QLabel, QFrame#Panel QLabel { background: transparent; border: none; }
QLabel#Title { font-size: 25px; font-weight: 700; }
QLabel#Section { font-size: 18px; font-weight: 600; }
QLabel#Muted { color: #94a9bc; }
QLabel#Badge { background: #204450; color: #74dedc; border-radius: 12px; padding: 6px 12px; }
QLabel#StatValue { font-size: 27px; font-weight: 600; }
QLabel#ModelName { font-size: 14px; font-weight: 600; }
QPushButton { background: #243b4b; border: 1px solid #395568; border-radius: 6px; padding: 7px 13px; }
QPushButton:hover { background: #304f60; border-color: #4d8b9a; }
QPushButton:disabled { color: #6e8293; background: #1b2b37; border-color: #2b3b46; }
QPushButton#Start { background: #059875; color: white; border: none; font-size: 16px; font-weight: 600; padding: 13px 28px; }
QPushButton#Start:hover { background: #10ad86; }
QPushButton#Stop { background: #c33849; color: white; border: none; font-size: 16px; font-weight: 600; padding: 13px 28px; }
QPushButton#Stop:hover { background: #da4557; }
QPushButton#Start:disabled, QPushButton#Stop:disabled { background: #293b46; color: #7d939f; }
QPushButton#Filter { padding: 6px 11px; }
QPushButton#Filter:checked { background: #215260; color: #7ae8e0; border-color: #38c8c3; }
QLineEdit, QComboBox { background: #142330; border: 1px solid #385265; border-radius: 6px; padding: 7px; }
QLineEdit:focus { border-color: #39d1c7; }
QTableWidget { background: #182735; alternate-background-color: #1c2c3a; border: 1px solid #2b4052; gridline-color: #2b4052; selection-background-color: #24505e; selection-color: #e0ffff; }
QHeaderView::section { background: #223748; color: #c9d8e6; border: none; border-bottom: 1px solid #395365; padding: 10px 5px; font-weight: 600; }
QTableWidget::item { padding: 5px; border: none; }
QPlainTextEdit { background: #13222e; border: 1px solid #2d4355; border-radius: 6px; padding: 6px; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: #1c2e3d; padding: 7px 16px; border: 1px solid #304858; }
QTabBar::tab:selected { background: #284755; color: #7fe4df; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: #162634; width: 10px; }
QScrollBar::handle:vertical { background: #3c586c; border-radius: 4px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QSplitter::handle { background: #111c27; }
"""


def pipeline_details(snapshot):
    """Plain-text stage activity and retry reasons for the desktop panel."""
    pipeline = snapshot.get("pipeline", {})
    if not pipeline.get("available", bool(pipeline.get("stages"))):
        return "四阶段流水线尚未初始化；当前显示原队列。"
    lines = ["四阶段流水线 · 当前知识库"]
    for stage, title in PIPELINE_STAGES.items():
        counts = pipeline.get("stages", {}).get(stage, {})
        lines.append(
            f"{title}：等待 {counts.get('waiting', 0)} · 运行 {counts.get('running', 0)}"
            f" · 重试等待 {counts.get('retry_wait', 0)} · 受阻 {counts.get('blocked', 0)}"
        )
    if pipeline.get("circuit_until"):
        lines.append("LLM 链路冷却至：" + str(pipeline["circuit_until"]))
    lines.append("\n正在执行：")
    for entry in pipeline.get("running", []):
        if isinstance(entry, dict):
            job_id, stage, owner = (
                entry.get("job_id", "—"),
                entry.get("stage", "—"),
                entry.get("owner", "—"),
            )
        else:
            job_id, stage, owner = entry[:3]
        lines.append(f"{job_id} · {PIPELINE_STAGES.get(stage, stage)} · 领取者 {owner}")
    if not pipeline.get("running"):
        lines.append("无")
    troubled = [
        job
        for job in snapshot.get("jobs", [])
        if job.get("pipeline_status") in {"retry_wait", "blocked"}
        or job.get("state") in {"blocked", "failed"}
    ]
    lines.append("\n重试 / 受阻：")
    for job in troubled[:100]:
        lines.append(
            f"{job['id']} · {job['title']} · {job.get('blocked_reason') or job.get('error_code') or '原因待记录'}"
            + (f" · 下次尝试 {job['retry_at']}" if job.get("retry_at") else "")
        )
    if not troubled:
        lines.append("无")
    return "\n".join(lines)


def call_metrics_text(metrics):
    attempts = metrics.get("attempts", 0)

    def rate(key):
        count = metrics.get(key, 0)
        return (
            f"{count}/{attempts}（{count / attempts:.1%}）" if attempts else "暂无记录"
        )

    published = metrics.get("searchable_completed")
    return (
        "CCR 近 24 小时留存调用 · HTTP 成功 "
        + rate("http_success")
        + " · 完整 JSON 输出 "
        + rate("valid_outputs")
        + f" · 当前可搜索完成任务 {published if published is not None else '待验证'}"
        + (
            f" · 缺少诊断 {metrics['unknown_attempts']} 次"
            if metrics.get("unknown_attempts")
            else ""
        )
    )


def profiles(config_path=None, video_path=None):
    # Explicit CLI / environment always wins over checkout runtime defaults.
    root = Path(__file__).resolve().parents[2]
    default_config = root / ".runtime/phase1/config.toml"
    default_video = root / ".runtime/videorag/video.toml"
    selected_config = config_path or os.environ.get("RAG_FAVORITE_CONFIG")
    selected_video = video_path or os.environ.get("RAG_VIDEO_CONFIG")
    if selected_config is None and default_config.is_file():
        selected_config = default_config
    if selected_video is None and default_video.is_file():
        selected_video = default_video
    return load_config(selected_config), load_video_config(selected_video)


def main(argv=None):
    parser = argparse.ArgumentParser(description="RAG 任务控制台")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--video-config", type=Path)
    parser.add_argument(
        "--snapshot", action="store_true", help="读取真实队列 JSON，不打开窗口"
    )
    parser.add_argument("--screenshot", type=Path, help="保存真实窗口截图后退出")
    args = parser.parse_args(argv)
    try:
        config, video = profiles(args.config, args.video_config)
        backend = QueueMonitor(config, video)
        if args.snapshot:
            print(json.dumps(backend.snapshot(), ensure_ascii=False, indent=2))
            return 0
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
        from PySide6.QtGui import QColor, QFont, QIcon
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        from PySide6.QtWidgets import (
            QAbstractItemView,
            QApplication,
            QButtonGroup,
            QComboBox,
            QFrame,
            QHBoxLayout,
            QHeaderView,
            QLabel,
            QLineEdit,
            QMainWindow,
            QPlainTextEdit,
            QPushButton,
            QScrollArea,
            QSplitter,
            QTableWidget,
            QTableWidgetItem,
            QTabWidget,
            QVBoxLayout,
            QWidget,
        )
    except ImportError:
        print('请先安装 GUI 依赖：python -m pip install -e ".[gui]"', file=sys.stderr)
        return 2

    class RefreshThread(QThread):
        done = Signal(object)
        failed = Signal(str)

        def __init__(self, action=None, job_id=None):
            super().__init__()
            self.action, self.job_id = action, job_id

        def run(self):
            message = ""
            try:
                if self.isInterruptionRequested():
                    return
                if self.action:
                    message = backend.action(self.action, self.job_id)
                if self.isInterruptionRequested():
                    return
                snapshot = backend.snapshot()
                if not self.isInterruptionRequested():
                    self.done.emit({"snapshot": snapshot, "message": message})
            except Exception as exc:  # noqa: BLE001 - report without leaking credentials
                detail = (
                    str(exc)
                    if isinstance(exc, ConfigError)
                    else (
                        f"连接或操作失败（{type(exc).__name__}），请检查数据库和 worker 服务。"
                    )
                )
                if not self.isInterruptionRequested():
                    self.failed.emit((message + " " + detail).strip())

    def label(text, name=None):
        widget = QLabel(text)
        widget.setTextFormat(Qt.TextFormat.PlainText)
        if name:
            widget.setObjectName(name)
        return widget

    def panel(name="Panel"):
        widget = QFrame()
        widget.setObjectName(name)
        return widget

    class Window(QMainWindow):
        snapshot_ready = Signal(object)

        def __init__(self):
            super().__init__()
            self.setWindowTitle("RAG 任务控制台")
            self.setWindowIcon(
                QIcon(str(Path(__file__).parent / "resources/queue-monitor.svg"))
            )
            geometry = QApplication.primaryScreen().availableGeometry()
            self.resize(
                min(1430, geometry.width() - 60), min(940, geometry.height() - 60)
            )
            self.setMinimumSize(1060, 700)
            self.refresh_thread = None
            self.pending_close = False
            self.snapshot = None
            self.jobs = []
            self.selected_id = None
            self.filter = "all"
            self.action_message = ""
            self.action_time = 0
            self.rendered_jobs = None
            self.rendered_paused = None
            from time import monotonic

            self.monotonic = monotonic
            outer = QWidget()
            self.setCentralWidget(outer)
            layout = QVBoxLayout(outer)
            layout.setContentsMargins(22, 16, 22, 12)
            layout.setSpacing(13)
            header = QHBoxLayout()
            title = QVBoxLayout()
            title.addWidget(label("RAG 任务控制台", "Title"))
            title.addWidget(label("Process Queue · 模型工作状态", "Muted"))
            header.addLayout(title)
            badge = label("实时队列 · 每 3 秒刷新", "Badge")
            badge.setFixedHeight(32)
            header.addWidget(badge)
            header.addStretch()
            self.start = QPushButton("▶  Start")
            self.start.setObjectName("Start")
            self.start.setToolTip("恢复领取新任务；保留单独暂停的任务")
            self.stop = QPushButton("■  Stop")
            self.stop.setObjectName("Stop")
            self.stop.setToolTip(
                "暂停新阶段领取和新模型调用；正在执行的操作保存结果后暂停"
            )
            self.start.clicked.connect(lambda: self.request("start"))
            self.stop.clicked.connect(lambda: self.request("stop"))
            self.start.setEnabled(False)
            self.stop.setEnabled(False)
            header.addWidget(self.start)
            header.addWidget(self.stop)
            layout.addLayout(header)

            counters = QHBoxLayout()
            self.stat_values = {}
            for name, color in (
                ("运行中", "#3ed5ce"),
                ("等待中", "#f5c85e"),
                ("已合并", "#a3b3d0"),
                ("已暂停", "#9faecd"),
                ("已完成", "#35d698"),
                ("受阻", "#f0aa5b"),
                ("失败", "#ff6779"),
            ):
                frame = panel("Stat")
                box = QVBoxLayout(frame)
                box.setContentsMargins(17, 9, 17, 9)
                box.addWidget(label(name, "Muted"))
                value = label("—", "StatValue")
                value.setStyleSheet(f"color: {color}")
                box.addWidget(value)
                self.stat_values[name] = value
                counters.addWidget(frame)
            layout.addLayout(counters)

            phases = QHBoxLayout()
            self.pipeline_values = {}
            for stage, title in PIPELINE_STAGES.items():
                frame = panel("Stat")
                box = QVBoxLayout(frame)
                box.setContentsMargins(12, 6, 12, 6)
                box.setSpacing(3)
                box.addWidget(label(title, "ModelName"))
                value = label("尚未初始化", "Muted")
                value.setWordWrap(True)
                box.addWidget(value)
                self.pipeline_values[stage] = value
                phases.addWidget(frame)
            layout.addLayout(phases)
            self.call_metrics = label("CCR 调用诊断：等待读取", "Muted")
            self.call_metrics.setWordWrap(True)
            layout.addWidget(self.call_metrics)

            split = QSplitter(Qt.Orientation.Horizontal)
            queue = panel()
            queue_layout = QVBoxLayout(queue)
            queue_layout.setContentsMargins(13, 12, 13, 10)
            heading = QHBoxLayout()
            heading.addWidget(label("任务队列", "Section"))
            heading.addStretch()
            self.collection = QComboBox()
            self.collection.addItem("全部知识", "all")
            for key in () if config.unified else config.collections:
                self.collection.addItem(key, key)
            self.collection.currentIndexChanged.connect(self.render_jobs)
            heading.addWidget(self.collection)
            self.search = QLineEdit()
            self.search.setPlaceholderText("搜索标题 / 任务 ID / 错误码")
            self.search.setMinimumWidth(200)
            self.search.textChanged.connect(self.render_jobs)
            heading.addWidget(self.search)
            queue_layout.addLayout(heading)
            filters = QHBoxLayout()
            self.filter_group = QButtonGroup(self)
            for key, title in (
                ("all", "全部"),
                ("active", "运行中"),
                ("queued", "等待中"),
                ("paused", "已暂停"),
                ("complete", "已完成"),
                ("duplicate", "已合并"),
                ("errors", "异常"),
            ):
                button = QPushButton(title)
                button.setObjectName("Filter")
                button.setCheckable(True)
                button.setChecked(key == "all")
                self.filter_group.addButton(button)
                button.clicked.connect(
                    lambda checked=False, value=key: self.set_filter(value)
                )
                filters.addWidget(button)
            filters.addStretch()
            self.refresh = QPushButton("刷新")
            self.refresh.clicked.connect(lambda: self.request())
            filters.addWidget(self.refresh)
            queue_layout.addLayout(filters)
            self.table = QTableWidget(0, 8)
            self.table.setHorizontalHeaderLabels(
                [
                    "顺序",
                    "任务",
                    "处理阶段",
                    "模型",
                    "状态",
                    "实际进度",
                    "更新于",
                    "尝试",
                ]
            )
            self.table.setSelectionBehavior(
                QAbstractItemView.SelectionBehavior.SelectRows
            )
            self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setAlternatingRowColors(True)
            self.table.setWordWrap(False)
            self.table.verticalHeader().setVisible(False)
            self.table.verticalHeader().setDefaultSectionSize(43)
            # Re-measuring all rows after every cell replacement blocked the
            # native event loop for ~11 seconds with only 207 queued jobs.
            header = self.table.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            for column, width in (
                (0, 44),
                (2, 100),
                (3, 68),
                (4, 64),
                (5, 144),
                (6, 77),
                (7, 44),
            ):
                self.table.setColumnWidth(column, width)
            self.table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Stretch
            )
            self.table.horizontalHeader().setMinimumSectionSize(42)
            self.table.itemSelectionChanged.connect(self.select_job)
            queue_layout.addWidget(self.table, 1)
            actions = QHBoxLayout()
            self.selected = label("选择任务以查看详情", "Muted")
            actions.addWidget(self.selected, 1)
            self.start_job = QPushButton("▶ Start 选中任务")
            self.stop_job = QPushButton("■ Stop 选中任务")
            self.start_job.setToolTip("恢复单独暂停的任务，或重试失败 / 受阻任务")
            self.stop_job.setToolTip("暂停此任务的领取；已经开始的处理会完成")
            self.start_job.clicked.connect(
                lambda: self.request("start_job", self.selected_id)
            )
            self.stop_job.clicked.connect(
                lambda: self.request("stop_job", self.selected_id)
            )
            actions.addWidget(self.start_job)
            actions.addWidget(self.stop_job)
            queue_layout.addLayout(actions)
            split.addWidget(queue)

            models_panel = panel()
            models_layout = QVBoxLayout(models_panel)
            models_layout.setContentsMargins(13, 12, 13, 10)
            models_layout.addWidget(label("模型状态", "Section"))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            models_container = QWidget()
            cards = QVBoxLayout(models_container)
            cards.setContentsMargins(0, 0, 0, 0)
            cards.setSpacing(9)
            self.model_cards = {}
            for key in ("CCR", "ASR", "Embedding", "ImageBind"):
                frame = panel("ModelCard")
                box = QVBoxLayout(frame)
                box.setContentsMargins(12, 8, 12, 8)
                box.setSpacing(3)
                line = QHBoxLayout()
                line.addWidget(label(key, "ModelName"))
                role = label("—", "Muted")
                role.setStyleSheet("font-size: 11px; color: #94a9bc;")
                line.addWidget(role, 1)
                state = label("等待读取")
                line.addWidget(state)
                box.addLayout(line)
                name = label("—")
                name.setWordWrap(True)
                reasoning = label("", "Muted")
                reasoning.setVisible(False)
                task = label("当前任务 —", "Muted")
                task.setWordWrap(True)
                task.setMaximumHeight(42)
                connection = label("—", "Muted")
                connection.setWordWrap(True)
                activity = label("正在读取调用记录…", "Muted")
                activity.setWordWrap(True)
                activity.setStyleSheet("font-size: 11px; color: #a2becd;")
                for widget in (name, reasoning, task, connection, activity):
                    box.addWidget(widget)
                cards.addWidget(frame)
                self.model_cards[key] = (
                    state,
                    name,
                    role,
                    task,
                    connection,
                    reasoning,
                    activity,
                )
            cards.addStretch()
            scroll.setWidget(models_container)
            models_layout.addWidget(scroll)
            hint = label(
                "不同素材的本地准备与 LLM 调用可重叠；短调用按请求保留记录。", "Muted"
            )
            hint.setWordWrap(True)
            models_layout.addWidget(hint)
            split.addWidget(models_panel)
            split.setSizes([970, 370])
            split.setStretchFactor(0, 3)
            split.setStretchFactor(1, 1)
            layout.addWidget(split, 1)

            self.tabs = QTabWidget()
            self.logs = QPlainTextEdit("正在读取真实队列…")
            self.details = QPlainTextEdit("选择任务以查看详情。")
            self.pipeline_details = QPlainTextEdit("正在读取流水线…")
            self.call_details = QPlainTextEdit("正在读取模型调用…")
            for widget in (
                self.logs,
                self.details,
                self.pipeline_details,
                self.call_details,
            ):
                widget.setReadOnly(True)
            self.tabs.addTab(self.logs, "任务日志")
            self.tabs.addTab(self.details, "选中任务详情")
            self.tabs.addTab(self.pipeline_details, "流水线 / 重试")
            self.tabs.addTab(self.call_details, "模型调用")
            self.tabs.setFixedHeight(160)
            layout.addWidget(self.tabs)
            self.status = label("连接数据库…", "Muted")
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            self.timer = QTimer(self)
            self.timer.timeout.connect(lambda: self.request())
            self.timer.start(3000)
            self.update_buttons()
            QTimer.singleShot(0, self.request)

        def set_filter(self, value):
            self.filter = value
            self.render_jobs()

        @Slot()
        def request(self, action=None, job_id=None):
            if self.refresh_thread is not None or self.pending_close:
                return
            self.refresh_thread = RefreshThread(action, job_id)
            self.refresh_thread.done.connect(
                self.received, Qt.ConnectionType.QueuedConnection
            )
            self.refresh_thread.failed.connect(
                self.failed, Qt.ConnectionType.QueuedConnection
            )
            self.refresh_thread.finished.connect(
                self.finished, Qt.ConnectionType.QueuedConnection
            )
            self.update_buttons()
            if action:
                self.status.setText("正在执行操作…")
            self.refresh_thread.start()

        @Slot()
        def finished(self):
            thread, self.refresh_thread = self.refresh_thread, None
            thread.deleteLater()
            self.update_buttons()
            if self.pending_close:
                self.close()
                QApplication.instance().quit()

        @Slot(object)
        def received(self, value):
            if self.pending_close:
                return
            self.snapshot = value["snapshot"]
            self.jobs = self.snapshot["jobs"]
            counts = self.snapshot["counts"]
            paused = self.snapshot["control"]["paused"]
            paused_count = (
                counts.get("queued", 0) if paused else self.snapshot["paused_pending"]
            )
            values = {
                "运行中": counts.get("running", 0) + counts.get("published", 0),
                "等待中": counts.get("queued", 0) - paused_count,
                "已暂停": paused_count,
                "已完成": counts.get("complete", 0),
                "已合并": counts.get("duplicate", 0),
                "受阻": counts.get("blocked", 0),
                "失败": counts.get("failed", 0),
            }
            for key, number in values.items():
                self.stat_values[key].setText(str(number))
            pipeline = self.snapshot.get("pipeline", {})
            for stage, widget in self.pipeline_values.items():
                counts = pipeline.get("stages", {}).get(stage)
                widget.setText(
                    f"等待 {counts.get('waiting', 0)} · 运行 {counts.get('running', 0)}\n"
                    f"重试 {counts.get('retry_wait', 0)} · 受阻 {counts.get('blocked', 0)}"
                    if counts is not None
                    else "尚未初始化"
                )
            metrics = self.snapshot.get("call_metrics", {})
            self.call_metrics.setText(call_metrics_text(metrics))
            self.call_metrics.setToolTip(
                "HTTP 和输出比例的分母：近 24 小时留存、已结束、含请求诊断的 CCR 调用；"
                "缓存复用与正在执行的调用不计入。最多保留 256 条结束调用，较早记录可能被截断。\n"
                f"窗口：{metrics.get('window_start', '—')} 至 {metrics.get('window_end', '—')}\n"
                "完整 JSON 输出表示流已完成且可解析为 JSON 对象；证据校验与最终发布独立统计。\n"
                "可搜索完成任务：当前知识库已完成且具有当前搜索索引向量的任务总数。"
            )
            phase_text = pipeline_details(self.snapshot)
            if phase_text != self.pipeline_details.toPlainText():
                self.pipeline_details.setPlainText(phase_text)
            calls_text = json.dumps(
                self.snapshot.get("model_calls", [])[:60],
                ensure_ascii=False,
                indent=2,
            )
            if calls_text != self.call_details.toPlainText():
                self.call_details.setPlainText(calls_text)
            self.render_jobs()
            for model in self.snapshot["models"]:
                state, name, role, task, connection, reasoning, activity = (
                    self.model_cards[model["key"]]
                )
                state.setText("● " + model["state"])
                color = {"处理中": "#36d8cc", "空闲": "#a0b4c8", "离线": "#ff7482"}.get(
                    model["state"], "#e8bf64"
                )
                state.setStyleSheet(f"color: {color}; font-weight: 600;")
                name.setText(model["name"])
                role.setText(model["role"])
                task.setText("当前任务 " + model["task"])
                task.setToolTip(model["task"])
                connection.setText(model["connection"])
                activity.setText(model.get("activity", "尚无调用记录"))
                if model["key"] == "CCR":
                    effort = model.get("reasoning_effort")
                    reasoning.setText(
                        "思考强度："
                        + ({"xhigh": "X-HIGH"}.get(effort, (effort or "未知").upper()))
                    )
                    reasoning.setVisible(True)
                    name.setToolTip(
                        "来源："
                        + model.get("source", "未知")
                        + "\n请求别名："
                        + model.get("requested_model", "未知")
                        + "\n路由规则："
                        + model.get("route_rule", "未知")
                        + "\n显示下一次请求的路由；不代表推理已通过。"
                    )
            log = "\n".join(self.snapshot["logs"])
            if log != self.logs.toPlainText():
                bar = self.logs.verticalScrollBar()
                follow = bar.value() == bar.maximum()
                self.logs.setPlainText(log)
                if follow:
                    bar.setValue(bar.maximum())
            if value["message"]:
                self.action_message = value["message"]
                self.action_time = self.monotonic()
            stamp = (
                datetime.fromisoformat(self.snapshot["captured_at"])
                .astimezone()
                .strftime("%H:%M:%S")
            )
            worker = self.snapshot["worker"]
            mode = "队列已暂停；正在执行的操作保存后停下" if paused else "队列运行中"
            if pipeline.get("circuit_until"):
                mode += " · LLM 链路冷却中"
            if not self.snapshot["control_supported"]:
                mode = "worker 未运行或尚未加载暂停控制；请检查服务"
            text = f"{mode} · worker {worker.get('ActiveState', 'unknown')} · 共 {self.snapshot['total']} 个任务 · 更新 {stamp}"
            if self.snapshot["truncated"]:
                text += " · 列表显示前 2000 个任务"
            if self.action_message and self.monotonic() - self.action_time < 20:
                text = self.action_message + "  |  " + text
            self.status.setText(text)
            self.snapshot_ready.emit(self.snapshot)

        @Slot(str)
        def failed(self, message):
            if self.pending_close:
                return
            self.status.setText(message + "  |  已显示的数据可能过时；稍后自动重连。")
            self.snapshot = None
            for (
                state,
                _name,
                _role,
                _task,
                connection,
                _reasoning,
                _activity,
            ) in self.model_cards.values():
                state.setText("● 信息过时")
                state.setStyleSheet("color: #e8bf64")
                connection.setText("读取失败 · 等待重新连接")
            self.update_buttons()
            if args.screenshot:
                print(message, file=sys.stderr)
                QTimer.singleShot(100, lambda: QApplication.instance().exit(2))

        def render_jobs(self, *_):
            if not hasattr(self, "table"):
                return
            search = self.search.text().casefold().strip()
            collection = self.collection.currentData()
            global_paused = bool(self.snapshot and self.snapshot["control"]["paused"])

            def matches(job):
                waiting = (
                    job.get("pipeline_status") in {"waiting", "retry_wait"}
                    if job.get("pipeline_status")
                    else job["state"] == "queued"
                )
                paused = waiting and (job["paused"] or global_paused)
                allowed = {
                    "all": True,
                    "active": job.get("pipeline_status") == "running"
                    if job.get("pipeline_status")
                    else job["state"] in {"running", "published"},
                    "queued": waiting and not paused,
                    "paused": paused,
                    "complete": job["state"] == "complete",
                    "duplicate": job["state"] == "duplicate",
                    "errors": job["state"] in {"failed", "blocked"},
                }[self.filter]
                return (
                    allowed
                    and (collection == "all" or job["collection"] == collection)
                    and (
                        not search
                        or search
                        in (
                            job["title"] + job["id"] + (job["error_code"] or "")
                        ).casefold()
                    )
                )

            visible = [job for job in self.jobs if matches(job)]
            if visible == self.rendered_jobs and global_paused == self.rendered_paused:
                self.update_buttons()
                return
            selected = self.selected_id
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                self.render_rows(visible, global_paused, selected)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)
            self.rendered_jobs = visible
            self.rendered_paused = global_paused
            self.select_job()

        def render_rows(self, visible, global_paused, selected):
            self.table.setRowCount(len(visible))
            selection = None
            for row, job in enumerate(visible):
                state = (
                    "已暂停"
                    if (global_paused or job["paused"])
                    and (
                        job.get("pipeline_status") in {"waiting", "retry_wait"}
                        or (not job.get("pipeline_status") and job["state"] == "queued")
                    )
                    else job["state_label"]
                )
                stamp = (
                    datetime.fromisoformat(job["updated_at"])
                    .astimezone()
                    .strftime("%H:%M:%S")
                )
                values = [
                    str(job["queue_rank"] or "—"),
                    job["title"],
                    job["stage_label"],
                    job["model"],
                    state,
                    job["progress"],
                    stamp,
                    str(job["attempts"]),
                ]
                for column, text in enumerate(values):
                    item = self.table.item(row, column)
                    if item is None:
                        item = QTableWidgetItem(text)
                        self.table.setItem(row, column, item)
                    elif item.text() != text:
                        item.setText(text)
                    if item.data(Qt.ItemDataRole.UserRole) != job["id"]:
                        item.setData(Qt.ItemDataRole.UserRole, job["id"])
                    tooltip = text if column != 1 else job["title"] + "\n" + job["id"]
                    if item.toolTip() != tooltip:
                        item.setToolTip(tooltip)
                    if column == 4:
                        color = {
                            "running": "#43d7d2",
                            "complete": "#38daa5",
                            "queued": "#eac86a",
                            "failed": "#fa7481",
                            "blocked": "#f2ab6b",
                        }.get(job["state"], "#b7c6d9")
                        item.setForeground(
                            QColor("#a8b4d2" if state == "已暂停" else color)
                        )
                if job["id"] == selected:
                    selection = row
            if selection is None and visible and selected is None:
                selection = 0
            if selection is not None:
                self.table.selectRow(selection)
            else:
                self.table.clearSelection()

        @Slot()
        def select_job(self):
            items = self.table.selectedItems()
            self.selected_id = (
                items[0].data(Qt.ItemDataRole.UserRole) if items else None
            )
            job = next(
                (job for job in self.jobs if job["id"] == self.selected_id), None
            )
            self.selected.setText(
                "已选 " + job["id"][:8] if job else "选择任务以查看详情"
            )
            if job:
                lines = [
                    f"标题：{job['title']}",
                    f"任务 ID：{job['id']}",
                    f"状态：{job['state_label']} ({job['state']})",
                    f"处理阶段：{job['stage_label']} ({job['stage']})",
                    f"实际进度：{job['progress']}（仅当前阶段，不代表总进度）",
                    f"错误码：{job['error_code'] or '无'}",
                    f"重试 / 受阻原因：{job.get('blocked_reason') or '无'}",
                    f"下次尝试：{job.get('retry_at') or '无'}",
                    f"阶段领取者：{job.get('stage_owner') or '无'}",
                    f"尝试次数：{job['attempts']}；优先级：{job['priority']}",
                    (
                        "知识库：统一知识库"
                        if config.unified
                        else f"存储分类：{job['collection']}"
                    ),
                    f"提交时间：{datetime.fromisoformat(job['created_at']).astimezone().isoformat()}",
                    f"更新时间：{datetime.fromisoformat(job['updated_at']).astimezone().isoformat()}",
                    f"单独暂停：{'是' if job['paused'] else '否'}",
                ]
                if job["media_expired"]:
                    lines.append("媒体已过期；需重新提交来源，不能直接重试。")
                self.details.setPlainText("\n".join(lines))
            else:
                self.details.setPlainText("选择任务以查看详情。")
            self.update_buttons()

        def update_buttons(self):
            busy = self.refresh_thread is not None or self.pending_close
            valid = self.snapshot is not None
            supported = valid and self.snapshot["control_supported"]
            paused = valid and self.snapshot["control"]["paused"]
            running = valid and self.snapshot["worker"].get("ActiveState") == "active"
            already_running = valid and running and not paused
            self.start.setText("▶  运行中" if already_running else "▶  Start")
            self.start.setToolTip(
                "队列已经启动；无需重复 Start。点击 Stop 可暂停后续任务领取。"
                if already_running
                else "恢复领取新任务；保留单独暂停的任务"
                if valid
                else "正在连接数据库，取得队列状态后可操作。"
            )
            self.start.setEnabled(not busy and valid and (paused or not running))
            self.stop.setEnabled(not busy and supported and not paused)
            self.refresh.setEnabled(not busy)
            job = next(
                (job for job in self.jobs if job["id"] == self.selected_id), None
            )
            self.start_job.setEnabled(
                bool(
                    not busy
                    and supported
                    and job
                    and not job["media_expired"]
                    and (
                        job["state"] in {"failed", "blocked"}
                        or (job["state"] in {"queued", "running"} and job["paused"])
                    )
                )
            )
            self.stop_job.setEnabled(
                bool(
                    not busy
                    and supported
                    and job
                    and not job["paused"]
                    and job["state"] in {"queued", "running"}
                )
            )

        def closeEvent(self, event):
            self.timer.stop()
            if self.refresh_thread is not None:
                self.pending_close = True
                self.refresh_thread.requestInterruption()
                # Hide immediately, and let the bounded background operation
                # finish before Qt destroys its QThread. Never wait on the UI.
                self.hide()
                event.ignore()
            else:
                event.accept()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("RAG 任务控制台")
    app.setDesktopFileName("rag-favorite-gui")
    app.setFont(QFont("Noto Sans CJK SC", 10))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    server = None
    if not args.screenshot:
        from .video_store import library_id

        address = f"rag-favorite-gui-{os.getuid()}-{library_id(video)[:12]}"
        socket = QLocalSocket()
        socket.connectToServer(address)
        if socket.waitForConnected(300):
            socket.write(b"show")
            socket.waitForBytesWritten(300)
            return 0
        QLocalServer.removeServer(address)
        server = QLocalServer()
        if not server.listen(address):
            print("无法创建桌面窗口实例。", file=sys.stderr)
            return 2
    window = Window()
    if args.screenshot:
        window.resize(1430, 940)
    if server:

        def activate():
            connection = server.nextPendingConnection()
            if connection:
                connection.close()
            window.pending_close = False
            window.timer.start(3000)
            window.update_buttons()
            window.showNormal()
            window.raise_()
            window.activateWindow()

        server.newConnection.connect(activate)
    if args.screenshot:
        saved = False

        def save_snapshot(_snapshot):
            nonlocal saved
            if saved:
                return
            saved = True

            def capture():
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                if not window.grab().save(str(args.screenshot)):
                    app.exit(2)
                else:
                    print(str(args.screenshot))
                    app.quit()

            QTimer.singleShot(200, capture)

        window.snapshot_ready.connect(save_snapshot)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
