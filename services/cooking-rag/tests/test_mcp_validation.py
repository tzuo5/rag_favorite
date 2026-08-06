from __future__ import annotations

import unittest

from validators import normalize_query


class McpValidationTests(unittest.TestCase):
    def test_query_rejects_empty_and_null(self) -> None:
        with self.assertRaises(ValueError):
            normalize_query("  ")
        with self.assertRaises(ValueError):
            normalize_query("bad\x00query")

    def test_query_normalizes_whitespace(self) -> None:
        self.assertEqual(normalize_query("  羊排怎么做  "), "羊排怎么做")


if __name__ == "__main__":
    unittest.main()
