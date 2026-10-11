"""Resumable legacy migration, verified OCR and one fresh source task per URL."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .config import CollectionConfig, ConfigError, load_config
from .database import connect_database
from .knowledge_summaries import atomic_text, text_hash
from .migrations import MigrationRunner
from .queue_control import read_control, set_control
from .unified_library import (
    activate_generation,
    active_generation,
    create_generation,
    extractive_summary,
    knowledge_root,
    publish_summary,
    queue_summary,
    register_document,
    sync_video_summary,
)
from .video_config import load_video_config
from .video_sources import normalize_media_url
from .video_store import digest_file, enqueue_reprocess, library_id
from .xhs_images import local_ocr

WIKI = re.compile(r"(!?)\[\[([^\]]+)\]\]")
IMAGES = {".jpg", ".jpeg", ".png", ".webp"}


def inspect_legacy(root):
    root = root.resolve()
    files = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    names = defaultdict(list)
    for p in files:
        names[p.name].append(p)
    images = {
        str(p): {"sha256": digest_file(p), "suffix": p.suffix.lower()}
        for p in files
        if p.suffix.lower() in IMAGES
    }
    documents = []
    used = set()
    unresolved = []
    for p in files:
        if p.suffix.lower() != ".md" or p == root / "README.md":
            continue
        raw = p.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        refs = {}
        for _, label in WIKI.findall(text):
            target = label.split("|")[0]
            if not Path(target).suffix:
                target = target.split("#")[0]
            if not target:
                continue
            candidates = [p.parent / target, root / target]
            if not Path(target).suffix:
                candidates += [p.parent / (target + ".md"), root / (target + ".md")]
            found = next(
                (
                    q.resolve()
                    for q in candidates
                    if q.is_file() and q.resolve().is_relative_to(root)
                ),
                None,
            )
            if found is None:
                matches = names.get(Path(target).name, []) or names.get(
                    Path(target).name + ".md", []
                )
                if len(matches) == 1:
                    found = matches[0]
            if found is None:
                unresolved.append(
                    {"source": str(p.relative_to(root)), "reference": target}
                )
                continue
            refs[target] = str(found)
            if str(found) in images:
                used.add(str(found))
        urls = {}
        for line in text.splitlines():
            if not any(x in line.lower() for x in ["来源", "链接", "source", "原始"]):
                continue
            for url in re.findall(r'https?://[^\s<>\[\]()"\']+', line):
                try:
                    key, _canonical = normalize_media_url(url)
                except ConfigError:
                    continue
                urls[key] = url
        bundle = hashlib.sha256(
            raw
            + json.dumps(
                sorted(images[q]["sha256"] for q in refs.values() if q in images)
            ).encode()
        ).hexdigest()
        ident = str(uuid5(NAMESPACE_URL, "rag-favorite:legacy:" + bundle))
        documents.append(
            {
                "id": ident,
                "path": str(p),
                "relative_path": p.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "title": p.stem,
                "references": refs,
                "sources": urls,
                "state": "pending",
            }
        )
    return {
        "version": 1,
        "documents": documents,
        "images": images,
        "unreferenced_images": sorted(set(images) - used),
        "unresolved_references": unresolved,
        "unique_documents": len({d["id"] for d in documents}),
        "unique_sources": len({k for d in documents for k in d["sources"]}),
    }


def copy_image(config, path, record):
    root = knowledge_root(config)
    target = root / "attachments" / (record["sha256"] + record["suffix"])
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        raise ConfigError("Unsafe attachment target.")
    if not target.exists():
        temp = target.with_suffix(target.suffix + ".tmp")
        if temp.is_symlink():
            raise ConfigError("Unsafe attachment temporary file.")
        shutil.copyfile(path, temp)
        temp.chmod(0o600)
        os.replace(temp, target)
    if digest_file(target) != record["sha256"]:
        raise ConfigError("Attachment hash mismatch.")
    return target


def ocr_image(config, path, record):
    target = copy_image(config, path, record)
    cache = target.with_suffix(target.suffix + ".ocr.json")
    if cache.is_symlink():
        raise ConfigError("Unsafe OCR cache.")
    if cache.exists():
        return json.loads(cache.read_text())
    result = local_ocr(target)
    atomic_text(
        cache,
        json.dumps({"sha256": record["sha256"], "ocr": result}, ensure_ascii=False),
    )
    return {"sha256": record["sha256"], "ocr": result}


def ocr_text(result):
    return "\n".join(
        item["text"] + (" [unknown: OCR置信度低]" if item["confidence"] < 0.85 else "")
        for item in result["ocr"]
    )


def materialize_document(config, video, doc, inventory, ocr):
    root = knowledge_root(config)
    directory = root / doc["id"]
    source = Path(doc["path"])
    if digest_file(source) != doc["sha256"]:
        raise ConfigError("Legacy source changed since inventory.")
    raw = source.read_text(encoding="utf-8", errors="replace")
    atomic_text(directory / "original.md", raw)
    mapping = {d["path"]: d["id"] for d in inventory["documents"]}

    def wiki(match):
        image, label = match.groups()
        name = label.split("|")[0]
        if not Path(name).suffix:
            name = name.split("#")[0]
        resolved = doc["references"].get(name)
        caption = label.split("|")[-1]
        if resolved in inventory["images"]:
            r = inventory["images"][resolved]
            return f"{image}[{caption}](../attachments/{r['sha256']}{r['suffix']})"
        if resolved in mapping:
            return f"[{caption}](../{mapping[resolved]}/knowledge.md)"
        return match.group(0)

    draft = WIKI.sub(wiki, raw)
    attached = []
    for path in dict.fromkeys(doc["references"].values()):
        if path not in inventory["images"]:
            continue
        record = inventory["images"][path]
        attached.append(record)
        evidence = ocr_text(ocr[record["sha256"]])
        if evidence:
            draft += f"\n\n## 图片文字识别：{Path(path).name}\n\n" + evidence + "\n"
    peers = [d for d in inventory["documents"] if d["id"] == doc["id"]]
    metadata = {
        "source_paths": [d["relative_path"] for d in peers],
        "source_hashes": [d["sha256"] for d in peers],
        "attachments": attached,
        "summary_method": "source_verified_extractive",
        "legacy_collections": sorted({d["relative_path"].split("/")[0] for d in peers}),
        "source_urls": list(doc["sources"].values()),
    }
    return register_document(
        config, video, doc["id"], draft, doc["title"], doc["title"], metadata
    )


def import_single_document(config, video, path):
    path = Path(path).expanduser().resolve()
    if not path.is_relative_to(knowledge_root(config).resolve()):
        raise ConfigError("Document must be within the unified knowledge root.")
    draft = path.read_text(encoding="utf-8")
    ident = str(uuid5(NAMESPACE_URL, "rag-favorite:document:" + text_hash(draft)))
    register_document(
        config,
        video,
        ident,
        draft,
        path.stem,
        path.name,
        {"source_paths": [path.name], "summary_method": "source_verified_extractive"},
    )
    generation = active_generation(config, video)
    queue_summary(config, video, generation, ident)
    publish_summary(
        config, video, generation, ident, extractive_summary(draft, path.stem)
    )
    return True


def wait_idle(config, video):
    deadline = time.monotonic() + 1800
    while True:
        with connect_database(config, register_pgvector=False) as c:
            count = c.execute(
                "SELECT count(*) FROM public.rag_video_jobs WHERE library_id=%s AND state IN ('running','published')",
                (library_id(video),),
            ).fetchone()[0]
            summary = c.execute(
                "SELECT count(*) FROM public.rag_summary_jobs WHERE library_id=%s AND state='running'",
                (library_id(video),),
            ).fetchone()[0]
        if not count and not summary:
            return
        if time.monotonic() > deadline:
            raise ConfigError("In-flight jobs did not finish; cutover deferred.")
        print(
            json.dumps({"stage": "wait_inflight", "video": count, "summary": summary}),
            flush=True,
        )
        time.sleep(10)


def migrate(config, video, source, run, *, apply=False):
    run.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_path = run / "report.json"
    manifest_path = run / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else inspect_legacy(source)
    )
    atomic_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    if not apply:
        return {
            k: manifest[k]
            for k in ["unique_documents", "unique_sources", "unresolved_references"]
        }
    if manifest["unresolved_references"]:
        raise ConfigError("Unresolved legacy links; inspect manifest before applying.")
    stage = replace(
        config,
        unified=True,
        collections={
            "general": CollectionConfig(
                "general", "知识库", video.root.parent / "knowledge"
            )
        },
    )
    generation = "unified-20261010"
    lid = library_id(video)
    baseline = run / "queue-baseline.json"
    if not baseline.exists():
        atomic_text(baseline, json.dumps({"paused": read_control(video).paused}))
    original_pause = json.loads(baseline.read_text())["paused"]
    report = {
        "stage": "preparing",
        "generation": generation,
        "legacy_documents": manifest["unique_documents"],
        "failures": [],
    }

    def save():
        atomic_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)

    set_control(video, paused=True)
    try:
        wait_idle(config, video)
        MigrationRunner(config).apply()
        create_generation(stage, video, generation)
        report["stage"] = "copy_and_ocr"
        save()
        ocr = {}
        for n, (path, record) in enumerate(manifest["images"].items(), 1):
            if record["sha256"] not in ocr:
                ocr[record["sha256"]] = ocr_image(stage, path, record)
            if n % 25 == 0:
                report["images_processed"] = n
                save()
        expected = []
        seen = set()
        report["images_processed"] = len(manifest["images"])
        report["stage"] = "legacy_summaries"
        save()
        for doc in manifest["documents"]:
            if doc["id"] in seen:
                continue
            seen.add(doc["id"])
            ident = materialize_document(stage, video, doc, manifest, ocr)
            with connect_database(stage) as c:
                draft = c.execute(
                    "SELECT draft FROM public.rag_knowledge_documents WHERE id=%s",
                    (ident,),
                ).fetchone()[0]
            queue_summary(stage, video, generation, ident)
            try:
                summary = extractive_summary(draft, doc["title"])
            except ConfigError as exc:
                with connect_database(stage) as c:
                    c.execute(
                        "UPDATE public.rag_document_summary_jobs SET state='unsearchable',error_code=%s WHERE library_id=%s AND generation=%s AND document_id=%s",
                        (str(exc), lid, generation, ident),
                    )
                report["failures"].append({"id": ident, "reason": str(exc)})
                continue
            publish_summary(stage, video, generation, ident, summary)
            expected.append(ident)
            if len(expected) % 25 == 0:
                report["legacy_summaries_published"] = len(expected)
                save()
        for path in manifest["unreferenced_images"]:
            record = manifest["images"][path]
            ident = str(uuid5(NAMESPACE_URL, "rag-favorite:image:" + record["sha256"]))
            text = ocr_text(ocr[record["sha256"]])
            title = Path(path).stem
            register_document(
                stage,
                video,
                ident,
                "# " + title + "\n\n" + text + "\n",
                title,
                Path(path).name,
                {"attachments": [record], "summary_method": "ocr_extract"},
                source_kind="image",
            )
            queue_summary(stage, video, generation, ident)
            if not text:
                with connect_database(stage) as c:
                    c.execute(
                        "UPDATE public.rag_document_summary_jobs SET state='unsearchable',error_code='OCR_NO_TEXT' WHERE library_id=%s AND generation=%s AND document_id=%s",
                        (lid, generation, ident),
                    )
                report["failures"].append({"id": ident, "reason": "OCR_NO_TEXT"})
                continue
            publish_summary(
                stage, video, generation, ident, "# " + title + "\n\n" + text + "\n"
            )
            expected.append(ident)
        report["stage"] = "current_summaries"
        save()
        with connect_database(stage) as c:
            ids = c.execute(
                "SELECT s.job_id FROM public.rag_knowledge_summaries s JOIN public.rag_video_jobs j ON j.id=s.job_id "
                "LEFT JOIN public.rag_videos v ON v.id=j.id WHERE s.library_id=%s AND j.state='complete' AND (v.id IS NULL OR v.source_deleted)",
                (lid,),
            ).fetchall()
        for n, (ident,) in enumerate(ids, 1):
            sync_video_summary(stage, video, str(ident), generation=generation)
            expected.append(str(ident))
            if n % 25 == 0:
                report["current_summaries_published"] = n
                save()
        # Preserve drafts from unfinished/failed media jobs as well, without publishing them.
        from .unified_library import materialize_media_draft

        with connect_database(stage) as c:
            jobs = c.execute(
                "SELECT id,payload,collection,state FROM public.rag_video_jobs WHERE library_id=%s",
                (lid,),
            ).fetchall()
        preserved = 0
        published_ids = {str(r[0]) for r in ids}
        for job_id, payload, collection, state in jobs:
            if str(job_id) in published_ids:
                continue
            path = video.root / "derived" / str(job_id) / "knowledge.md"
            if not path.is_file():
                continue
            draft = path.read_text(encoding="utf-8")
            draft, attachments = materialize_media_draft(stage, video, job_id, draft)
            register_document(
                stage,
                video,
                job_id,
                draft,
                payload.get("title") or "知识记录",
                payload.get("source_label")
                or payload.get("source_url")
                or "media source",
                {
                    "legacy_collection": collection,
                    "source_job_id": str(job_id),
                    "source_state": state,
                    "attachments": attachments,
                },
                source_kind="media",
                source_job_id=job_id,
            )
            preserved += 1
        report["unpublished_current_drafts_preserved"] = preserved
        report["activation"] = activate_generation(stage, video, generation, expected)
        # Config is switched only after a complete source-verified generation exists.
        old = config.source.read_text()
        new = old.split("[[collections]]")[0]
        # Idempotently remove a previous knowledge table before writing the new one.
        new = re.sub(r"(?ms)^\[knowledge\]\n.*?(?=^\[|\Z)", "", new)
        new += '\n[[collections]]\nkey="general"\nname="知识库"\npath="../videorag/knowledge"\n\n[knowledge]\nunified=true\nvideo_config="../videorag/video.toml"\n'
        atomic_text(config.source, new)
        from .migration_acceptance import verify

        report["stage"] = "mcp_acceptance"
        save()
        report["mcp_acceptance"] = verify(stage, video)
        atomic_text(
            run / "mcp-acceptance.json",
            json.dumps(report["mcp_acceptance"], ensure_ascii=False, indent=2),
        )
        # Keep the favorites cursor when its historical storage alias changes.
        from .xhs_favorites import state_path

        old_cursor = state_path(video, video.default_collection)
        new_cursor = state_path(video, "general")
        if old_cursor.exists() and not new_cursor.exists():
            shutil.copy2(old_cursor, new_cursor)
        sources = defaultdict(lambda: {"url": None, "documents": []})
        for doc in manifest["documents"]:
            for key, url in doc["sources"].items():
                sources[key]["url"] = url
                sources[key]["documents"].append(doc["id"])
        report["stage"] = "enqueue_reprocessing"
        save()
        for n, item in enumerate(sources.values(), 1):
            result = enqueue_reprocess(
                stage, video, item["url"], generation, item["documents"]
            )
            for doc in manifest["documents"]:
                if doc["id"] in item["documents"]:
                    doc["reprocess_job_id"] = result["job_id"]
                    doc["state"] = "reprocess_queued"
            if n % 100 == 0:
                report["sources_enqueued"] = n
                save()
        report["sources_enqueued"] = len(sources)
        report["stage"] = "activated"
        report["current_summaries_published"] = len(ids)
        report["legacy_summaries_published"] = len(seen) - sum(
            1 for f in report["failures"] if f["reason"] != "OCR_NO_TEXT"
        )
        unsearchable = {item["id"]: item["reason"] for item in report["failures"]}
        for doc in manifest["documents"]:
            if doc["state"] == "pending":
                doc["state"] = (
                    "unsearchable" if doc["id"] in unsearchable else "summary_published"
                )
            if doc["id"] in unsearchable:
                doc["summary_error"] = unsearchable[doc["id"]]
        atomic_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
        from .summary_worker import enqueue_summaries

        report["current_summary_retries_queued"] = enqueue_summaries(
            stage, video, retry_failed=True
        )
        report["summary_strategy"] = (
            "source_verified_extractive_for_legacy; existing_summaries_reused_for_current"
        )
        save()
        import subprocess

        migration_pauses = run / "migration-paused-jobs.json"
        if migration_pauses.exists():
            from .queue_control import _write_control, locked_control

            with locked_control(video) as control:
                removed = set(json.loads(migration_pauses.read_text()))
                _write_control(
                    video,
                    replace(
                        control,
                        paused_jobs=tuple(sorted(set(control.paused_jobs) - removed)),
                    ),
                )
            migration_pauses.unlink()
        for service in [
            "rag-favorite-video-worker.service",
            "rag-favorite-summary-worker.service",
            "rag-favorite-web.service",
        ]:
            result = subprocess.run(
                ["systemctl", "--user", "try-restart", service],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise ConfigError("Service reload failed: " + service)
        return report
    except Exception as exc:
        snapshot = run / "before" / ".runtime/phase1/config.toml"
        if snapshot.exists():
            atomic_text(config.source, snapshot.read_text())
        # A rollback must not let the old profile claim new unified-storage jobs.
        from .queue_control import _write_control, locked_control

        try:
            with connect_database(stage) as c:
                present = c.execute(
                    "SELECT to_regclass('public.rag_legacy_reprocess')"
                ).fetchone()[0]
                new_jobs = (
                    c.execute(
                        "SELECT job_id FROM public.rag_legacy_reprocess WHERE library_id=%s AND migration_id=%s",
                        (lid, generation),
                    ).fetchall()
                    if present
                    else []
                )
            ids_to_pause = {str(row[0]) for row in new_jobs}
            if ids_to_pause:
                with locked_control(video) as control:
                    _write_control(
                        video,
                        replace(
                            control,
                            paused_jobs=tuple(
                                sorted(set(control.paused_jobs) | ids_to_pause)
                            ),
                        ),
                    )
                atomic_text(
                    run / "migration-paused-jobs.json", json.dumps(sorted(ids_to_pause))
                )
        except Exception:  # noqa: BLE001 - keep claims paused if rollback isolation fails
            original_pause = True
            report["rollback_queue_isolation_failed"] = True
        report["stage"] = "failed"
        report["error"] = type(exc).__name__ + ": " + str(exc)[:300]
        save()
        import subprocess

        for service in [
            "rag-favorite-video-worker.service",
            "rag-favorite-summary-worker.service",
            "rag-favorite-web.service",
        ]:
            subprocess.run(
                ["systemctl", "--user", "try-restart", service],
                capture_output=True,
                check=False,
            )
        raise
    finally:
        set_control(video, paused=original_pause)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument(
        "--run", type=Path, default=Path(".runtime/migrations/unified-20261010")
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    import signal

    def interrupted(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    import fcntl

    args.run.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.run / "migration.lock").open("a") as guard:
        os.fchmod(guard.fileno(), 0o600)
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigError(
                "This migration is already running; inspect report.json."
            ) from exc
        config, video = load_config(), load_video_config()
        report_path = args.run / "report.json"
        previous = json.loads(report_path.read_text()) if report_path.exists() else {}
        if args.apply and config.unified and previous.get("stage") == "activated":
            result = previous
        else:
            result = migrate(config, video, args.source, args.run, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
