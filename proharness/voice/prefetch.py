"""Speculative route/recall prefetch on STT partials (2026-08-19 #3).

The router's ``_route_cache`` makes prefetch warming nearly free: calling
``router.route(partial)`` on each stable partial hypothesis embeds + decides
*while the user is still speaking*, so when the utterance closes the final
transcript reuses the warmed route on an exact/prefix match — worst case is
the status quo (recompute), best case is that the route+embed latency is
entirely hidden behind the user's own speech.

Reuse policy (honest about what a partial can promise):
  - ROUTE: reused on exact OR prefix match — single-shot tier choice is
    robust to a trailing partial word.
  - RECALL HITS: reused only on EXACT match — Park-ranked recall is
    query-sensitive, so a prefix match recomputes rather than risks a
    mis-ranked block.

Config: ``PROHARNESS_STT_PREFETCH`` (default on), ``min_chars`` gate so
gibberish partials never trigger embedding work.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class SpeculativePrefetch:
    """Feed STT partials; warm the router cache; hand back reusable results."""

    def __init__(self, loop, *, min_chars: int = 12, warm_recall: bool = True):
        self.loop = loop
        self.min_chars = min_chars
        self.warm_recall = warm_recall
        self._latest: dict | None = None
        self.hits = 0  # how many prefetches were actually reused

    def feed(self, partial: str) -> None:
        """Warm route (and optionally recall) on a stable partial hypothesis."""
        partial = (partial or "").strip()
        if len(partial) < self.min_chars:
            return
        try:
            route = self.loop.router.route(partial)  # fills the route cache
            entry: dict = {"partial": partial, "route": route, "hits": None}
            if self.warm_recall:
                from proharness.memory.recall import recall as _recall

                # touch=False: a speculative lookup must not bump recency.
                entry["hits"] = _recall(self.loop.store, partial, top_k=5,
                                        touch=False, defer_touch=False)
            self._latest = entry
        except Exception as e:  # prefetch must never break the voice path
            log.debug("speculative prefetch failed: %s", e)

    def match(self, final_text: str) -> dict | None:
        """Return reusable precomputed work for the final transcript, or None.

        Route on exact/prefix; recall hits on exact only (see module doc)."""
        entry = self._latest
        if not entry:
            return None
        final = (final_text or "").strip()
        partial = entry["partial"]
        if not final:
            return None
        out: dict = {}
        if final == partial:
            out["route"] = entry["route"]
            if entry.get("hits") is not None:
                out["hits"] = entry["hits"]
        elif final.startswith(partial) or partial.startswith(final):
            out["route"] = entry["route"]
        else:
            return None
        self.hits += 1
        return out


def make_prefetch(loop) -> SpeculativePrefetch | None:
    """Build a prefetch from the loop's settings (None when disabled)."""
    settings = getattr(loop, "settings", None)
    if settings is None or not getattr(settings, "stt_prefetch", True):
        return None
    return SpeculativePrefetch(
        loop,
        min_chars=int(getattr(settings, "stt_prefetch_min_chars", 12)),
    )
