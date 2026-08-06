from __future__ import annotations

import unittest

from recipe_writer import (
    RecipeCreateError,
    build_relative_path,
    canonical_recipe_markdown,
    normalize_title,
    recipe_request_hash,
)


class RecipeWriterValidationTests(unittest.TestCase):
    def test_builds_only_fixed_recipe_paths(self) -> None:
        self.assertEqual(
            build_relative_path("红烧肉", "chinese", "main").as_posix(),
            "2. Chinese（中餐）/主菜/红烧肉.md",
        )
        self.assertEqual(
            build_relative_path("莫吉托", "alcohol", "drink").as_posix(),
            "4. Alcohol（酒精）/莫吉托.md",
        )

    def test_rejects_path_like_titles(self) -> None:
        for value in ("../红烧肉", ".hidden", "a/b", "a\\b", ""):
            with self.subTest(value=value), self.assertRaises(RecipeCreateError):
                normalize_title(value)

    def test_canonical_markdown_replaces_model_h1(self) -> None:
        result = canonical_recipe_markdown(
            "红烧肉",
            "# 另一个标题\n\n## 食材\n- 五花肉 500g\n\n## 步骤\n1. 煸炒",
        )
        self.assertTrue(result.startswith("# 红烧肉\n\n## 食材"))
        self.assertNotIn("另一个标题", result)

    def test_request_hash_is_deterministic_and_content_bound(self) -> None:
        first = recipe_request_hash("红烧肉", "chinese", "main", "content")
        second = recipe_request_hash("红烧肉", "chinese", "main", "content")
        changed = recipe_request_hash("红烧肉", "chinese", "main", "changed")
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)


if __name__ == "__main__":
    unittest.main()
