"""Real-provider sampling proof using isolated synthetic sources, never owner media."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.video_config import load_video_config
from rag_favorite.video_provider import local_encoder
from rag_favorite.video_store import enqueue, job_status, library_id, stage_asset
from rag_favorite.video_worker import atomic_json, ffmpeg, process

REPO = Path(__file__).resolve().parents[1]


def main():
    config, owner = load_config(), load_video_config()
    for attempt in range(30):
        try:
            local_encoder(owner, "health", {})
            break
        except Exception:
            if attempt == 29:
                raise
            time.sleep(1)
    results = []
    proof_root = owner.root / "sampling-proof" / time.strftime("%Y%m%d-%H%M%S")
    cases = [
        ("silent", 2, False, 0.5, 4),
        ("subtitles", 2, True, 1.0, 2),
        ("over-600", 601, True, None, 0),
    ]
    with tempfile.TemporaryDirectory(dir=REPO / ".runtime") as tmp:
        tmp = Path(tmp)
        for label, duration, subtitles, interval, count in cases:
            video = replace(owner, root=proof_root / label)
            source = tmp / (label + ".mp4")
            ffmpeg(
                video,
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"testsrc2=size=320x180:rate={1 if duration > 600 else 8}:duration={duration}",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    str(source),
                ],
            )
            asset = stage_asset(source, video)
            transcript_id = None
            if subtitles:
                text = tmp / (label + ".srt")
                text.write_text(
                    "1\n00:00:00,000 --> 00:00:01,500\n这是本地抽帧验证，画面是彩色测试图。\n\n"
                )
                transcript_id = stage_asset(text, video)["asset_id"]
            submitted = enqueue(
                config,
                video,
                asset["asset_id"],
                video.default_collection,
                transcript_id,
                "抽帧验证 " + label,
            )
            status = job_status(config, video, submitted["job_id"])
            with connect_database(config) as connection:
                row = connection.execute(
                    "SELECT payload FROM public.rag_video_jobs WHERE id=%s AND library_id=%s",
                    (submitted["job_id"], library_id(video)),
                ).fetchone()
            if row is None:
                raise RuntimeError("Synthetic proof job was not persisted.")
            job = {
                "id": status["job_id"],
                "collection": status["collection"],
                "state": status["state"],
                "payload": row[0],
            }
            probe = local_encoder(
                video, "probe", {"path": str(video.root / "assets" / asset["asset_id"])}
            )
            process(config, video, job)
            final = job_status(config, video, submitted["job_id"])
            path = video.root / "derived" / submitted["job_id"]
            record = json.loads((path / "record.json").read_text())
            assert final["state"] == "complete"
            assert record["analysed_frame_count"] == count
            assert record["visual_policy"]["frame_interval_seconds"] == interval
            assert bool(record["visual_executed"]) == (count > 0)
            assert (
                sum(c["images"] for c in record["usage"]) >= count
                if count
                else not any(c["images"] for c in record["usage"])
            )
            assert not (video.root / "assets" / asset["asset_id"]).exists()
            assert not (video.root / "work" / submitted["job_id"]).exists()
            item = {
                "case": label,
                "synthetic_source": True,
                "real_models": True,
                "duration_seconds": probe["duration"],
                "has_audio": probe["has_audio"],
                "interval_seconds": interval,
                "analysed_frames": count,
                "state": final["state"],
                "visual_policy": record["visual_policy"],
                "visual_vectors": sum(
                    bool(s.get("video_embedding")) for s in record["segments"]
                ),
                "original_owned_media_removed": True,
                "markdown_path": str(path / "knowledge.md"),
            }
            results.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
            atomic_json(
                REPO / "docs/implementation/dense-sampling-live-results.json",
                {
                    "scope": "synthetic_sources_real_provider_calls_not_real_corpus_quality",
                    "cases": results,
                },
            )
    return 0


if __name__ == "__main__":
    os.environ.setdefault(
        "RAG_FAVORITE_CONFIG", str(REPO / ".runtime/phase1/config.toml")
    )
    os.environ.setdefault(
        "RAG_VIDEO_CONFIG", str(REPO / ".runtime/videorag/video.toml")
    )
    raise SystemExit(main())
