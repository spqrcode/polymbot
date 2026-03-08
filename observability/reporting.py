"""
Persistenza snapshot/report per sessioni lunghe di paper trading.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from observability.metrics import Metrics
from strategy.inventory import InventoryManager


@dataclass
class SessionReporter:
    report_dir: Path
    mode_label: str
    started_at: datetime
    target_end_at: Optional[datetime] = None
    run_config: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.run_history_path = self.report_dir / "run_history.jsonl"
        self.run_id = self.started_at.strftime("%Y%m%dT%H%M%S.%fZ")

    def write_snapshot(
        self,
        metrics: Metrics,
        inventory: InventoryManager,
        status: str,
        stop_reason: Optional[str] = None,
    ) -> None:
        if status != "starting":
            return
        self._append_payload(metrics, inventory, status, stop_reason)

    def write_final_summary(
        self,
        metrics: Metrics,
        inventory: InventoryManager,
        status: str,
        stop_reason: Optional[str] = None,
    ) -> None:
        self._append_payload(metrics, inventory, status, stop_reason)

    def _append_payload(
        self,
        metrics: Metrics,
        inventory: InventoryManager,
        status: str,
        stop_reason: Optional[str],
    ) -> None:
        payload = self._build_payload(metrics, inventory, status, stop_reason)
        with self.run_history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def _build_payload(
        self,
        metrics: Metrics,
        inventory: InventoryManager,
        status: str,
        stop_reason: Optional[str],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "run_id": self.run_id,
            "timestamp_utc": now.isoformat(),
            "mode": self.mode_label,
            "status": status,
            "stop_reason": stop_reason,
            "started_at_utc": self.started_at.isoformat(),
            "ended_at_utc": now.isoformat() if status in {"completed", "stopped"} else None,
            "target_end_at_utc": self.target_end_at.isoformat() if self.target_end_at else None,
            "run_config": self.run_config,
            "metrics": metrics.summary(),
            "positions_open": inventory.active_positions_count,
            "positions": inventory.get_display_data(),
        }


def compute_target_end(started_at: datetime, duration_hours: float) -> Optional[datetime]:
    if duration_hours <= 0:
        return None
    return started_at + timedelta(hours=duration_hours)
