"""A bounded bootstrap with room for standing facts and recent activity."""
from __future__ import annotations

import json
from contextlib import nullcontext

from hunter_gatherer.memory.output_budget import budget_report, fits, output_bytes, validate_budget
from hunter_gatherer.memory.retrieval_feedback import current_memory


def _candidate_lower_bound(entry):
    return len(json.dumps(entry, separators=(",", ":"), default=str).encode("utf-8"))


def build_profile(harness, query="", top_k=8, include_sensitive=False, max_tokens=4096):
    validate_budget(max_tokens)
    if type(top_k) is not int or not 1 <= top_k <= 100:
        raise ValueError("top_k must be 1–100")
    if type(include_sensitive) is not bool:
        raise ValueError("include_sensitive must be boolean")
    query = "" if query is None else query
    if not isinstance(query, str) or len(query) > 2000:
        raise ValueError("query must be at most 2000 characters")
    store = harness.store
    if query.strip():
        from hunter_gatherer.memory.simple_search import _embed_before_locking

        _embed_before_locking(store, [query])  # the model runs before the lock is held, as in Find
    # A profile is some eighty small reads. Taking the store lock for each one
    # lets another client's whole operation cut in before every read: with
    # three busy clients in one server a 26 ms profile took about a second. One
    # hold makes it wait once, and gives it a single consistent view.
    with getattr(store, "_lock", None) or nullcontext():
        return _build_profile(harness, query, top_k, include_sensitive, max_tokens)


def _build_profile(harness, query, top_k, include_sensitive, max_tokens):
    from hunter_gatherer.mcp.tools import _public_metadata, _recall_visible, _store_identity

    visible = [m for m in harness.store.iter_all() if current_memory(harness.store, m.id, include_sensitive, memory=m)]
    anchors = sorted(visible, key=lambda m: (-m.importance, m.id))
    pins = [m for m in anchors if m.metadata.get("core") is True]
    recent = sorted((m for m in visible if m.type == "episodic"),
                    key=lambda m: (m.created_at, m.id), reverse=True)
    ranked = _recall_visible(harness, query, top_k, include_sensitive) if query.strip() else []
    # Reserve two early slots for episodic context before anchors can fill 12 slots.
    candidates = [(m, "recent_episodic") for m in recent[:2]]
    candidates += [(m, "pinned_core") for m in pins[:2]]
    candidates += [(h.memory, "recall") for h in ranked]
    candidates += [(m, "pinned_core") for m in pins[2:]]
    candidates += [(m, "high_importance") for m in anchors]
    report = {"query": query[:300] or None, "query_truncated": len(query) > 300,
              "count": 0, "memory_total": harness.store.count(), "profile": [],
              "core": {"text": "", "memories": [], "tokens": 0, "omitted_count": len(pins), "omitted_tokens": 0},
              "omitted_count": 0, "store": _store_identity(harness),
              "follow_up": "Use recall for focused retrieval or get_memory with an included ID."}
    if not fits(report, max_tokens):
        report["query"] = None
        report["query_truncated"] = bool(query)
    seen = set()
    remaining_bytes = max_tokens * 4 - output_bytes(budget_report(report, max_tokens)) - 128
    for memory, reason in candidates:
        if memory.id in seen:
            continue
        seen.add(memory.id)
        if len(report["profile"]) >= 12:
            continue
        entry = {"id": memory.id, "citation": f"[mem:{memory.id}]", "text": memory.text,
                 "type": memory.type, "importance": round(memory.importance, 3),
                 "reason": reason, "source": memory.source,
                 "metadata": {}}
        # Compact JSON is a lower bound on the indented entry plus its array
        # framing. Reject impossible candidates without repeatedly serializing
        # the entire accumulated report. Exact checks still govern acceptance.
        if _candidate_lower_bound(entry) > remaining_bytes:
            continue
        fresh = current_memory(harness.store, memory.id, include_sensitive)
        if fresh is None:
            continue
        entry.update(text=fresh.text, type=fresh.type, importance=round(fresh.importance, 3),
                     source=fresh.source, metadata=_public_metadata(harness, fresh, include_sensitive))
        report["profile"].append(entry)
        if not fits(report, max_tokens):
            report["profile"].pop()
        else:
            remaining_bytes = max_tokens * 4 - output_bytes(report) - 128
    # Preserve the legacy core block when it fits; never let duplication displace
    # recent/query context. The profile still labels every included pinned fact.
    pin_ids = {m.id for m in pins}
    for entry in report["profile"]:
        if entry["id"] not in pin_ids:
            continue
        core = report["core"]
        previous_text = core["text"]
        line = f"- {entry['citation']} {entry['text']}"
        core["text"] = "\n".join(filter(None, [previous_text, line]))
        core["memories"].append({k: entry[k] for k in ("id", "text", "citation", "type", "importance", "source")})
        if not fits(report, max_tokens):
            core["memories"].pop()
            core["text"] = previous_text
    report["count"] = len(report["profile"])
    report["omitted_count"] = max(0, len(seen) - report["count"])
    report["core"]["tokens"] = len(report["core"]["text"]) // 4
    report["core"]["omitted_count"] = len(pins) - len(report["core"]["memories"])
    if not fits(report, max_tokens, reserve=0):
        raise ValueError("Budget too small for profile framing")
    return budget_report(report, max_tokens)
