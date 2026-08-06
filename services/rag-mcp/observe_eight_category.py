from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SESSION_DIR = Path("/home/ubuntu/.openclaw/agents/main/sessions")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report metadata-only eight-category RAG usage. "
            "Queries and retrieved content are never printed."
        )
    )
    parser.add_argument("--after", required=True, type=parse_timestamp)
    parser.add_argument("--before", type=parse_timestamp)
    return parser


def _message(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("message")
    return value if isinstance(value, dict) else {}


def report(after: datetime, before: datetime | None) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    calls: dict[str, dict[str, Any]] = {}

    for path in SESSION_DIR.glob("*.jsonl"):
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    timestamp = parse_timestamp(str(item["timestamp"]))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if timestamp < after or (before is not None and timestamp > before):
                    continue

                message = _message(item)
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "toolCall":
                        tool_name = str(part.get("name") or "")
                        arguments = part.get("arguments")
                        arguments = arguments if isinstance(arguments, dict) else {}
                        call_id = str(part.get("id") or "")
                        calls[call_id] = {
                            "tool": tool_name,
                            "arguments": arguments,
                        }
                        _count_call(counts, tool_name, arguments)
                    elif message.get("role") == "toolResult":
                        _count_result(counts, message, calls)

    return {
        "window": {
            "after": after.isoformat(),
            "before": before.isoformat() if before is not None else None,
        },
        "metrics": dict(sorted(counts.items())),
        "privacy": {
            "queries_printed": False,
            "retrieved_content_printed": False,
            "manual_wrong_category_review_required": True,
        },
    }


def _count_call(
    counts: Counter[str],
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    counts["all_tool_calls"] += 1
    if tool_name == "gordon-rag__rag_search":
        counts["unified_search_calls"] += 1
        primary = arguments.get("knowledge_base")
        extras = arguments.get("additional_knowledge_bases")
        extras = extras if isinstance(extras, list) else []
        if primary == "cooking" or "cooking" in extras:
            counts["unified_cooking_search_calls"] += 1
        if extras:
            counts["explicit_cross_library_calls"] += 1
    elif tool_name == "cooking-rag__cooking_recipe_search":
        counts["legacy_cooking_search_calls"] += 1
    elif tool_name == "cooking-rag__cooking_rag_status":
        counts["legacy_cooking_status_calls"] += 1
    elif tool_name == "cooking-rag__cooking_recipe_get":
        counts["cooking_full_get_calls"] += 1
    elif tool_name == "cooking-rag__cooking_recipe_create":
        counts["cooking_create_calls"] += 1


def _count_result(
    counts: Counter[str],
    message: dict[str, Any],
    calls: dict[str, dict[str, Any]],
) -> None:
    call_id = str(message.get("toolCallId") or "")
    call = calls.get(call_id)
    if call is None:
        return
    if bool(message.get("isError")):
        counts["tool_errors"] += 1
    details = message.get("details")
    details = details if isinstance(details, dict) else {}
    structured = details.get("structuredContent")
    if not isinstance(structured, dict):
        return
    if call["tool"] == "gordon-rag__rag_search":
        if structured.get("ok") is False:
            counts["unified_search_errors"] += 1
        if structured.get("no_reliable_match") is True:
            counts["unified_no_reliable_match"] += 1
    elif call["tool"] == "cooking-rag__cooking_recipe_get":
        if structured.get("found") is False:
            counts["cooking_full_get_not_found"] += 1
        failures = structured.get("image_delivery_failures")
        if isinstance(failures, list):
            counts["image_delivery_failures"] += len(failures)


def main() -> None:
    arguments = build_parser().parse_args()
    print(
        json.dumps(
            report(arguments.after, arguments.before),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
