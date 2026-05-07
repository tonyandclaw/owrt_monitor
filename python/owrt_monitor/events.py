from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from owrt_monitor.storage import JobStore


class EventLogger:
    def __init__(self, *, store: JobStore, job_id: str, path: Path) -> None:
        self.store = store
        self.job_id = job_id
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        *,
        level: str,
        component: str,
        event: str,
        message: str,
        fields: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "job_id": self.job_id,
            "level": level,
            "component": component,
            "event": event,
            "message": message,
            "fields": fields or {},
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, sort_keys=True) + "\n")
        self.store.record_event(
            job_id=self.job_id,
            level=level,
            component=component,
            event=event,
            message=message,
            fields=fields or {},
            ts=payload["ts"],
        )
