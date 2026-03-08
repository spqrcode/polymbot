"""Structured audit log for orders, fills, and cancellations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, report_dir: Path, run_id: str, mode_label: str):
        self.report_dir = report_dir
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.mode_label = mode_label
        self.audit_path = self.report_dir / "order_audit.jsonl"

    def record(self, event_type: str, **payload: Any) -> None:
        event = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "mode": self.mode_label,
            "event_type": event_type,
            **payload,
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
