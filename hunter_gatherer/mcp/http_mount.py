"""Expose the MCP streamable-http transport on a Starlette/FastAPI app.

Lets the desktop UI server serve both the REST/WS UI AND the MCP tool
interface on one port — the MCP endpoint lives at ``/mcp``. An MCP host (or a
coding-plan agent) connects to ``http://<host>:<port>/mcp`` instead of
spawning the stdio subprocess.

Two non-obvious requirements handled here:

  - We register the transport as a DIRECT ``Route`` (class endpoint → all
    methods, no prefix rewriting) rather than a ``Mount``: a mounted
    Starlette sub-app strips the mount prefix, so a "/"-rooted endpoint sees
    an empty child path and a "/mcp"-rooted one doubles the URL — both
    misroute. A direct route keeps the endpoint at exactly ``path``.
  - Starlette does NOT run lifespans for routes/sub-apps, and the MCP session
    manager needs its task group started. We stash the sub-app on
    ``app.state.mcp_app``; the server lifespan
    (``hunter_gatherer.server.lifespan``) enters/exits its lifespan context.
"""
from __future__ import annotations


def mount(app, server, *, path: str = "/mcp") -> None:
    """Expose the MCP server's streamable-http endpoint on `app` at `path`.

    `app` is a Starlette or FastAPI application. `server` is the MCPServer
    returned by ``hunter_gatherer.mcp.server.build_server``.
    """
    from starlette.routing import Route

    mcp_app = server.streamable_http_app(streamable_http_path=path)
    app.state.mcp_app = mcp_app  # the lifespan bridges its startup/shutdown

    asgi_app = mcp_app.router.routes[0].endpoint  # the StreamableHTTPASGIApp
    app.router.routes.append(Route(path, endpoint=asgi_app))
