from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .security import redact_sensitive_text


class JsonFormatter(logging.Formatter):
    FIELDS = ("job_id", "telegram_user_id", "stage", "platform", "source_id", "duration_ms", "error_code")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": redact_sensitive_text(record.getMessage()),
        }
        for field in self.FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = redact_sensitive_text(
                self.formatException(record.exc_info)
            )
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
