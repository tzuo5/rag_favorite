"""The video profile must load an explicit, bounded CCR parallelism setting."""

import pytest

from rag_favorite.config import ConfigError
from rag_favorite.video_config import load_video_config


def test_existing_profile_remains_serial_until_explicitly_enabled(tmp_path):
    path = tmp_path / "video.toml"
    path.write_text("")
    assert load_video_config(path).ccr_concurrency == 1
    path.write_text("ccr_concurrency = 6\n")
    assert load_video_config(path).ccr_concurrency == 6


@pytest.mark.parametrize("value", ["0", "17", "true", "6.5", '"6"'])
def test_invalid_concurrency_is_rejected(tmp_path, value):
    path = tmp_path / "video.toml"
    path.write_text(f"ccr_concurrency = {value}\n")
    with pytest.raises(ConfigError):
        load_video_config(path)
