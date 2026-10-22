"""FastAPI app factory for the hunter/gatherer UI server.

Wires:
  - lifespan (constructs the shared AgentLoop / harness)
  - SingleTokenMiddleware (Host/Origin admission; LAN requires token)
  - REST routers: memory, telemetry, config, files
  - WebSocket: /ws/stream (streaming chat)
  - MCP mount: /mcp (the tool interface for agents)
  - static file serving (the built React frontend)
  - health endpoints
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from hunter_gatherer.config import Settings
from hunter_gatherer.server.lifespan import lifespan


def build_app(settings: Settings | None = None, *, static_dir: Path | None = None) -> FastAPI:
    """Construct the FastAPI app. Does not start the server."""
    settings = settings or Settings()
    app = FastAPI(
        title="hunter/gatherer",
        version="0.1.0",
        description="UI + tool server for the hunter/gatherer AI harness",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # One boundary covers HTTP and WebSocket routes, including static files.
    from hunter_gatherer.server.auth import SingleTokenMiddleware

    app.add_middleware(SingleTokenMiddleware, settings=settings)

    # REST routers.
    from hunter_gatherer.server.routers import config as config_router
    from hunter_gatherer.server.routers import files as files_router
    from hunter_gatherer.server.routers import memory as memory_router
    from hunter_gatherer.server.routers import pipelines as pipelines_router
    from hunter_gatherer.server.routers import telemetry as telemetry_router

    app.include_router(memory_router.router, prefix="/api/memory", tags=["memory"])
    app.include_router(telemetry_router.router, prefix="/api", tags=["telemetry"])
    app.include_router(config_router.router, prefix="/api/settings", tags=["settings"])
    app.include_router(files_router.router, prefix="/api/files", tags=["files"])
    app.include_router(pipelines_router.router, prefix="/api/pipelines", tags=["pipelines"])

    # WebSocket streaming endpoint.
    from hunter_gatherer.server.ws import register_ws

    register_ws(app)

    # Optional MCP mount (tool interface for agents) at /mcp.
    if settings.ui_mount_mcp:
        try:
            from hunter_gatherer.mcp.http_mount import mount as mount_mcp
            from hunter_gatherer.mcp.server import build_server

            mcp_server, _harness = build_server(settings)
            # Expose the MCP server + its harness on app.state so the pipelines
            # router can auto-register pipeline tools at runtime (one tool per
            # saved pipeline, no restart needed).
            app.state.mcp_server = mcp_server
            app.state.mcp_harness = _harness
            mount_mcp(app, mcp_server, path="/mcp")
        except Exception as e:  # MCP is optional; never block UI startup on it
            import logging

            logging.getLogger(__name__).warning("MCP mount skipped: %s", e)

    # Health endpoints obey the same Host/Origin and LAN-token boundary.
    @app.get("/healthz", tags=["meta"])
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", tags=["meta"])
    async def readyz():
        loop = getattr(app.state, "loop", None)
        return {"status": "ok" if loop is not None else "starting"}

    # Static frontend (built React app). Falls back to index.html for SPA routes.
    static_dir = (static_dir or Path(__file__).parent / "static").resolve()
    if static_dir.exists():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            # Serve real files if they exist; otherwise index.html (SPA routing).
            candidate = (static_dir / full_path).resolve()
            if not candidate.is_relative_to(static_dir):
                raise HTTPException(404, "Not found")
            if candidate.is_file():
                return FileResponse(candidate)
            index = (static_dir / "index.html").resolve()
            if index.is_relative_to(static_dir) and index.is_file():
                return FileResponse(index)
            return {"detail": "frontend not built; run `npm run build` in frontend/"}
    else:
        @app.get("/", include_in_schema=False)
        async def root():
            return {
                "name": "hunter/gatherer",
                "status": "ok",
                "detail": "frontend not built. API at /api/*, MCP at /mcp, docs at /docs",
            }

    return app


def create_app() -> FastAPI:
    """Console-script-friendly factory; reads settings from env."""
    return build_app(Settings())
