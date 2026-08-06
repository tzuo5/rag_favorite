from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from .config import Settings
from .models import Destination
from .security import safe_filename

TOPIC_ROOTS = {
    "thought-politics": Path(
        "/home/ubuntu/知识库/Thought and Politics/4. Video Transcripts"
    ),
    "tech": Path("/home/ubuntu/知识库/Technology/3. Video Transcripts"),
    "finance": Path("/home/ubuntu/知识库/Finance and Investment/2. Video Transcripts"),
    "career": Path("/home/ubuntu/知识库/Career Development/2. Video Transcripts"),
    "social-conduct": Path(
        "/home/ubuntu/知识库/Chinese Social Relations and Conduct/2. Video Transcripts"
    ),
    "literature-culture": Path(
        "/home/ubuntu/知识库/Literature and Culture/3. Video Transcripts"
    ),
    "general": Path("/home/ubuntu/知识库/General Resources/2. Video Transcripts"),
}

DOMAIN_TO_TOPIC = {
    "technology": "tech",
    "finance": "finance",
    "career": "career",
    "social-conduct": "social-conduct",
    "learning": "general",
    "reference": "general",
}


def _frontmatter_metadata(markdown_path: Path) -> dict[str, Any]:
    text = markdown_path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    loaded = yaml.safe_load(text[4:end])
    return loaded if isinstance(loaded, dict) else {}


def classify_main_metadata(metadata: dict[str, Any]) -> str:
    source = metadata.get("source")
    source = source if isinstance(source, dict) else {}
    enrichment = metadata.get("enrichment")
    enrichment = enrichment if isinstance(enrichment, dict) else {}
    author = str(metadata.get("author") or source.get("author") or "")
    title = str(
        metadata.get("title")
        or metadata.get("original_title")
        or enrichment.get("normalized_title")
        or source.get("original_title")
        or ""
    )
    if "老周横眉" in author or "老周横眉" in title or "老周快评" in title:
        return "thought-politics"
    if "mr jonathan" in author.casefold():
        return "career"
    return DOMAIN_TO_TOPIC.get(str(metadata.get("domain") or ""), "general")


def classify_main_markdown(markdown_path: Path) -> str:
    return classify_main_metadata(_frontmatter_metadata(markdown_path))


@dataclass(frozen=True)
class IndexResult:
    knowledge_base: str
    indexed: bool


class KnowledgeIndexer(Protocol):
    def ingest(
        self,
        *,
        knowledge_base: str,
        markdown_path: Path,
    ) -> IndexResult: ...


