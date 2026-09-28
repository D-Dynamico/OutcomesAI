# Scriptable in-process generate_summary for tests (design section 7, test harness).
# Counts every call, records whether the input was a real transcript or the probe, and
# can be scripted per call to succeed, fail with a chosen error class, delay, or hang.
import hashlib
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

from app.summary.client import PROBE_TRANSCRIPT, SummaryClient, TransientError

Behaviour = str | TransientError | Callable[[str], str]


@dataclass(frozen=True)
class Call:
    kind: str            # 'real' or 'probe'
    transcription: str   # kept in memory for test assertions only; never logged


def default_summary(transcription: str, n: int) -> str:
    # Tied to the input so tests can tell which version was summarised; the call number
    # makes wording differ between calls on the same input (non-idempotent)
    digest = hashlib.sha256(transcription.encode("utf-8")).hexdigest()[:10]
    return f"summary-of-{digest} (call {n})"


class Gate:
    """Holds a call until released, e.g. to keep a worker mid-call past its lease."""

    def __init__(self, result: Behaviour | None = None):
        self.entered = threading.Event()
        self._release = threading.Event()
        self._result = result

    def release(self) -> None:
        self._release.set()

    def __call__(self, transcription: str) -> str:
        self.entered.set()
        if not self._release.wait(timeout=30):
            raise RuntimeError("gate never released")
        if isinstance(self._result, TransientError):
            raise self._result
        return self._result if isinstance(self._result, str) else "gated summary"


class ScriptedSummaryClient(SummaryClient):
    def __init__(self, default: Behaviour | None = None):
        self._lock = threading.Lock()
        self._script: deque[Behaviour] = deque()
        self._default = default
        self.calls: list[Call] = []

    def push(self, *behaviours: Behaviour) -> None:
        """Queue behaviours for the next calls, in order. After they run out, the default applies."""
        with self._lock:
            self._script.extend(behaviours)

    def set_default(self, behaviour: Behaviour | None) -> None:
        with self._lock:
            self._default = behaviour

    @property
    def real_calls(self) -> int:
        return sum(c.kind == "real" for c in self.calls)

    @property
    def probe_calls(self) -> int:
        return sum(c.kind == "probe" for c in self.calls)

    def generate_summary(self, transcription: str) -> str:
        with self._lock:
            kind = "probe" if transcription == PROBE_TRANSCRIPT else "real"
            self.calls.append(Call(kind, transcription))
            n = len(self.calls)
            behaviour = self._script.popleft() if self._script else self._default

        if behaviour is None:
            return default_summary(transcription, n)
        if isinstance(behaviour, TransientError):
            raise behaviour
        if callable(behaviour):
            return behaviour(transcription)
        return behaviour
