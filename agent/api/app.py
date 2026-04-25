"""
FastAPI application factory for the DCS agent.

Usage:
    from agent.api.app import create_app
    from agent.config import load_config
    app = create_app(load_config())
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..analytics import run_reporter
from ..scheduler import run_scheduler
from ..config import AgentConfig
from ..controller import DcsController
from ..jobs import JobStore
from .auth import NonceStore, require_api_key
from .models import Problem
from .routes import health as health_routes
from .routes import capabilities as capabilities_routes
from .routes import instances as instances_routes
from .routes import actions as actions_routes
from .routes import bench as bench_routes
from .routes import jobs as jobs_routes
from .routes import schedule as schedule_routes

logger = logging.getLogger(__name__)

_AUTH_DEP = [Depends(require_api_key)]


def create_app(config: AgentConfig) -> FastAPI:
    app = FastAPI(
        title="DCS Agent",
        description="FastAPI agent for Windows DCS server nodes",
        version="0.1.0",
        docs_url="/agent/v1/docs",
        redoc_url="/agent/v1/redoc",
        openapi_url="/agent/v1/openapi.json",
    )

    # Attach shared state
    app.state.config = config
    app.state.controller = DcsController(config)
    app.state.job_store = JobStore()
    app.state.nonce_store = NonceStore()
    app.state.bench_monitor = {"proc": None, "service_name": None}

    # /health — no auth, no prefix
    app.include_router(health_routes.router)

    # All other endpoints — require auth, under /agent/v1
    _v1_kwargs = dict(prefix="/agent/v1", dependencies=_AUTH_DEP)
    app.include_router(capabilities_routes.router, **_v1_kwargs)
    app.include_router(instances_routes.router, **_v1_kwargs)
    app.include_router(actions_routes.router, **_v1_kwargs)
    app.include_router(bench_routes.router, **_v1_kwargs)
    app.include_router(jobs_routes.router, **_v1_kwargs)
    app.include_router(schedule_routes.router, **_v1_kwargs)

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

    @app.on_event("startup")
    async def _startup() -> None:
        instance_names = [i.service_name for i in config.instances]
        auth_status = "DISABLED (dev mode)" if not config.api_key else "enabled"
        logger.info(
            "DCS Agent starting — instances: %s | auth: %s | listen: %s:%s",
            instance_names,
            auth_status,
            config.host,
            config.port,
        )
        if not config.api_key:
            logger.warning(
                "WARNING: api_key is empty — authentication is disabled. "
                "Set 'api_key' in config before exposing this agent on a network."
            )

        # Auto-start any instances flagged with auto_start=True that aren't already running
        auto_instances = [i for i in config.instances if i.auto_start]
        if auto_instances:
            asyncio.create_task(
                _auto_start_instances(app.state.controller, auto_instances)
            )

        # Analytics reporter -- push player/mission events to orchestrator
        asyncio.create_task(run_reporter(config, app.state.controller))

        # Mission scheduler -- idle restarts, rotation, open/close windows
        asyncio.create_task(run_scheduler(config, app.state.controller))

    async def _auto_start_instances(ctrl: DcsController, instances: list) -> None:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(5)  # brief delay so agent is fully up before starting tasks
        for inst in instances:
            try:
                status = await loop.run_in_executor(None, ctrl.status, inst)
                if status != "SERVICE_RUNNING":
                    logger.info(
                        "Auto-starting instance %s (was: %s)", inst.service_name, status
                    )
                    await loop.run_in_executor(None, ctrl.start, inst)
                else:
                    logger.info("Auto-start: %s already running", inst.service_name)
            except Exception as exc:
                logger.warning("Auto-start failed for %s: %s", inst.service_name, exc)

    return app


def _http_status_phrase(code: int) -> str:
    import http

    try:
        return http.HTTPStatus(code).phrase
    except ValueError:
        return "HTTP Error"
