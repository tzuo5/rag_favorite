"""Backup the isolated MVP and verify restore in a new disposable database."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from psycopg import sql

from rag_favorite.config import database_credentials, load_config
from rag_favorite.database import connect_database
from rag_favorite.video_config import load_video_config
from rag_favorite.video_retrieval import evidence_record, search_video_knowledge
from rag_favorite.video_store import library_id


def main():
    config, video = load_config(), load_video_config()
    credentials = database_credentials(config)
    if not credentials["name"].startswith("rag_phase1"):
        raise RuntimeError(
            "This restore demonstration is restricted to the isolated MVP."
        )
    repo = Path(__file__).resolve().parents[1]
    root = repo / ".runtime/videorag/backups" / uuid4().hex
    root.mkdir(parents=True, mode=0o700)
    binary = repo / ".runtime/phase1/postgres/usr/lib/postgresql/18/bin"
    env = dict(os.environ)
    env.update(
        {
            "PGHOST": credentials["host"],
            "PGPORT": credentials["port"],
            "PGUSER": credentials["user"],
            "PGPASSWORD": credentials["password"],
            "PGDATABASE": credentials["name"],
            "LD_LIBRARY_PATH": str(
                repo / ".runtime/phase1/postgres/usr/lib/x86_64-linux-gnu"
            ),
        }
    )
    tables = ("rag_videos", "rag_video_segments", "rag_video_edges", "rag_video_jobs")
    with connect_database(config, register_pgvector=False) as snapshot:
        snapshot.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot_id = snapshot.execute("SELECT pg_export_snapshot()").fetchone()[0]
        original_counts = {
            table: snapshot.execute(
                sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(table))
            ).fetchone()[0]
            for table in tables
        }
        videos = snapshot.execute(
            "SELECT id FROM public.rag_videos WHERE library_id=%s", (library_id(video),)
        ).fetchall()
        subprocess.run(
            [
                str(binary / "pg_dump"),
                "-Fc",
                "--snapshot",
                snapshot_id,
                "--file",
                str(root / "database.dump"),
            ],
            env=env,
            check=True,
            capture_output=True,
        )
        (root / "database.dump").chmod(0o600)
        with tarfile.open(root / "derived-knowledge.tar.gz", "w:gz") as archive:
            for row in videos:
                path = video.root / "derived" / str(row[0])
                archive.add(path, arcname="derived/" + str(row[0]))
    (root / "derived-knowledge.tar.gz").chmod(0o600)
    name = "rag_phase1_restore_" + uuid4().hex[:12]
    created = False
    try:
        with connect_database(config, register_pgvector=False) as owner:
            owner.autocommit = True
            owner.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
        env["PGDATABASE"] = name
        subprocess.run(
            [
                str(binary / "pg_restore"),
                "--exit-on-error",
                "--dbname",
                name,
                str(root / "database.dump"),
            ],
            env=env,
            check=True,
            capture_output=True,
        )
        secret = root / "restore.env"
        secret.touch(mode=0o600)
        secret.write_text("RAG_DATABASE_PASSWORD=" + credentials["password"] + "\n")
        restored = replace(
            config,
            database=replace(config.database, name=name, credentials_file=secret),
        )
        counts = {"original": original_counts}
        with connect_database(restored) as c:
            counts["restored"] = {
                table: c.execute(
                    sql.SQL("SELECT count(*) FROM public.{}").format(
                        sql.Identifier(table)
                    )
                ).fetchone()[0]
                for table in tables
            }
        assert counts["original"] == counts["restored"]
        restored_root = root / "restored-knowledge"
        restored_root.mkdir(mode=0o700)
        with tarfile.open(root / "derived-knowledge.tar.gz") as archive:
            archive.extractall(restored_root, filter="data")
        restored_video = replace(video, root=restored_root)
        # A restore into another directory needs a new library identity, only in
        # the disposable restored database. The original database is untouched.
        with connect_database(restored) as c:
            for table in ("rag_videos", "rag_video_jobs"):
                c.execute(
                    sql.SQL(
                        "UPDATE public.{} SET library_id=%s WHERE library_id=%s"
                    ).format(sql.Identifier(table)),
                    (library_id(restored_video), library_id(video)),
                )
        rows = search_video_knowledge(
            "芥末虾球最后加入什么水果？", ["cooking"], 3, restored, restored_video
        )
        assert rows and all(row["metadata"]["source_deleted"] for row in rows)
        images = [e for row in rows for e in row["metadata"].get("evidence_ids", [])]
        assert images
        _, evidence = evidence_record(
            images[0], "cooking", restored, restored_video
        )
        assert evidence.is_relative_to(restored_root)
        result = {
            "backup_directory": str(root.relative_to(repo)),
            "database_dump_bytes": (root / "database.dump").stat().st_size,
            "derived_archive_bytes": (root / "derived-knowledge.tar.gz").stat().st_size,
            "counts": counts,
            "restored_retrieval_results": len(rows),
            "restored_image_verified": True,
            "database_consistency": "exported PG snapshot",
            "original_media_in_archive": False,
            "scratch_database_removed": True,
            "scope": "isolated MVP; production migration remains pending",
        }
        Path("docs/implementation/video-backup-restore-results.json").write_text(
            json.dumps(result, indent=2)
        )
        print(json.dumps(result, indent=2))
    finally:
        if created:
            with connect_database(config, register_pgvector=False) as owner:
                owner.autocommit = True
                owner.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )
        (root / "restore.env").unlink(missing_ok=True)
        shutil.rmtree(root / "restored-knowledge", ignore_errors=True)


if __name__ == "__main__":
    main()
