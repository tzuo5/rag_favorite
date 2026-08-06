from __future__ import annotations

import sys
import unittest
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

import rag  # noqa: E402


class KnowledgeBasePartitioningTests(unittest.TestCase):
    def test_configured_topic_roots_share_the_knowledge_directory(self) -> None:
        self.assertEqual(
            rag.KNOWLEDGE_BASES["thought-politics"].root,
            Path("/home/ubuntu/知识库/Thought and Politics"),
        )
        self.assertEqual(
            rag.KNOWLEDGE_BASES["tech"].root,
            Path("/home/ubuntu/知识库/Technology"),
        )
        self.assertEqual(
            rag.KNOWLEDGE_BASES["social-conduct"].root,
            Path("/home/ubuntu/知识库/Chinese Social Relations and Conduct"),
        )

    def test_path_within_root_accepts_selected_root(self) -> None:
        result = rag.path_within_root(
            Path("/home/ubuntu/知识库/Technology/1. Programming and Software/example.md"),
            Path("/home/ubuntu/知识库/Technology"),
        )
        self.assertEqual(
            result,
            Path("/home/ubuntu/知识库/Technology/1. Programming and Software/example.md"),
        )

    def test_path_within_root_rejects_directory_traversal(self) -> None:
        with self.assertRaises(RuntimeError):
            rag.path_within_root(
                Path("/home/ubuntu/知识库/Technology/../Thought and Politics/file.md"),
                Path("/home/ubuntu/知识库/Technology"),
            )

    def test_search_requires_an_explicit_knowledge_base(self) -> None:
        parser = rag.create_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["search", "test query"])

    def test_cross_library_search_requires_repeated_explicit_flags(self) -> None:
        parser = rag.create_parser()
        arguments = parser.parse_args(
            [
                "search",
                "test query",
                "--knowledge-base",
                "tech",
                "--knowledge-base",
                "finance",
            ]
        )
        self.assertEqual(arguments.knowledge_base, ["tech", "finance"])


if __name__ == "__main__":
    unittest.main()
