"""Bounded, direct-to-gateway CCR benchmark; never changes production state.

Raw responses and fixtures stay in a private runtime directory. Gateway routing
retries remain enabled; client calls are counted separately from upstream attempts.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import shutil
import statistics
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw, ImageFont

from rag_favorite.video_config import load_video_config


def timestamp():
    return datetime.now(UTC).isoformat()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def append_json(path, value):
    with path.open("a") as file:
        file.write(json.dumps(value, ensure_ascii=False) + "\n")
    path.chmod(0o600)


async def management():
    service = json.loads((Path.home() / ".claude-code-router/service.json").read_text())
    url = urllib.parse.urlsplit(service["url"])
    token = urllib.parse.parse_qs(url.query)["ccr_web_token"][0]
    origin = urllib.parse.urlunsplit((url.scheme, url.netloc, "", "", ""))
    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        results = {}
        for method in ("getGatewayStatus", "getConfig"):
            response = await client.post(
                origin + "/api/ccr/rpc",
                json={"method": method, "args": []},
                headers={"x-ccr-web-auth": token},
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError("CCR management rejected " + method)
            results[method] = payload["value"]
    return results["getGatewayStatus"], results["getConfig"]


def production_activity(video):
    path = video.root / "model-activity.json"
    try:
        raw = json.loads(path.read_text())
        calls = raw.get("calls", [])
        if isinstance(calls, dict):
            calls = calls.values()
        active = []
        for row in calls:
            if not isinstance(row, dict):
                continue
            if (
                str(row.get("model", "")).lower() != "ccr"
                or row.get("status") != "running"
            ):
                continue
            pid = row.get("pid")
            if isinstance(pid, int) and Path(f"/proc/{pid}").exists():
                active.append(
                    {k: row.get(k) for k in ("pid", "started_at", "operation")}
                )
        control = video.root / "queue-control.json"
        return {
            "observed_at": timestamp(),
            "active_ccr_calls": active,
            "queue_paused": json.loads(control.read_text()).get("paused")
            if control.exists()
            else False,
        }
    except (OSError, ValueError, TypeError):
        return {"observed_at": timestamp(), "activity_unavailable": True}


def ccr_processes():
    found = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            command = (path / "cmdline").read_bytes().replace(b"\0", b" ")
            if b"claude-code-router" in command and (
                b"node" in command or b"ccr" in command
            ):
                found.append(int(path.name))
        except OSError:
            pass
    return found


def process_sample(pids):
    sample = []
    for pid in pids:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            sample.append(
                {
                    "pid": pid,
                    "cpu_seconds": (int(stat[11]) + int(stat[12]))
                    / os.sysconf("SC_CLK_TCK"),
                    "rss_bytes": int(stat[21]) * os.sysconf("SC_PAGE_SIZE"),
                }
            )
        except (OSError, ValueError, IndexError):
            pass
    return sample


def fixture(root, index):
    path = root / "fixtures" / f"card-{index:02}.png"
    number = 100 + (index * 37) % 899
    red, blue = 1 + index % 3, 1 + (index // 3) % 3
    expected = {"number": number, "red_circles": red, "blue_squares": blue}
    if not path.exists():
        image = Image.new("RGB", (512, 512), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 72)
        draw.text((40, 25), str(number), font=font, fill="black")
        for i in range(red):
            x = 40 + 145 * i
            draw.ellipse((x, 180, x + 80, 260), fill=(230, 20, 20))
        for i in range(blue):
            x = 40 + 145 * i
            draw.rectangle((x, 350, x + 80, 430), fill=(20, 40, 230))
        image.save(path)
        path.chmod(0o600)
    return path, expected


def payload(root, kind, index, request_id, model):
    prefix = f"Request nonce {request_id}. Copy it exactly as request_id. "
    if kind == "text":
        a, b, c = 11 + index % 7, 23 + index % 11, 4 + index % 5
        expected = {"request_id": request_id, "sum": a + b + c, "product": a * c}
        content = [
            {
                "type": "input_text",
                "text": prefix + f"Compute sum={a}+{b}+{c} and product={a}*{c}. "
                "Return only JSON with keys request_id, sum, product; values are integers except request_id.",
            }
        ]
        images = []
    elif kind == "real_eight_frames":
        from rag_favorite.video_visual import DENSE_INSTRUCTION

        groups = json.loads((root / "real-fixtures.json").read_text())
        group = groups[index % len(groups)]
        images = [Path(item["path"]) for item in group["images"]]
        expected = {"request_id": request_id, "frame_indices": list(range(8))}
        context = {
            "transcript": group["transcript"],
            "frame_times_seconds": [item["seconds"] for item in group["images"]],
        }
        instruction = (
            '只根据图片返回 JSON：{"request_id":"复制请求nonce",'
            '"caption":"主题","facts":[],"edges":[],"frame_summaries":'
            '[{"frame_index":0,"caption":"可观察的关键文字、动作或步骤"}]}。不要猜测。'
        )
        content = [
            {
                "type": "input_text",
                "text": prefix
                + instruction
                + DENSE_INSTRUCTION
                + "\n"
                + json.dumps(context, ensure_ascii=False),
            }
        ]
        for path in images:
            encoded = base64.b64encode(path.read_bytes()).decode()
            content.append(
                {
                    "type": "input_image",
                    "image_url": "data:image/jpeg;base64," + encoded,
                }
            )
    else:
        count = 8 if kind == "eight_images" else 1
        cards = [fixture(root, (index + i) % 24) for i in range(count)]
        images = [path for path, _ in cards]
        if count == 1:
            expected = {"request_id": request_id, **cards[0][1]}
            instruction = (
                "Read the black number and count red circles and blue squares in the image. "
                "Return only JSON with keys request_id, number, red_circles, blue_squares; "
                "all values are integers except request_id."
            )
        else:
            expected = {
                "request_id": request_id,
                "frames": [
                    {"index": i, **answer} for i, (_, answer) in enumerate(cards)
                ],
            }
            instruction = (
                "For each of the eight images in order read the black number and count "
                "red circles and blue squares. Return only JSON with keys request_id and "
                "frames. frames is an array of eight objects with integer keys "
                "index (zero-based), number, red_circles, blue_squares."
            )
        content = [{"type": "input_text", "text": prefix + instruction}]
        for path in images:
            encoded = base64.b64encode(path.read_bytes()).decode()
            content.append(
                {"type": "input_image", "image_url": "data:image/png;base64," + encoded}
            )
    body = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "stream": True,
        "store": False,
    }
    return body, expected, images


def parse_output(text):
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(value)


def valid_answer(kind, answer, expected):
    if kind != "real_eight_frames":
        return answer == expected
    from rag_favorite.video_provider import ProviderUnavailable
    from rag_favorite.video_visual import validate_frame_summaries

    if (
        not isinstance(answer, dict)
        or answer.get("request_id") != expected["request_id"]
    ):
        return False
    if not isinstance(answer.get("caption"), str) or not answer["caption"].strip():
        return False
    if not isinstance(answer.get("facts"), list) or not isinstance(
        answer.get("edges"), list
    ):
        return False
    try:
        validate_frame_summaries(answer, list(range(8)))
    except (ProviderUnavailable, TypeError, ValueError):
        return False
    return True


def quantile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    left = int(position)
    return ordered[left] + (
        ordered[min(left + 1, len(ordered) - 1)] - ordered[left]
    ) * (position - left)


class Benchmark:
    def __init__(self, args, video, key):
        self.args, self.video, self.key = args, video, key
        self.root = args.output
        self.started = time.monotonic()
        self.sent = 0
        self.in_flight = 0
        self.peak = 0
        self.stop_monitor = asyncio.Event()
        self.pids = ccr_processes()

    def allowed(self, count=1):
        return (
            self.sent + count <= self.args.max_calls
            and time.monotonic() - self.started < self.args.max_seconds
            and not (self.root / "STOP").exists()
        )

    async def monitor(self):
        while not self.stop_monitor.is_set():
            append_json(
                self.root / "resources.jsonl",
                {
                    **production_activity(self.video),
                    "elapsed": time.monotonic() - self.started,
                    "benchmark_in_flight": self.in_flight,
                    "ccr_processes": process_sample(self.pids),
                },
            )
            try:
                await asyncio.wait_for(self.stop_monitor.wait(), timeout=1)
            except TimeoutError:
                pass

    async def call(self, client, kind, index, phase, level, batch, gate=None):
        request_id = str(uuid4())
        body, expected, images = payload(
            self.root, kind, index, request_id, self.video.model
        )
        encoded = json.dumps(body).encode()
        row = {
            "request_id": request_id,
            "kind": kind,
            "phase": phase,
            "concurrency": level,
            "batch": batch,
            "expected": expected,
            "input_sha256": hashlib.sha256(encoded).hexdigest(),
            "request_bytes": len(encoded),
            "images": len(images),
            "image_bytes": sum(path.stat().st_size for path in images),
            "http_status": None,
            "completed": False,
            "json_valid": False,
            "correct": False,
            "ttft_seconds": None,
            "usage": {},
            "failure": "",
        }
        if gate:
            await gate.wait()
        self.sent += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        start = time.monotonic()
        row["started_at"], row["dispatch_offset"] = timestamp(), start - self.started
        output, raw = [], []
        try:
            async with asyncio.timeout(self.args.timeout):
                async with client.stream(
                    "POST",
                    self.video.router_url,
                    content=encoded,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + self.key,
                        "X-Request-ID": request_id,
                    },
                ) as response:
                    row["http_status"] = response.status_code
                    row["headers_seconds"] = time.monotonic() - start
                    row["ccr_headers"] = {
                        k: v
                        for k, v in response.headers.items()
                        if k
                        in {
                            "x-ccr-fallback-attempts",
                            "x-ccr-fallback-failures",
                            "x-ccr-fallback-delays-ms",
                            "x-request-id",
                            "retry-after",
                        }
                    }
                    if response.status_code != 200:
                        error = (await response.aread())[:4096].decode(errors="replace")
                        raw.append(error.replace(self.key, "<REDACTED>"))
                        row["failure"] = "http_" + str(response.status_code)
                    else:
                        async for line in response.aiter_lines():
                            raw.append(line.replace(self.key, "<REDACTED>"))
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            event = json.loads(data)
                            event_type = event.get("type")
                            if event_type == "response.output_text.delta":
                                if row["ttft_seconds"] is None:
                                    row["ttft_seconds"] = time.monotonic() - start
                                output.append(event.get("delta", ""))
                            if event_type == "response.completed":
                                result = event.get("response") or {}
                                row.update(
                                    completed=True,
                                    response_model=result.get("model"),
                                    reasoning_effort=(
                                        result.get("reasoning") or {}
                                    ).get("effort"),
                                    usage=result.get("usage") or {},
                                )
                                if not output:
                                    output = [
                                        c.get("text", "")
                                        for item in result.get("output", [])
                                        for c in item.get("content", [])
                                        if c.get("type") == "output_text"
                                    ]
                                break
                            if event_type in {
                                "response.failed",
                                "response.incomplete",
                                "error",
                            }:
                                row["failure"] = event_type
                                break
                    if row["completed"]:
                        try:
                            answer = parse_output("".join(output))
                            row["answer"], row["json_valid"] = answer, True
                            row["correct"] = valid_answer(kind, answer, expected)
                            row["content_correct"] = (
                                (
                                    isinstance(answer, dict)
                                    and {
                                        k: v
                                        for k, v in answer.items()
                                        if k != "request_id"
                                    }
                                    == {
                                        k: v
                                        for k, v in expected.items()
                                        if k != "request_id"
                                    }
                                )
                                if kind != "real_eight_frames"
                                else row["correct"]
                            )
                            row["validation"] = (
                                "frame_coverage_and_schema"
                                if kind == "real_eight_frames"
                                else "exact_answer"
                            )
                            if not row["correct"]:
                                row["failure"] = "incorrect_answer"
                        except (ValueError, TypeError):
                            row["failure"] = "invalid_json"
                    elif not row["failure"]:
                        row["failure"] = "stream_truncated"
        except TimeoutError:
            row["failure"] = "timeout"
        except (httpx.HTTPError, OSError, ValueError, TypeError) as error:
            row["failure"] = type(error).__name__
        finally:
            if not row["completed"] and not row["failure"]:
                row["failure"] = "interrupted"
            self.in_flight -= 1
            row["seconds"], row["ended_at"] = time.monotonic() - start, timestamp()
            raw_path = self.root / "responses" / (request_id + ".sse")
            raw_path.write_text("\n".join(raw) + "\n")
            raw_path.chmod(0o600)
            row["raw_response"] = str(raw_path.relative_to(self.root))
            append_json(self.root / "calls.jsonl", row)
        return row

    async def burst(self, client, kind, level, batch, phase):
        gate = asyncio.Event()
        tasks = [
            asyncio.create_task(
                self.call(
                    client, kind, (batch * level + i) % 24, phase, level, batch, gate
                )
            )
            for i in range(level)
        ]
        await asyncio.sleep(0)
        start = time.monotonic()
        gate.set()
        rows = await asyncio.gather(*tasks)
        seconds = time.monotonic() - start
        offsets = [r["dispatch_offset"] for r in rows]
        result = {
            "kind": kind,
            "phase": phase,
            "concurrency": level,
            "batch": batch,
            "seconds": seconds,
            "correct": sum(r["correct"] for r in rows),
            "requests": len(rows),
            "dispatch_spread_ms": (max(offsets) - min(offsets)) * 1000,
            "effective_rpm": sum(r["correct"] for r in rows) * 60 / seconds,
        }
        append_json(self.root / "batches.jsonl", result)
        print(json.dumps(result), flush=True)
        return rows, result

    async def sweep(self, client, kind):
        reports, flat_steps = [], 0
        for level in self.args.levels:
            if not self.allowed(level * self.args.rounds):
                break
            rows, batches = [], []
            for batch in range(self.args.rounds):
                calls, report = await self.burst(client, kind, level, batch, "sweep")
                rows.extend(calls)
                batches.append(report)
                if any(not row["correct"] for row in calls):
                    break
                await asyncio.sleep(self.args.cooldown)
            current = {
                "kind": kind,
                "concurrency": level,
                "requests": len(rows),
                "correct": sum(row["correct"] for row in rows),
                "latency_median": statistics.median(row["seconds"] for row in rows),
                "latency_p95": quantile([row["seconds"] for row in rows], 0.95),
                "effective_rpm": sum(row["correct"] for row in rows)
                * 60
                / sum(batch["seconds"] for batch in batches),
                "failures": [row["failure"] for row in rows if row["failure"]],
            }
            reports.append(current)
            write_json(self.root / f"{kind}-sweep.json", reports)
            if current["correct"] != current["requests"]:
                if self.allowed(level):
                    await asyncio.sleep(10)
                    await self.burst(client, kind, level, 99, "failure_recheck")
                break
            if len(reports) > 1:
                growth = current["effective_rpm"] / reports[-2]["effective_rpm"] - 1
                flat_steps = flat_steps + 1 if growth < 0.10 else 0
                if flat_steps >= 2:
                    break
        return reports

    async def sustain(self, client, kind, level):
        start = time.monotonic()
        deadline = start + self.args.duration
        rows = []
        lock = asyncio.Lock()
        index = 0
        stop = False

        async def lane():
            nonlocal index, stop
            while time.monotonic() < deadline and not stop:
                async with lock:
                    if not self.allowed():
                        return
                    current = index
                    index += 1
                    # No await between reservation and dispatch: call increments sent.
                row = await self.call(
                    client, kind, current % 24, "sustain", level, current
                )
                rows.append(row)
                if not row["correct"] and not row.get("content_correct", False):
                    stop = True
                print(
                    json.dumps(
                        {
                            "phase": "sustain",
                            "kind": kind,
                            "concurrency": level,
                            "completed": len(rows),
                            "seconds": row["seconds"],
                            "correct": row["correct"],
                            "failure": row["failure"],
                        }
                    ),
                    flush=True,
                )

        await asyncio.gather(*(lane() for _ in range(level)))
        elapsed = time.monotonic() - start
        result = {
            "kind": kind,
            "concurrency": level,
            "scheduled_seconds": self.args.duration,
            "elapsed_seconds": elapsed,
            "requests": len(rows),
            "correct": sum(row["correct"] for row in rows),
            "early_stop": stop,
            "effective_rpm": sum(row["correct"] for row in rows) * 60 / elapsed,
            "latency_median": statistics.median(row["seconds"] for row in rows)
            if rows
            else None,
            "latency_p95": quantile([row["seconds"] for row in rows], 0.95),
        }
        write_json(self.root / f"{kind}-sustain-{level}.json", result)
        return result

    async def run(self):
        monitor = asyncio.create_task(self.monitor())
        results = {}
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=self.args.timeout,
                limits=httpx.Limits(
                    max_connections=max(self.args.levels) + 2,
                    max_keepalive_connections=max(self.args.levels) + 2,
                ),
            ) as client:
                for kind in self.args.kinds:
                    if self.args.phase == "sweep":
                        results[kind] = await self.sweep(client, kind)
                    else:
                        results[kind] = await self.sustain(
                            client, kind, self.args.levels[0]
                        )
        finally:
            self.stop_monitor.set()
            await monitor
            write_json(
                self.root / "run.json",
                {
                    "results": results,
                    "client_calls": self.sent,
                    "peak_client_in_flight": self.peak,
                    "seconds": time.monotonic() - self.started,
                    "upstream_attempt_count": None,
                    "upstream_note": "Routing headers do not count all internal OAuth/socket retries.",
                },
            )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path(".runtime/videorag/video.toml")
    )
    parser.add_argument(
        "--phase", choices=("preflight", "sweep", "sustain"), default="preflight"
    )
    parser.add_argument(
        "--kinds",
        nargs="+",
        choices=("text", "image", "eight_images", "real_eight_frames"),
        default=["text", "image"],
    )
    parser.add_argument("--levels", nargs="+", type=int, default=[1, 2, 4, 8])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--cooldown", type=float, default=3)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--max-seconds", type=float, default=1200)
    parser.add_argument("--max-calls", type=int, default=110)
    parser.add_argument("--duration", type=float, default=180)
    parser.add_argument("--real-fixtures", type=Path)
    args = parser.parse_args()
    if any(not 1 <= level <= 16 for level in args.levels):
        parser.error("Concurrency must be in [1, 16].")
    if args.output.exists() and (args.output / "calls.jsonl").exists():
        parser.error("Use a new output directory to preserve previous evidence.")
    os.umask(0o077)
    for path in (args.output, args.output / "fixtures", args.output / "responses"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if "real_eight_frames" in args.kinds:
        if not args.real_fixtures:
            parser.error("--real-fixtures is required for real_eight_frames.")
        groups = json.loads(args.real_fixtures.read_text())
        for i, group in enumerate(groups):
            if len(group["images"]) != 8:
                parser.error("Each retained fixture must contain eight frames.")
            for j, item in enumerate(group["images"]):
                source = Path(item["path"])
                if hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
                    parser.error("Retained fixture fingerprint mismatch.")
                target = args.output / "fixtures" / f"real-{i}-{j}.jpg"
                shutil.copyfile(source, target)
                target.chmod(0o600)
                item["path"] = str(target.resolve())
        write_json(args.output / "real-fixtures.json", groups)
    video = load_video_config(args.config)
    status, cfg = await management()
    snapshot = {
        "created_at": timestamp(),
        "gateway": status,
        "requested_model": video.model,
        "router_url": video.router_url,
        "fallback": cfg.get("Router", {}).get("fallback"),
        "rules": cfg.get("Router", {}).get("rules"),
        "activity": production_activity(video),
        "ccr_pids": ccr_processes(),
        "parameters": {
            k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()
        },
        "production_changes": False,
    }
    write_json(args.output / "preflight.json", snapshot)
    print(json.dumps(snapshot, ensure_ascii=False), flush=True)
    if status.get("state") != "running":
        raise RuntimeError("CCR gateway is not running; benchmark not started.")
    if args.phase != "preflight":
        await Benchmark(args, video, video.secret("RAG_CCR_API_KEY")).run()


if __name__ == "__main__":
    asyncio.run(main())
