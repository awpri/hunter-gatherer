"""App lifespan: construct shared state at startup, tear down at shutdown.

Holds one shared ``AgentLoop`` (the harness) per process — its store, router,
and cost tracker. The REST routers, the WebSocket, and the MCP server all read
from the same harness via ``request.app.state.harness``.
"""
from __future__ import annotations

import contextlib
import logging
import secrets

from hunter_gatherer.config import Settings

log = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan context: build the harness, yield, close it."""
    settings: Settings = app.state.settings
    if settings.ui_lan_mode and not settings.ui_token:
        # Generate a token and log it so the user can grab it for LAN access.
        settings.ui_token = secrets.token_urlsafe(24)
        log.warning(
            "LAN mode enabled with no configured token — generated one: %s",
            settings.ui_token,
        )
        print(f"\n  hunter/gatherer LAN token: {settings.ui_token}\n")  # noqa: T201 — user-facing
    # Build the shared harness (one AgentLoop per process). If the MCP mount
    # already built a harness at app-construction time (unified-harness path —
    # 2026-08-06 #1), adopt its AgentLoop so the UI and MCP transports share
    # one loop: token accounting, router decisions, and the α-tune dataset see
    # the whole system instead of per-transport halves.
    from hunter_gatherer.loop import AgentLoop

    mcp_harness = getattr(app.state, "mcp_harness", None)
    loop = mcp_harness.loop if mcp_harness is not None else AgentLoop(settings)
    app.state.loop = loop
    app.state.harness_loop = loop  # alias for clarity
    app.state.harness = loop  # for the MCP tools' Harness compat (loop.store etc.)
    log.info("hunter/gatherer UI ready: %d memories, %s", loop.store.count(),
             f"bind={settings.ui_host}:{settings.ui_port}")

    # Auto-register saved pipelines as MCP tools (survives restart).
    mcp_server = getattr(app.state, "mcp_server", None)
    if mcp_server is not None:
        try:
            from hunter_gatherer.mcp.pipeline_tools import register_all_pipeline_tools

            n = register_all_pipeline_tools(mcp_server, loop)
            if n:
                log.info("registered %d pipeline tools from DB", n)
        except Exception as e:
            log.warning("pipeline tool registration skipped: %s", e)

    # Unattended pipeline scheduler (2026-08-20 #3): a minute ticker for
    # triggered pipelines (cron / interval / importance threshold), seeding
    # the daily-brief template on first boot. Runs ride the interpreter's
    # budget guardrail; each run appends a summary memory + WS event.
    scheduler = None
    try:
        from hunter_gatherer.pipelines.scheduler import PipelineScheduler

        scheduler = PipelineScheduler(loop)
        # Always start: triggers may be added at runtime via the REST API.
        scheduler.start()
        app.state.pipeline_scheduler = scheduler
    except Exception as e:
        log.warning("pipeline scheduler not started: %s", e)

    # Mounted-subapp lifespan bridge: Starlette does not run a mounted app's
    # lifespan, and the MCP streamable-HTTP session manager needs its task
    # group started — enter its lifespan context for the process lifetime.
    mcp_ctx = None
    mcp_app = getattr(app.state, "mcp_app", None)
    if mcp_app is not None:
        with contextlib.suppress(Exception):
            mcp_ctx = mcp_app.router.lifespan_context(mcp_app)
            await mcp_ctx.__aenter__()
            log.info("MCP streamable-http session manager started (/mcp)")

    try:
        yield
    finally:
        worker = getattr(app.state, "library_import_worker", None)
        if worker is not None:
            worker.shutdown()
        if mcp_ctx is not None:
            with contextlib.suppress(Exception):
                await mcp_ctx.__aexit__(None, None, None)
        if scheduler is not None:
            scheduler.stop()
        loop.close()
        log.info("hunter/gatherer UI shut down")
