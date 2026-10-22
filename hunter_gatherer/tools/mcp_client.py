"""Inbound MCP — external servers as first-class agent tools (2026-09-17 #1).

The harness has been an MCP *server* since the beginning; this module adds
the client half the charter explicitly deferred: configured external MCP
servers' tools are wrapped as internal ``Tool`` objects the RAG-over-tools
selector ranks exactly like built-ins.

Design:
  - **Allowlist-first**: nothing connects unless ``HG_MCP_SERVERS``
    configures it (JSON list of name + stdio command or URL). Strictly no-op
    until then.
  - **One background event loop per manager** — the ``mcp`` SDK client is
    async while ``Tool.run`` is sync, so sessions live on a dedicated loop
    thread and dispatch goes through ``run_coroutine_threadsafe``.
  - **Reconnect/backoff** per server (the failure-ladder pattern): a dead
    server drops its tools (the registry refresh removes them) rather than
    breaking the turn; the next refresh tick retries the connection.
  - Remote tool defs → ``Tool`` subclasses via the same dynamic-closure
    machinery ``tools/pipeline_tool.py`` uses for pipelines.

Every shipped guardrail applies for free: big external payloads ride the
tool-result condensation sidecar, the selector's description-embedding cache
re-embeds on refresh, and worker-scoped registries can exclude them.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from typing import Any

from hunter_gatherer.tools.base import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)


def parse_mcp_servers(raw: str) -> list[dict]:
    """Parse the HG_MCP_SERVERS JSON list. Entries:
    {"name": str, "command": str, "args": [str]} (stdio) or
    {"name": str, "url": str} (streamable-http)."""
    raw = (raw or "").strip()
    if not raw:
        return []
    with contextlib.suppress(Exception):
        entries = json.loads(raw)
        if isinstance(entries, list):
            return [e for e in entries
                    if isinstance(e, dict) and e.get("name")
                    and (e.get("command") or e.get("url"))]
    log.warning("HG_MCP_SERVERS is set but unparseable — ignoring")
    return []


class _RemoteTool(Tool):
    """One remote MCP tool wrapped as an internal Tool. Instances are created
    per refresh with the manager closed over."""

    def __init__(self, server_name: str, remote_def: dict, manager: MCPClientManager):
        self.server_name = server_name
        self.remote_name = remote_def.get("name", "tool")
        # Namespaced like the plugin convention: mcp_<server>_<tool>.
        self.name = f"mcp_{server_name}_{self.remote_name}"[:64]
        self.description = (remote_def.get("description") or
                            f"Remote tool '{self.remote_name}' from MCP server '{server_name}'.")
        schema = remote_def.get("inputSchema") or remote_def.get("input_schema") or {}
        self.parameters: dict[str, Any] = {
            "type": "object",
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
        }
        self._manager = manager

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        content, is_error = self._manager.call_tool(self.server_name, self.remote_name, args)
        if is_error:
            return ToolResult.err(content[:2000])
        return ToolResult.ok(content[:8000], server=self.server_name)


class MCPClientManager:
    """Owns the background loop, one client session per configured server,
    and the wrapped-tool registry. Sync API; async internals."""

    def __init__(self, entries: list[dict]):
        self._entries = entries
        self._loop = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sessions: dict[str, Any] = {}  # server name → ClientSession
        self._exit_stacks: dict[str, Any] = {}
        self._last_error: dict[str, float] = {}
        self._lock = threading.RLock()
        if entries:
            self._start_loop()

    # ── background loop ─────────────────────────────────────────────────────
    def _start_loop(self) -> None:
        import asyncio

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                        name="mcp-client-loop")
        self._thread.start()

    def _run_async(self, coro, timeout: float = 60.0):
        import asyncio

        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("MCP client loop not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        self._stop.set()
        for name in list(self._exit_stacks):
            with contextlib.suppress(Exception):
                self._run_async(self._aclose(name), timeout=5.0)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # ── connection lifecycle ────────────────────────────────────────────────
    async def _ensure_session_async(self, entry: dict):
        from mcp import ClientSession

        if entry.get("url"):
            from mcp.client.streamable_http import streamable_http_client

            cm = streamable_http_client(entry["url"])
        else:
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(command=entry["command"], args=entry.get("args", []))
            cm = stdio_client(params)
        streams_ctx = await cm.__aenter__()
        if isinstance(streams_ctx, tuple):
            read, write = streams_ctx[0], streams_ctx[1]
        else:  # pragma: no cover — legacy shape
            read, write = streams_ctx, None
        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()
        return cm, session

    async def _aclose(self, name: str) -> None:
        from contextlib import suppress

        with suppress(Exception):
            await self._sessions[name].__aexit__(None, None, None)
        cm = self._exit_stacks.pop(name, None)
        if cm is not None:
            with suppress(Exception):
                await cm.__aexit__(None, None, None)
        self._sessions.pop(name, None)

    def _backoff_active(self, name: str) -> bool:
        last = self._last_error.get(name, 0.0)
        return last and (time.time() - last) < 30.0  # retry a dead server every 30s

    def ensure_connected(self, entry: dict) -> bool:
        name = entry["name"]
        with self._lock:
            if name in self._sessions:
                return True
            if self._backoff_active(name):
                return False
        try:
            cm, session = self._run_async(self._ensure_session_async(entry), timeout=45.0)
            with self._lock:
                self._sessions[name] = session
                self._exit_stacks[name] = cm
            return True
        except Exception as e:
            log.warning("MCP server '%s' connect failed: %s", name, e)
            self._last_error[name] = time.time()
            return False

    # ── public sync API ─────────────────────────────────────────────────────
    def list_tools(self) -> list[Tool]:
        """Connect configured servers (respecting backoff) and return the
        wrapped remote tools. A dead server contributes nothing."""
        tools: list[Tool] = []
        for entry in self._entries:
            name = entry["name"]
            if not self.ensure_connected(entry):
                continue
            try:
                async def _list(session=self._sessions[name]):
                    res = await session.list_tools()
                    return [t.model_dump() if hasattr(t, "model_dump") else dict(t)
                            for t in res.tools]

                for remote_def in self._run_async(_list(), timeout=30.0):
                    with contextlib.suppress(Exception):
                        tools.append(_RemoteTool(name, remote_def, self))
            except Exception as e:
                log.warning("MCP server '%s' list_tools failed: %s", name, e)
                self._last_error[name] = time.time()
                with contextlib.suppress(Exception):
                    self._run_async(self._aclose(name), timeout=5.0)
        return tools

    def call_tool(self, server_name: str, tool_name: str, args: dict) -> tuple[str, bool]:
        """Dispatch one call; returns (text, is_error)."""
        entry = next((e for e in self._entries if e["name"] == server_name), None)
        if entry is None or not self.ensure_connected(entry):
            return f"MCP server '{server_name}' unavailable", True
        session = self._sessions.get(server_name)
        if session is None:
            return f"MCP server '{server_name}' unavailable", True

        async def _call():
            res = await session.call_tool(tool_name, args)
            texts = [c.text for c in (res.content or [])
                     if getattr(c, "type", "") == "text" and hasattr(c, "text")]
            return "\n".join(texts) or "(empty result)", bool(getattr(res, "isError", False))

        try:
            return self._run_async(_call(), timeout=120.0)
        except Exception as e:
            log.warning("MCP call %s/%s failed: %s", server_name, tool_name, e)
            self._last_error[server_name] = time.time()
            with contextlib.suppress(Exception):
                self._run_async(self._aclose(server_name), timeout=5.0)
            return f"{type(e).__name__}: {e}", True
