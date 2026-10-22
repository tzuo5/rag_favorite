"""Observe existing ingestion until a fixed deadline; never execute queue jobs."""

from __future__ import annotations

import argparse
import csv
import fcntl
import html
import json
import os
import signal
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.model_activity import read_calls
from rag_favorite.queue_control import read_control
from rag_favorite.video_config import load_video_config
from rag_favorite.video_store import library_id

SERVICES = (
    "rag-favorite-video-worker.service",
    "rag-favorite-summary-worker.service",
    "rag-favorite-imagebind.service",
)
JOB_FIELDS = (
    "job_id",
    "state",
    "stage",
    "attempts",
    "error_code",
    "created_at",
    "updated_at",
    "collection",
    "title",
)
STAGE_FIELDS = (
    "job_id",
    "stage",
    "status",
    "token",
    "attempts",
    "started_at",
    "completed_at",
    "elapsed_seconds",
    "retry_at",
    "lease_until",
    "error_code",
    "updated_at",
)
LEDGER_FIELDS = {
    "model",
    "response_model",
    "reasoning_effort",
    "images",
    "seconds",
    "usage",
    "completed",
    "completed_at",
    "created_at",
    "client_request_id",
    "request_id",
    "call_kind",
    "request_bytes",
    "image_bytes",
    "http_status",
    "json_valid",
    "error_code",
    "error_category",
    "failure_category",
    "retryable",
    "request_sent",
    "ccr_attempts",
    "ccr_delays_ms",
}


def stamp():
    return datetime.now(UTC).isoformat()


def epoch(value):
    if not value:
        return None
    return datetime.fromisoformat(value).timestamp()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def append(path, value):
    with path.open("a", encoding="utf-8") as file:
        os.chmod(path, 0o600)
        file.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def rows(path):
    if not path.exists():
        return []
    result = []
    with path.open() as file:
        for line in file:
            try:
                result.append(json.loads(line))
            except ValueError:
                continue
    return result


def service_state():
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            *SERVICES,
            "-p",
            "Id",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "MainPID",
        ],
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )
    states = {}
    for block in result.stdout.strip().split("\n\n"):
        record = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if record.get("Id"):
            states[record.pop("Id")] = record
    return states


def resources():
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            memory[key + "_kib"] = int(value.split()[0])
    cpu = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    result = {
        "at": stamp(),
        "memory": memory,
        "cpu_ticks": cpu,
        "loadavg": [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]],
    }
    try:
        gpu = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if gpu.returncode == 0:
            result["gpu"] = [
                dict(
                    zip(
                        (
                            "index",
                            "utilization_percent",
                            "used_mib",
                            "total_mib",
                            "temperature_c",
                            "power_w",
                        ),
                        [x.strip() for x in line.split(",")],
                        strict=True,
                    )
                )
                for line in gpu.stdout.splitlines()
            ]
    except (OSError, subprocess.TimeoutExpired):
        result["gpu_unavailable"] = True
    return result


