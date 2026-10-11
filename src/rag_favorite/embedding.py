"""Validated local and explicitly configured cloud embedding providers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from .config import (
    DEFAULT_QUERY_INSTRUCTION,
    LOOPBACK_HOSTS,
    AppConfig,
    EmbeddingConfig,
    load_config,
)
from .model_activity import model_call

QUERY_INSTRUCTION = DEFAULT_QUERY_INSTRUCTION
MAX_RESPONSE_BYTES = 16_000_000


class EmbeddingError(RuntimeError):
    """Stable signal for unavailable or malformed embeddings."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class EmbeddingChargeUnknown(EmbeddingError):
    """A request may have been charged and must not be automatically replayed."""

    charge_unknown = True


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    def embed_document(self, text: str) -> tuple[float, ...]: ...

    def embed_documents(self, texts: list[str]) -> list[tuple[float, ...]]: ...

    def embed_query(self, text: str) -> tuple[float, ...]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaEmbeddingClient:
    def __init__(
        self,
        settings: EmbeddingConfig | None = None,
        *,
        opener: object | None = None,
    ) -> None:
        self.settings = settings or load_config().embedding
        self.model = self.settings.model
        self.dimensions = self.settings.dimensions
        self._opener = (
            opener
            if opener is not None
            else urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirect()
            )
        )
        self._urlopen = getattr(self._opener, "urlopen", None) or self._opener.open

    def embed_document(self, text: str) -> tuple[float, ...]:
        return self.embed_documents([text])[0]

    @model_call("Embedding", "documents")
    def embed_documents(self, texts: list[str]) -> list[tuple[float, ...]]:
        return [
            v
            for start in range(0, len(texts), self.settings.batch_size)
            for v in self._embed(texts[start : start + self.settings.batch_size])
        ]

    @model_call("Embedding", "query")
    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._embed([self.settings.query_instruction + text])[0]

    def inspect_model(self) -> dict[str, object]:
        request = urllib.request.Request(self.settings.url[:-5] + "tags")
        try:
            with self._urlopen(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise EmbeddingError("Model inventory response is too large.")
            result = json.loads(payload)
        except (OSError, TimeoutError, ValueError) as exc:
            raise EmbeddingError("Local model inventory is unavailable.") from exc
        models = result.get("models") if isinstance(result, dict) else None
        if not isinstance(models, list):
            raise EmbeddingError("Local model inventory is invalid.")
        selected = self.model if ":" in self.model else self.model + ":latest"
        for model in models:
            if not isinstance(model, dict):
                continue
            name = model.get("name") or model.get("model")
            if name not in {self.model, selected}:
                continue
            digest = model.get("digest")
            if not isinstance(digest, str):
                raise EmbeddingError("Local model digest is missing.")
            digest = digest.removeprefix("sha256:")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise EmbeddingError("Local model digest is invalid.")
            return {
                "model": self.model,
                "model_digest": digest,
                "size": model.get("size"),
            }
        raise EmbeddingError("Configured local embedding model is not installed.")

    def verify_model(self) -> None:
        if not self.settings.model_digest:
            raise EmbeddingError(
                "Local model_digest is not pinned. Run 'rag-favorite embedding inspect' "
                "and set [embedding].model_digest before indexing."
            )
        if self.inspect_model()["model_digest"] != self.settings.model_digest:
            raise EmbeddingError(
                "Local model digest differs from the pinned encoder; rebuild a new generation."
            )

    def _embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        if not texts:
            return []
        self.verify_model()
        request = urllib.request.Request(
            self.settings.url,
            data=json.dumps(
                {
                    "model": self.model,
                    "input": texts,
                    "truncate": False,
                    "dimensions": self.dimensions,
                    "options": {"num_gpu": self.settings.num_gpu},
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._urlopen(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise EmbeddingError("Embedding response is too large.")
            result = json.loads(payload)
        except EmbeddingError:
            raise
        except (
            OSError,
            TimeoutError,
            urllib.error.URLError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise EmbeddingError("Embedding service is unavailable.") from exc

        if not isinstance(result, dict):
            raise EmbeddingError("Embedding response is invalid.")
        returned_model = result.get("model")
        if returned_model not in {self.model, self.model + ":latest"}:
            raise EmbeddingError(
                "Embedding response model differs from the configured encoder."
            )
        values = result.get("embeddings")
        if not isinstance(values, list) or len(values) != len(texts):
            raise EmbeddingError("Embedding response count is invalid.")

        embeddings: list[tuple[float, ...]] = []
        for raw_vector in values:
            if not isinstance(raw_vector, list) or len(raw_vector) != self.dimensions:
                raise EmbeddingError("Embedding dimensions are invalid.")
            vector: list[float] = []
            for raw_value in raw_vector:
                if isinstance(raw_value, bool) or not isinstance(
                    raw_value, (int, float)
                ):
                    raise EmbeddingError("Embedding value is invalid.")
                value = float(raw_value)
                if not math.isfinite(value):
                    raise EmbeddingError("Embedding value is invalid.")
                vector.append(value)
            norm = math.sqrt(sum(v * v for v in vector))
            if not math.isfinite(norm) or norm == 0:
                raise EmbeddingError("Embedding vector has zero or invalid norm.")
            if self.settings.normalization == "l2":
                vector = [v / norm for v in vector]
            embeddings.append(tuple(vector))
        self.verify_model()
        return embeddings


@lru_cache(maxsize=32)
def _gguf_digest(path: str, signature: tuple[int, ...]) -> str:
    with Path(path).open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if _file_signature(Path(path)) != signature:
        raise EmbeddingError("Model file changed during verification.")
    return digest


def _file_signature(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class LMStudioEmbeddingClient(OllamaEmbeddingClient):
    """LM Studio HTTP embeddings, bound to a loaded local GGUF via lms metadata."""

    def __init__(self, settings=None, *, opener=None):
        super().__init__(settings, opener=opener)
        parsed = urlparse(self.settings.url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in LOOPBACK_HOSTS
            or parsed.path != "/v1/embeddings"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise EmbeddingError(
                "LM Studio embedding URL must be a loopback HTTP /v1/embeddings endpoint."
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = os.getenv(self.settings.api_key_env)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _json(self, request: urllib.request.Request) -> dict:
        try:
            with self._urlopen(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise EmbeddingError("LM Studio response is too large.")
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise EmbeddingError("LM Studio response is invalid.")
            return value
        except (OSError, ValueError) as exc:
            raise EmbeddingError("LM Studio local API is unavailable.") from exc

    def inspect_model(self) -> dict[str, object]:
        base = self.settings.url.removesuffix("/v1/embeddings")
        models = self._json(
            urllib.request.Request(base + "/api/v1/models", headers=self._headers())
        ).get("models")
        if not isinstance(models, list):
            raise EmbeddingError("LM Studio model inventory is invalid.")
        if any(
            not isinstance(m.get("loaded_instances", []), list)
            for m in models
            if isinstance(m, dict)
        ):
            raise EmbeddingError("LM Studio loaded instance inventory is invalid.")
        loaded = [
            (model, instance)
            for model in models
            if isinstance(model, dict)
            for instance in model.get("loaded_instances", [])
            if isinstance(instance, dict)
            and instance.get("id") == self.model
            and model.get("type") == "embedding"
        ]
        if len(loaded) != 1:
            raise EmbeddingError(
                "Configured LM Studio embedding model is not loaded uniquely."
            )
        model, instance = loaded[0]
        home_pointer = Path.home() / ".lmstudio-home-pointer"
        home = (
            Path(home_pointer.read_text().strip())
            if home_pointer.is_file()
            else Path.home() / ".lmstudio"
        )
        executable = (
            self.settings.lms_path or shutil.which("lms") or str(home / "bin/lms")
        )
        try:
            process = subprocess.run(
                [executable, "ps", "--json"],
                capture_output=True,
                text=True,
                timeout=min(10, self.settings.timeout_seconds),
                check=True,
            )
            inventory = json.loads(process.stdout)
            if not isinstance(inventory, list):
                raise EmbeddingError("LM Studio CLI inventory is invalid.")
            matches = [
                m
                for m in inventory
                if isinstance(m, dict)
                and m.get("identifier") == self.model
                and m.get("modelKey") == model.get("key")
            ]
            if len(matches) != 1 or matches[0].get("deviceIdentifier") is not None:
                raise EmbeddingError(
                    "LM Studio API and local CLI model identity differ."
                )
            info = matches[0]
            if info.get("type") != "embedding" or info.get("format") != "gguf":
                raise EmbeddingError("LM Studio requires a local GGUF embedding model.")
            settings_path = home / "settings.json"
            folder = json.loads(settings_path.read_text()).get(
                "downloadsFolder", str(home / "models")
            )
            root = Path(folder).expanduser().resolve()
            relative = Path(info["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise EmbeddingError("LM Studio model path is invalid.")
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise EmbeddingError(
                    "LM Studio model file is outside its configured directory."
                )
            digest = _gguf_digest(str(path), _file_signature(path))
            context_config = instance.get("config")
            context = (
                context_config.get("context_length")
                if isinstance(context_config, dict)
                else None
            )
            if (
                type(context) is not int
                or context <= 0
                or context != info.get("contextLength")
            ):
                raise EmbeddingError(
                    "LM Studio loaded context metadata is inconsistent."
                )
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            raise EmbeddingError(
                "LM Studio local model file verification is unavailable."
            ) from exc
        return {
            "model": self.model,
            "model_digest": digest,
            "model_path": str(path),
            "size": path.stat().st_size,
            "context_length": context,
            "model_key": model["key"],
        }

    def _verified_model(self) -> dict[str, object]:
        if not self.settings.model_digest:
            raise EmbeddingError(
                "Local model_digest is not pinned. Run embedding inspect before indexing."
            )
        info = self.inspect_model()
        if info["model_digest"] != self.settings.model_digest:
            raise EmbeddingError(
                "Local model digest differs from the pinned encoder; rebuild a new generation."
            )
        return info

    def document_byte_limit(self) -> int:
        """Match the conservative bound enforced by _embed, including reserve."""
        limit = int(self._verified_model()["context_length"]) - 16
        if limit <= 0:
            raise EmbeddingError("LM Studio context has no room for document text.")
        return limit

    def _embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        if not texts:
            return []
        before = self._verified_model()
        # Conservative bound for byte tokenizers; reserve space for special tokens.
        if any(
            len(text.encode("utf-8")) + 16 > before["context_length"] for text in texts
        ):
            raise EmbeddingError(
                "Input exceeds the conservative LM Studio context limit; split it before encoding.",
                code="EMBEDDING_CONTEXT_LIMIT",
            )
        result = self._json(
            urllib.request.Request(
                self.settings.url,
                data=json.dumps(
                    {"model": self.model, "input": texts, "encoding_format": "float"},
                    ensure_ascii=False,
                ).encode(),
                headers=self._headers(),
                method="POST",
            )
        )
        if result.get("model") != self.model:
            raise EmbeddingError(
                "Embedding response model differs from the configured encoder."
            )
        data = result.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError("Embedding response count is invalid.")
        vectors = {}
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingError("Embedding response item is invalid.")
            index, raw = item.get("index"), item.get("embedding")
            if (
                type(index) is not int
                or not 0 <= index < len(texts)
                or index in vectors
            ):
                raise EmbeddingError("Embedding response index is invalid.")
            if not isinstance(raw, list) or len(raw) != self.dimensions:
                raise EmbeddingError("Embedding dimensions are invalid.")
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in raw):
                raise EmbeddingError("Embedding value is invalid.")
            vector = tuple(float(v) for v in raw)
            norm = math.sqrt(sum(v * v for v in vector))
            if not math.isfinite(norm) or norm == 0:
                raise EmbeddingError("Embedding vector has zero or invalid norm.")
            vectors[index] = (
                tuple(v / norm for v in vector)
                if self.settings.normalization == "l2"
                else vector
            )
        after = self._verified_model()
        if before != after:
            raise EmbeddingError("LM Studio loaded model changed during embedding.")
        return [vectors[i] for i in range(len(texts))]


def embedding_space_id(settings: EmbeddingConfig) -> str:
    payload = {
        "backend": settings.backend,
        "endpoint": settings.url,
        "model": settings.model,
        "dimensions": settings.dimensions,
        "revision": settings.revision,
        "model_digest": settings.model_digest,
        "preprocessing": settings.preprocessing_version
        if settings.backend in {"ollama", "lmstudio"}
        else "plain-v1",
        "query_instruction": settings.query_instruction
        if settings.backend in {"ollama", "lmstudio"}
        else "",
        "normalization": settings.normalization,
        "provider_normalization": "ollama-l2"
        if settings.backend == "ollama"
        else "provider",
        "truncate": False if settings.backend == "ollama" else None,
    }
    if settings.backend == "lmstudio":
        payload.update(
            {
                "provider_normalization": "lmstudio-provider",
                "truncate": "reject-byte-bound-v1",
            }
        )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def index_model_id(settings: EmbeddingConfig) -> str:
    # Unversioned legacy model names must never silently match a new space.
    return f"{settings.backend}:{embedding_space_id(settings)}:{settings.model}"


class OpenRouterEmbeddingClient(OllamaEmbeddingClient):
    """OpenAI-compatible text embeddings; never apply Qwen query instructions."""

    def __init__(
        self, settings=None, *, opener=urllib.request, usage_path: Path | None = None
    ):
        super().__init__(settings, opener=opener)
        self.usage_path = usage_path

    def _record_usage(self, request_id: str, state: str, usage=None) -> None:
        if self.usage_path is None:
            return
        usage = usage if isinstance(usage, dict) else {}
        cost = usage.get("cost")
        known = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
        record = {
            "request_id": request_id,
            "role": "text_embedding",
            "model": self.model,
            "space_id": embedding_space_id(self.settings),
            "timestamp": time.time(),
            "state": state,
            "actual_cost_usd": cost if known else None,
            "usage": {
                k: usage[k] for k in ("prompt_tokens", "total_tokens") if k in usage
            },
        }
        self.usage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.usage_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._embed([text])[0]

    def _embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        if not texts:
            return []
        key = os.environ.get(self.settings.api_key_env)
        if not key:
            raise EmbeddingError(
                f"Missing embedding credential: {self.settings.api_key_env}."
            )
        if len(texts) > self.settings.batch_size:
            return [
                v
                for start in range(0, len(texts), self.settings.batch_size)
                for v in self._embed(texts[start : start + self.settings.batch_size])
            ]
        request = urllib.request.Request(
            self.settings.url,
            data=json.dumps(
                {
                    "model": self.model,
                    "input": texts,
                    "encoding_format": "float",
                    "dimensions": self.dimensions,
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        request_id = str(uuid.uuid4())
        self._record_usage(request_id, "reserved")
        try:
            with self._urlopen(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError("Embedding response is too large.")
            result = json.loads(payload)
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            self._record_usage(request_id, "usage_unknown")
            raise EmbeddingChargeUnknown(
                "OpenRouter embedding service is unavailable; the charge is unresolved."
            ) from exc
        if not isinstance(result, dict):
            self._record_usage(request_id, "usage_unknown")
            raise EmbeddingChargeUnknown(
                "Embedding response is invalid; the charge is unresolved."
            )
        usage = result.get("usage") or {}
        cost = usage.get("cost") if isinstance(usage, dict) else None
        self._record_usage(
            request_id,
            "completed"
            if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
            else "usage_unknown",
            usage,
        )
        error_type = (
            EmbeddingError
            if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
            else EmbeddingChargeUnknown
        )
        if result.get("model") is not None and result["model"] != self.model:
            raise error_type(
                "Embedding response model differs from the configured encoder."
            )
        data = result.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise error_type("Embedding response count is invalid.")
        vectors = {}
        for item in data:
            if not isinstance(item, dict):
                raise error_type("Embedding entry is invalid.")
            index = item.get("index")
            if (
                type(index) is not int
                or not 0 <= index < len(texts)
                or index in vectors
            ):
                raise error_type("Embedding response index is invalid.")
            raw = item.get("embedding")
            if not isinstance(raw, list) or len(raw) != self.dimensions:
                raise error_type("Embedding dimensions are invalid.")
            try:
                if any(
                    type(v) not in (int, float) or not math.isfinite(v) for v in raw
                ):
                    raise error_type("Embedding value is invalid.")
                vectors[index] = tuple(float(v) for v in raw)
                if self.settings.normalization == "l2":
                    norm = math.sqrt(sum(v * v for v in vectors[index]))
                    if not math.isfinite(norm) or norm == 0:
                        raise error_type("Embedding vector has zero or invalid norm.")
                    vectors[index] = tuple(v / norm for v in vectors[index])
            except OverflowError as exc:
                raise error_type("Embedding value is invalid.") from exc
        self.last_usage = result.get("usage") or {}
        return [vectors[i] for i in range(len(texts))]


def client_from_config(config: AppConfig | None = None) -> EmbeddingProvider:
    resolved = config or load_config()
    if resolved.embedding.backend == "lmstudio":
        return LMStudioEmbeddingClient(resolved.embedding)
    if resolved.embedding.backend == "openrouter":
        return OpenRouterEmbeddingClient(
            resolved.embedding,
            usage_path=resolved.paths.state_dir / "text-embedding-usage.jsonl",
        )
    return OllamaEmbeddingClient(resolved.embedding)
