from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from office_api import errors, migrations
from office_api.config import Settings
from office_api.db import create_engine, create_sessionmaker
from office_api.events import EventBroker
from office_api.routes import (
    agents,
    approvals,
    events,
    interventions,
    metrics,
    orchestrator,
    projects,
    work,
)
from office_api.runtime import Runtime
from office_api.storage import ContentStore

# The UI loads no third-party script; agent output is never rendered as HTML.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; connect-src 'self';"
        " img-src 'self' data: blob:; media-src 'self' blob:;"
        " style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;"
        " font-src https://fonts.gstatic.com; frame-ancestors 'none'; base-uri 'none';"
        " form-action 'self'"
    ),
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_engine(settings.database_url)
        async with engine.begin() as conn:
            await conn.run_sync(migrations.upgrade)  # Alembic, to head (ADR-0006)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.broker = EventBroker(settings.stream_queue_size)
        app.state.runtime = Runtime(
            settings, app.state.sessionmaker, app.state.broker, ContentStore(settings.data_dir)
        )
        await app.state.runtime.start()
        yield
        await app.state.runtime.stop()
        await engine.dispose()

    app = FastAPI(title="AI Project Office API", version="0.1.0", lifespan=lifespan)
    errors.install(app)
    app.include_router(projects.router)
    app.include_router(events.router)
    app.include_router(agents.router)
    app.include_router(orchestrator.router)
    app.include_router(work.router)
    app.include_router(interventions.router)
    app.include_router(approvals.router)
    app.include_router(metrics.router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)  # routes may set stricter ones
        return response

    @app.get("/healthz", tags=["meta"])
    async def healthz():
        """Liveness: the process answers."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["meta"])
    async def readyz():
        """Readiness: the database answers and the runtime's dispatcher is running."""
        try:
            async with app.state.sessionmaker() as s:
                await s.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "unavailable", "reason": "database"}, 503)
        if not app.state.runtime.running:
            return JSONResponse({"status": "unavailable", "reason": "runtime"}, 503)
        return {"status": "ready"}

    # Mounted last so API routes take precedence (ADR-0001).
    if settings.web_dir and Path(settings.web_dir).is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")

    return app