class Recorder:
    def __init__(self, args):
        self.config = load_config(args.config)
        self.video = load_video_config(args.video)
        self.output = Path(args.output).resolve()
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.deadline = datetime.fromisoformat(args.until).timestamp()
        self.stopping = False
        self.connection = None
        self.jobs, self.stages, self.calls, self.offsets = {}, {}, {}, {}
        self.watched = set()
        manifest = self.output / "manifest.json"
        if manifest.exists():
            self.manifest = json.loads(manifest.read_text())
            self.started = epoch(self.manifest["started_at"])
        else:
            self.started = time.time()
            self.manifest = {
                "version": 1,
                "started_at": stamp(),
                "ends_at": args.until,
                "timezone": "America/Chicago",
                "run_id": self.output.name,
                "sampling_seconds": 1,
                "resource_sampling_seconds": 10,
                "ccr_concurrency": self.video.ccr_concurrency,
                "requested_ccr_model": self.video.model,
                "asr_device": self.video.asr_device,
                "asr_compute_type": self.video.asr_compute_type,
                "asr_model": self.video.asr_model.name
                if self.video.asr_model
                else None,
                "imagebind_device": self.video.device,
                "embedding_model": self.config.embedding.model,
                "queue_gate_before": asdict(read_control(self.video)),
                "services_before": service_state(),
                "scope": "existing queue; no synthetic model calls or repair",
                "instrumentation": "pending",
            }
            save(manifest, self.manifest)
        premature = self.output / "final.json"
        if (
            premature.exists()
            and epoch(json.loads(premature.read_text())["at"]) < self.deadline
        ):
            premature.rename(
                self.output / f"collector-reload-snapshot-{time.time_ns()}.json"
            )
        checkpoint = self.output / "recorder-checkpoint.json"
        if checkpoint.exists():
            state = json.loads(checkpoint.read_text())
            self.offsets = state.get("ledger_offsets", {})
            self.watched.update(state.get("watched_jobs", []))

    def database(self):
        if self.connection is None or self.connection.closed:
            self.connection = connect_database(
                self.config,
                register_pgvector=False,
                application_name="rag-overnight-readonly",
            )
            self.connection.autocommit = True
            self.connection.execute("SET default_transaction_read_only=on")
            self.connection.execute("SET statement_timeout=3000")
        c = self.connection
        jobs = c.execute(
            "SELECT id,state,stage,attempts,error_code,created_at,updated_at,collection,left(payload->>'title',300) FROM public.rag_video_jobs WHERE library_id=%s",
            (library_id(self.video),),
        ).fetchall()
        stages = c.execute(
            "SELECT job_id,stage,status,token,attempts,started_at,completed_at,elapsed_seconds,retry_at,lease_until,error_code,updated_at FROM public.rag_video_pipeline WHERE library_id=%s",
            (library_id(self.video),),
        ).fetchall()

        def record(fields, row):
            return {
                k: v.isoformat()
                if isinstance(v, datetime)
                else str(v)
                if k == "job_id"
                else v
                for k, v in zip(fields, row, strict=True)
            }

        return [record(JOB_FIELDS, row) for row in jobs], [
            record(STAGE_FIELDS, row) for row in stages
        ]

    def poll(self):
        at = stamp()
        jobs, stages = self.database()
        if not (self.output / "baseline.json").exists():
            save(
                self.output / "baseline.json",
                {
                    "at": at,
                    "jobs": jobs,
                    "stages": stages,
                    "queue_control": asdict(read_control(self.video)),
                },
            )
        changes = [job for job in jobs if self.jobs.get(job["job_id"]) != job]
        for job in changes:
            self.jobs[job["job_id"]] = job
            if (
                job["state"] in {"running", "published"}
                or epoch(job["updated_at"]) >= self.started
            ):
                self.watched.add(job["job_id"])
        if changes:
            append(self.output / "task-changes.jsonl", {"at": at, "jobs": changes})
        changes = []
        for stage in stages:
            key = stage["job_id"] + ":" + stage["stage"]
            # Lease heartbeats are not stage transitions.
            significant = {
                k: v for k, v in stage.items() if k not in {"lease_until", "updated_at"}
            }
            if self.stages.get(key) != significant:
                self.stages[key] = significant
                changes.append(stage)
        if changes:
            append(self.output / "stage-changes.jsonl", {"at": at, "stages": changes})
        changed_calls = []
        live = []
        for identifier, call in read_calls(self.video).items():
            if call["status"] == "running" and Path(f"/proc/{call['pid']}").exists():
                live.append(call)
            if (
                epoch(call.get("started_at")) or epoch(call.get("updated_at")) or 0
            ) < self.started and call["status"] != "running":
                continue
            if self.calls.get(identifier) != call:
                self.calls[identifier] = call
                changed_calls.append(call)
        if changed_calls:
            append(
                self.output / "call-snapshots.jsonl", {"at": at, "calls": changed_calls}
            )
        sample = {
            "at": at,
            "queue_counts": dict(Counter(j["state"] for j in jobs)),
            "active_stages": [
                {k: v for k, v in s.items() if k != "updated_at"}
                for s in stages
                if s["status"] == "running"
            ],
            "active_calls": [
                {
                    k: c.get(k)
                    for k in ("call_id", "job_id", "model", "operation", "started_at")
                }
                for c in live
            ],
            "queue_control": asdict(read_control(self.video)),
        }
        append(self.output / "samples.jsonl", sample)
        save(
            self.output / "status.json",
            {
                "state": "running",
                "pid": os.getpid(),
                "heartbeat_at": at,
                "started_at": self.manifest["started_at"],
                "ends_at": self.manifest["ends_at"],
                "watched_jobs": len(self.watched),
                "queue_counts": sample["queue_counts"],
                "active_model_calls": len(live),
            },
        )

    def ledgers(self):
        for job_id in self.watched:
            for name in (
                "provider-usage.jsonl",
                "summary-usage.jsonl",
                "image-provider-usage.jsonl",
            ):
                path = self.video.root / "derived" / job_id / name
                if not path.exists():
                    continue
                key = str(path)
                offset = self.offsets.get(key, 0)
                size = path.stat().st_size
                if size < offset:
                    append(
                        self.output / "observer-errors.jsonl",
                        {
                            "at": stamp(),
                            "kind": "ledger_truncated",
                            "job_id": job_id,
                            "ledger": name,
                        },
                    )
                    offset = 0
                with path.open("rb") as file:
                    file.seek(offset)
                    while True:
                        position = file.tell()
                        line = file.readline()
                        if not line or not line.endswith(b"\n"):
                            file.seek(position)
                            break
                        try:
                            raw = json.loads(line)
                        except ValueError:
                            append(
                                self.output / "observer-errors.jsonl",
                                {
                                    "at": stamp(),
                                    "kind": "invalid_provider_ledger_line",
                                    "job_id": job_id,
                                    "ledger": name,
                                    "source_offset": position,
                                },
                            )
                            continue
                        if (
                            epoch(raw.get("completed_at"))
                            or epoch(raw.get("created_at"))
                            or 0
                        ) >= self.started:
                            append(
                                self.output / "provider-ledgers.jsonl",
                                {
                                    "at": stamp(),
                                    "job_id": job_id,
                                    "ledger": name,
                                    "source_offset": position,
                                    **{
                                        k: v
                                        for k, v in raw.items()
                                        if k in LEDGER_FIELDS
                                    },
                                },
                            )
                    self.offsets[key] = file.tell()
        save(
            self.output / "recorder-checkpoint.json",
            {"ledger_offsets": self.offsets, "watched_jobs": sorted(self.watched)},
        )
        # Flush timing events without forcing an fsync on every model boundary.
        events = self.output / "events.jsonl"
        if events.exists():
            with events.open("rb") as file:
                os.fsync(file.fileno())

    def final(self):
        self.ledgers()
        if time.time() < self.deadline:
            save(
                self.output / "status.json",
                {
                    "state": "stopped",
                    "pid": os.getpid(),
                    "heartbeat_at": stamp(),
                    "ends_at": self.manifest["ends_at"],
                    "cutoff_reason": "observer_stopped_before_deadline",
                },
            )
            return
        query_error = None
        try:
            jobs, stages = self.database()
            published = self.connection.execute(
                "SELECT DISTINCT d.source_job_id FROM public.rag_knowledge_documents d JOIN public.rag_library_state l ON l.library_id=d.library_id JOIN public.rag_document_summaries s ON s.library_id=d.library_id AND s.generation=l.active_generation AND s.document_id=d.id JOIN public.rag_document_summary_chunks ch ON ch.library_id=s.library_id AND ch.generation=s.generation AND ch.document_id=s.document_id WHERE d.library_id=%s AND d.source_job_id IS NOT NULL",
                (library_id(self.video),),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001 - preserve partial evidence at deadline
            query_error = type(exc).__name__
            jobs, stages = list(self.jobs.values()), list(self.stages.values())
            published = []
            append(
                self.output / "observer-errors.jsonl",
                {
                    "at": stamp(),
                    "kind": "final_snapshot_unavailable",
                    "error_type": query_error,
                },
            )
        save(
            self.output / "final.json",
            {
                "at": stamp(),
                "jobs": jobs,
                "stages": stages,
                "searchable_job_ids": [str(r[0]) for r in published],
                "queue_control": asdict(read_control(self.video)),
                "services": service_state(),
                "database_snapshot_available": query_error is None,
                "snapshot_error_type": query_error,
            },
        )
        report(self.output)
        save(
            self.output / "status.json",
            {
                "state": "complete"
                if query_error is None
                else "complete_with_missing_final_snapshot",
                "pid": os.getpid(),
                "heartbeat_at": stamp(),
                "started_at": self.manifest["started_at"],
                "ends_at": self.manifest["ends_at"],
                "cutoff_reason": "deadline",
                "queue_kept_running": not read_control(self.video).paused,
            },
        )

    def run(self):
        with (self.output / "monitor.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: setattr(self, "stopping", True))
            next_resources, next_services, next_ledgers = 0, 0, 0
            while time.time() < self.deadline and not self.stopping:
                start = time.monotonic()
                try:
                    self.poll()
                    if start >= next_resources:
                        append(self.output / "resources.jsonl", resources())
                        next_resources = start + 10
                    if start >= next_services:
                        append(
                            self.output / "services.jsonl",
                            {"at": stamp(), "services": service_state()},
                        )
                        next_services = start + 30
                    if start >= next_ledgers:
                        self.ledgers()
                        next_ledgers = start + 10
                except Exception as exc:  # noqa: BLE001 - record failures without mutating queue
                    append(
                        self.output / "observer-errors.jsonl",
                        {"at": stamp(), "kind": type(exc).__name__},
                    )
                    save(
                        self.output / "status.json",
                        {
                            "state": "degraded",
                            "pid": os.getpid(),
                            "heartbeat_at": stamp(),
                            "error_type": type(exc).__name__,
                            "ends_at": self.manifest["ends_at"],
                        },
                    )
                    if self.connection:
                        self.connection.close()
                    self.connection = None
                time.sleep(
                    min(
                        max(0.05, 1 - (time.monotonic() - start)),
                        max(0.05, self.deadline - time.time()),
                    )
                )
            self.final()


def csv_file(path, records, fields):
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    k: json.dumps(v, ensure_ascii=False)
                    if isinstance(v, (dict, list))
                    else v
                    for k, v in record.items()
                }
            )
    path.chmod(0o600)


