from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

import rag_mcp_server  # noqa: E402


class TopicToolTests(unittest.TestCase):
    def test_social_conduct_collection_is_searchable(self) -> None:
        self.assertIn(
            "social-conduct",
            rag_mcp_server.SEARCHABLE_KNOWLEDGE_BASES,
        )

    def test_search_passes_one_selected_collection_to_cli(self) -> None:
        with patch.object(
            rag_mcp_server.TOPIC_ADAPTER,
            "search",
            return_value=[],
        ) as search:
            with patch.object(rag_mcp_server, "GOVERNED_MEMORY_RETRIEVER", None):
                result = rag_mcp_server.rag_search(
                    "PostgreSQL",
                    "tech",
                    3,
                )

        self.assertEqual(result["knowledge_bases"], ["tech"])
        self.assertFalse(result["cross_library"])
        search.assert_called_once_with("PostgreSQL", ["tech"], 3)

    def test_cross_library_search_is_explicit_and_labeled(self) -> None:
        with patch.object(
            rag_mcp_server.TOPIC_ADAPTER,
            "search",
            return_value=[],
        ):
            with patch.object(rag_mcp_server, "GOVERNED_MEMORY_RETRIEVER", None):
                result = rag_mcp_server.rag_search(
                    "SpaceX",
                    "tech",
                    additional_knowledge_bases=["finance"],
                )

        self.assertEqual(result["knowledge_bases"], ["tech", "finance"])
        self.assertTrue(result["cross_library"])

    def test_unknown_collection_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            rag_mcp_server.rag_search("query", "unknown")

    def test_governed_memory_is_not_queried_by_default(self) -> None:
        fake_retriever = unittest.mock.Mock()
        with patch.object(
            rag_mcp_server.TOPIC_ADAPTER,
            "search",
            return_value=[],
        ):
            with patch.object(
                rag_mcp_server,
                "GOVERNED_MEMORY_RETRIEVER",
                fake_retriever,
            ):
                result = rag_mcp_server.rag_search("query", "tech")

        fake_retriever.search.assert_not_called()
        self.assertFalse(result["governed_memory_requested"])


if __name__ == "__main__":
    unittest.main()
