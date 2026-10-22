"""Test utilities for the server tests.

``stub_router`` replaces the app's loop.router with a fake that returns a
fixed answer without needing a real model — so WS/router tests are fast and
deterministic.
"""
from __future__ import annotations

from hunter_gatherer.models.base import ChatResult, Message, Usage


def stub_router(app) -> None:
    """Replace app.state.loop.router with a fake that returns a canned answer.

    The fake supports both chat() and stream() so the streaming path is
    exercised. It routes everything to 'local'.
    """
    loop = app.state.loop

    class _FakeLocal:
        model = "fake"

        def chat(self, messages, tools=None, temperature=0.2, max_tokens=None):
            return ChatResult(
                message=Message(role="assistant", content="Hello from the fake model."),
                usage=Usage(in_tokens=5, out_tokens=5),
            )

        def stream(self, messages, tools=None, temperature=0.2):
            yield from ["Hello", " from", " the", " fake", " model."]

    class FakeRouter:
        model = "fake"

        def __init__(self):
            self.local = _FakeLocal()
            self.cheap = None
            self.cloud = None
            self.settings = loop.settings
            self._route_cache = {}
            self._exemplar_vecs = None
            self._embedder = None

        def route(self, q):
            from hunter_gatherer.models.router import Route

            return Route("local", "fake", "test", score=None)

        def select(self, r):
            return self.local

        def chat(self, messages, tools=None, temperature=0.2, max_tokens=None):
            return self.local.chat(messages, tools=tools, temperature=temperature, max_tokens=max_tokens)

        def stream(self, messages, tools=None, temperature=0.2):
            return self.local.stream(messages, tools=tools, temperature=temperature)

    loop.router = FakeRouter()
