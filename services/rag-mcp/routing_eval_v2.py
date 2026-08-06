from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


AUDIT_DIR = (
    Path.home()
    / "openclaw-audit"
    / "phase4-routing-eval-v2"
)

AUDIT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


TESTS: list[dict[str, Any]] = [
    {
        "name": "implicit-server-model",
        "expected": "search",
        "prompt": (
            "我服务器上当前使用的 embedding 模型叫什么？"
            "同时告诉我向量维度。"
        ),
    },
    {
        "name": "implicit-investment-history",
        "expected": "search",
        "prompt": (
            "我之前记录的长期投资重点是什么？"
        ),
    },
    {
        "name": "implicit-cpp-history",
        "expected": "search",
        "prompt": (
            "我之前学 C++ 时主要记录了哪些算法主题？"
        ),
    },
    {
        "name": "explicit-rag-health",
        "expected": "status",
        "prompt": (
            "检查我的私人 RAG 当前是否健康，"
            "并告诉我文档数和 chunk 数。"
        ),
    },
    {
        "name": "current-context-overrides-history",
        "expected": "none",
        "prompt": (
            "只使用当前消息，不要查阅任何历史资料："
            "本次实验模型叫 test-model-x，维度为 777。"
            "本次实验的模型和维度是什么？"
        ),
    },
    {
        "name": "explicit-private-opt-out",
        "expected": "none",
        "prompt": (
            "不要访问我的私人知识库或历史资料。"
            "用两句话解释 pgvector 是什么。"
        ),
    },
    {
        "name": "public-model-knowledge",
        "expected": "none",
        "prompt": (
            "Qwen embedding 模型通常可以用来做什么？"
            "只回答一般技术原理。"
        ),
    },
    {
        "name": "translation-with-private-term",
        "expected": "none",
        "prompt": (
            "把“My private knowledge base uses vector search”"
            "翻译成中文。"
        ),
    },
]


def extract_answer(payload: dict[str, Any]) -> str:
    payloads = (
        payload.get("result", {})
        .get("payloads", [])
    )

    if not payloads:
        return ""

    first = payloads[0]

    if not isinstance(first, dict):
        return ""

    return str(first.get("text", ""))


def run_test(
    index: int,
    test: dict[str, Any],
) -> dict[str, Any]:
    session_key = (
        f"agent:main:rag-route-v2-"
        f"{int(time.time())}-{index}"
    )

    command = [
        "openclaw",
        "--no-color",
        "agent",
        "--agent",
        "main",
        "--session-key",
        session_key,
        "--message",
        str(test["prompt"]),
        "--thinking",
        "off",
        "--verbose",
        "on",
        "--timeout",
        "90",
        "--json",
    ]

    started = time.monotonic()

    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=110,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "name": test["name"],
            "expected": test["expected"],
            "passed": False,
            "reason": "timed out",
            "duration_seconds": round(
                time.monotonic() - started,
                2,
            ),
            "tools": [],
            "answer": "",
        }

    duration = round(
        time.monotonic() - started,
        2,
    )

    (AUDIT_DIR / f"{test['name']}.json").write_text(
        completed.stdout,
        encoding="utf-8",
    )

    (AUDIT_DIR / f"{test['name']}.stderr.txt").write_text(
        completed.stderr,
        encoding="utf-8",
    )

    if completed.returncode != 0:
        return {
            "name": test["name"],
            "expected": test["expected"],
            "passed": False,
            "reason": (
                f"OpenClaw exit code "
                f"{completed.returncode}"
            ),
            "duration_seconds": duration,
            "tools": [],
            "answer": "",
        }

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "name": test["name"],
            "expected": test["expected"],
            "passed": False,
            "reason": f"invalid JSON: {exc}",
            "duration_seconds": duration,
            "tools": [],
            "answer": "",
        }

    tool_summary = (
        payload.get("result", {})
        .get("meta", {})
        .get("toolSummary", {})
    )

    tools = tool_summary.get("tools", []) or []
    failures = tool_summary.get("failures", 0) or 0

    used_search = (
        "gordon-rag__rag_search" in tools
    )
    used_status = (
        "gordon-rag__rag_status" in tools
    )

    expected = str(test["expected"])

    if expected == "search":
        passed = (
            used_search
            and not used_status
            and failures == 0
        )
    elif expected == "status":
        passed = (
            used_status
            and not used_search
            and failures == 0
        )
    elif expected == "none":
        passed = (
            not used_search
            and not used_status
        )
    else:
        passed = False

    return {
        "name": test["name"],
        "expected": expected,
        "passed": passed,
        "reason": (
            "routing matched expectation"
            if passed
            else "routing did not match expectation"
        ),
        "duration_seconds": duration,
        "tools": tools,
        "tool_failures": failures,
        "answer": extract_answer(payload),
    }


def main() -> int:
    results: list[dict[str, Any]] = []

    try:
        for index, test in enumerate(
            TESTS,
            start=1,
        ):
            print(
                f"[{index}/{len(TESTS)}] "
                f"Running {test['name']}...",
                flush=True,
            )

            result = run_test(index, test)
            results.append(result)

            print(
                f"  passed={result['passed']} "
                f"expected={result['expected']} "
                f"tools={result['tools']} "
                f"duration={result['duration_seconds']}s",
                flush=True,
            )

    except KeyboardInterrupt:
        print(
            "\nEvaluation interrupted.",
            flush=True,
        )

        (AUDIT_DIR / "summary.json").write_text(
            json.dumps(
                results,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        return 130

    summary_path = AUDIT_DIR / "summary.json"

    summary_path.write_text(
        json.dumps(
            results,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print("===== ADVANCED ROUTING EVALUATION =====")

    passed_count = 0

    for result in results:
        status = (
            "PASS"
            if result["passed"]
            else "FAIL"
        )

        if result["passed"]:
            passed_count += 1

        print(
            f"{status:4} "
            f"{result['name']:<34} "
            f"expected={result['expected']:<6} "
            f"tools={result['tools']}"
        )

    print()
    print(
        f"Passed: {passed_count}/{len(results)}"
    )
    print(f"Summary: {summary_path}")

    return (
        0
        if passed_count == len(results)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
