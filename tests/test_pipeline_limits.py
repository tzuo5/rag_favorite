"""Storage accounting remains safe while publication removes owned copies."""

from pathlib import Path
from uuid import uuid4

from rag_favorite import video_pipeline
from rag_favorite.video_config import VideoConfig


def test_publication_cleanup_race_does_not_stop_other_consumers(tmp_path, monkeypatch):
    video = VideoConfig(root=tmp_path, credentials_file=tmp_path / "unused")
    asset = tmp_path / "assets" / ("a" * 32)
    asset.parent.mkdir()
    asset.write_bytes(b"owned task copy")

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, *_):
            return self

        def fetchall(self):
            return [(uuid4(), {"asset_id": asset.name})]

    monkeypatch.setattr(video_pipeline, "connect_database", lambda *_: Database())
    original_stat = Path.stat
    checks = 0

    def disappearing_stat(path, *args, **kwargs):
        nonlocal checks
        if path == asset and kwargs.get("follow_symlinks", True):
            checks += 1
            if checks == 2:
                raise FileNotFoundError("publication removed the copy after is_file")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", disappearing_stat)
    assert video_pipeline.temporary_bytes(object(), video) == 0
