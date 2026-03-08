"""
Lock di processo per impedire istanze multiple del bot nello stesso workspace.
"""

from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TextIO


class ProcessLockError(RuntimeError):
    """Sollevato quando esiste gia' un'altra istanza attiva."""


@dataclass
class ProcessLock:
    path: Path
    _handle: Optional[TextIO] = field(default=None, init=False, repr=False)

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            holder = handle.read().strip() or "owner sconosciuto"
            handle.close()
            raise ProcessLockError(f"un'altra istanza del bot e' gia' attiva ({holder})") from exc

        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()

        self._handle = handle
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self._handle:
            return False

        try:
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.flush()
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

        return False
