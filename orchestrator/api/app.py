"""
FastAPI application factory for the DCS orchestrator.

Usage:
    from orchestrator.api.app import create_app
    from orchestrator.config import load_config
    app = create_app(load_config())
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..agent_client import AgentClient
from ..config import OrchestratorConfig
from ..database import Database
from ..events import Event, EventBus
from ..jobs import JobStore
from .auth import require_api_key
from .models import Problem
from .routes import health as health_routes
from .routes import hosts as hosts_routes
from .routes import instances as instances_routes
from .routes import actions as actions_routes
from .routes import jobs as jobs_routes
from .routes import events as events_routes
from .routes import analytics as analytics_routes
from .routes import bench as bench_routes
from .routes import registration as registration_routes

logger = logging.getLogger(__name__)

_AUTH_DEP = [Depends(require_api_key)]
_STATUS_POLL_INTERVAL = 30.0  # seconds between agent status polls


async def _status_poller(app: FastAPI) -> None:
    """
    Background task: poll all known agents every _STATUS_POLL_INTERVAL seconds.
    Publishes instance.status_changed when a status transition is detected.
    """
    last_known: dict[str, str] = {}  # instance_id → last status string
    while True:
        await asyncio.sleep(_STATUS_POLL_INTERVAL)
        db: Database = app.state.db
        bus: EventBus = app.state.event_bus
        try:
            rows = await db.list_instances()
        except Exception:
            continue

        for row in rows:
            inst_id: str = row["id"]
            try:
                host_row = await db.get_host(row["host_id"])
                if not host_row:
                    continue
                agent_base = host_row["agent_url"].rstrip("/") + "/agent/v1"
                async with AgentClient(agent_base, host_row["agent_api_key"]) as client:
                    status_data = await client.get_instance_status(row["service_name"])
                status = status_data.get("status", "unknown")
            except Exception:
                status = "unknown"

            prev = last_known.get(inst_id)
            if prev is not None and prev != status:
                bus.publish(
                    Event(
                        type="instance.status_changed",
                        instance_id=inst_id,
                        host_id=row["host_id"],
                        data={
                            "instanceId": inst_id,
                            "name": row.get("name", inst_id),
                            "status": status,
                            "previousStatus": prev,
                        },
                    )
                )
            last_known[inst_id] = status


def create_app(config: OrchestratorConfig) -> FastAPI:
    db = Database(config.db_path)
    job_store = JobStore()
    event_bus = EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        auth_status = "DISABLED (dev mode)" if not config.api_key else "enabled"
        logger.info(
            "DCS Orchestrator starting — db: %s | auth: %s | listen: %s:%s",
            config.db_path,
            auth_status,
            config.host,
            config.port,
        )
        if not config.api_key:
            logger.warning(
                "WARNING: api_key is empty — authentication is disabled. "
                "Set 'api_key' in config before exposing this orchestrator on a network."
            )
        await db.connect()
        poller = asyncio.create_task(_status_poller(app))
        try:
            yield
        finally:
            poller.cancel()
            try:
                await poller
            except asyncio.CancelledError:
                pass
            await db.close()

    app = FastAPI(
        title="DCS Orchestrator",
        description="FastAPI orchestrator hub for DCS World server nodes",
        version="0.1.0",
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # Attach shared state
    app.state.config = config
    app.state.db = db
    app.state.job_store = job_store
    app.state.event_bus = event_bus

    # /health — no auth, no prefix
    app.include_router(health_routes.router)

    # All other endpoints — require auth, under /api/v1
    _v1_kwargs = dict(prefix="/api/v1", dependencies=_AUTH_DEP)
    app.include_router(hosts_routes.router, **_v1_kwargs)
    app.include_router(instances_routes.router, **_v1_kwargs)
    app.include_router(actions_routes.router, **_v1_kwargs)
    app.include_router(jobs_routes.router, **_v1_kwargs)
    app.include_router(events_routes.router, **_v1_kwargs)

    # Registration endpoints — /register is invite-code gated (no master key required)
    # /invites management requires admin key (enforced inside the router itself)
    app.include_router(registration_routes.router, prefix="/api/v1")

    # Analytics POST -- agent-key auth only (no master key); GET covered by _AUTH_DEP via separate include
    app.include_router(analytics_routes.router, prefix="/api/v1")

    # Bench -- POST is agent-key gated; GET is public (no auth, CORS open for GitHub Pages)
    app.include_router(bench_routes.router, prefix="/api/v1")

    # Installer static files — served unauthenticated from /install/
    # Contains agent.zip, install.ps1 (no secrets — secrets come from registration)
    import os

    _install_dir = os.environ.get("DCS_INSTALL_DIR", "/opt/dcs-platform/install")
    if os.path.isdir(_install_dir):
        app.mount("/install", StaticFiles(directory=_install_dir), name="install")

    # Global exception handlers → Problem JSON
    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=Problem(
                type="about:blank",
                title=_http_status_phrase(exc.status_code),
                status=exc.status_code,
                detail=exc.detail,
            ).model_dump(exclude_none=True),
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content=Problem(
                type="about:blank",
                title="Internal Server Error",
                status=500,
                detail=str(exc),
            ).model_dump(exclude_none=True),
        )

    return app


def _http_status_phrase(code: int) -> str:
    import http

    try:
        return http.HTTPStatus(code).phrase
    except ValueError:
        return "HTTP Error"
