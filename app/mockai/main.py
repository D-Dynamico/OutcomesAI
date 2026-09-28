# Mock generate_summary provider, run as its own Compose service (DECISIONS.md D3).
# Milestone 1 only serves /healthz; latency, failures, outage mode and call counts
# arrive with the worker in milestone 4. Request bodies are never logged.
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Mock generate_summary")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
