"""Safe filesystem bootstrap helpers used by first-run setup."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from .config import AppConfig, ConfigError, default_config, load_config
from .paths import AppPaths


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def config_template(paths: AppPaths) -> str:
    """Return the starter configuration without reading machine-local state."""

    rows = [
        "# rag-favorite local configuration",
        "",
        "[database]",
        'host = "127.0.0.1"',
        "port = 5432",
        'name = "ragdb"',
        'user = "rag_admin"',
        'credentials_file = "secrets.env"',
        "",
        "[embedding]",
        'url = "http://127.0.0.1:11434/api/embed"',
        'model = "qwen3-embedding:0.6b"',
        "dimensions = 1024",
        "timeout_seconds = 120",
        "batch_size = 4",
    ]
    for collection in default_config(paths).collections.values():
        rows.extend(
            [
                "",
                "[[collections]]",
                f"key = {_quote(collection.key)}",
                f"name = {_quote(collection.name)}",
                f"path = {_quote(str(paths.knowledge_dir / collection.key))}",
                f"template = {_quote(collection.template)}",
            ]
        )
    return "\n".join(rows) + "\n"


def initialize_config(path: Path, *, force: bool = False) -> AppConfig:
    """Create a private starter config and credentials file."""

    selected = path.expanduser().resolve()
    if selected.exists() and not force:
        raise ConfigError(f"Configuration already exists: {selected}")
    paths = AppPaths.discover()
    selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    selected.write_text(config_template(paths), encoding="utf-8")
    selected.chmod(0o600)
    secrets_file = selected.parent / "secrets.env"
    if not secrets_file.exists():
        secrets_file.write_text(
            "RAG_DATABASE_PASSWORD=" + secrets.token_urlsafe(36) + "\n",
            encoding="utf-8",
        )
        secrets_file.chmod(0o600)
    config = load_config(selected)
    ensure_directories(config)
    return config


def ensure_directories(config: AppConfig) -> tuple[Path, ...]:
    """Create the private runtime directory tree and collection roots."""

    private = (
        config.paths.config_dir,
        config.paths.data_dir,
        config.paths.cache_dir,
        config.paths.state_dir,
    )
    for directory in private:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    collection_paths: list[Path] = []
    for collection in config.collections.values():
        if collection.read_only:
            if not collection.path.is_dir():
                raise ConfigError(
                    f"Read-only collection directory does not exist: {collection.path}"
                )
        else:
            collection.path.mkdir(parents=True, exist_ok=True)
        collection_paths.append(collection.path)
    return (*private, *collection_paths)
