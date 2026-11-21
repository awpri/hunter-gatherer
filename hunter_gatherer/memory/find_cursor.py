"""Bounded, process-local Find continuation state; no retrieved bodies.

A cursor keeps the matches its first page admitted: for each kind of result,
the citations still to be delivered, in the order that page ranked them (at
most ``ADMITTED`` ranked matches per kind). Later pages are built from that
list rather than by ranking the library again, so a write elsewhere cannot
shift what comes next: nothing is skipped and nothing is repeated.

Each admitted record is checked against the library as it is when its page is
built. One that has since been forgotten, corrected, replaced by a newer
version or made sensitive is withdrawn and never delivered. Records added
after the first page are not admitted; the page says so, and a new search
includes them.

The state holds citations, counts and row high-water marks only: never text,
quotes, titles or scores.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections import OrderedDict
from copy import deepcopy

_MAX_ENTRIES = 128
_TTL = 15 * 60
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}\Z")
_ERROR = "Find cursor is invalid, expired, or stale; restart Find."
ADMITTED = 128
"""Ranked matches the first page admits per kind (memories, passages, catalog)."""
REGIONS = 256
"""Image-region descriptions admitted; the region discovery limit."""
# Requirement searches add at most two memories and two passages for each of
# up to four requirements, beyond what the main ranking admitted.
_CAPACITY = {"memories": ADMITTED + 8, "passages": ADMITTED + 8, "catalog": ADMITTED,
             "visual_regions": REGIONS}
_PREFIX = {"memories": "[mem:", "passages": "[doc:", "catalog": "[file:", "visual_regions": "[region:"}
# Tables whose new rows can become Find results. Their rowid high-water marks
# tell a later page that records were added after the first.
_SOURCES = ("memories", "archive_documents", "document_enrichment_records")


def new_token() -> str:
    return secrets.token_urlsafe(24)


def watermark(store) -> list[int]:
    """Highest row ID of each record table, as seen by the caller's snapshot."""
    with store._lock:
        db = store._sqlite
        present = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)", _SOURCES)}
        return [db.execute(f'SELECT coalesce(max(rowid),0) FROM "{name}"').fetchone()[0]
                if name in present else 0 for name in _SOURCES]


def added_since(store, state) -> bool:
    """Whether any record table gained rows after the cursor's first page."""
    return any(now > then for now, then in zip(watermark(store), state["watermark"], strict=True))


def _config(store) -> str:
    # Retain only the digest, never settings or credentials themselves.
    settings = store.settings.model_dump()
    return hashlib.sha256(json.dumps(settings, sort_keys=True, default=str,
                                    separators=(",", ":")).encode()).hexdigest()


def _citations(values, prefix, capacity):
    return (type(values) is list and len(values) <= capacity and len(set(values)) == len(values)
            and all(type(v) is str and v.startswith(prefix) and v.endswith("]") and len(v) <= 4096
                    for v in values))


def _state(state):
    required = {"query", "requirements", "include_sensitive", "offsets", "pending",
                "admitted", "limit_reached", "watermark"}
    if (type(state) is not dict or not required <= set(state)
            or set(state) - required - {"constraints", "contextual_requirements"}
            or ("contextual_requirements" in state and type(state["contextual_requirements"]) is not bool)
            or type(state["query"]) is not str or not state["query"].strip()
            or len(state["query"]) > 4000
            or type(state["include_sensitive"]) is not bool
            or type(state["requirements"]) is not list
            or len(state["requirements"]) > 4
            or any(type(v) is not str or len(v) > 4000 for v in state["requirements"])
            or type(state["offsets"]) is not dict
            or any(type(k) is not str or len(k) > 100 or type(v) is not int
                   or not 0 <= v <= 1_000_000 for k, v in state["offsets"].items())
            or len(state["offsets"]) > 16
            or type(state["pending"]) is not dict or len(state["pending"]) > 8
            or any(type(k) is not str or len(k) > 100 or type(values) is not list
                   or len(values) > 32
                   or any(type(v) is not str or not v.startswith("[")
                          or not v.endswith("]") or len(v) > 4096 for v in values)
                   for k, values in state["pending"].items())
            or type(state["admitted"]) is not dict or set(state["admitted"]) - set(_PREFIX)
            or any(not _citations(values, _PREFIX[kind], _CAPACITY[kind])
                   for kind, values in state["admitted"].items())
            or type(state["limit_reached"]) is not list
            or len(set(state["limit_reached"])) != len(state["limit_reached"])
            or set(state["limit_reached"]) - set(_PREFIX)
            or type(state["watermark"]) is not list or len(state["watermark"]) != len(_SOURCES)
            or any(type(v) is not int or v < 0 for v in state["watermark"])):
        raise ValueError("Invalid Find continuation state")
    if "constraints" in state:
        from hunter_gatherer.memory.relationship_constraints import validate_constraints
        validate_constraints(state["constraints"])
    return deepcopy(state)


def _cache(store, now):
    cache = getattr(store, "_find_cursor_cache", None)
    if cache is None:
        cache = store._find_cursor_cache = OrderedDict()
    for token in list(cache):
        if now - cache[token]["created"] >= _TTL:
            del cache[token]
    return cache


def issue(store, state, *, token=None) -> str:
    state = _state(state)
    token = new_token() if token is None else token
    if type(token) is not str or not _TOKEN.fullmatch(token):
        raise ValueError("Invalid Find continuation token")
    with store._lock:
        now = time.monotonic()
        cache = _cache(store, now)
        if token in cache:
            raise ValueError("Find continuation token already exists")
        while len(cache) >= _MAX_ENTRIES:
            cache.popitem(last=False)
        cache[token] = {"created": now, "config": _config(store), "state": state}
        return token


def resume(store, token, include_sensitive, query=None, requirements=None, constraints=None,
           contextual_requirements=None):
    if type(token) is not str or not _TOKEN.fullmatch(token):
        raise ValueError(_ERROR)
    with store._lock:
        cache = _cache(store, time.monotonic())
        entry = cache.get(token)
        if entry is None:
            raise ValueError(_ERROR)
        state = entry["state"]
        if (entry["config"] != _config(store)
                or type(include_sensitive) is not bool
                or include_sensitive != state["include_sensitive"]
                or (query is not None and (type(query) is not str
                                          or query.strip() != state["query"]))
                or (requirements is not None and requirements != state["requirements"])
                or (contextual_requirements is not None and (
                    type(contextual_requirements) is not bool
                    or contextual_requirements != state.get("contextual_requirements", False)))
                or (constraints is not None and constraints != state.get("constraints", {}))):
            raise ValueError(_ERROR)
        return deepcopy(state)
