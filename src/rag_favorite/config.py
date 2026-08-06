"""Validated product configuration shared by CLI, RAG, MCP and ingestion."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .paths import AppPaths

COLLECTION_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigError(RuntimeError):
    """A safe, user-facing configuration error."""


@dataclass(frozen=True, slots=True)
class CollectionConfig:
    key: str
    name: str
    path: Path
    template: str = "documents"
    read_only: bool = False

    @property
    def root(self) -> Path:
        """Compatibility alias used by the pre-package RAG entrypoint."""

        return self.path


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    name: str = "ragdb"
    user: str = "rag_admin"
    credentials_file: Path | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    url: str = "http://127.0.0.1:11434/api/embed"
    model: str = "qwen3-embedding:0.6b"
    dimensions: int = 1024
    timeout_seconds: int = 120
    batch_size: int = 4


@dataclass(frozen=True, slots=True)
class AppConfig:
    source: Path
    paths: AppPaths
    database: DatabaseConfig
    embedding: EmbeddingConfig
    collections: Mapping[str, CollectionConfig]

    def collection(self, key: str) -> CollectionConfig:
        try:
            return self.collections[key]
        except KeyError as exc:
            choices = ", ".join(self.collections) or "none configured"
            raise ConfigError(
                f"Unknown collection {key!r}; configured collections: {choices}"
            ) from exc


def _expand_path(value: object, *, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("Configured path must be a non-empty string.")
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _default_collections(paths: AppPaths) -> dict[str, CollectionConfig]:
    templates = (
        ("thought-politics", "Thought and Politics", "documents"),
        ("tech", "Technology", "documents"),
        ("finance", "Finance and Investment", "documents"),
        ("career", "Career Development", "documents"),
        ("social-conduct", "Social Relations and Conduct", "documents"),
        ("literature-culture", "Literature and Culture", "documents"),
        ("general", "General", "documents"),
        ("cooking", "Cooking", "cooking"),
    )
    return {
        key: CollectionConfig(
            key=key,
            name=name,
            path=paths.knowledge_dir / key,
            template=template,
        )
        for key, name, template in templates
    }


def default_config(paths: AppPaths | None = None) -> AppConfig:
    resolved_paths = paths or AppPaths.discover()
    return AppConfig(
        source=resolved_paths.config_file,
        paths=resolved_paths,
        database=DatabaseConfig(credentials_file=resolved_paths.secrets_file),
        embedding=EmbeddingConfig(),
        collections=_default_collections(resolved_paths),
    )


def _table(value: object, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table.")
    return value


def load_config(path: Path | str | None = None) -> AppConfig:
    selected = (
        Path(
            path
            or os.environ.get("RAG_FAVORITE_CONFIG")
            or AppPaths.discover().config_file
        )
        .expanduser()
        .resolve()
    )
    paths = AppPaths.discover()
    if not selected.exists():
        defaults = default_config(paths)
        return AppConfig(
            source=selected,
            paths=paths,
            database=DatabaseConfig(credentials_file=selected.parent / "secrets.env"),
            embedding=defaults.embedding,
            collections=defaults.collections,
        )

    try:
        raw = tomllib.loads(selected.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Unable to load configuration: {selected}") from exc

    database_raw = _table(raw.get("database"), "database")
    embedding_raw = _table(raw.get("embedding"), "embedding")
    base = selected.parent
    credentials_value = database_raw.get("credentials_file", "secrets.env")
    database = DatabaseConfig(
        host=str(database_raw.get("host", "127.0.0.1")),
        port=int(database_raw.get("port", 5432)),
        name=str(database_raw.get("name", "ragdb")),
        user=str(database_raw.get("user", "rag_admin")),
        credentials_file=_expand_path(credentials_value, base=base),
    )
    embedding_defaults = EmbeddingConfig()
    embedding = EmbeddingConfig(
        url=str(embedding_raw.get("url", embedding_defaults.url)),
        model=str(embedding_raw.get("model", embedding_defaults.model)),
        dimensions=int(embedding_raw.get("dimensions", 1024)),
        timeout_seconds=int(embedding_raw.get("timeout_seconds", 120)),
        batch_size=int(embedding_raw.get("batch_size", 4)),
    )

    collection_rows = raw.get("collections")
    if collection_rows is None:
        collections = _default_collections(paths)
    elif not isinstance(collection_rows, list):
        raise ConfigError("[[collections]] must be an array of TOML tables.")
    else:
        collections: dict[str, CollectionConfig] = {}
        for row in collection_rows:
            if not isinstance(row, dict):
                raise ConfigError("Every collection must be a TOML table.")
            key = str(row.get("key", "")).strip()
            if not COLLECTION_KEY.fullmatch(key):
                raise ConfigError(f"Invalid collection key: {key!r}")
            if key in collections:
                raise ConfigError(f"Duplicate collection key: {key}")
            collections[key] = CollectionConfig(
                key=key,
                name=str(row.get("name") or key),
                path=_expand_path(row.get("path"), base=base),
                template=str(row.get("template") or "documents"),
                read_only=bool(row.get("read_only", False)),
            )

    config = AppConfig(
        source=selected,
        paths=paths,
        database=database,
        embedding=embedding,
        collections=collections,
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    if config.database.host not in LOOPBACK_HOSTS:
        raise ConfigError("Database host must be loopback-only in local mode.")
    if not 1 <= config.database.port <= 65535:
        raise ConfigError("Database port must be between 1 and 65535.")
    parsed = urlparse(config.embedding.url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise ConfigError("Embedding URL must use HTTP on a loopback host.")
    if config.embedding.dimensions <= 0:
        raise ConfigError("Embedding dimensions must be positive.")
    if config.embedding.timeout_seconds <= 0 or config.embedding.batch_size <= 0:
        raise ConfigError("Embedding timeout and batch size must be positive.")
    if not config.collections:
        raise ConfigError("At least one collection must be configured.")


def read_env_file(path: Path | None) -> dict[str, str]:
    """Read a small dotenv-compatible credential file without interpolation."""

    if path is None or not path.is_file():
        return {}
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ConfigError(f"Credential file permissions are too broad: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ConfigError(f"Credential file owner is invalid: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def database_credentials(config: AppConfig) -> dict[str, str]:
    values = read_env_file(config.database.credentials_file)
    aliases = {
        "password": ("RAG_DATABASE_PASSWORD", "PGPASSWORD", "POSTGRES_PASSWORD"),
        "name": ("RAG_DATABASE_NAME", "PGDATABASE", "POSTGRES_DB"),
        "user": ("RAG_DATABASE_USER", "PGUSER", "POSTGRES_USER"),
    }

    def resolve(names: tuple[str, ...], fallback: str | None = None) -> str:
        for name in names:
            value = os.environ.get(name) or values.get(name)
            if value:
                return value
        if fallback:
            return fallback
        raise ConfigError(
            f"Database credentials are incomplete in {config.database.credentials_file}."
        )

    return {
        "host": config.database.host,
        "port": str(config.database.port),
        "name": resolve(aliases["name"], config.database.name),
        "user": resolve(aliases["user"], config.database.user),
        "password": resolve(aliases["password"]),
    }
