from __future__ import annotations

import json
from pathlib import Path

from embedding import OllamaEmbeddingClient
from repository import RecipeRetriever


def main() -> int:
    cases = json.loads(
        (Path(__file__).parent / "eval_cases.json").read_text(encoding="utf-8")
    )
    retriever = RecipeRetriever(OllamaEmbeddingClient())
    passed = 0

    for case in cases:
        results = retriever.search(case["query"], 5)
        reliable = [item for item in results if item["reliable"]]
        titles = [item["title"] for item in reliable]
        if case.get("expect_no_match"):
            ok = not reliable
        else:
            ok = case["expected_title"] in titles
        passed += int(ok)
        print(
            json.dumps(
                {
                    "ok": ok,
                    "query": case["query"],
                    "reliable_titles": titles,
                },
                ensure_ascii=False,
            )
        )

    print(json.dumps({"passed": passed, "total": len(cases)}))
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
