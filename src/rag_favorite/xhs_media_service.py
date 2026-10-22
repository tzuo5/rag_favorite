"""Download and checkpoint one complete note while the session owns the route."""

import re
import time

from .config import ConfigError
from .video_sources import download_note_video
from .video_store import digest_file
from .xhs_images import download_image
from .xhs_private import atomic_private_json, private_directory, private_json
from .xhs_session_policy import failure


class MediaBundle:
    def __init__(self, video, note, job_id):
        if not re.fullmatch(
            r"(?:[a-f0-9-]{36}|note-[a-f0-9]{24})", str(job_id)
        ) or not re.fullmatch(r"[a-f0-9]{24}", str(note.get("note_id", ""))):
            raise ConfigError("Invalid XHS media identity.")
        self.video, self.note = video, note
        self.root = video.root / "derived" / job_id
        private_directory(self.root)
        private_directory(self.root / "images")
        self.path = self.root / "download-bundle.json"
        self.data = (
            private_json(self.path)
            if self.path.exists()
            else {
                "note_id": note["note_id"],
                "video": None,
                "live": {},
                "downloaded": False,
            }
        )
        if self.data["note_id"] != note["note_id"]:
            raise ConfigError("XHS download identity changed.")
        self.image_path = self.root / "image-note.json"
        self.images = (
            private_json(self.image_path)
            if self.image_path.exists()
            else {"images": {}}
        )
        self.is_video = bool(note.get("media_url"))

    def valid_asset(self, entry):
        if not isinstance(entry, dict) or not re.fullmatch(
            r"[a-f0-9]{32}", entry.get("asset_id", "")
        ):
            return False
        return self.valid_file(self.video.root / "assets" / entry["asset_id"], entry)

    @staticmethod
    def valid_file(path, entry):
        return (
            not path.is_symlink()
            and path.is_file()
            and path.stat().st_size == entry.get("bytes")
            and digest_file(path) == entry.get("sha256")
        )

    def image_file(self, image):
        index = image["image_index"]
        if type(index) is not int or not 0 <= index < 1000:
            raise ConfigError("Invalid XHS image index.")
        return self.root / "images" / f"{index:04d}.jpg"

    def ready(self):
        if not self.data.get("complete"):
            return False
        if self.is_video:
            return self.valid_asset(self.data["video"])
        for item in self.note.get("images", []):
            key = str(item["image_index"])
            if not self.valid_file(
                self.image_file(item), self.images["images"].get(key, {})
            ):
                return False
            if item.get("live_video_url") and not self.valid_asset(
                self.data["live"].get(key)
            ):
                return False
        return True

    def save(self):
        atomic_private_json(self.path, self.data)

    def count_request(self):
        self.data["media_http_requests"] = self.data.get("media_http_requests", 0) + 1
        self.save()

    def download(self, proxy, generation):
        if self.is_video:
            if not self.valid_asset(self.data["video"]):
                self.data["video"] = download_note_video(
                    self.video, self.note, proxy=proxy, on_request=self.count_request
                )
                self.data["downloaded"] = True
                self.save()
        else:
            for item in self.note.get("images", []):
                key = str(item["image_index"])
                path = self.image_file(item)
                entry = self.images["images"].get(key, {})
                if not self.valid_file(path, entry):
                    result = download_image(
                        item["url"],
                        path,
                        limit=min(self.video.max_asset_bytes, 20_000_000),
                        proxy=proxy,
                        on_request=self.count_request,
                    )
                    self.images["images"][key] = {
                        **result,
                        "image_index": item["image_index"],
                        "relative_path": str(path.relative_to(self.video.root)),
                        "live_photo": item.get("live_photo", False),
                    }
                    atomic_private_json(self.image_path, self.images)
                    self.data["downloaded"] = True
                    self.save()
                if item.get("live_video_url") and not self.valid_asset(
                    self.data["live"].get(key)
                ):
                    self.data["live"][key] = download_note_video(
                        self.video,
                        {
                            "note_id": self.note["note_id"],
                            "media_url": item["live_video_url"],
                        },
                        proxy=proxy,
                        on_request=self.count_request,
                    )
                    self.data["downloaded"] = True
                    self.save()
        self.data.update(complete=True, generation=generation, completed_at=time.time())
        if not self.ready():
            self.data["complete"] = False
            self.save()
            raise failure("SOURCE_DOWNLOAD_INCOMPLETE")
        self.save()
        return self.data
