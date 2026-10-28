from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from office_api import errors
from office_api.config import Settings
from office_api.db import Base, create_engine, create_sessionmaker
from office_api.events import EventBroker
from office_api.routes import agents, events, interventions, orchestrator, projects, work
from office_api.runtime import Runtime
from office_api.storage import ContentStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_engine(settings.database_url)
        # Phase 0: create tables directly; Alembic migrations come with Postgres (ADR-0002).
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
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

    @app.get("/healthz", tags=["meta"])
    async def healthz():
        return {"status": "ok"}

    # Mounted last so API routes take precedence (ADR-0001).
    if settings.web_dir and Path(settings.web_dir).is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")

    return app
