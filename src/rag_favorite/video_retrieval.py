"""Summary-only public retrieval and retained evidence access."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from pgvector import Vector

from .config import ConfigError
from .database import connect_database
from .embedding import client_from_config, embedding_space_id
from .indexes import resolve_index
from .knowledge_summaries import search_summaries
from .video_provider import local_encoder
from .video_store import digest_file, library_id


def reciprocal_rank_fusion(rankings: list[list[dict]], limit: int) -> list[dict]:
    scores, items = {}, {}
    for ranking in rankings:
        seen = set()
        for rank, item in enumerate(ranking, 1):
            key = (item["document_id"], item["section"])
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
            # Keep the primary text score when available; independent score
            # spaces are never added as cosine similarities.
            items.setdefault(key, item)
    ordered = sorted(items, key=lambda key: (-scores[key], key))[:limit]
    return [dict(items[key], combined_score=scores[key]) for key in ordered]


def _result(row, score: float, channel: str) -> dict:
    return {
        "result_type": "video_segment",
        "document_id": "video:" + str(row[0]),
        "knowledge_base": row[1],
        "title": row[2],
        "source_relative_path": "video/" + str(row[0]) + "/knowledge.md",
        "section": row[3],
        "excerpt": "\n".join(
            [
                row[4],
                row[5],
                *[
                    f["statement"]
                    + (" [unknown]" if f["uncertain"] else "")
                    + (" [conflict]" if f["conflict"] else "")
                    for f in row[6]
                ],
            ]
        ),
        "semantic_score": score if channel == "text" else None,
        "lexical_score": None,
        "combined_score": None,
        "reliable": channel == "text" and score >= 0.3,
        "content_hash": row[10],
        "metadata": {
            "start_seconds": row[7],
            "end_seconds": row[8],
            "timing_precision": row[9],
            "source_deleted": True,
            "facts": row[6],
            "evidence_ids": [x["id"] for x in row[11]][:2],
            "channel": channel,
            "video_score": score if channel == "video" else None,
        },
    }


def search_video_rankings(
    query: str,
    collections: list[str],
    limit: int,
    config,
    video,
    *,
    visual_query: str | None = None,
) -> list[list[dict]]:
    profile = resolve_index(config)
    selected = [profile.collection(key).key for key in collections]
    vector = Vector(list(client_from_config(profile).embed_query(query)))
    columns = "v.id,v.collection,v.title,s.id,s.transcript,s.caption,s.facts,s.start_seconds,s.end_seconds,s.timing_precision,v.content_sha256,s.evidence"
    scope = "FROM public.rag_video_segments s JOIN public.rag_videos v ON v.id=s.video_id JOIN public.rag_video_jobs j ON j.id=v.id WHERE v.library_id=%s AND v.collection=ANY(%s) AND v.source_deleted AND j.state='complete'"
    rankings = []
    with connect_database(profile) as c:
        rows = c.execute(
            "SELECT "
            + columns
            + ",1-(s.text_embedding <=> %s) "
            + scope
            + " AND s.text_space_id=%s ORDER BY s.text_embedding <=> %s LIMIT %s",
            (
                vector,
                library_id(video),
                selected,
                embedding_space_id(profile.embedding),
                vector,
                limit * 3,
            ),
        ).fetchall()
        rankings.append([_result(row, float(row[-1]), "text") for row in rows])
        rows = c.execute(
            "SELECT "
            + columns
            + " "
            + scope
            + " AND EXISTS(SELECT 1 FROM public.rag_video_edges e WHERE e.segment_id=s.id AND (position(e.subject in %s)>0 OR position(e.object in %s)>0)) ORDER BY v.created_at DESC LIMIT %s",
            (library_id(video), selected, query, query, limit * 3),
        ).fetchall()
        rankings.append([_result(row, 0, "graph") for row in rows])
    if visual_query:
        encoded = local_encoder(video, "embed-text", {"text": visual_query})
        video_vector = Vector(encoded["vector"])
        with connect_database(profile) as c:
            rows = c.execute(
                "SELECT "
                + columns
                + ",1-(s.video_embedding <=> %s) "
                + scope
                + " AND s.video_space_id=%s ORDER BY s.video_embedding <=> %s LIMIT %s",
                (
                    video_vector,
                    library_id(video),
                    selected,
                    video.space_id,
                    video_vector,
                    limit * 3,
                ),
            ).fetchall()
        rankings.append([_result(row, float(row[-1]), "video") for row in rows])
    return rankings


def search_video_knowledge(
    query, collections, limit, config, video, *, visual_query=None
):
    return search_all(
        query, collections, limit, config, video, visual_query=visual_query
    )


def common_text_ranking(documents, video_text):
    # Both lists use the same pinned Qwen encoder. Comparing these cosine
    # scores is valid; comparing ImageBind scores with Qwen is not.
    return sorted(
        [*documents, *video_text], key=lambda r: -(r.get("semantic_score") or 0)
    )


def search_all(
    query: str,
    collections: list[str],
    limit: int,
    config,
    video,
    *,
    visual_query: str | None = None,
):
    if visual_query:
        raise ValueError("Summary-only retrieval does not accept visual_query.")
    return search_summaries(query, collections, limit, config, video)


def evidence_record(
    evidence_id: str, collection: str, config, video
) -> tuple[dict, Path]:
    selected = (
        [
            "general",
            "cooking",
            "tech",
            "social-conduct",
            "career",
            "finance",
            "thought-politics",
            "literature-culture",
        ]
        if config.unified
        else list(config.collections)
        if collection == "all"
        else [config.collection(collection).key]
    )
    pieces = evidence_id.split(":")
    if (
        len(pieces) != 4
        or pieces[2] != "image"
        or not pieces[1].isdigit()
        or pieces[3] not in {"0", "1"}
    ):
        raise ConfigError("Invalid evidence ID.")
    job_id = str(UUID(pieces[0]))
    segment_id = job_id + ":" + pieces[1]
    with connect_database(config) as c:
        row = c.execute(
            "SELECT s.evidence FROM public.rag_video_segments s JOIN public.rag_videos v ON v.id=s.video_id JOIN public.rag_video_jobs j ON j.id=v.id WHERE s.id=%s AND v.library_id=%s AND v.collection=ANY(%s) AND v.source_deleted AND j.state='complete'",
            (segment_id, library_id(video), selected),
        ).fetchone()
    if not row:
        raise ConfigError("Evidence is unavailable in the requested collection.")
    records = [x for x in row[0] if x["id"] == evidence_id]
    if not records:
        raise ConfigError("Evidence is unavailable.")
    record = records[0]
    path = (video.root / record["relative_path"]).resolve()
    if (
        not path.is_relative_to((video.root / "derived" / job_id).resolve())
        or not path.is_file()
        or digest_file(path) != record["sha256"]
    ):
        raise ConfigError("Evidence integrity verification failed.")
    return record, path
