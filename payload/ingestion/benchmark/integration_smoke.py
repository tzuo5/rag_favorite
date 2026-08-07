from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

from backend.ingestion.config import Settings
from backend.ingestion.models import Destination
from backend.ingestion.service import VideoIngestionService


class NullNotifier:
    def awaiting(self, *args, **kwargs): pass
    def completed(self, *args, **kwargs): pass
    def failed(self, *args, **kwargs): pass


def make_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    voice = path.with_suffix(".wav")
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "145", "-w", str(voice), "This is a Telegram video knowledge integration test for transcription, database storage, vector search, cleanup, and duplicate detection."], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=10", "-i", str(voice), "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
    voice.unlink(missing_ok=True)


async def run() -> dict:
    settings = Settings()
    service = VideoIngestionService(settings, notifier=NullNotifier())
    media = settings.root / "test-fixtures" / "telegram-native-smoke.mp4"
    make_fixture(media)
    jobs: list[str] = []
    paths: set[Path] = set()
    report: dict[str, object] = {}
    try:
        first = service.sql.create_job(user_id="1000000001", chat_id="test", message_id="1001", input_kind="media", input_value=str(media), media_type="video/mp4", caption="Telegram native integration smoke")
        jobs.append(str(first["id"])); await service.process(first)
        first = service.sql.get_job(str(first["id"])); assert first and first["state"] == "AWAITING_DESTINATION"
        assert Path(first["staging_path"]).exists()
        assert not (settings.temp_root / str(first["id"])).exists()
        assert service.sql.select_destination(str(first["id"]), "1000000001", Destination.MAIN) == "accepted"
        first = service.sql.get_job(str(first["id"])); await service.process(first)
        first = service.sql.get_job(str(first["id"])); assert first and first["state"] == "COMPLETED"
        main_path = service.destinations.target_path(Destination.MAIN, first["title"], str(first["document_id"])); paths.add(main_path)
        with service.sql.connection() as conn:
            main_row = conn.execute("SELECT d.id,count(c.id) AS chunks FROM rag_documents d JOIN rag_chunks c ON c.document_id=d.id WHERE d.source_path=%s GROUP BY d.id", (str(main_path),)).fetchone()
        assert main_row and main_row["chunks"] > 0
        report["telegram_native_to_main"] = {"state": first["state"], "chunks": main_row["chunks"], "media_cleaned": True}

        second = service.sql.create_job(user_id="1000000001", chat_id="test", message_id="1002", input_kind="media", input_value=str(media), media_type="video/mp4", caption="Duplicate smoke")
        jobs.append(str(second["id"])); await service.process(second)
        second = service.sql.get_job(str(second["id"])); assert second and second["state"] == "AWAITING_DESTINATION"
        assert second["transcript_checksum"] == first["transcript_checksum"]
        assert service.sql.select_destination(str(second["id"]), "1000000001", Destination.COOKING) == "accepted"
        second = service.sql.get_job(str(second["id"])); await service.process(second)
        second = service.sql.get_job(str(second["id"])); assert second and second["state"] == "COMPLETED"
        cooking_path = service.destinations.target_path(Destination.COOKING, second["title"], str(second["document_id"])); paths.add(cooking_path)
        with service.sql.connection() as conn:
            cooking_row = conn.execute(
                "SELECT d.id,count(c.id) AS chunks FROM rag_documents d "
                "JOIN rag_chunks c ON c.document_id=d.id "
                "WHERE d.source_path=%s AND d.knowledge_base='cooking' GROUP BY d.id",
                (str(cooking_path),),
            ).fetchone()
        assert cooking_row and cooking_row["chunks"] > 0
        report["duplicate_to_cooking"] = {"state": second["state"], "chunks": cooking_row["chunks"], "reused_transcript": True}

        third = service.sql.create_job(user_id="1000000001", chat_id="test", message_id="1003", input_kind="media", input_value=str(media), media_type="video/mp4", caption="Cancel smoke")
        jobs.append(str(third["id"])); await service.process(third)
        assert service.sql.select_destination(str(third["id"]), "1000000001", None) == "cancelled"
        assert service.sql.get_job(str(third["id"]))["state"] == "CANCELLED"
        report["cancel"] = True

        fourth = service.sql.create_job(user_id="1000000001", chat_id="test", message_id="1004", input_kind="media", input_value=str(media), media_type="video/mp4", caption="Vector failure smoke")
        jobs.append(str(fourth["id"])); await service.process(fourth)
        assert service.sql.select_destination(str(fourth["id"]), "1000000001", Destination.MAIN) == "accepted"
        original_ingest = service.destinations.vectors.ingest
        service.destinations.vectors.ingest = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated vector failure"))
        fourth = service.sql.get_job(str(fourth["id"])); await service.process(fourth)
        service.destinations.vectors.ingest = original_ingest
        fourth = service.sql.get_job(str(fourth["id"])); assert fourth["state"] == "PERSISTING" and fourth["retry_count"] == 1
        report["vector_failure_retry"] = True
        return report
    finally:
        with service.sql.connection() as conn:
            for path in paths:
                conn.execute("DELETE FROM rag_documents WHERE source_path=%s", (str(path),))
            if jobs:
                conn.execute("DELETE FROM video_knowledge_documents WHERE job_id=ANY(%s::uuid[])", (jobs,))
                conn.execute("DELETE FROM video_ingestion_jobs WHERE id=ANY(%s::uuid[])", (jobs,))
        for path in paths: path.unlink(missing_ok=True)
        for job_id in jobs:
            for staged in settings.staging_root.glob(f"*--{job_id[:8]}.md"):
                staged.unlink(missing_ok=True)
            shutil.rmtree(settings.temp_root / job_id, ignore_errors=True)
        media.unlink(missing_ok=True)


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False))
