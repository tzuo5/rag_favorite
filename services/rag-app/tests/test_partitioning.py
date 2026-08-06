from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

import rag  # noqa: E402


class KnowledgeBasePartitioningTests(unittest.TestCase):
    def test_default_roots_use_portable_product_data_directory(self) -> None:
        expected = Path.home() / ".local/share/rag-favorite/knowledge"
        self.assertEqual(rag.KNOWLEDGE_BASES["tech"].root, expected / "tech")
        self.assertEqual(
            rag.KNOWLEDGE_BASES["cooking"].root,
            expected / "cooking",
        )

    def test_path_within_root_accepts_selected_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "example.md"
            path.write_text("example", encoding="utf-8")
            self.assertEqual(rag.path_within_root(path, root), path.resolve())

    def test_path_within_root_rejects_directory_traversal(self) -> None:
        with self.assertRaises(RuntimeError):
            rag.path_within_root(Path("/tmp/outside.md"), Path("/tmp/inside"))

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
        self.assertEqual(arguments.collections, ["tech", "finance"])


if __name__ == "__main__":
    unittest.main()
