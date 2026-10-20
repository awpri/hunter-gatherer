"""ProHarness MCP server — exposes the harness as tools any MCP host can call.

A Model Context Protocol server that wraps ProHarness's internal API
(AgentLoop, MemoryStore, router_tune, eval, Reflector) as standard MCP tools.
Spawned via stdio by a host (the canonical "tool server" pattern), or mounted
at an HTTP/SSE endpoint alongside the UI server (``http_mount.mount``).

This is the "make it a tool" piece: a coding-plan agent (or any MCP client —
Claude, Cursor, etc.) calls these like any other tool. ``proharness_delegate``
is the headline — it hands a subtask to ProHarness's routed, memory-primed,
sub-agent-orchestrating loop and returns a condensed answer, so the calling
agent delegates instead of doing the work itself.

Transport:
  - stdio (default): ``proharness-mcp`` console script, or
    ``python -m proharness.mcp.server``.
  - HTTP/SSE: ``mount(app, mcp_server)`` in ``http_mount.py`` mounts the MCP
    streamable-http transport onto a FastAPI/Starlette app at ``/mcp``.
"""
from proharness.mcp.server import build_server, main

__all__ = ["build_server", "main"]
