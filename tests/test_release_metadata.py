from __future__ import annotations

import re
import tomllib
from pathlib import Path

import rag_favorite

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^]]*]\(([^)]+)\)")


def test_release_version_is_consistent() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]

    assert version == "1.2.0"
    assert rag_favorite.__version__ == version
    assert f"## [{version}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_readme_local_links_exist() -> None:
    missing: list[str] = []
    for readme in (ROOT / "README.md", ROOT / "README_ZH.md"):
        text = readme.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if not (readme.parent / target).resolve().exists():
                missing.append(f"{readme.name}: {raw_target}")

    assert not missing, "Missing local README links:\n" + "\n".join(missing)


def test_release_workflows_and_community_files_exist() -> None:
    expected = (
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        ".github/dependabot.yml",
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
    )

    assert all((ROOT / path).is_file() for path in expected)


def test_macos_release_metadata_tracks_product_version() -> None:
    version = rag_favorite.__version__
    release_workflow = (ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    ci_workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    macos_guide = (ROOT / "docs/macos.md").read_text(encoding="utf-8")

    expected_wheel = f"rag_favorite-{version}-py3-none-any.whl"
    expected_archive = f"rag-favorite-{version}-macos-"
    assert expected_wheel in release_workflow
    assert expected_wheel in ci_workflow
    assert expected_archive in release_workflow
    assert expected_archive in ci_workflow
    assert expected_archive in macos_guide
