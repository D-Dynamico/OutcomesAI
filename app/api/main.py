# FastAPI app and routes. Ingestion is design section 3 (app/api/ingest.py); read (section 4)
# and redrive (section 5) are added in later milestones in their own modules.
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.ingest import InvalidRequest, ingest, parse_event
from app.config import Config
from app.db import apply_schema_url, create_pool
from app.hooks import Hooks

log = logging.getLogger("api")

EXPECTED_TABLES = ("encounters", "encounter_events", "summary_jobs", "job_attempts", "circuit_breaker")


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        apply_schema_url(config.database_url)
        app.state.pool = create_pool(config.database_url, config.db_pool_size)
        try:
            yield
        finally:
            app.state.pool.close()

    app = FastAPI(title="Encounter summaries", lifespan=lifespan)
    app.state.config = config
    app.state.hooks = Hooks(config.test_hooks, config.crash_at)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # Fixed body and exception type only: messages can carry request data (section 7)
        log.error("unhandled_error", extra={"error_type": type(exc).__name__, "path": request.url.path})
        return JSONResponse({"error": "internal"}, status_code=500)

    @app.post("/encounters/events")
    async def post_event(request: Request):
        # Section 3: size limit before parsing, then validation, then the transaction
        too_large = JSONResponse(
            {"error": "payload_too_large", "max_bytes": config.max_body_bytes}, status_code=413)
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > config.max_body_bytes:
            return too_large
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > config.max_body_bytes:
                return too_large

        try:
            event = parse_event(bytes(body))
        except InvalidRequest as e:
            return JSONResponse({"error": "invalid_request", "detail": e.detail}, status_code=400)

        result = await run_in_threadpool(ingest, app.state.pool, event, app.state.hooks)
        return JSONResponse(result.body, status_code=result.status_code, headers=result.headers)

    @app.get("/healthz")
    def healthz():
        try:
            with app.state.pool.connection(timeout=5) as conn:
                present = conn.execute(
                    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(%s)",
                    (list(EXPECTED_TABLES),),
                ).fetchone()[0]
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        if present != len(EXPECTED_TABLES):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    return app
