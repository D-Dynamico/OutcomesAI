# Test-only hooks at named points (design section 7, test harness). Inert unless
# TEST_HOOKS is set: fire() returns immediately and nothing registered is ever called.
#
# Named points in ingestion (design section 3):
#   ingest.after_lock     encounter row locked with FOR UPDATE; transaction still open
#   ingest.before_commit  job inserted; COMMIT not yet sent
#   ingest.after_commit   committed; HTTP response not yet sent
#
# CRASH_AT=<point> kills the process at that point with os._exit, simulating a crash.
import os
from collections import defaultdict
from typing import Callable


class Hooks:
    def __init__(self, enabled: bool, crash_at: str = ""):
        self.enabled = enabled
        self.crash_at = crash_at if enabled else ""
        self._callbacks: dict[str, list[Callable[..., None]]] = defaultdict(list)

    def register(self, point: str, callback: Callable[..., None]) -> None:
        self._callbacks[point].append(callback)

    def clear(self) -> None:
        self._callbacks.clear()

    def fire(self, point: str, **context) -> None:
        if not self.enabled:
            return
        if self.crash_at == point:
            os._exit(17)
        for callback in self._callbacks.get(point, ()):
            callback(**context)
