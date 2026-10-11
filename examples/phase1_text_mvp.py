"""Reproduce Phase 1 with real local embeddings and loopback-only Python sockets."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from rag_favorite.config import database_credentials, load_config
from rag_favorite.embedding import embedding_space_id
from rag_favorite.indexes import (
    activate_generation,
    build_generation,
    create_generation,
    list_generations,
    resolve_index,
)
from rag_favorite.rag import search_documents, status_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--prefix",
        default="phase1-"
        + datetime.now(UTC).strftime("%Y%m%d")
        + "-"
        + uuid4().hex[:6],
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    effective_database = database_credentials(config)["name"]
    if (
        config.embedding.backend not in {"lmstudio", "ollama"}
        or not config.database.name.startswith("rag_phase1")
        or effective_database != config.database.name
    ):
        parser.error(
            "Use an isolated rag_phase1* database with a local LM Studio/Ollama profile."
        )
    connect = socket.socket.connect
    allowed, blocked = [], []

    def loopback_only(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            if not ipaddress.ip_address(address[0]).is_loopback:
                blocked.append(address)
                raise PermissionError(
                    "Non-loopback Python TCP connections are disabled for this demo."
                )
            allowed.append(address)
        return connect(sock, address)

    socket.socket.connect = loopback_only
    started = time.monotonic()
    records = []
    first, second = args.prefix + "-a", args.prefix + "-b"

    def verify(profile, generation):
        for query, collection, expected in [
            ("凉拌料汁生抽老抽醋各几勺？", "cooking", "sauce.md"),
            ("如何备份和恢复 PostgreSQL 数据库？", "tech", "database.md"),
        ]:
            rows = search_documents(query, 3, [collection], profile)
            assert rows and rows[0]["source_relative_path"] == expected
            assert all(
                r["knowledge_base"] == collection
                and r["metadata"]["generation"] == generation
                for r in rows
            )
            records.append(
                {
                    "generation": generation,
                    "query": query,
                    "collection": collection,
                    "top1": rows[0]["source_relative_path"],
                    "score": rows[0]["semantic_score"],
                }
            )

    try:
        try:
            socket.create_connection(("192.0.2.1", 443), timeout=0.1)
        except PermissionError:
            pass
        else:
            raise AssertionError("Outbound guard was not effective")
        with redirect_stdout(sys.stderr):
            create_generation(config, first)
            build_generation(config, first)
            activate_generation(config, first)
            verify(config, first)
            frozen = resolve_index(config)
            alternate = replace(
                config,
                embedding=replace(
                    config.embedding,
                    preprocessing_version="qwen-query-mvp-v2",
                    query_instruction="Instruct: Retrieve passages that answer the question.\nQuery: ",
                ),
            )
            assert embedding_space_id(config.embedding) != embedding_space_id(
                alternate.embedding
            )
            create_generation(alternate, second)
            build_generation(config, second)
            activate_generation(config, second)
            assert (
                resolve_index(config).embedding.query_instruction
                == alternate.embedding.query_instruction
            )
            verify(config, second)
            verify(frozen, first)
            activate_generation(config, first)
            verify(config, first)
        result = {
            "status": "passed",
            "timestamp": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "model": config.embedding.model,
            "backend": config.embedding.backend,
            "model_digest": config.embedding.model_digest,
            "dimensions": config.embedding.dimensions,
            "queries": records,
            "active_generation": status_data(config=config)["index_generation"],
            "generations": [
                g
                for g in list_generations(config)
                if g["generation"] in (first, second)
            ],
            "network_validation": {
                "python_non_loopback_tcp": "denied",
                "denied_probe_count": len(blocked),
                "allowed_loopback_connections": len(allowed),
                "note": "Client socket guard; PostgreSQL uses native libpq on loopback. Local model inventory and pinned weights are checked. No system firewall changes.",
            },
        }
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload)
        print(payload, end="")
    finally:
        socket.socket.connect = connect


if __name__ == "__main__":
    main()