def concurrency(intervals, start, end):
    """Integrate half-open intervals; simultaneous ends precede starts."""
    points = []
    for left, right in intervals:
        left, right = max(start, left), min(end, right)
        if right > left:
            points.extend(((left, 1), (right, -1)))
    active, peak, previous, area, overlap = 0, 0, start, 0.0, 0.0
    for at, delta in sorted(points):
        duration = at - previous
        area += active * duration
        if active >= 2:
            overlap += duration
        active += delta
        peak = max(peak, active)
        previous = at
    return {
        "peak": peak,
        "average_over_window": area / max(1, end - start),
        "overlap_seconds": overlap,
        "total_call_seconds": area,
    }


def different_models(groups, start, end):
    points = []
    for model, intervals in groups.items():
        for left, right in intervals:
            left, right = max(start, left), min(end, right)
            if right > left:
                points.extend(((left, 1, model), (right, -1, model)))
    counts, previous, peak, overlap = Counter(), start, 0, 0.0
    pairs = Counter()
    for at, delta, model in sorted(points):
        active = sorted(k for k, count in counts.items() if count > 0)
        if len(active) >= 2:
            overlap += at - previous
        for i, left in enumerate(active):
            for right in active[i + 1 :]:
                pairs[left + "+" + right] += at - previous
        counts[model] += delta
        peak = max(peak, sum(count > 0 for count in counts.values()))
        previous = at
    return {
        "peak_distinct_models": peak,
        "overlap_seconds": overlap,
        "pair_overlap_seconds": dict(pairs),
    }


