from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


AUDIT_DIR = (
    Path.home()
    / "openclaw-audit"
    / "phase4-routing-eval"
)

AUDIT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


TESTS: list[dict[str, Any]] = [
    {
        "name": "private-server-project",
        "expect_rag": True,
        "prompt": (
            "我以前记录的个人 AI 服务器使用什么向量数据库、"
            "embedding 服务和 embedding 模型？"
            "请根据我保存的私人项目文档回答。"
        ),
    },
    {
        "name": "private-investment-note",
        "expect_rag": True,
        "prompt": (
            "根据我的私人投资笔记，我的长期投资主要关注什么？"
            "不要根据一般投资建议猜测。"
        ),
    },
    {
        "name": "private-cpp-note",
        "expect_rag": True,
        "prompt": (
            "我的私人 C++ 学习笔记中记录了什么内容？"
            "请根据保存的笔记回答。"
        ),
    },
    {
        "name": "general-knowledge",
        "expect_rag": False,
        "prompt": "什么是向量数据库？用两句话解释。",
    },
    {
        "name": "translation",
        "expect_rag": False,
        "prompt": "把“向量数据库”翻译成英文。",
    },
    {
        "name": "current-context",
        "expect_rag": False,
        "prompt": (
            "只根据这条消息回答："
            "我的测试编号是 847291。"
            "我的测试编号是多少？"
        ),
    },
]


def extract_meta(payload: dict[str, Any]) -> dict[str, Any]:
    return (
        payload.get("result", {})
        .get("meta", {})
    )


def get_visible_answer(payload: dict[str, Any]) -> str:
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
    timestamp = int(time.time())

    session_key = (
        f"agent:main:rag-route-"
        f"{timestamp}-{index}"
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
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - started, 2)

        timeout_stdout = exc.stdout or ""
        timeout_stderr = exc.stderr or ""

        if isinstance(timeout_stdout, bytes):
            timeout_stdout = timeout_stdout.decode(
                "utf-8",
                errors="replace",
            )

        if isinstance(timeout_stderr, bytes):
            timeout_stderr = timeout_stderr.decode(
                "utf-8",
                errors="replace",
            )

        (AUDIT_DIR / f"{test['name']}.stdout.txt").write_text(
            timeout_stdout,
            encoding="utf-8",
        )

        (AUDIT_DIR / f"{test['name']}.stderr.txt").write_text(
            timeout_stderr,
            encoding="utf-8",
        )

        return {
            "name": test["name"],
            "expected_rag": test["expect_rag"],
            "passed": False,
            "reason": "OpenClaw test timed out",
            "duration_seconds": duration,
            "tools": [],
            "tool_failures": None,
            "answer": "",
        }

    duration = round(time.monotonic() - started, 2)

    stdout_path = AUDIT_DIR / f"{test['name']}.json"
    stderr_path = AUDIT_DIR / f"{test['name']}.stderr.txt"

    stdout_path.write_text(
        completed.stdout,
        encoding="utf-8",
    )

    stderr_path.write_text(
        completed.stderr,
        encoding="utf-8",
    )

    if completed.returncode != 0:
        return {
            "name": test["name"],
            "expected_rag": test["expect_rag"],
            "passed": False,
            "reason": (
                f"OpenClaw exited with "
                f"code {completed.returncode}"
            ),
            "duration_seconds": duration,
            "tools": [],
            "tool_failures": None,
            "answer": "",
        }

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "name": test["name"],
            "expected_rag": test["expect_rag"],
            "passed": False,
            "reason": f"Invalid JSON output: {exc}",
            "duration_seconds": duration,
            "tools": [],
            "tool_failures": None,
            "answer": "",
        }

    meta = extract_meta(payload)
    tool_summary = meta.get("toolSummary", {})

    tools = tool_summary.get("tools", []) or []
    failures = tool_summary.get("failures", 0) or 0

    used_rag_search = (
        "rag-favorite__rag_search" in tools
    )

    used_rag_status = (
        "rag-favorite__rag_status" in tools
    )

    if bool(test["expect_rag"]):
        passed = (
            used_rag_search
            and not used_rag_status
            and failures == 0
        )

        if passed:
            reason = "rag_search called correctly"
        else:
            reason = (
                "Expected rag_search exactly once "
                "without rag_status"
            )
    else:
        passed = (
            not used_rag_search
            and not used_rag_status
        )

        if passed:
            reason = "Private RAG correctly skipped"
        else:
            reason = "Private RAG called unnecessarily"

    return {
        "name": test["name"],
        "expected_rag": test["expect_rag"],
        "passed": passed,
        "reason": reason,
        "duration_seconds": duration,
        "tools": tools,
        "tool_failures": failures,
        "answer": get_visible_answer(payload),
    }


def write_summary(
    results: list[dict[str, Any]],
) -> Path:
    summary_path = AUDIT_DIR / "summary.json"

    summary_path.write_text(
        json.dumps(
            results,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return summary_path


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
                f"duration={result['duration_seconds']}s "
                f"tools={result['tools']}",
                flush=True,
            )

            if not result["passed"]:
                print(
                    f"  reason={result['reason']}",
                    flush=True,
                )

    except KeyboardInterrupt:
        print(
            "\nEvaluation interrupted by user.",
            flush=True,
        )

        summary_path = write_summary(results)

        print(
            f"Partial summary written to: "
            f"{summary_path}",
            flush=True,
        )

        return 130

    summary_path = write_summary(results)

    print()
    print("===== ROUTING EVALUATION =====")

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
            f"{status:4}  "
            f"{result['name']:<26} "
            f"{result['duration_seconds']:>6}s  "
            f"tools={result['tools']}"
        )

    print()
    print(
        f"Passed: {passed_count}/{len(results)}"
    )
    print(f"Summary: {summary_path}")
    print(f"Audit directory: {AUDIT_DIR}")

    if passed_count != len(results):
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
