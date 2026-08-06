from __future__ import annotations

from pathlib import Path

import pytest

from rag_favorite.config import ConfigError
from rag_favorite.rag import MAX_CHUNK_CHARACTERS, chunk_text, path_within_root


def test_mixed_language_chunking_is_bounded() -> None:
    chunks = chunk_text("中文段落。English paragraph. " * 500)
    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_CHUNK_CHARACTERS for chunk in chunks)


def test_collection_path_boundary(tmp_path: Path) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    inside = root / "note.md"
    inside.write_text("note", encoding="utf-8")
    assert path_within_root(inside, root) == inside.resolve()

    with pytest.raises(ConfigError):
        path_within_root(tmp_path / "outside.md", root)