def duration_metrics(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "total_seconds": 0}

    def quantile(p):
        return values[min(len(values) - 1, int((len(values) - 1) * p))]

    return {
        "count": len(values),
        "total_seconds": sum(values),
        "mean_seconds": sum(values) / len(values),
        "p50_seconds": quantile(0.5),
        "p95_seconds": quantile(0.95),
        "max_seconds": values[-1],
    }


def report(output):
    output = Path(output)
    manifest = json.loads((output / "manifest.json").read_text())
    baseline = json.loads((output / "baseline.json").read_text())
    final_path = output / "final.json"
    final = (
        json.loads(final_path.read_text())
        if final_path.exists()
        else {"at": stamp(), "jobs": [], "stages": [], "searchable_job_ids": []}
    )
    start = epoch(manifest["started_at"])
    end = min(epoch(final["at"]), epoch(manifest["ends_at"]))
    baseline_jobs = {j["job_id"]: j for j in baseline["jobs"]}
    latest = dict(baseline_jobs)
    changed = set()
    for sample in rows(output / "task-changes.jsonl"):
        for job in sample["jobs"]:
            latest[job["job_id"]] = job
            if epoch(job["updated_at"]) >= start:
                changed.add(job["job_id"])
    latest.update({j["job_id"]: j for j in final["jobs"]})
    events = rows(output / "events.jsonl")
    spans, calls = {}, {}
    for sample in rows(output / "call-snapshots.jsonl"):
        for call in sample["calls"]:
            calls[call["call_id"]] = {
                **calls.get(call["call_id"], {}),
                **call,
                "source": "activity_snapshot",
            }
    for event in events:
        changed.add(event["job_id"])
        if event["event"] in {"span_start", "span_end"}:
            identifier = event["span_id"]
            item = spans.setdefault(
                identifier,
                {
                    "span_id": identifier,
                    "job_id": event["job_id"],
                    "kind": event["kind"],
                    "operation": event["operation"],
                    "parent_span_id": event.get("parent_span_id"),
                    "token": event.get("token"),
                    "attempt": event.get("attempt"),
                },
            )
            if event["event"] == "span_start":
                item["started_at"] = event["at"]
            else:
                item.update(
                    completed_at=event["at"],
                    duration_seconds=event["duration_seconds"],
                    outcome=event["outcome"],
                    error_code=event.get("error_code"),
                )
            for key in ("request_id", "model", "images", "request_bytes", "call_kind"):
                if key in event:
                    item[key] = event[key]
        elif event["event"].startswith("model_"):
            identifier = event["call_id"]
            item = calls.setdefault(identifier, {"call_id": identifier})
            item.update(
                job_id=event["job_id"],
                model=event["model"],
                operation=event["operation"],
                source="durable_event",
            )
            item["parent_span_id"] = event.get("parent_span_id")
            if event["event"] == "model_start":
                item.update(started_at=event["at"], status="running")
            elif event["event"] == "model_end":
                item.update(
                    completed_at=event["at"],
                    duration_seconds=event["duration_seconds"],
                    status=event["status"],
                )
            elif event["event"] == "model_cached":
                item.update(
                    started_at=event["at"],
                    completed_at=event["at"],
                    status="cached",
                    duration_seconds=0,
                )
            else:
                item.update(event.get("metadata", {}))
    ledgers = {
        (r["job_id"], r["ledger"], r["source_offset"]): r
        for r in rows(output / "provider-ledgers.jsonl")
    }
    by_request = {
        r.get("client_request_id"): r
        for r in ledgers.values()
        if r.get("client_request_id")
    }
    for item in calls.values():
        ledger = by_request.get(item.get("request_id"))
        if ledger:
            item.update(
                {
                    k: v
                    for k, v in ledger.items()
                    if k
                    not in {
                        "model",
                        "at",
                        "job_id",
                        "seconds",
                        "completed_at",
                        "created_at",
                        "error_code",
                        "request_id",
                    }
                }
            )
            item["requested_model"] = ledger.get("model")
            item["gateway_request_id"] = ledger.get("request_id")
        if item.get("status") == "running":
            item["status"] = "unfinished_at_cutoff"
            item["observed_seconds"] = max(
                0, end - max(start, epoch(item.get("started_at")) or start)
            )
    matched_requests = {c.get("request_id") for c in calls.values()}
    unmatched = [
        r
        for r in ledgers.values()
        if r.get("client_request_id") not in matched_requests
    ]
    for record in unmatched:
        identifier = (
            record.get("client_request_id")
            or f"{record['job_id']}:{record['ledger']}:{record['source_offset']}"
        )
        completed = epoch(record.get("completed_at"))
        created = record.get("created_at")
        calls[identifier] = {
            **record,
            "call_id": identifier,
            "request_id": identifier,
            "gateway_request_id": record.get("request_id"),
            "model": "CCR",
            "requested_model": record.get("model"),
            "operation": record.get("call_kind", "extract"),
            "started_at": created
            or datetime.fromtimestamp(
                completed - record.get("seconds", 0), UTC
            ).isoformat(),
            "duration_seconds": record.get("seconds"),
            "status": "success"
            if record.get("completed") and record.get("json_valid")
            else "failure",
            "source": "provider_ledger_client_interval",
        }
    for item in spans.values():
        if not item.get("completed_at"):
            item.update(
                outcome="unfinished_at_cutoff",
                observed_seconds=max(
                    0, end - max(start, epoch(item.get("started_at")) or start)
                ),
            )
    groups = defaultdict(list)
    for call in calls.values():
        if call.get("status") == "cached":
            continue
        left = epoch(call.get("started_at"))
        right = epoch(call.get("completed_at")) or end
        if left is not None:
            groups[call["model"]].append((left, right))
    model_metrics = {
        key: concurrency(value, start, end) for key, value in groups.items()
    }
    # Actual CCR HTTP spans exclude admission and encoding time in model wrappers.
    request_intervals = [
        (epoch(s["started_at"]), epoch(s.get("completed_at")) or end)
        for s in spans.values()
        if s["kind"] == "request"
        and s["operation"] == "CCR.http"
        and s.get("started_at")
    ]
    http_metrics = concurrency(request_intervals, start, end)
    request_groups = defaultdict(list)
    for item in spans.values():
        family = item["operation"].split(".", 1)[0]
        if (
            item["kind"] == "request"
            and family in {"CCR", "ASR", "ImageBind", "Embedding"}
            and item.get("started_at")
        ):
            request_groups[family].append(
                (epoch(item["started_at"]), epoch(item.get("completed_at")) or end)
            )
    first_stage_starts = {}
    for snapshot in rows(output / "stage-changes.jsonl"):
        for stage in snapshot["stages"]:
            at = epoch(stage.get("started_at"))
            if at is not None and start <= at <= end:
                first_stage_starts[stage["job_id"]] = min(
                    at, first_stage_starts.get(stage["job_id"], at)
                )
    calls_by_job, spans_by_job = defaultdict(list), defaultdict(list)
    for call in calls.values():
        calls_by_job[call.get("job_id")].append(call)
        parent = call.get("parent_span_id")
        while parent in spans:
            ancestor = spans[parent]
            if ancestor["kind"] == "stage":
                call["pipeline_stage"] = ancestor["operation"]
                break
            parent = ancestor.get("parent_span_id")
    for item in spans.values():
        spans_by_job[item["job_id"]].append(item)
    live_path = output / "live-searchability.json"
    live_searchability = json.loads(live_path.read_text()) if live_path.exists() else {}
    searchable = (
        set(final.get("searchable_job_ids", []))
        if final_path.exists()
        else {
            identifier
            for identifier, verified in live_searchability.get("results", {}).items()
            if verified is True
        }
    )
    job_records = []
    for identifier, job in sorted(latest.items()):
        old = baseline_jobs.get(identifier, {})
        job_calls = calls_by_job[identifier]
        job_spans = spans_by_job[identifier]
        touched = (
            identifier in changed
            or bool(job_calls)
            or old.get("state") in {"running", "published"}
        )
        first_start = min(
            [
                epoch(s["started_at"])
                for s in job_spans
                if s["kind"] == "stage" and s.get("started_at")
            ]
            + (
                [first_stage_starts[identifier]]
                if identifier in first_stage_starts
                else []
            )
            or [end]
        )
        job_records.append(
            {
                **job,
                "initial_state": old.get("state", "arrived_during_window"),
                "observed_activity": touched,
                "new_completion": job["state"] == "complete"
                and old.get("state") != "complete"
                and epoch(job["updated_at"]) <= end,
                "searchable_at_final": identifier in searchable,
                "model_calls": sum(c.get("status") != "cached" for c in job_calls),
                "cache_reuses": sum(c.get("status") == "cached" for c in job_calls),
                "model_client_seconds": sum(
                    c.get("duration_seconds") or 0 for c in job_calls
                ),
                "first_observed_stage_start": datetime.fromtimestamp(
                    first_start, UTC
                ).isoformat()
                if first_start < end
                else None,
                "queue_wait_within_window_seconds": max(
                    0, min(first_start, end) - max(start, epoch(job["created_at"]))
                )
                if (old.get("state") == "queued" or not old)
                and (first_start < end or job["state"] == "queued")
                else None,
            }
        )
    samples = rows(output / "samples.jsonl")
    gaps = []
    for previous, following in pairwise(samples):
        gap = epoch(following["at"]) - epoch(previous["at"])
        if gap > 5:
            gaps.append({"from": previous["at"], "to": following["at"], "seconds": gap})
    errors = rows(output / "observer-errors.jsonl")
    terminal = Counter(c.get("status") for c in calls.values())
    summary = {
        "started_at": manifest["started_at"],
        "cutoff_at": datetime.fromtimestamp(end, UTC).isoformat(),
        "ends_at": manifest["ends_at"],
        "final_snapshot_at": final["at"],
        "final_report": final_path.exists(),
        "final_database_snapshot_available": final.get(
            "database_snapshot_available", final_path.exists()
        ),
        "searchability_checked_at": final["at"]
        if final_path.exists()
        else live_searchability.get("checked_at"),
        "instrumentation_started_at": manifest.get("instrumentation_started_at"),
        "durable_event_write_errors": sum(
            len(rows(p))
            for p in output.parent.parent.glob("observation-errors-*.jsonl")
        ),
        "elapsed_seconds": max(0, end - start),
        "initial_queue_counts": dict(Counter(j["state"] for j in baseline["jobs"])),
        "final_queue_counts": dict(Counter(j["state"] for j in latest.values())),
        "new_complete_jobs": sum(j["new_completion"] for j in job_records),
        "new_complete_and_searchable_jobs": sum(
            j["new_completion"] and j["searchable_at_final"] for j in job_records
        ),
        "jobs_with_observed_activity": sum(j["observed_activity"] for j in job_records),
        "new_blocked_jobs": sum(
            j["state"] == "blocked" and j["initial_state"] != "blocked"
            for j in job_records
        ),
        "model_call_outcomes": dict(terminal),
        "model_client_concurrency": model_metrics,
        "ccr_http_inflight_concurrency": http_metrics,
        "request_concurrency_by_model": {
            family: concurrency(intervals, start, end)
            for family, intervals in request_groups.items()
        },
        "different_model_request_overlap": different_models(request_groups, start, end),
        "stage_execution_metrics": {
            stage: duration_metrics(
                [
                    s["duration_seconds"]
                    for s in spans.values()
                    if s["kind"] == "stage"
                    and s["operation"] == stage
                    and "duration_seconds" in s
                ]
            )
            for stage in ("download", "prepare", "llm", "publish")
        },
        "model_latency_metrics": {
            family: duration_metrics(
                [
                    c["duration_seconds"]
                    for c in calls.values()
                    if c["model"] == family
                    and c.get("status") != "cached"
                    and c.get("duration_seconds") is not None
                ]
            )
            for family in groups
        },
        "searchable_completions_per_hour": sum(
            j["new_completion"] and j["searchable_at_final"] for j in job_records
        )
        * 3600
        / max(1, end - start),
        "task_error_counts": dict(
            Counter(
                j.get("error_code") or "unspecified"
                for j in job_records
                if j["observed_activity"] and j["state"] in {"failed", "blocked"}
            )
        ),
        "transport_retry_attempts": sum(
            max(0, (c.get("ccr_attempts") or 1) - 1) for c in calls.values()
        ),
        "observer_errors": len(errors),
        "sampling_gaps": gaps,
        "stage_attempts_recorded": sum(s["kind"] == "stage" for s in spans.values()),
        "limitations": [
            "客户端在途重叠不证明服务端同时推理。",
            "模型调用时长可能包含服务端资源锁等待；CCR HTTP span 单独排除客户端名额等待。",
            "嵌套细分耗时不能直接相加；数据库阶段耗时包含历次执行，需扣除基线。",
            "开始前已有执行和截止时未结束的操作仅报告窗口内可观测部分。",
            "最终数据库快照在截止后采集，个别状态可能在收尾期间变化。",
        ],
    }
    save(output / "report.json", summary)
    csv_file(
        output / "jobs.csv",
        job_records,
        [
            *JOB_FIELDS,
            "initial_state",
            "observed_activity",
            "new_completion",
            "searchable_at_final",
            "model_calls",
            "cache_reuses",
            "model_client_seconds",
            "first_observed_stage_start",
            "queue_wait_within_window_seconds",
        ],
    )
    csv_file(
        output / "calls.csv",
        list(calls.values()),
        [
            "call_id",
            "job_id",
            "model",
            "requested_model",
            "response_model",
            "reasoning_effort",
            "operation",
            "status",
            "started_at",
            "completed_at",
            "duration_seconds",
            "observed_seconds",
            "request_id",
            "gateway_request_id",
            "pipeline_stage",
            "http_status",
            "stream_complete",
            "valid_output",
            "json_valid",
            "images",
            "request_bytes",
            "image_bytes",
            "call_kind",
            "ccr_attempts",
            "ccr_delays_ms",
            "usage",
            "error_category",
            "source",
        ],
    )
    csv_file(
        output / "operations.csv",
        list(spans.values()),
        [
            "span_id",
            "parent_span_id",
            "job_id",
            "kind",
            "operation",
            "token",
            "attempt",
            "started_at",
            "completed_at",
            "duration_seconds",
            "observed_seconds",
            "outcome",
            "error_code",
            "request_id",
            "model",
            "images",
            "request_bytes",
        ],
    )
    baseline_stages = {(s["job_id"], s["stage"]): s for s in baseline["stages"]}
    latest_stages = dict(baseline_stages)
    for sample in rows(output / "stage-changes.jsonl"):
        latest_stages.update({(s["job_id"], s["stage"]): s for s in sample["stages"]})
    latest_stages.update({(s["job_id"], s["stage"]): s for s in final["stages"]})
    stage_records = [
        {
            **stage,
            "window_completed_execution_seconds": max(
                0,
                stage["elapsed_seconds"]
                - baseline_stages.get(key, {}).get("elapsed_seconds", 0),
            ),
            "initial_attempts": baseline_stages.get(key, {}).get("attempts", 0),
        }
        for key, stage in latest_stages.items()
    ]
    csv_file(
        output / "stages.csv",
        stage_records,
        [*STAGE_FIELDS, "window_completed_execution_seconds", "initial_attempts"],
    )
    csv_file(
        output / "resources.csv",
        [
            {
                "at": r["at"],
                "loadavg": r.get("loadavg"),
                "cpu_ticks": r.get("cpu_ticks"),
                "memory": r.get("memory"),
                "gpu": r.get("gpu"),
            }
            for r in rows(output / "resources.jsonl")
        ],
        ["at", "loadavg", "cpu_ticks", "memory", "gpu"],
    )

    def table(records, fields, limit=500):
        return (
            "<table><thead><tr>"
            + "".join("<th>" + html.escape(f) + "</th>" for f in fields)
            + "</tr></thead><tbody>"
            + "".join(
                "<tr>"
                + "".join(
                    "<td>" + html.escape(str(r.get(f, ""))) + "</td>" for f in fields
                )
                + "</tr>"
                for r in records[:limit]
            )
            + "</tbody></table>"
        )

    local = lambda v: (
        datetime.fromisoformat(v)
        .astimezone(ZoneInfo("America/Chicago"))
        .strftime("%Y-%m-%d %H:%M:%S %Z")
    )
    timeline = []
    colors = {
        "CCR": "#2563eb",
        "ASR": "#dc2626",
        "ImageBind": "#7c3aed",
        "Embedding": "#059669",
    }
    for lane, (name, intervals) in enumerate(sorted(groups.items())):
        timeline.append(f'<text x="0" y="{lane * 42 + 22}">{html.escape(name)}</text>')
        for left, right in intervals:
            x = 100 + 900 * (max(start, left) - start) / max(1, end - start)
            width = max(
                1, 900 * (min(end, right) - max(start, left)) / max(1, end - start)
            )
            if min(end, right) > max(start, left):
                timeline.append(
                    f'<rect x="{x:.2f}" y="{lane * 42 + 7}" width="{width:.2f}" height="20" fill="{colors.get(name, "#64748b")}" opacity=".55"><title>{html.escape(name)} {right - left:.3f}s</title></rect>'
                )
    content = f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>RAG 隔夜监控</title><style>body{{font:16px system-ui;margin:32px;color:#172033;background:#f8fafc}}main{{max-width:1400px;margin:auto}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{border:1px solid #cbd5e1;padding:6px;text-align:left;overflow-wrap:anywhere}}th{{background:#e2e8f0}}section{{overflow:auto;margin:24px 0;background:white;padding:16px;border-radius:10px}}svg{{width:100%;min-width:800px}}a{{color:#2563eb}}code{{white-space:pre-wrap}}h1{{font-size:27px}}</style><main><h1>RAG 隔夜监控 {"最终报告" if final_path.exists() else "实时预览"}</h1><p>{local(manifest["started_at"])} → {local(summary["cutoff_at"])}</p><p>新完成 {summary["new_complete_jobs"]}，最终可检索 {summary["new_complete_and_searchable_jobs"]}；新受阻 {summary["new_blocked_jobs"]}；观察到活动的任务 {summary["jobs_with_observed_activity"]}。</p><p>配置 CCR 上限 {manifest["ccr_concurrency"]}；实测 CCR HTTP 在途峰值 {http_metrics["peak"]}；采样缺口 {len(gaps)} 段；记录器异常 {len(errors)} 条。</p><p><a href="jobs.csv">任务 CSV</a> · <a href="stages.csv">阶段 CSV</a> · <a href="calls.csv">调用 CSV</a> · <a href="operations.csv">细分操作 CSV</a> · <a href="resources.csv">资源 CSV</a> · <a href="report.json">完整统计 JSON</a></p><section><h2>模型调用时间线</h2><svg viewBox="0 0 1020 {max(80, len(groups) * 42)}">{"".join(timeline)}</svg>{table([dict(model=k, **v) for k, v in model_metrics.items()], ["model", "peak", "average_over_window", "overlap_seconds", "total_call_seconds"])}</section><section><h2>期间任务明细（前 500 条，完整记录见 CSV）</h2>{table([j for j in job_records if j["observed_activity"]], ["job_id", "title", "initial_state", "state", "stage", "error_code", "model_calls", "model_client_seconds", "searchable_at_final"])}</section><section><h2>细分操作（前 500 条）</h2>{table(list(spans.values()), ["job_id", "kind", "operation", "started_at", "completed_at", "duration_seconds", "outcome"])}</section><section><h2>测量边界</h2><ul>{"".join("<li>" + html.escape(x) + "</li>" for x in summary["limitations"])}</ul></section></main></html>'
    (output / "report.html").write_text(content, encoding="utf-8")
    (output / "report.html").chmod(0o600)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "report"))
    parser.add_argument("--config", default=".runtime/phase1/config.toml")
    parser.add_argument("--video", default=".runtime/videorag/video.toml")
    parser.add_argument("--output", required=True)
    parser.add_argument("--until", default="2026-10-11T09:00:00-05:00")
    args = parser.parse_args()
    os.umask(0o077)
    if args.mode == "report":
        print(json.dumps(report(Path(args.output)), ensure_ascii=False))
    else:
        Recorder(args).run()


if __name__ == "__main__":
    main()
