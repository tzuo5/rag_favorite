from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


AUDIT_DIR = (
    Path.home()
    / "openclaw-audit"
    / "phase4-routing-eval-v3"
)

AUDIT_DIR.mkdir(parents=True, exist_ok=True)


TESTS: list[dict[str, Any]] = [
    {
        "name": "current-runtime-model",
        "expected_tools": {
            "rag-favorite__rag_status",
        },
        "prompt": (
            "检查我的 RAG 服务当前实际使用的 embedding 模型"
            "和向量维度。"
        ),
    },
    {
        "name": "documented-server-model",
        "expected_tools": {
            "rag-favorite__rag_search",
        },
        "prompt": (
            "我之前保存的服务器项目文档里记录了哪个 "
            "embedding 模型和向量维度？"
        ),
    },
    {
        "name": "investment-single-source",
        "expected_tools": {
            "rag-favorite__rag_search",
        },
        "prompt": (
            "我之前记录的长期投资重点是什么？"
        ),
    },
    {
        "name": "cpp-single-source",
        "expected_tools": {
            "rag-favorite__rag_search",
        },
        "prompt": (
            "我之前学 C++ 时主要记录了哪些算法主题？"
        ),
    },
]


def run_test(
    index: int,
    test: dict[str, Any],
) -> dict[str, Any]:
    session_key = (
        f"agent:main:rag-route-v3-"
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
            "passed": False,
            "reason": "timeout",
            "tools": [],
        }

    duration = round(time.monotonic() - started, 2)

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
            "passed": False,
            "reason": (
                f"OpenClaw exit code "
                f"{completed.returncode}"
            ),
            "duration_seconds": duration,
            "tools": [],
        }

    payload = json.loads(completed.stdout)

    summary = (
        payload.get("result", {})
        .get("meta", {})
        .get("toolSummary", {})
    )

    tools = summary.get("tools", []) or []
    tool_set = set(tools)
    failures = summary.get("failures", 0) or 0

    expected_tools = set(test["expected_tools"])

    passed = (
        tool_set == expected_tools
        and failures == 0
    )

    payloads = (
        payload.get("result", {})
        .get("payloads", [])
    )

    answer = ""

    if payloads and isinstance(payloads[0], dict):
        answer = str(payloads[0].get("text", ""))

    return {
        "name": test["name"],
        "passed": passed,
        "reason": (
            "exact tool set matched"
            if passed
            else "unexpected or duplicate tools used"
        ),
        "expected_tools": sorted(expected_tools),
        "tools": tools,
        "tool_failures": failures,
        "duration_seconds": duration,
        "answer": answer,
    }


def main() -> int:
    results: list[dict[str, Any]] = []

    for index, test in enumerate(TESTS, start=1):
        print(
            f"[{index}/{len(TESTS)}] "
            f"Running {test['name']}...",
            flush=True,
        )

        result = run_test(index, test)
        results.append(result)

        print(
            f"  passed={result['passed']} "
            f"tools={result['tools']}",
            flush=True,
        )

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
    print("===== STRICT ROUTING EVALUATION =====")

    passed_count = 0

    for result in results:
        status = "PASS" if result["passed"] else "FAIL"

        if result["passed"]:
            passed_count += 1

        print(
            f"{status:4} "
            f"{result['name']:<28} "
            f"expected={result['expected_tools']} "
            f"actual={result['tools']}"
        )

    print()
    print(f"Passed: {passed_count}/{len(results)}")
    print(f"Summary: {summary_path}")

    return 0 if passed_count == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
