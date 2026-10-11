"""Human-verified retrieval evaluation; drafts are never counted as acceptance."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from .config import ConfigError
from .database import connect_database
from .video_retrieval import search_all
from .video_store import library_id


def validate_gold(value: dict, *, final=False):
    questions = value.get("questions", [])
    if not questions or any(q.get("human_verified") is not True for q in questions):
        raise ConfigError(
            "Every acceptance question must be verified by a human; draft/model labels do not count."
        )
    for question in questions:
        if (
            not question.get("query")
            or not question.get("collection")
            or not question.get("expected_segment_ids")
        ):
            raise ConfigError(
                "Gold questions need query, collection and expected segment IDs."
            )
        try:
            for segment_id in question["expected_segment_ids"]:
                video_id, ordinal = segment_id.split(":")
                UUID(video_id)
                if int(ordinal) < 0:
                    raise ValueError("Invalid ordinal")
        except (TypeError, ValueError, AttributeError):
            raise ConfigError("Invalid expected segment ID.")
    videos = {s.split(":")[0] for q in questions for s in q["expected_segment_ids"]}
    if final and (len(questions) < 60 or len(videos) < 10):
        raise ConfigError(
            "Final acceptance requires at least 60 verified questions from 10 videos."
        )
    if final and (
        len({(q["collection"], q["query"].strip()) for q in questions}) < 60
        or any(not q.get("expected_answer") for q in questions)
    ):
        raise ConfigError(
            "Final gold requires 60 different questions with human-verified reference answers."
        )
    return questions


def evaluate(config, video, gold: dict, *, final=False):
    questions = validate_gold(gold, final=final)
    ids = list({s for q in questions for s in q["expected_segment_ids"]})
    with connect_database(config) as c:
        rows = c.execute(
            "SELECT s.id,v.id,v.collection,v.classification,v.content_sha256 FROM public.rag_video_segments s JOIN public.rag_videos v ON v.id=s.video_id JOIN public.rag_video_jobs j ON j.id=v.id JOIN public.rag_knowledge_summaries k ON k.job_id=v.id WHERE s.id=ANY(%s) AND v.library_id=%s AND j.state='complete' AND v.source_deleted",
            (ids, library_id(video)),
        ).fetchall()
    metadata = {r[0]: r for r in rows}
    if set(metadata) != set(ids):
        raise ConfigError(
            "Expected segments must exist in complete, source-cleaned videos with published summaries."
        )
    unique_sources = set()
    for question in questions:
        for segment_id in question["expected_segment_ids"]:
            row = metadata[segment_id]
            if question["collection"] != "all" and row[2] != question["collection"]:
                raise ConfigError("Expected segment belongs to another collection.")
            source = video.root / "derived" / str(row[1]) / "source.json"
            note_id = (
                json.loads(source.read_text()).get("note_id")
                if source.is_file()
                else None
            )
            unique_sources.add("note:" + note_id if note_id else "sha256:" + row[4])
    if final and len(unique_sources) < 10:
        raise ConfigError(
            "Final acceptance needs 10 different sources; duplicate imports do not count."
        )
    outcomes = []
    for question in questions:
        results = search_all(
            question["query"],
            list(config.collections)
            if question["collection"] == "all"
            else [question["collection"]],
            5,
            config,
            video,
            visual_query=question.get("visual_query"),
        )
        ids = [r["document_id"] for r in results]
        expected = {"video:" + s.split(":")[0] for s in question["expected_segment_ids"]}
        hit = bool(expected.intersection(ids))
        outcomes.append(
            {
                "query": question["query"],
                "expected_document_ids": sorted(expected),
                "returned_document_ids": ids,
                "top5_document_hit": hit,
            }
        )
    recall = sum(row["top5_document_hit"] for row in outcomes) / len(outcomes)
    return {
        "scope": "retrieval_only_not_answer_accuracy",
        "human_verified": True,
        "questions": len(outcomes),
        "distinct_sources": len(unique_sources),
        "retrieval_scope": "summaries_only",
        "top5_document_recall": recall,
        "passes_retrieval_threshold": recall >= 0.9,
        "final_corpus_requirements_checked": final,
        "outcomes": outcomes,
    }


def draft_from_records(video, output: Path, *, allowed_job_ids=None):
    questions = []
    for path in sorted((video.root / "derived").glob("*/record.json")):
        record = json.loads(path.read_text())
        if allowed_job_ids is not None and record["id"] not in allowed_job_ids:
            continue
        for segment in record["segments"]:
            for fact in segment["facts"]:
                if not fact.get("entity") or fact.get("uncertain"):
                    continue
                questions.append(
                    {
                        "query": f"这段视频中，{fact['entity']}的{fact.get('attribute', '做法')}是什么？",
                        "collection": "all",
                        "expected_segment_ids": [segment["id"]],
                        "reference_quote": fact["quote"],
                        "expected_answer": fact["statement"],
                        "reference_time": [segment["start"], segment["end"]],
                        "evidence_ids": [e["id"] for e in segment.get("evidence", [])],
                        "human_verified": False,
                    }
                )
    value = {
        "status": "draft_requires_human_review",
        "scope": "selected_manifest_sources"
        if allowed_job_ids is not None
        else "all_records_may_include_test_fixtures",
        "questions": questions,
    }
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    output.chmod(0o600)
    return {
        "status": value["status"],
        "questions": len(questions),
        "output": str(output),
    }
