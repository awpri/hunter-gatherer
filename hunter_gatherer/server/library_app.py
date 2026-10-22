"""Serve the personal library and MCP from one already-created Harness.

This surface deliberately excludes the legacy workbench's file-system tools,
settings editor, websocket agent, and unattended pipeline scheduler. It does
not create another store or embedding model.
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecurityMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles


class LibraryBoundary:
    """Apply the MCP host/origin boundary to the whole local web application."""

    def __init__(self, app, security):
        self.app = app
        self.security = TransportSecurityMiddleware(security)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        response = await self.security.validate_request(request)
        if response is not None:
            await response(scope, receive, send)
            return

        async def protected_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"content-security-policy",
                            (
                                b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                b"img-src 'self' data:; font-src 'self'; connect-src 'self'; "
                                b"object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                            ),
                        ),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, protected_send)


def build_library_app(harness, mcp_server, *, http_options: dict, static_dir: Path | None = None):
    """Build a loopback web surface without creating a second Harness."""
    from hunter_gatherer.maintenance import maintenance
    from hunter_gatherer.mcp.server import build_server
    from hunter_gatherer.server.routers.library import router

    options = {key: value for key, value in http_options.items() if key != "port"}
    mcp_app = mcp_server.streamable_http_app(**options)
    # A second protocol view, not a second runtime/store/model. Existing clients
    # keep /mcp; new library clients can choose the smaller discovery catalog.
    library_server, _ = build_server(harness=harness, tool_profile="library")
    library_mcp_app = library_server.streamable_http_app(
        **{**options, "streamable_http_path": "/mcp/library"}
    )

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
            await stack.enter_async_context(library_mcp_app.router.lifespan_context(library_mcp_app))
            upkeep = maintenance(harness)
            upkeep.start()
            try:
                yield
            finally:
                upkeep.stop()
                worker = getattr(app.state, "library_import_worker", None)
                if worker is not None:
                    worker.shutdown()

    app = FastAPI(
        title="hunter/gatherer Library",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.library_harness = harness
    app.include_router(router, prefix="/api/library")
    # Keep the SDK endpoint at exactly /mcp; mounting would rewrite its path.
    app.router.routes.append(Route("/mcp", endpoint=mcp_app.router.routes[0].endpoint))
    app.router.routes.append(Route("/mcp/library", endpoint=library_mcp_app.router.routes[0].endpoint))

    @app.get("/healthz")
    def health():
        return {"status": "ok", "service": "hunter_gatherer-library", "shared_harness": True}

    @app.get("/readyz")
    def ready():
        from hunter_gatherer.operations import readiness

        report = readiness(harness, import_worker=getattr(app.state, "library_import_worker", None))
        return JSONResponse(report, status_code=200 if report["ready"] else 503)

    static_dir = static_dir or Path(__file__).parent / "library_static"
    for directory in ("assets", "fonts", "licenses", "brand"):
        if (static_dir / directory).is_dir():
            app.mount(
                f"/{directory}", StaticFiles(directory=static_dir / directory), name=directory
            )

    @app.get("/hunter-gatherer.svg")
    @app.get("/hunter_gatherer.svg")
    def brand_icon():
        path = static_dir / "hunter-gatherer.svg"
        if not path.is_file():
            path = static_dir / "hunter_gatherer.svg"
        if not path.is_file():
            return JSONResponse({"detail": "Brand icon not found."}, status_code=404)
        return FileResponse(path, media_type="image/svg+xml")

    @app.get("/")
    def index():
        path = static_dir / "index.html"
        if not path.is_file():
            return JSONResponse(
                {"detail": "Library frontend is not built. Run npm run build in frontend."},
                status_code=503,
            )
        return FileResponse(path, media_type="text/html")

    app.add_middleware(LibraryBoundary, security=http_options["transport_security"])
    return app
