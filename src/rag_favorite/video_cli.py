"""Remote-Terminal asset submission, job inspection and derived knowledge search."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_config
from .database import connect_database
from .video_backfill import backfill
from .video_config import load_video_config
from .video_retrieval import search_all
from .video_store import (
    enqueue,
    enqueue_url,
    ingestion_collection,
    job_status,
    library_id,
    stage_asset,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    following = commands.add_parser("import-following")
    following.add_argument("--collection", default="all")
    following.add_argument("--max-pages", type=int, default=0)
    following.add_argument("--restart", action="store_true")
    following.add_argument("--continue-only", action="store_true")
    following.add_argument("--seconds", type=int, default=1800)
    for command in ("following-status", "following-pause", "following-resume"):
        entry = commands.add_parser(command)
        entry.add_argument("--collection", default="all")
        entry.add_argument("--json", action="store_true", help="JSON output (also the default).")
    favorites = commands.add_parser("import-favorites")
    favorites.add_argument("--collection", default="all")
    favorites.add_argument("--max-pages", type=int, default=0)
    favorites.add_argument("--restart", action="store_true")
    favorites_status_parser = commands.add_parser("favorites-status")
    favorites_status_parser.add_argument("--collection", default="all")
    stage = commands.add_parser("stage")
    stage.add_argument("path", type=Path)
    submit = commands.add_parser("import")
    submit.add_argument("asset_id")
    submit.add_argument("--collection", default="all")
    submit.add_argument("--transcript-asset")
    submit.add_argument("--title", default="")
    submit.add_argument("--backfill", action="store_true")
    source = commands.add_parser("import-url")
    source.add_argument("url")
    source.add_argument("--collection", default="all")
    source.add_argument("--title", default="")
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("gold", type=Path)
    evaluation.add_argument("--final", action="store_true")
    draft = commands.add_parser("draft-questions")
    draft.add_argument("output", type=Path)
    draft.add_argument("--manifest", type=Path)
    recheck = commands.add_parser("recheck-asr")
    recheck.add_argument("job_id")
    author = commands.add_parser("index-author-note")
    author.add_argument("job_id")
    attach = commands.add_parser("attach-transcript")
    attach.add_argument("job_id")
    attach.add_argument("transcript_asset_id")
    status = commands.add_parser("status")
    status.add_argument("job_id", nargs="?")
    retry = commands.add_parser("retry")
    retry.add_argument("job_id")
    retry.add_argument(
        "--visual-seconds",
        type=int,
        help="Optional cumulative time budget; 0 disables it.",
    )
    expiry = commands.add_parser("expire-media")
    expiry.add_argument("--apply", action="store_true")
    legacy = commands.add_parser("backfill")
    legacy.add_argument("directory", type=Path)
    legacy.add_argument("--collection", required=True)
    legacy.add_argument("--manifest", type=Path, required=True)
    legacy.add_argument("--apply", action="store_true")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--collection", default="all")
    search.add_argument("--visual-query")
    search.add_argument("--limit", type=int, default=5, choices=range(1, 6))
    summarize = commands.add_parser("summarize")
    summarize.add_argument("job_id")
    summary_backfill = commands.add_parser("summary-backfill")
    summary_backfill.add_argument("--retry-failed", action="store_true")
    commands.add_parser("summary-status")
    document = commands.add_parser("document-read")
    document.add_argument("document_id")
    document.add_argument("--collection", default="all")
    document.add_argument("--version")
    document.add_argument("--offset", type=int, default=0)
    document.add_argument("--max-characters", type=int, default=16000)
    args = parser.parse_args()
    config, video = load_config(), load_video_config()
    if args.command in {"import-favorites", "favorites-status", "import", "import-url"}:
        args.collection = ingestion_collection(config, video, args.collection)
    if args.command == "summarize":
        from .knowledge_summaries import summarize_job
        output = summarize_job(config, video, args.job_id)
    elif args.command == "summary-backfill":
        from .knowledge_summaries import summary_status
        from .summary_worker import enqueue_summaries
        output = {"queued": enqueue_summaries(config, video, retry_failed=args.retry_failed),
                  **summary_status(config, video)}
    elif args.command == "summary-status":
        from .knowledge_summaries import summary_status
        output = summary_status(config, video)
    elif args.command == "document-read":
        from .knowledge_summaries import document_context
        output = document_context(args.document_id,
            list(config.collections) if args.collection == "all" else [args.collection],
            config, video, version=args.version, offset=args.offset, max_characters=args.max_characters)
    elif args.command == "import-following":
        from .xhs_following import sync_following
        output = sync_following(config, video, args.collection, max_pages=args.max_pages,
                                restart=args.restart, continue_only=args.continue_only,
                                seconds=args.seconds)
    elif args.command == "following-status":
        from .xhs_following import following_status
        output = following_status(config, video, args.collection)
    elif args.command in {"following-pause", "following-resume"}:
        from .xhs_following import following_control
        output = following_control(config, video, args.command == "following-pause", args.collection)
    elif args.command == "import-favorites":
        from .xhs_favorites import sync_favorites

        output = sync_favorites(
            config,
            video,
            args.collection,
            max_pages=args.max_pages,
            restart=args.restart,
        )
    elif args.command == "favorites-status":
        from .xhs_favorites import favorites_status

        config.collection(args.collection)
        output = favorites_status(video, args.collection)
    elif args.command == "stage":
        output = stage_asset(args.path, video)
    elif args.command == "backfill":
        output = backfill(
            config,
            video,
            args.directory,
            args.collection,
            args.manifest,
            apply=args.apply,
        )
    elif args.command == "import-url":
        output = enqueue_url(config, video, args.url, args.collection, args.title)
    elif args.command == "evaluate":
        from .video_evaluation import evaluate

        output = evaluate(
            config, video, json.loads(args.gold.read_text()), final=args.final
        )
    elif args.command == "draft-questions":
        from .video_evaluation import draft_from_records

        allowed = (
            {
                row["job_id"]
                for row in json.loads(args.manifest.read_text())["samples"]
                if row.get("job_id")
            }
            if args.manifest
            else None
        )
        output = draft_from_records(video, args.output, allowed_job_ids=allowed)
    elif args.command == "recheck-asr":
        from .video_maintenance import recheck_asr

        output = recheck_asr(config, video, args.job_id)
    elif args.command == "index-author-note":
        from .video_notes import index_author_note

        output = index_author_note(config, video, args.job_id)
    elif args.command == "attach-transcript":
        from .video_retention import attach_transcript

        attach_transcript(config, video, args.job_id, args.transcript_asset_id)
        output = job_status(config, video, args.job_id)
    elif args.command == "import":
        output = enqueue(
            config,
            video,
            args.asset_id,
            args.collection,
            args.transcript_asset,
            args.title,
            priority=0 if args.backfill else 10,
        )
    elif args.command == "retry":
        from .video_retention import retry_job

        retry_job(config, video, args.job_id, visual_seconds=args.visual_seconds)
        output = job_status(config, video, args.job_id)
    elif args.command == "expire-media":
        from .video_retention import expire_failed_media

        output = expire_failed_media(config, video, apply=args.apply)
    elif args.command == "search":
        output = search_all(
            args.query,
            list(config.collections) if args.collection == "all" else [args.collection],
            args.limit,
            config,
            video,
            visual_query=args.visual_query,
        )
    elif args.job_id:
        output = job_status(config, video, args.job_id)
    else:
        with connect_database(config) as c:
            rows = c.execute(
                "SELECT id,collection,state,stage,error_code FROM public.rag_video_jobs WHERE library_id=%s ORDER BY created_at DESC LIMIT 30",
                (library_id(video),),
            ).fetchall()
        output = [
            dict(
                zip(
                    ("job_id", "collection", "state", "stage", "error_code"),
                    (str(row[0]), *row[1:]),
                    strict=True,
                )
            )
            for row in rows
        ]
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    if args.command in {"import-favorites", "import-following"} and output.get("state") in {
        "blocked",
        "partial",
        "backpressure",
        "paused",
        "already_running",
    }:
        return 1 if args.command == "import-favorites" or output["state"] == "blocked" else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
