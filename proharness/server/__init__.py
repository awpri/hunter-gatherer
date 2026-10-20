"""ProHarness UI server (FastAPI + WebSockets) and desktop shell.

Wraps the existing harness (AgentLoop, MemoryStore, router, eval) in a web UI
plus a LAN-reachable API. Two reach paths share one process:
  - desktop shell: pywebview opens a window on localhost (token bypassed)
  - LAN mode: same server on 0.0.0.0, single shared token required

Run:
    python -m proharness.server.desktop    # opens the desktop window
    uvicorn proharness.server.app:app      # server-only (browser)
"""
from proharness.server.app import build_app, create_app

__all__ = ["build_app", "create_app"]
