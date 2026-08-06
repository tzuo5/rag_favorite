from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recipe_documents import (
    MAX_SECTION_CHARACTERS,
    classify_path,
    discover_markdown_files,
    load_recipe_document,
    resolve_recipe_images,
    stage_recipe_images,
    split_markdown_sections,
)


class RecipeDocumentTests(unittest.TestCase):
    def test_discovers_markdown_but_ignores_obsidian_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / "菜谱").mkdir()
            (vault / ".obsidian").mkdir()
            (vault / "菜谱" / "汤.md").write_text("# 汤", encoding="utf-8")
            (vault / ".obsidian" / "note.md").write_text("hidden", encoding="utf-8")
            self.assertEqual(
                [path.name for path in discover_markdown_files(vault)],
                ["汤.md"],
            )

    def test_loads_relative_source_and_keeps_images_as_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            path = vault / "1. Western Cuisine" / "2. Main Courses" / "羊排.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "![[羊排.png]]\n# 香草羊排\n## 食材\n- 500 g 羊排",
                encoding="utf-8",
            )
            document = load_recipe_document(vault, path)
            self.assertEqual(
                document.source_path,
                "1. Western Cuisine/2. Main Courses/羊排.md",
            )
            self.assertEqual(document.title, "羊排")
            self.assertEqual(document.cuisine, "Western Cuisine")
            self.assertEqual(document.category, "2. Main Courses")
            self.assertEqual(document.image_refs, ("羊排.png",))
            self.assertNotIn("![[", document.content)

    def test_menu_plans_are_not_misclassified_as_recipes(self) -> None:
        relative = Path("6. Fine Dining Planning/2026-01-13/timeline.md")
        self.assertEqual(
            classify_path(relative),
            ("fine-dining", "menu-planning", "menu_plan"),
        )

    def test_resolves_unique_obsidian_image_by_basename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            recipe = vault / "西餐" / "前菜" / "带子.md"
            image = vault / "西餐" / "screenshot" / "带子.png"
            recipe.parent.mkdir(parents=True)
            image.parent.mkdir(parents=True)
            recipe.write_text("![[带子.png]]", encoding="utf-8")
            image.write_bytes(b"image")

            paths, missing = resolve_recipe_images(
                vault,
                "西餐/前菜/带子.md",
                ["带子.png"],
            )

            self.assertEqual(paths, (str(image.resolve()),))
            self.assertEqual(missing, ())

    def test_rejects_traversal_and_ambiguous_image_refs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            recipe = vault / "菜谱" / "带子.md"
            recipe.parent.mkdir(parents=True)
            recipe.write_text("菜谱", encoding="utf-8")
            for folder in (vault / "a", vault / "b"):
                folder.mkdir()
                (folder / "重复.png").write_bytes(b"image")

            paths, missing = resolve_recipe_images(
                vault,
                "菜谱/带子.md",
                ["../secret.png", "重复.png", "notes.txt"],
            )

            self.assertEqual(paths, ())
            self.assertEqual(missing, ("../secret.png", "重复.png", "notes.txt"))

    def test_stages_images_in_managed_outbound_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "vault" / "带子.png"
            outbound = root / "media" / "outbound" / "cooking-rag"
            source.parent.mkdir()
            source.write_bytes(b"recipe-image")

            paths, failures = stage_recipe_images([str(source)], outbound)

            self.assertEqual(failures, ())
            self.assertEqual(len(paths), 1)
            staged = Path(paths[0])
            self.assertEqual(staged.parent, outbound.resolve())
            self.assertEqual(staged.read_bytes(), b"recipe-image")
            self.assertEqual(staged.stat().st_mode & 0o777, 0o600)

    def test_staging_reports_missing_or_unsupported_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsupported = root / "notes.txt"
            unsupported.write_text("not an image", encoding="utf-8")
            missing = root / "missing.png"

            paths, failures = stage_recipe_images(
                [str(unsupported), str(missing)],
                root / "outbound",
            )

            self.assertEqual(paths, ())
            self.assertEqual(failures, (str(unsupported), str(missing)))

    def test_sections_are_heading_aware_and_bounded(self) -> None:
        content = "# 菜\n## 食材\n" + ("土豆 100g。" * 300)
        sections = split_markdown_sections("菜", content)
        self.assertGreater(len(sections), 1)
        self.assertTrue(all(len(item.content) <= MAX_SECTION_CHARACTERS for item in sections))
        self.assertTrue(all("食材" in item.heading_path for item in sections))


if __name__ == "__main__":
    unittest.main()
