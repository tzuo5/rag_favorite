from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

import rag_mcp_server  # noqa: E402
from retrieval_adapters import (  # noqa: E402
    CookingRetrievalAdapter,
    TopicRetrievalAdapter,
)
from retrieval_contracts import ALL_KNOWLEDGE_BASES  # noqa: E402


class EightCategoryContractTests(unittest.TestCase):
    def test_exactly_eight_knowledge_bases_are_exposed(self) -> None:
        self.assertEqual(len(ALL_KNOWLEDGE_BASES), 8)
        self.assertEqual(
            tuple(rag_mcp_server.SEARCHABLE_KNOWLEDGE_BASES),
            ALL_KNOWLEDGE_BASES,
        )
        self.assertIn("cooking", ALL_KNOWLEDGE_BASES)

    def test_cooking_uses_unified_search_contract(self) -> None:
        cooking_result = {
            "result_type": "recipe",
            "document_id": "cooking:123",
            "title": "红烧肉",
            "knowledge_base": "cooking",
            "source_relative_path": "2. Chinese Cuisine/红烧肉.md",
            "section": "步骤",
            "excerpt": "小火炖煮",
            "semantic_score": 0.82,
            "lexical_score": 0.91,
            "combined_score": 0.86,
            "reliable": True,
            "content_hash": None,
            "metadata": {"recipe_id": 123},
        }
        with patch.object(
            rag_mcp_server.TOPIC_ADAPTER,
            "search",
            return_value=[cooking_result],
        ) as unified:
            result = rag_mcp_server.rag_search("红烧肉", "cooking", 5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["results"], [cooking_result])
        self.assertFalse(result["cross_library"])
        self.assertFalse(result["no_reliable_match"])
        unified.assert_called_once_with("红烧肉", ["cooking"], 5)

    def test_explicit_topic_and_cooking_cross_library_calls_both(self) -> None:
        topic_result = {
            "result_type": "document",
            "document_id": "topic:1",
            "title": "文学.md",
            "knowledge_base": "literature-culture",
            "source_relative_path": "文学.md",
            "section": "chunk:0",
            "excerpt": "宴席",
            "semantic_score": 0.8,
            "lexical_score": None,
            "combined_score": 0.8,
            "reliable": True,
            "content_hash": "abc",
            "metadata": {},
        }
        cooking_result = {
            "result_type": "recipe",
            "document_id": "cooking:1",
            "title": "宴席菜单",
            "knowledge_base": "cooking",
            "source_relative_path": "宴席菜单.md",
            "section": "菜单",
            "excerpt": "菜品",
            "semantic_score": 0.7,
            "lexical_score": 0.5,
            "combined_score": 0.01,
            "reliable": True,
            "content_hash": None,
            "metadata": {"recipe_id": 1},
        }
        def unified_results(
            query: str, collections: list[str], limit: int
        ) -> list[dict[str, object]]:
            return [cooking_result] if collections == ["cooking"] else [topic_result]

        with patch.object(
            rag_mcp_server.TOPIC_ADAPTER,
            "search",
            side_effect=unified_results,
        ) as unified:
            result = rag_mcp_server.rag_search(
                "食物描写",
                "cooking",
                additional_knowledge_bases=["literature-culture"],
            )
        self.assertTrue(result["cross_library"])
        self.assertEqual(
            result["knowledge_bases"],
            ["cooking", "literature-culture"],
        )
        self.assertEqual(
            [item["knowledge_base"] for item in result["results"]],
            ["cooking", "literature-culture"],
        )
        self.assertEqual(unified.call_count, 2)

    def test_backend_failure_returns_safe_error_envelope(self) -> None:
        with patch.object(
            rag_mcp_server.TOPIC_ADAPTER,
            "search",
            side_effect=RuntimeError("/home/ubuntu/secret"),
        ):
            result = rag_mcp_server.rag_search("红烧肉", "cooking")
        rendered = json.dumps(result)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"]["code"],
            "RETRIEVAL_UNAVAILABLE",
        )
        self.assertNotIn("/home/ubuntu", rendered)


class AdapterContractTests(unittest.TestCase):
    def test_topic_adapter_preserves_required_fields_and_redacts_host_paths(
        self,
    ) -> None:
        payload = {
            "results": [
                {
                    "result_type": "document",
                    "document_id": "topic:7",
                    "title": "server.md",
                    "knowledge_base": "tech",
                    "source_relative_path": "notes/server.md",
                    "section": "chunk:0",
                    "excerpt": "配置位于 /home/ubuntu/private/config",
                    "semantic_score": 0.7,
                    "lexical_score": None,
                    "combined_score": 0.7,
                    "reliable": True,
                    "content_hash": "abc",
                    "metadata": {},
                }
            ]
        }

        def runner(arguments: list[str], *, timeout_seconds: int) -> str:
            self.assertIn("--json", arguments)
            self.assertGreater(timeout_seconds, 0)
            return json.dumps(payload, ensure_ascii=False)

        adapter = TopicRetrievalAdapter(
            runner,
            search_timeout_seconds=10,
            status_timeout_seconds=10,
        )
        results = adapter.search("配置", ["tech"], 3)
        self.assertEqual(results[0]["knowledge_base"], "tech")
        self.assertNotIn("/home/ubuntu", results[0]["excerpt"])
        self.assertEqual(
            set(results[0]),
            {
                "result_type",
                "document_id",
                "title",
                "knowledge_base",
                "source_relative_path",
                "section",
                "excerpt",
                "semantic_score",
                "lexical_score",
                "combined_score",
                "reliable",
                "content_hash",
                "metadata",
            },
        )

    def test_cooking_adapter_maps_original_hybrid_result(self) -> None:
        payload = [
            {
                "recipe_id": 123,
                "title": "红烧肉",
                "cuisine": "chinese",
                "category": "main",
                "document_type": "recipe",
                "heading_path": "步骤",
                "excerpt": "小火炖煮",
                "source_path": "2. Chinese Cuisine/红烧肉.md",
                "cosine_similarity": 0.82,
                "lexical_similarity": 0.91,
                "hybrid_score": 0.86,
                "reliable": True,
            }
        ]

        def runner(arguments: list[str], *, timeout_seconds: int) -> str:
            self.assertEqual(arguments[0], "search")
            self.assertGreater(timeout_seconds, 0)
            return json.dumps(payload, ensure_ascii=False)

        adapter = CookingRetrievalAdapter(
            runner,
            search_timeout_seconds=10,
            status_timeout_seconds=10,
        )
        result = adapter.search("红烧肉", ["cooking"], 5)[0]
        self.assertEqual(result["document_id"], "cooking:123")
        self.assertEqual(result["semantic_score"], 0.82)
        self.assertEqual(result["lexical_score"], 0.91)
        self.assertEqual(result["combined_score"], 0.86)
        self.assertEqual(result["metadata"]["recipe_id"], 123)

    def test_adapter_rejects_absolute_source_path(self) -> None:
        payload = [
            {
                "recipe_id": 1,
                "source_path": "/home/ubuntu/知识库/Cooking/a.md",
                "reliable": True,
            }
        ]

        adapter = CookingRetrievalAdapter(
            lambda arguments, *, timeout_seconds: json.dumps(payload),
            search_timeout_seconds=10,
            status_timeout_seconds=10,
        )
        with self.assertRaises(RuntimeError):
            adapter.search("a", ["cooking"], 3)


if __name__ == "__main__":
    unittest.main()
