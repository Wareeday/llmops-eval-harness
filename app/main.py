"""FastAPI application factory: API, dashboard, metrics and lifecycle management."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.middleware import install_middleware
from app.api.routes import router as api_router
from app.api.routes import system as system_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.services.dataset import DatasetError
from app.services.runner import EvaluationRunner, NoCasesSelectedError, RunInProgressError
from app.services.target_adapter import TargetConfigError
from app.services.telemetry import Telemetry

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    configure_logging(cfg.log_level, cfg.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runner = EvaluationRunner(settings=cfg, telemetry=Telemetry())
        await runner.startup()
        app.state.runner = runner
        logger.info(
            "Started %s v%s (cases=%d, cache=%s, langfuse=%s, auth=%s)",
            cfg.app_name,
            __version__,
            len(runner.dataset),
            runner.cache.backend,
            runner.obs.langfuse_enabled,
            bool(cfg.api_key.get_secret_value()),
        )
        try:
            yield
        finally:
            await runner.aclose()
            logger.info("Stopped %s", cfg.app_name)

    app = FastAPI(
        title=cfg.app_name,
        version=__version__,
        description=(
            "Evaluate, red-team, cache and observe any AI agent. Bilingual (Arabic/English) "
            "benchmark, prompt-injection and PII zero-leak testing, Redis caching, Langfuse "
            "tracing, Prometheus metrics and a CI/CD quality gate."
        ),
        lifespan=lifespan,
    )
    app.state.settings = cfg

    install_middleware(app)
    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
        )

    @app.exception_handler(RunInProgressError)
    async def _busy(_: Request, exc: RunInProgressError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(NoCasesSelectedError)
    @app.exception_handler(TargetConfigError)
    async def _unprocessable(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(DatasetError)
    async def _bad_dataset(_: Request, exc: DatasetError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    app.include_router(system_router)
    app.include_router(api_router)

    if FRONTEND_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        async def serve_frontend() -> FileResponse:
            return FileResponse(FRONTEND_DIR / "index.html")

    return app


app = create_app()
