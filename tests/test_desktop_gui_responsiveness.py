"""Exercise the native event loop under large queues and slow background calls."""

import os
import subprocess
import sys
import textwrap

import pytest


def test_large_queue_repeated_clicks_slow_action_and_close_remain_responsive(tmp_path):
    pytest.importorskip("PySide6")
    source = textwrap.dedent("""
        import sys, time, traceback, threading
        from pathlib import Path
        from datetime import datetime, UTC
        from uuid import UUID
        from PySide6 import QtWidgets
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtTest import QTest
        from rag_favorite import desktop_gui as gui
        from rag_favorite.config import default_config
        from rag_favorite.video_config import VideoConfig

        gui.profiles = lambda *args: (default_config(), VideoConfig(root=Path(sys.argv[1]), credentials_file=Path(sys.argv[1]) / 'unused.env'))
        stamp = datetime.now(UTC).isoformat()
        jobs = [{
            'id':str(UUID(int=i+1)), 'title':'任务 ' + str(i), 'collection':'cooking',
            'state':'queued', 'stage':'queued', 'stage_label':'等待领取', 'model':'—',
            'progress':'—', 'state_label':'等待中', 'paused':False, 'queue_rank':i+1,
            'error_code':None, 'attempts':0, 'priority':0, 'created_at':stamp,
            'updated_at':stamp, 'media_expired':False,
        } for i in range(2000)]
        # A prepared job may retain overall running state while its next phase waits.
        jobs[0].update(state='running', pipeline_status='waiting', pipeline_stage='llm')
        class Monitor:
            def __init__(self, *args):
                self.calls = 0
                self.paused = False
                self.fail_once = False
            def action(self, action, job=None):
                time.sleep(1.2 if action == 'slow' else .2)
                if action == 'stop': self.paused = True
                if action == 'start': self.paused = False
                if action == 'failure': self.fail_once = True
                return '完成'
            def snapshot(self):
                time.sleep(.05)
                if self.fail_once:
                    self.fail_once = False
                    raise ConnectionError('temporary database outage')
                self.calls += 1
                models = [dict(key=key, name=key, state='空闲', role='test', task='—', connection='fixture')
                          for key in ('CCR','ASR','Embedding','ImageBind')]
                models[0].update(name='gpt-6-luna' if self.calls == 1 else 'live-model-changed',
                                 reasoning_effort='xhigh', source='fixture', requested_model='old-alias')
                return dict(captured_at=stamp, counts={'queued':2000}, total=2000, paused_pending=0,
                            jobs=jobs, control={'paused':self.paused}, worker={'ActiveState':'active'},
                            control_supported=True, models=models, logs=['test'], truncated=False,
                            pipeline={'available':True,'stages':{
                                'prepare':{'waiting':2,'running':1,'retry_wait':0,'blocked':0},
                                'llm':{'waiting':1,'running':0,'retry_wait':1,'blocked':1},
                            },'running':[{'job_id':'stage-job','stage':'prepare','owner':'worker-123'}]},
                            call_metrics={'attempts':3,'http_success':2,'valid_outputs':1,
                                          'searchable_completed':7},
                            model_calls=[{'call_id':'request-123','model':'CCR','status':'running'}],
                            sequence=self.calls)
        gui.QueueMonitor = Monitor
        OriginalApplication = QtWidgets.QApplication
        class Application(OriginalApplication):
            def __init__(self, args):
                super().__init__(args)
                self.step = 0
                self.ticks = 0
                self.last = time.monotonic()
                self.max_gap = 0
                self.main_thread = threading.get_ident()
                self.timer = QTimer(self)
                self.timer.timeout.connect(self.exercise)
                self.timer.start(10)
                QTimer.singleShot(10000, lambda: self.exit(3))
            def exercise(self):
                now = time.monotonic()
                self.max_gap = max(self.max_gap, now - self.last)
                self.last = now
                self.ticks += 1
                window = next((w for w in self.topLevelWidgets() if hasattr(w,'snapshot_ready')), None)
                if window is None or window.pending_close or window.refresh_thread is not None:
                    return
                try:
                    assert threading.get_ident() == self.main_thread
                    assert callable(window.thread)  # QObject.thread is not shadowed.
                    if self.step == 4 and window.snapshot is None:
                        assert window.model_cards['CCR'][0].text() == '● 信息过时'
                        assert window.refresh.isEnabled() and not window.stop.isEnabled()
                        window.request()
                        self.step = 5
                        return
                    if window.snapshot is None:
                        return
                    if self.step == 0:
                        assert window.table.rowCount() == 2000
                        assert '等待 2 · 运行 1' in window.pipeline_values['prepare'].text()
                        assert '完整 JSON 输出 1/3' in window.call_metrics.text()
                        assert 'stage-job' in window.pipeline_details.toPlainText()
                        assert 'request-123' in window.call_details.toPlainText()
                        assert window.start.text() == '▶  运行中'
                        assert not window.start.isEnabled()
                        window.search.setText('任务 1999')
                        assert window.table.rowCount() == 1
                        window.search.clear()
                        assert window.table.rowCount() == 2000
                        QTest.mouseClick(window.stop, Qt.MouseButton.LeftButton)
                    elif self.step == 1:
                        assert window.snapshot['control']['paused']
                        assert window.start.text() == '▶  Start'
                        assert window.start.isEnabled()
                        assert window.model_cards['CCR'][1].text() == 'live-model-changed'
                        assert window.model_cards['CCR'][5].text() == '思考强度：X-HIGH'
                        window.set_filter('paused')
                        assert window.table.rowCount() == 2000
                        window.set_filter('queued')
                        assert window.table.rowCount() == 0
                        window.set_filter('all')
                        QTest.mouseClick(window.start, Qt.MouseButton.LeftButton)
                    elif self.step == 2:
                        assert not window.snapshot['control']['paused']
                        window.table.selectRow(500)
                        self.selected = window.selected_id
                        for _ in range(20): window.request()
                    elif self.step == 3:
                        assert window.snapshot['sequence'] == 4
                        assert window.selected_id == self.selected
                        window.request('failure')
                    elif self.step == 5:
                        assert window.snapshot['sequence'] == 5
                        assert window.model_cards['CCR'][0].text() == '● 空闲'
                        window.request('slow')
                        QTimer.singleShot(100, lambda: self.check_close(window))
                    self.step += 1
                except Exception:
                    traceback.print_exc()
                    self.exit(1)
            def check_close(self, window):
                try:
                    window.close()
                    assert not window.isVisible()
                    assert window.pending_close
                    assert self.max_gap < .75, self.max_gap
                    print('RESPONSIVE', self.ticks, 'max_gap', round(self.max_gap, 3), flush=True)
                except Exception:
                    traceback.print_exc()
                    self.exit(1)
        QtWidgets.QApplication = Application
        raise SystemExit(gui.main([]))
    """)
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path / "library")],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESPONSIVE" in result.stdout
