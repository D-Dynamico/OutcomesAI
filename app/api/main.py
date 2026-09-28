# FastAPI app and routes. Ingestion is design section 3 (app/api/ingest.py), read is section 4
# (app/api/read.py), redrive is section 5 (app/api/admin.py).
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.admin import parse_window, redrive_job, redrive_window
from app.api.ingest import InvalidRequest, ingest, parse_event
from app.api.read import read_summary
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

    # :path so an encounter_id containing "/" (percent-encoded in Location) still routes
    @app.get("/encounters/{encounter_id:path}/summary")
    def get_summary(encounter_id: str):
        result = read_summary(app.state.pool, encounter_id, config.sla_seconds)
        return JSONResponse(result.body, status_code=result.status_code)

    # DECISIONS.md D4, D26: operator actions. No auth, as for every endpoint (design section 8).
    @app.post("/admin/jobs/redrive")
    async def post_redrive_window(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        try:
            failed_from, failed_to = parse_window(body)
        except ValueError as e:   # fixed messages only (admin.parse_window)
            return JSONResponse({"error": "invalid_request", "detail": str(e)}, status_code=400)
        result = await run_in_threadpool(redrive_window, app.state.pool, failed_from, failed_to)
        return JSONResponse(result.body, status_code=result.status_code)

    @app.post("/admin/jobs/{job_id}/redrive")
    def post_redrive_job(job_id: str):
        if not job_id.isdigit() or int(job_id) > 2**63 - 1:
            return JSONResponse({"error": "invalid_request", "detail": "job_id must be a positive integer"},
                                status_code=400)
        result = redrive_job(app.state.pool, int(job_id))
        return JSONResponse(result.body, status_code=result.status_code)

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
