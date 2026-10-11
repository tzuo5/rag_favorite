from __future__ import annotations

import json
import os

from openai import AsyncOpenAI
from openai import APIError
from pydantic import ValidationError

from .markdown import fallback_enrichment, transcript_body
from .models import Enrichment, SourceMetadata


class MetadataEnricher:
    """Schema-validated optional LLM enrichment with a deterministic fallback."""

    async def enrich(self, metadata: SourceMetadata, transcript: str) -> Enrichment:
        try:
            return await self._enrich(metadata, transcript)
        except (APIError, ValidationError, OSError, TimeoutError, RuntimeError):
            return fallback_enrichment(metadata.original_title, transcript)

    async def _enrich(self, metadata: SourceMetadata, transcript: str) -> Enrichment:
        key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL")
        if not key or not model:
            return fallback_enrichment(metadata.original_title, transcript)
        client = AsyncOpenAI(api_key=key, base_url=os.getenv("OPENAI_BASE_URL") or None)
        schema = Enrichment.model_json_schema()
        prompt = {
            "source": metadata.model_dump(mode="json"),
            "transcript": transcript_body(transcript)[:80000],
            "requirements": "Return a faithful Chinese summary, 3-10 key points, and 8-20 evidence-based detailed tags. Never invent the author.",
        }
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You extract faithful metadata from video transcripts. Return only schema-valid JSON."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            response_format={"type": "json_schema", "json_schema": {"name": "video_enrichment", "strict": True, "schema": schema}},
            temperature=0,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("LLM enrichment returned no content")
        return Enrichment.model_validate_json(content)
