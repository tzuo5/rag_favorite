"""Durable request reservations. Unknown charges remain reserved across restarts."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from pathlib import Path


class ModelUsageLedger:
    def __init__(self, path: Path, *, maximum_usd: float = 0.03):
        if not math.isfinite(maximum_usd) or maximum_usd <= 0:
            raise ValueError("Cloud cost budget must be positive")
        self.path, self.maximum_usd = path, maximum_usd
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, role TEXT NOT NULL, model TEXT NOT NULL, reserved REAL NOT NULL, actual REAL, state TEXT NOT NULL, usage TEXT, price_version TEXT NOT NULL)"
            )
        path.chmod(0o600)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def reserve(
        self,
        role: str,
        model: str,
        estimate_usd: float,
        *,
        price_version: str = "2026-10-01",
    ) -> str:
        if not math.isfinite(estimate_usd) or estimate_usd < 0:
            raise ValueError("Invalid cloud cost reservation")
        request_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM requests WHERE state IN ('reserved', 'usage_unknown') LIMIT 1"
            ).fetchone():
                raise ValueError(
                    "Unresolved cloud charge; reconcile before sending another request"
                )
            committed = db.execute(
                "SELECT COALESCE(SUM(COALESCE(actual, reserved)),0) FROM requests"
            ).fetchone()[0]
            if committed + estimate_usd > self.maximum_usd:
                raise ValueError("Cloud cost budget exhausted")
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,?,NULL,'reserved',NULL,?)",
                (request_id, role, model, estimate_usd, price_version),
            )
        return request_id

    def settle(self, request_id: str, usage: dict | None) -> None:
        usage = usage if isinstance(usage, dict) else {}
        actual = usage.get("cost")
        valid = type(actual) in {int, float} and math.isfinite(actual) and actual >= 0
        with self._connect() as db:
            db.execute(
                "UPDATE requests SET actual=?,state=?,usage=? WHERE id=?",
                (
                    actual if valid else None,
                    "completed" if valid else "usage_unknown",
                    json.dumps(usage),
                    request_id,
                ),
            )

    def summary(self) -> dict:
        with self._connect() as db:
            rows = db.execute(
                "SELECT role, model, reserved, actual, state, usage FROM requests"
            ).fetchall()
        return {
            "requests": len(rows),
            "estimated_usd": sum(r[2] for r in rows),
            "actual_usd": sum(r[3] for r in rows if r[3] is not None),
            "unknown_requests": sum(r[4] != "completed" for r in rows),
            "overrun": sum(r[3] if r[3] is not None else r[2] for r in rows)
            > self.maximum_usd,
            "roles": sorted({r[0] for r in rows}),
        }
