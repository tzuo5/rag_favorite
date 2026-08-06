"""Cross-platform application paths with XDG support."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "rag-favorite"


def _xdg_path(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else fallback


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    state_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def secrets_file(self) -> Path:
        return self.config_dir / "secrets.env"

    @property
    def knowledge_dir(self) -> Path:
        return self.data_dir / "knowledge"

    @classmethod
    def discover(cls) -> AppPaths:
        home = Path.home()
        return cls(
            config_dir=_xdg_path("XDG_CONFIG_HOME", home / ".config") / APP_NAME,
            data_dir=_xdg_path("XDG_DATA_HOME", home / ".local" / "share") / APP_NAME,
            cache_dir=_xdg_path("XDG_CACHE_HOME", home / ".cache") / APP_NAME,
            state_dir=_xdg_path("XDG_STATE_HOME", home / ".local" / "state") / APP_NAME,
        )
