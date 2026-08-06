from __future__ import annotations

import json
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
OPENCLAW_CONFIG = REPOSITORY / "openclaw/openclaw.example.json"
SKILLS = REPOSITORY / "openclaw/skills"


class OpenClawEightCategoryRoutingTests(unittest.TestCase):
    def test_standard_registration_exposes_only_read_only_tools(self) -> None:
        config = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
        included = set(
            config["mcp"]["servers"]["rag-favorite"]["toolFilter"]["include"]
        )

        self.assertEqual({"rag_search", "rag_status"}, included)
        self.assertFalse(any("create" in tool or "write" in tool for tool in included))

    def test_legacy_cooking_tools_remain_available_for_rollback(self) -> None:
        config = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
        included = set(config["mcp"]["servers"]["cooking-rag"]["toolFilter"]["include"])

        self.assertIn("cooking_recipe_search", included)
        self.assertIn("cooking_rag_status", included)
        self.assertIn("cooking_recipe_create", included)
        self.assertIn("cooking_recipe_get", included)

    def test_packaged_skills_define_cooking_as_equal_unified_category(self) -> None:
        private_skill = (
            SKILLS / "rag-favorite-private-rag/SKILL.md"
        ).read_text(encoding="utf-8")
        cooking_skill = (
            SKILLS / "rag-favorite-cooking-rag/SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertIn("`cooking`", private_skill)
        self.assertIn("equal category", private_skill)
        self.assertIn("knowledge_base=cooking", cooking_skill)
        self.assertIn("default Agent routes", cooking_skill)


if __name__ == "__main__":
    unittest.main()
