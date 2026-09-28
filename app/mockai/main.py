# Mock generate_summary provider, run as its own Compose service (DECISIONS.md D3).
#
# Behaves like the brief's summary contract: 2-8 s typical latency with occasional calls
# over 10 s, transient failures (timeout, rate limit, unavailable), non-idempotent wording.
# Outage mode fails every call until switched off. Counts real and probe calls separately.
#
# Request bodies are never logged, and output is not derived from the transcript's content.
import asyncio
import os
import random
import threading
from dataclasses import asdict, dataclass, fields

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.logs import configure_logging
from app.summary.client import PROBE_TRANSCRIPT

PHRASES = (
    "Encounter reviewed; key concerns and plan documented.",
    "Visit summarised; follow-up actions noted.",
    "Conversation condensed; symptoms, history and next steps captured.",
    "Summary prepared; clinician review recommended.",
)


@dataclass
class MockSettings:
    latency_min_seconds: float = 2.0
    latency_max_seconds: float = 8.0
    slow_rate: float = 0.05           # share of calls that take slow_seconds instead (brief: "occasionally longer than 10 s")
    slow_seconds: float = 12.0
    failure_rate: float = 0.05        # share of calls that fail, split evenly across the three classes
    timeout_hang_seconds: float = 90.0  # an ai_timeout hangs past the worker's 30 s client timeout
    outage: bool = False

    @classmethod
    def from_env(cls) -> "MockSettings":
        values = {}
        for f in fields(cls):
            raw = os.environ.get("MOCK_" + f.name.upper())
            if raw is not None:
                values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on") if f.type is bool else float(raw)
        return cls(**values)

    def update(self, changes: dict) -> None:
        for f in fields(self):
            if f.name in changes:
                value = changes[f.name]
                setattr(self, f.name, bool(value) if f.type is bool else float(value))


class Counters:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.real = 0
        self.probe = 0
        self.results: dict[str, int] = {}

    def record_call(self, kind: str) -> int:
        with self._lock:
            setattr(self, kind, getattr(self, kind) + 1)
            return self.real + self.probe

    def record_result(self, result: str) -> None:
        with self._lock:
            self.results[result] = self.results.get(result, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"calls_total": self.real + self.probe, "real": self.real, "probe": self.probe,
                    "results": dict(self.results)}


def create_app(settings: MockSettings | None = None, rng: random.Random | None = None,
               sleep=asyncio.sleep) -> FastAPI:
    configure_logging("mock-ai")
    app = FastAPI(title="Mock generate_summary")
    app.state.settings = settings or MockSettings.from_env()
    app.state.counters = Counters()
    rng = rng or random.Random()

    def fail(error_class: str, status: int) -> JSONResponse:
        app.state.counters.record_result(error_class)
        return JSONResponse({"error": error_class}, status_code=status)

    @app.post("/generate_summary")
    async def generate_summary(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        transcription = body.get("transcription") if isinstance(body, dict) else None
        if not isinstance(transcription, str):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        kind = "probe" if transcription == PROBE_TRANSCRIPT else "real"
        n = app.state.counters.record_call(kind)
        s = app.state.settings

        if s.outage:
            return fail("ai_unavailable", 503)
        if rng.random() < s.failure_rate:
            error_class = rng.choice(("ai_timeout", "rate_limited", "ai_unavailable"))
            if error_class == "rate_limited":
                return fail("rate_limited", 429)
            if error_class == "ai_unavailable":
                return fail("ai_unavailable", 503)
            await sleep(s.timeout_hang_seconds)  # the client gives up first
            return fail("ai_timeout", 504)

        latency = s.slow_seconds if rng.random() < s.slow_rate \
            else rng.uniform(s.latency_min_seconds, s.latency_max_seconds)
        await sleep(latency)
        app.state.counters.record_result("succeeded")
        # Varies per call (non-idempotent); uses only the length, never the content
        words = len(transcription.split())
        return {"summary": f"{rng.choice(PHRASES)} Source length: {words} words. Ref {n}."}

    @app.get("/admin/stats")
    def stats():
        return app.state.counters.snapshot()

    @app.post("/admin/reset")
    def reset():
        app.state.counters.reset()
        return app.state.counters.snapshot()

    @app.get("/admin/settings")
    def get_settings():
        return asdict(app.state.settings)

    @app.post("/admin/settings")
    async def update_settings(request: Request):
        # Partial update, e.g. {"outage": true} to start the 20-minute outage demo
        app.state.settings.update(await request.json())
        return asdict(app.state.settings)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
