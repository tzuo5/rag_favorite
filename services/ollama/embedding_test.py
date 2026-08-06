#!/usr/bin/env python3

from __future__ import annotations

import json
import urllib.request


OLLAMA_URL = "http://127.0.0.1:11434/api/embed"
MODEL = "qwen3-embedding:0.6b"

QUERY_INSTRUCTION = (
    "Instruct: Given a user question, retrieve relevant passages "
    "from a personal knowledge base.\n"
    "Query: "
)


def embed(texts: list[str]) -> list[list[float]]:
    payload = json.dumps(
        {
            "model": MODEL,
            "input": texts,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=300) as response:
        result = json.load(response)

    embeddings = result.get("embeddings")

    if not embeddings:
        raise RuntimeError("Ollama returned no embeddings.")

    for embedding in embeddings:
        if len(embedding) != 1024:
            raise RuntimeError(
                f"Expected 1024 dimensions, got {len(embedding)}."
            )

    return embeddings


def dot_product(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def main() -> None:
    question = "我的 RAG 使用什么数据库？"

    documents = [
        (
            "server",
            "Gordon 的 RAG 系统使用 PostgreSQL 和 pgvector "
            "保存并检索文档向量。",
        ),
        (
            "cooking",
            "做红烧肉时可以先把肉焯水，然后小火炖煮。",
        ),
        (
            "school",
            "Gordon 正在 UIUC 学习机械工程和计算机科学课程。",
        ),
    ]

    inputs = [
        QUERY_INSTRUCTION + question,
        *[content for _, content in documents],
    ]

    embeddings = embed(inputs)
    query_embedding = embeddings[0]

    results = []

    for (name, content), document_embedding in zip(
        documents,
        embeddings[1:],
        strict=True,
    ):
        similarity = dot_product(
            query_embedding,
            document_embedding,
        )

        results.append((similarity, name, content))

    results.sort(reverse=True)

    for similarity, name, content in results:
        print(f"{similarity:.4f} | {name}")
        print(f"  {content}")


if __name__ == "__main__":
    main()