class TopicIndexerAdapter:
    """Adapter for the seven topic collections."""

    def ingest(
        self,
        *,
        knowledge_base: str,
        markdown_path: Path,
    ) -> IndexResult:
        if knowledge_base not in TOPIC_ROOTS:
            raise ValueError("topic indexer received an invalid knowledge base")
        spec = importlib.util.spec_from_file_location(
            "existing_main_rag",
            "/home/ubuntu/services/rag-app/rag.py",
        )
        if not spec or not spec.loader:
            raise RuntimeError("main knowledge indexer is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        indexed = bool(module.ingest_file(markdown_path, knowledge_base))
        return IndexResult(knowledge_base=knowledge_base, indexed=indexed)


class CookingIndexerAdapter:
    """Adapter that preserves the Cooking Vault and recipe index."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def ingest(
        self,
        *,
        knowledge_base: str,
        markdown_path: Path,
    ) -> IndexResult:
        if knowledge_base != "cooking":
            raise ValueError("cooking indexer received an invalid knowledge base")
        service_dir = Path("/home/ubuntu/services/cooking-rag")
        sys.path.insert(0, str(service_dir))
        previous = os.environ.get("COOKING_RAG_ENV_FILE")
        os.environ["COOKING_RAG_ENV_FILE"] = str(self.settings.cooking_env)
        try:
            from embedding import OllamaEmbeddingClient
            from recipe_documents import load_recipe_document
            from repository import RecipeIndexer

            class PgvectorCompatibleEmbeddingClient(OllamaEmbeddingClient):
                def embed_documents(self, texts: list[str]) -> list[list[float]]:
                    return [list(vector) for vector in super().embed_documents(texts)]

            vault = Path("/home/ubuntu/知识库/Cooking")
            document = load_recipe_document(vault, markdown_path)
            indexed = bool(
                RecipeIndexer(
                    vault,
                    PgvectorCompatibleEmbeddingClient(),
                ).index_document(document)
            )
            return IndexResult(knowledge_base="cooking", indexed=indexed)
        finally:
            if previous is None:
                os.environ.pop("COOKING_RAG_ENV_FILE", None)
            else:
                os.environ["COOKING_RAG_ENV_FILE"] = previous
            try:
                sys.path.remove(str(service_dir))
            except ValueError:
                pass


class UnifiedIndexingService:
    """One indexing entrypoint with isolated Topic and Cooking backends."""

    def __init__(
        self,
        topic_indexer: KnowledgeIndexer,
        cooking_indexer: KnowledgeIndexer,
    ) -> None:
        self.topic_indexer = topic_indexer
        self.cooking_indexer = cooking_indexer

    @classmethod
    def for_runtime(cls, settings: Settings) -> UnifiedIndexingService:
        return cls(
            topic_indexer=TopicIndexerAdapter(),
            cooking_indexer=CookingIndexerAdapter(settings),
        )

    def ingest(
        self,
        *,
        knowledge_base: str,
        markdown_path: Path,
    ) -> IndexResult:
        if knowledge_base == "cooking":
            return self.cooking_indexer.ingest(
                knowledge_base=knowledge_base,
                markdown_path=markdown_path,
            )
        if knowledge_base in TOPIC_ROOTS:
            return self.topic_indexer.ingest(
                knowledge_base=knowledge_base,
                markdown_path=markdown_path,
            )
        raise ValueError("unknown indexing knowledge base")


class VectorRepository:
    """Compatibility facade over the unified indexing service."""

    def __init__(self, settings: Settings):
        self.indexing = UnifiedIndexingService.for_runtime(settings)

    def ingest(
        self,
        destination: Destination,
        markdown_path: Path,
    ) -> IndexResult:
        knowledge_base = (
            classify_main_markdown(markdown_path)
            if destination == Destination.MAIN
            else destination.value
        )
        return self.indexing.ingest(
            knowledge_base=knowledge_base,
            markdown_path=markdown_path,
        )


class KnowledgeDestinationService:
    def __init__(self, settings: Settings, vectors: VectorRepository):
        self.settings = settings
        self.vectors = vectors

    def target_path(
        self,
        destination: Destination,
        title: str,
        document_id: str,
        source_markdown: Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        if destination in {
            Destination.THOUGHT_POLITICS,
            Destination.TECH,
            Destination.FINANCE,
            Destination.CAREER,
            Destination.SOCIAL_CONDUCT,
            Destination.LITERATURE_CULTURE,
            Destination.GENERAL,
        }:
            root = TOPIC_ROOTS[destination.value]
        elif (
            destination == Destination.MAIN
            and source_markdown is not None
            and source_markdown.exists()
        ):
            root = TOPIC_ROOTS[classify_main_markdown(source_markdown)]
        elif destination == Destination.MAIN and metadata is not None:
            root = TOPIC_ROOTS[classify_main_metadata(metadata)]
        else:
            root = self.settings.cooking_markdown_root
        return root / f"{safe_filename(title)}--{document_id[:8]}.md"

    def move_and_index(
        self, staging: Path, destination: Destination, title: str, document_id: str
    ) -> Path:
        target = self.target_path(
            destination,
            title,
            document_id,
            source_markdown=staging,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            os.replace(staging, target)
        self.vectors.ingest(destination, target)
        return target
