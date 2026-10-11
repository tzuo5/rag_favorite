"""Isolated CCR transport checks and a quota-bounded visual comparison.

Run with the repository Python environment. No production configuration is
changed. The forwarding server counts every real upstream HTTP attempt.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import signal
import socket
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.model_activity import read_activity
from rag_favorite.queue_control import read_control, set_control
from rag_favorite.video_config import VideoConfig, load_video_config
from rag_favorite.video_provider import CCRProvider, ProviderUnavailable
from rag_favorite.video_store import library_id
from rag_favorite.video_visual import DENSE_INSTRUCTION, validate_frame_summaries


def rpc(url, token, method, args=()):
    request = urllib.request.Request(
        url + "/api/ccr/rpc",
        data=json.dumps({"method": method, "args": list(args)}).encode(),
        headers={"Content-Type": "application/json", "x-ccr-web-auth": token},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("CCR management request failed: " + method)
    return result["value"]


def production_connection():
    service = json.loads((Path.home() / ".claude-code-router/service.json").read_text())
    url = urllib.parse.urlsplit(service["url"])
    token = urllib.parse.parse_qs(url.query)["ccr_web_token"][0]
    origin = urllib.parse.urlunsplit((url.scheme, url.netloc, "", "", ""))
    return origin, token


def production_config():
    return rpc(*production_connection(), "getConfig")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def isolated_ccr(config, mode):
    with tempfile.TemporaryDirectory(prefix="rag-ccr-check-") as directory:
        root = Path(directory)
        cfg = copy.deepcopy(config)
        port, core, web = free_port(), free_port(), free_port()
        cfg.update(
            HOST="127.0.0.1", PORT=port, routerEndpoint=f"http://127.0.0.1:{port}"
        )
        cfg["gateway"] = {
            "host": "127.0.0.1",
            "port": port,
            "coreHost": "127.0.0.1",
            "corePort": core,
            "enabled": True,
        }
        cfg["Router"]["fallback"] = {"mode": mode, "retryCount": 2, "models": []}
        for rule in cfg["Router"].get("rules", []):
            rule.pop("fallback", None)
        cfg["profile"] = {
            "enabled": False,
            "profiles": [],
            "claudeCode": {"enabled": False},
            "codex": {"enabled": False},
        }
        cfg["autoStart"] = False
        cfg["observability"] = {
            "requestLogs": False,
            "agentAnalysis": False,
            "requestLogBodyCapture": "none",
        }
        cfg["contextArchive"] = {"enabled": False}
        cfg["botGateway"] = {"enabled": False}
        cfg["proxy"] = {"enabled": False}
        for provider in cfg["Providers"]:
            provider["account"] = {"enabled": False}
            provider.pop("autoFetchKnownModels", None)
        config_root = root / ".claude-code-router"
        config_root.mkdir(mode=0o700)
        database = config_root / "config.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE app_config (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO app_config VALUES ('default', ?, ?)",
                (json.dumps(cfg), "2026-10-10T00:00:00Z"),
            )
        database.chmod(0o600)
        auth = str(uuid4())
        env = {
            **os.environ,
            "CCR_INTERNAL_HOME_DIR": str(root),
            "CCR_INTERNAL_USER_DATA_DIR": str(config_root / "app-data"),
            "CCR_WEB_AUTH_TOKEN": auth,
        }
        env.pop("CODEXL_HOME", None)
        log = root / "service.log"
        log.touch(mode=0o600)
        with log.open("w") as output:
            process = subprocess.Popen(
                ["ccr", "serve", "--no-open", "--gateway", "--port", str(web)],
                cwd=root,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            url = f"http://127.0.0.1:{web}"
            try:
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(
                            "Isolated CCR exited; no production config changed."
                        )
                    try:
                        status = rpc(url, auth, "getGatewayStatus")
                        if (
                            status.get("state") == "running"
                            and status.get("endpoint") == f"http://127.0.0.1:{port}"
                        ):
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.25)
                else:
                    raise RuntimeError("Isolated CCR startup timed out.")
                effective = rpc(url, auth, "getConfig")
                assert effective["Router"]["fallback"]["mode"] == mode
                yield f"http://127.0.0.1:{port}/v1/responses", effective["APIKEY"]
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                except ProcessLookupError:
                    pass


class CountingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *, target=None, limit=80):
        super().__init__(("127.0.0.1", 0), Forwarder)
        self.target = target
        self.limit = limit
        self.attempts = []
        self.sequence = []
        self.lock = threading.Lock()
        self.audit_path = None

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_port}"


class Forwarder(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        if not self.path.endswith("/responses"):
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with self.server.lock:
            if len(self.server.attempts) >= self.server.limit:
                self.send_error(402, "Experiment upstream limit reached")
                return
            entry = {"started": time.monotonic(), "bytes": len(body)}
            self.server.attempts.append(entry)
            if self.server.audit_path:
                save_private(self.server.audit_path, self.server.attempts)
            status = self.server.sequence.pop(0) if self.server.sequence else 200
        if self.server.target is None:
            if status == "reset":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            entry["status"] = status
            self.send_response(status)
            self.send_header(
                "Content-Type", "text/event-stream" if status == 200 else "text/plain"
            )
            if status == 429:
                self.send_header("Retry-After", "1")
            self.end_headers()
            if status == 200:
                events = [
                    {"type": "response.output_text.delta", "delta": '{"ok":true}'},
                    {
                        "type": "response.completed",
                        "response": {
                            "model": "gpt-6-luna",
                            "reasoning": {"effort": "medium"},
                        },
                    },
                ]
                self.wfile.write(
                    b"".join(
                        b"data: " + json.dumps(e).encode() + b"\n\n" for e in events
                    )
                )
            else:
                self.wfile.write(b"transient upstream error")
            return
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower()
            not in {
                "host",
                "connection",
                "content-length",
                "transfer-encoding",
                "accept-encoding",
            }
        }
        request = urllib.request.Request(
            self.server.target.rstrip("/") + "/responses", data=body, headers=headers
        )
        try:
            try:
                response = urllib.request.urlopen(request, timeout=600)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                entry["status"] = response.status
                self.send_response(response.status)
                for key in ["Content-Type", "Retry-After", "X-Request-ID"]:
                    if response.headers.get(key):
                        self.send_header(key, response.headers[key])
                self.end_headers()
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (OSError, ValueError):
            had_headers = "status" in entry
            entry["status"] = entry.get("status", 502)
            if not had_headers:
                self.send_error(502)


@contextmanager
def counting_server(**kwargs):
    server = CountingServer(**kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def stub_config(base):
    return {
        "APIKEY": str(uuid4()),
        "API_TIMEOUT_MS": 600000,
        "Providers": [
            {
                "id": "stub",
                "name": "Stub",
                "type": "openai_responses",
                "api_base_url": base + "/v1",
                "api_key": "stub",
                "models": ["gpt-6-luna"],
            }
        ],
        "Router": {
            "rules": [
                {
                    "id": "stub",
                    "enabled": True,
                    "type": "condition",
                    "condition": {
                        "left": "request.body.model",
                        "operator": "contains",
                        "right": "gpt-",
                    },
                    "rewrites": [
                        {
                            "key": "request.body.model",
                            "operation": "set",
                            "value": "stub/gpt-6-luna",
                        }
                    ],
                }
            ]
        },
    }


def stub_check():
    results = []
    with (
        counting_server(limit=100) as server,
        isolated_ccr(stub_config(server.base), "retry") as (url, key),
    ):
        config = VideoConfig(
            root=Path("/tmp"), credentials_file=Path("/nonexistent"), router_url=url
        )
        os.environ["RAG_CCR_API_KEY"] = key
        provider = CCRProvider(config)
        for sequence, expected, success in [
            ([503, 200], 2, True),
            ([507, 200], 2, True),
            ([503, 503, 503], 3, False),
            ([400], 1, False),
            ([401, 401], 2, False),
            ([429, 200], 2, True),
            (["reset", 200], 2, True),
            (["reset"] * 6, 6, False),
        ]:
            server.sequence = sequence.copy()
            start = len(server.attempts)
            before = time.monotonic()
            try:
                value = provider.json("Return JSON.", "stub test")
                ok = value == {"ok": True}
            except ProviderUnavailable:
                ok = False
            attempts = server.attempts[start:]
            assert len(attempts) == expected, (
                sequence,
                len(attempts),
                provider.calls[-1],
                attempts,
            )
            assert ok == success, (sequence, ok)
            if expected > 1 and sequence[0] not in {401, "reset"}:
                assert attempts[1]["started"] - attempts[0]["started"] >= 0.9, (
                    sequence,
                    attempts,
                )
            if sequence[0] == "reset":
                assert attempts[1]["started"] - attempts[0]["started"] >= 0.1
            results.append(
                {
                    "sequence": sequence,
                    "attempts": expected,
                    "success": ok,
                    "seconds": round(time.monotonic() - before, 2),
                }
            )
    with (
        counting_server(limit=1) as server,
        isolated_ccr(stub_config(server.base), "off") as (url, key),
    ):
        os.environ["RAG_CCR_API_KEY"] = key
        provider = CCRProvider(
            VideoConfig(
                root=Path("/tmp"), credentials_file=Path("/nonexistent"), router_url=url
            )
        )
        assert provider.json("Return JSON.", "limit test") == {"ok": True}
        try:
            provider.json("Return JSON.", "limit test")
        except ProviderUnavailable as error:
            assert error.diagnostics["http_status"] == 402
        else:
            raise AssertionError("Upstream quota limit did not stop dispatch.")
        assert len(server.attempts) == 1
    print(
        json.dumps(
            {
                "stub_checks": results,
                "quota_check": "second request rejected; one upstream dispatch",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def save_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".private-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fixtures(video, output):
    candidates = []
    for directory in sorted((video.root / "work").glob("*/frames-*")):
        images = sorted(directory.glob("*.jpg"))
        checkpoint = (
            video.root
            / "derived"
            / directory.parent.name
            / f"segment-{directory.name.split('-')[-1]}.json"
        )
        if len(images) >= 8 and checkpoint.is_file():
            segment = json.loads(checkpoint.read_text())["segment"]
            if segment.get("frame_interval_seconds"):
                candidates.append((images[:8], segment))
    # Spread fixtures across existing jobs rather than taking one long video.
    grouped = {}
    for images, segment in candidates:
        grouped.setdefault(images[0].parents[1].name, (images, segment))
    selected = list(grouped.values())[:20]
    manifest = []
    for i, (images, segment) in enumerate(selected):
        row = []
        for j, path in enumerate(images):
            target = output / "fixtures" / str(i) / f"{j}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            data = path.read_bytes()
            target.write_bytes(data)
            target.chmod(0o600)
            row.append(
                {
                    "path": str(target.resolve()),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "seconds": segment["start"] + j * segment["frame_interval_seconds"],
                }
            )
        manifest.append({"images": row, "transcript": segment.get("transcript", "")})
    save_private(output / "fixtures.json", manifest)
    return manifest


def benchmark(output, upstream_limit=80):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    video = load_video_config(".runtime/videorag/video.toml")
    manifest = fixtures(video, output)
    if not manifest:
        raise RuntimeError("No retained eight-frame fixture available.")
    cfg = production_config()
    if len(cfg["Providers"]) != 1:
        raise RuntimeError(
            "Expected one production provider; refusing ambiguous experiment."
        )
    target = cfg["Providers"][0]["api_base_url"]
    oauth = next(
        (
            p["codexOauth"]
            for p in cfg.get("providerPlugins", [])
            if p.get("codexOauth", {}).get("accessToken")
        ),
        None,
    )
    if not oauth:
        raise RuntimeError(
            "No current Codex access token available for the isolated forwarding provider."
        )
    rows = []
    instruction = (
        '只根据图片返回 JSON：{"caption":"主题","facts":[],"edges":[],"frame_summaries":[{"frame_index":0,"caption":"可观察的关键文字、动作或步骤"}]}。不要猜测。'
        + DENSE_INSTRUCTION
    )
    with (
        counting_server(target=target, limit=upstream_limit) as server,
        ExitStack() as stack,
    ):
        server.audit_path = output / "upstream-attempts.json"
        # Codex OAuth plugins force their own upstream URL. Use the current
        # token through an ordinary provider so every model request MUST pass
        # the counter. The same upstream, model and reasoning effort are used.
        cfg["Providers"] = [
            {
                "id": "bench",
                "name": "Benchmark Codex",
                "type": "openai_responses",
                "api_base_url": server.base + "/v1",
                "api_key": oauth["accessToken"],
                "models": ["gpt-6-luna"],
            }
        ]
        cfg["providerPlugins"] = [
            {
                "key": "benchmark-headers",
                "providerName": "Benchmark Codex",
                "auth": {
                    "headers": {
                        "ChatGPT-Account-Id": oauth.get("accountId", ""),
                        "User-Agent": "codex-cli",
                    }
                },
                "request": {
                    "bodyRemove": ["max_output_tokens"],
                    "bodySet": {"reasoning.effort": "medium"},
                },
            }
        ]
        cfg["plugins"] = []
        cfg["Router"]["rules"] = [
            {
                "id": "benchmark",
                "enabled": True,
                "type": "condition",
                "condition": {
                    "left": "request.body.model",
                    "operator": "contains",
                    "right": "gpt-",
                },
                "rewrites": [
                    {
                        "key": "request.body.model",
                        "operation": "set",
                        "value": "bench/gpt-6-luna",
                    },
                    {
                        "key": "request.body.reasoning.effort",
                        "operation": "set",
                        "value": "medium",
                    },
                ],
            }
        ]
        routes = {
            mode: stack.enter_context(isolated_ccr(cfg, mode))
            for mode in ["off", "retry"]
        }
        stop = False
        for index, group in enumerate(manifest):
            arms = ["baseline", "retry8", "retry4"]
            arms = arms[index % 3 :] + arms[: index % 3]
            for arm in arms:
                # Reserve the core's one socket-reset retry as well as routing retries.
                maximum = 2 if arm == "baseline" else 12 if arm == "retry4" else 6
                if len(server.attempts) + maximum > upstream_limit:
                    stop = True
                    break
                mode = "off" if arm == "baseline" else "retry"
                url, key = routes[mode]
                os.environ["RAG_CCR_API_KEY"] = key
                provider = CCRProvider(
                    replace(video, router_url=url), output / "calls.jsonl"
                )
                start = len(server.attempts)
                row = {
                    "group": index,
                    "arm": arm,
                    "outputs": [],
                    "success": True,
                    "call_start": len(provider.calls),
                }
                before = time.monotonic()
                width = 4 if arm == "retry4" else 8
                for offset in range(0, 8, width):
                    images = [
                        Path(v["path"])
                        for v in group["images"][offset : offset + width]
                    ]
                    times = [
                        v["seconds"] for v in group["images"][offset : offset + width]
                    ]
                    try:
                        raw = provider.json(
                            instruction,
                            json.dumps(
                                {
                                    "frame_times_seconds": times,
                                    "transcript": group["transcript"],
                                },
                                ensure_ascii=False,
                            ),
                            images,
                        )
                        validate_frame_summaries(raw, times)
                        row["outputs"].append({"offset": offset, "raw": raw})
                    except (ProviderUnavailable, ValueError, TypeError) as error:
                        row["success"] = False
                        row["error_code"] = getattr(error, "code", type(error).__name__)
                        if getattr(error, "diagnostics", {}).get("http_status") in {
                            400,
                            401,
                            403,
                            404,
                        }:
                            stop = True
                        break
                row.update(
                    seconds=round(time.monotonic() - before, 3),
                    upstream_attempts=len(server.attempts) - start,
                    attempts=server.attempts[start:],
                )
                if row["upstream_attempts"] == 0:
                    raise RuntimeError(
                        "Request bypassed upstream counter; experiment stopped."
                    )
                rows.append(row)
                save_private(output / "results.json", rows)
                save_private(output / "upstream-attempts.json", server.attempts)
                print(
                    json.dumps(
                        {
                            k: row[k]
                            for k in [
                                "group",
                                "arm",
                                "success",
                                "seconds",
                                "upstream_attempts",
                            ]
                        }
                    ),
                    flush=True,
                )
                if stop:
                    break
            if stop:
                break
        report = {
            "real_upstream_attempts": len(server.attempts),
            "upstream_limit": upstream_limit,
            "fixture_groups": len(manifest),
            "completed_arms": len(rows),
            "arms": {},
        }
        for arm in ["baseline", "retry8", "retry4"]:
            values = [r for r in rows if r["arm"] == arm]
            report["arms"][arm] = {
                "groups": len(values),
                "successes": sum(r["success"] for r in values),
                "median_seconds": statistics.median(r["seconds"] for r in values)
                if values
                else None,
                "upstream_attempts": sum(r["upstream_attempts"] for r in values),
            }
        report["quality_review"] = (
            "pending manual evidence review; keep production batch size at 8"
        )
        save_private(output / "report.json", report)
        print(json.dumps(report, ensure_ascii=False), flush=True)


def deploy(output):
    """Apply the scoped routing change only after the shared queue drains."""
    video = load_video_config(".runtime/videorag/video.toml")
    config = load_config(".runtime/phase1/config.toml")
    previous_gate = json.loads((output / "queue-before-rollout.json").read_text())
    if not read_control(video).paused:
        raise RuntimeError("Queue gate must be paused before deployment.")
    services = [
        "rag-favorite-video-worker.service",
        "rag-favorite-summary-worker.service",
    ]
    originally_active = [
        s
        for s in services
        if subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", s], check=False
        ).returncode
        == 0
    ]
    worker_pids = {
        int(
            subprocess.check_output(
                ["systemctl", "--user", "show", "--value", "-p", "MainPID", service]
            )
        )
        for service in originally_active
    }
    deadline = time.monotonic() + 1800
    last = None
    while True:
        with connect_database(config, register_pgvector=False) as connection:
            active = []
            for table, column in [
                ("rag_video_jobs", "id"),
                ("rag_summary_jobs", "job_id"),
            ]:
                active.extend(
                    (str(row[0]), row[1])
                    for row in connection.execute(
                        f"SELECT {column},stage FROM public.{table} WHERE library_id=%s AND state='running'",
                        (library_id(video),),
                    )
                )
        active.extend(
            (entry.get("job_id", ""), "model:" + key)
            for key, entry in read_activity(video).items()
            if entry.get("status") == "running" and entry.get("pid") in worker_pids
        )
        if not active:
            break
        if active != last:
            print(json.dumps({"waiting_for_task_boundary": active}), flush=True)
            last = active
        if time.monotonic() >= deadline:
            set_control(video, paused=previous_gate["paused"])
            raise RuntimeError(
                "Task boundary wait exceeded 30 minutes; original pause flag restored."
            )
        time.sleep(5)
    origin, token = production_connection()
    before = rpc(origin, token, "getConfig")
    save_private(output / "rollback" / "ccr-config-before-deployment.json", before)
    updated = copy.deepcopy(before)
    updated["Router"]["fallback"] = {"mode": "retry", "models": [], "retryCount": 2}
    for rule in updated["Router"].get("rules", []):
        if rule.get("enabled") and rule.get("id") == "gpt-luna-medium":
            rule.pop("fallback", None)
    stopped = False
    changed = False
    try:
        if originally_active:
            stopped = True
            subprocess.run(
                ["systemctl", "--user", "stop", *originally_active], check=True
            )
        # Recheck after stopping: never intentionally change a gateway while a
        # job is still marked running, including a claim racing the first read.
        with connect_database(config, register_pgvector=False) as connection:
            assert not connection.execute(
                "SELECT 1 FROM public.rag_video_jobs WHERE library_id=%s AND state='running' UNION ALL SELECT 1 FROM public.rag_summary_jobs WHERE library_id=%s AND state='running'",
                (library_id(video), library_id(video)),
            ).fetchone()
        changed = True
        rpc(origin, token, "saveConfig", [updated, {"applyProfile": False}])
        effective = rpc(origin, token, "getConfig")
        status = rpc(origin, token, "getGatewayStatus")
        assert effective["Router"]["fallback"] == updated["Router"]["fallback"]
        assert effective["Providers"] == before["Providers"]
        assert effective["providerPlugins"] == before["providerPlugins"]
        assert effective["API_TIMEOUT_MS"] == 600000
        assert status["state"] == "running"
        if originally_active:
            subprocess.run(
                ["systemctl", "--user", "start", *originally_active], check=True
            )
        for service in originally_active:
            subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", service], check=True
            )
        set_control(video, paused=previous_gate["paused"])
        result = {
            "applied": True,
            "fallback": effective["Router"]["fallback"],
            "gateway_state": status["state"],
            "worker_services": originally_active,
            "queue_paused": read_control(video).paused,
            "frames_per_request": video.visual_frames_per_request,
            "provider_and_reasoning_preserved": True,
            "source_sha256": {
                name: hashlib.sha256(
                    (Path("src/rag_favorite") / name).read_bytes()
                ).hexdigest()
                for name in [
                    "video_provider.py",
                    "video_visual.py",
                    "knowledge_summaries.py",
                ]
            },
        }
        save_private(output / "deployment.json", result)
        print(json.dumps(result), flush=True)
    except BaseException:
        try:
            if changed:
                rpc(origin, token, "saveConfig", [before, {"applyProfile": False}])
        finally:
            if stopped and originally_active:
                subprocess.run(
                    ["systemctl", "--user", "start", *originally_active], check=False
                )
            set_control(video, paused=previous_gate["paused"])
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["stub", "benchmark", "deploy"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--upstream-limit", type=int, default=80)
    args = parser.parse_args()
    if args.mode == "stub":
        stub_check()
    elif not args.output:
        parser.error("benchmark/deploy requires --output")
    elif args.mode == "deploy":
        deploy(args.output.resolve())
    else:
        if not 1 <= args.upstream_limit <= 80:
            parser.error("upstream limit must be between 1 and 80")
        benchmark(args.output.resolve(), args.upstream_limit)


if __name__ == "__main__":
    main()
