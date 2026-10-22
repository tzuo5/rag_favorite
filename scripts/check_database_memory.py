"""Bounded adapter leak regression against an explicitly selected test database.

The private Psycopg counter is read only as a diagnostic, never modified.
"""

import argparse
import gc
import json
import time
from pathlib import Path

from pgvector.psycopg import register_vector
from psycopg.adapt import AdaptersMap

from rag_favorite.config import load_config
from rag_favorite.database import connect_database


def sample():
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
    )
    return {
        "rss_kib": int(status["VmRSS"].split()[0]),
        "adapter_classes": len(AdaptersMap._optimised),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--connections", type=int, default=10_000)
    parser.add_argument("--legacy", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)

    def connect():
        with connect_database(config, register_pgvector=not args.legacy) as connection:
            if args.legacy:
                register_vector(connection)
            assert (
                connection.execute("SELECT '[1,2,3]'::vector").fetchone()[0] is not None
            )

    for _ in range(100):
        connect()
    gc.collect()
    before, start = sample(), time.monotonic()
    for _ in range(args.connections):
        connect()
    gc.collect()
    after = sample()
    growth = (after["rss_kib"] - before["rss_kib"]) / 1024
    stable = after["adapter_classes"] == before["adapter_classes"] and growth <= 10
    result = {
        "mode": "legacy" if args.legacy else "fixed",
        "connections": args.connections,
        "before": before,
        "after": after,
        "rss_growth_mib": growth,
        "seconds": round(time.monotonic() - start, 3),
        "stable": stable,
    }
    print(json.dumps(result), flush=True)
    if args.legacy:
        assert after["adapter_classes"] > before["adapter_classes"], result
    else:
        assert stable, result


if __name__ == "__main__":
    main()
