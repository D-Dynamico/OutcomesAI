# FastAPI app and routes. Ingestion (section 3), read (section 4) and redrive (section 5)
# are added in later milestones in their own modules.
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import Config
from app.db import apply_schema_url, create_pool

EXPECTED_TABLES = ("encounters", "encounter_events", "summary_jobs", "job_attempts", "circuit_breaker")


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        apply_schema_url(config.database_url)
        app.state.config = config
        app.state.pool = create_pool(config.database_url, config.db_pool_size)
        try:
            yield
        finally:
            app.state.pool.close()

    app = FastAPI(title="Encounter summaries", lifespan=lifespan)

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
