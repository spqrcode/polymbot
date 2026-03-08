"""
Rate limiter per le chiamate API.
"""

import time
import threading


class RateLimiter:
    """Token bucket rate limiter thread-safe."""

    def __init__(self, max_per_second: float = 5.0):
        self.max_per_second = max_per_second
        self.min_interval = 1.0 / max_per_second
        self._last_call = 0.0
        self._lock = threading.Lock()

    def wait(self):
        """Aspetta se necessario per rispettare il rate limit."""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            if elapsed < self.min_interval:
                sleep_time = self.min_interval - elapsed
                time.sleep(sleep_time)
            self._last_call = time.time()

    def try_acquire(self) -> bool:
        """Prova ad acquisire un token senza aspettare."""
        with self._lock:
            now = time.time()
            if now - self._last_call >= self.min_interval:
                self._last_call = now
                return True
            return False
