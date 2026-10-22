from dataclasses import replace

import pytest

from rag_favorite.config import ConfigError
from rag_favorite.video_config import load_video_config


def test_asr_device_is_independent_of_visual_embedding_space(tmp_path):
    path = tmp_path / "video.toml"
    path.write_text('asr_device="cuda"\nasr_compute_type="float16"\n')
    config = load_video_config(path)
    assert config.asr_device == "cuda"
    assert config.asr_compute_type == "float16"
    assert (
        config.space_id
        == replace(config, asr_device="cpu", asr_compute_type="int8").space_id
    )


@pytest.mark.parametrize(
    "device,precision", [("cpu", "float16"), ("gpu", "int8"), ("cuda", "unknown")]
)
def test_invalid_asr_device_precision_rejected(tmp_path, device, precision):
    path = tmp_path / "video.toml"
    path.write_text(f'asr_device="{device}"\nasr_compute_type="{precision}"\n')
    with pytest.raises(ConfigError):
        load_video_config(path)


def test_existing_profiles_keep_cpu_int8(tmp_path):
    path = tmp_path / "video.toml"
    path.write_text('root="data"\n')
    config = load_video_config(path)
    assert (config.asr_device, config.asr_compute_type) == ("cpu", "int8")
