"""Bounded, process-local Find continuation state; no retrieved bodies."""
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


def new_token() -> str:
    return secrets.token_urlsafe(24)


def revision(store) -> tuple[int, int]:
    with store._lock:
        if store._sqlite.in_transaction:
            raise ValueError("Find continuation requires no active transaction; restart Find.")
        return (store._sqlite.total_changes,
                store._sqlite.execute("PRAGMA data_version").fetchone()[0])


def _config(store) -> str:
    # Retain only the digest, never settings or credentials themselves.
    settings = store.settings.model_dump()
    return hashlib.sha256(json.dumps(settings, sort_keys=True, default=str,
                                    separators=(",", ":")).encode()).hexdigest()


def _state(state):
    required = {"query", "requirements", "include_sensitive", "offsets", "pending"}
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
                   for k, values in state["pending"].items())):
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


def issue(store, state, expected_revision, *, token=None) -> str:
    state = _state(state)
    token = new_token() if token is None else token
    if type(token) is not str or not _TOKEN.fullmatch(token):
        raise ValueError("Invalid Find continuation token")
    with store._lock:
        if revision(store) != tuple(expected_revision):
            raise ValueError(_ERROR)
        now = time.monotonic()
        cache = _cache(store, now)
        if token in cache:
            raise ValueError("Find continuation token already exists")
        while len(cache) >= _MAX_ENTRIES:
            cache.popitem(last=False)
        cache[token] = {"created": now, "revision": tuple(expected_revision),
                        "config": _config(store), "state": state}
        return token


def resume(store, token, include_sensitive, query=None, requirements=None, constraints=None,
           contextual_requirements=None):
    if type(token) is not str or not _TOKEN.fullmatch(token):
        raise ValueError(_ERROR)
    with store._lock:
        current = revision(store)
        cache = _cache(store, time.monotonic())
        entry = cache.get(token)
        if entry is None:
            raise ValueError(_ERROR)
        state = entry["state"]
        if (entry["revision"] != current or entry["config"] != _config(store)
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
