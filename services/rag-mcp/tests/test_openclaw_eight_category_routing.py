from __future__ import annotations

import json
import unittest
from pathlib import Path

OPENCLAW_CONFIG = Path("/home/ubuntu/.openclaw/openclaw.json")
WORKSPACE = Path("/home/ubuntu/.openclaw/workspace")


class OpenClawEightCategoryRoutingTests(unittest.TestCase):
    def test_main_agent_uses_unified_search_but_keeps_recipe_create_get(
        self,
    ) -> None:
        config = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
        main = next(
            agent for agent in config["agents"]["list"] if agent["id"] == "main"
        )
        allowed = set(main["tools"]["sandbox"]["tools"]["alsoAllow"])
        self.assertIn("gordon-rag__rag_search", allowed)
        self.assertIn("cooking-rag__cooking_recipe_create", allowed)
        self.assertIn("cooking-rag__cooking_recipe_get", allowed)
        self.assertNotIn("cooking-rag__cooking_recipe_search", allowed)
        self.assertNotIn("cooking-rag__cooking_rag_status", allowed)

    def test_legacy_cooking_tools_remain_available_for_rollback(self) -> None:
        config = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
        included = set(config["mcp"]["servers"]["cooking-rag"]["toolFilter"]["include"])
        self.assertIn("cooking_recipe_search", included)
        self.assertIn("cooking_rag_status", included)
        self.assertIn("cooking_recipe_create", included)
        self.assertIn("cooking_recipe_get", included)

    def test_skills_define_cooking_as_equal_unified_category(self) -> None:
        private_skill = (WORKSPACE / "skills/gordon-private-rag/SKILL.md").read_text(
            encoding="utf-8"
        )
        cooking_skill = (WORKSPACE / "skills/gordon-cooking-rag/SKILL.md").read_text(
            encoding="utf-8"
        )
        agents = (WORKSPACE / "AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("`cooking`", private_skill)
        self.assertIn("eight-category", private_skill)
        self.assertIn("knowledge_base=cooking", cooking_skill)
        self.assertIn("eight equal knowledge categories", agents)
        self.assertNotIn(
            "Use `cooking-rag__cooking_recipe_search` first",
            agents,
        )


if __name__ == "__main__":
    unittest.main()
