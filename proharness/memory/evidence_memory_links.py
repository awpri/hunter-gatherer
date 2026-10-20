"""Discover live memories citing retrieved passages, without treating links as support."""
from proharness.memory.evidence import _kind, _normal_category, _source_refs, _verification
from proharness.memory.retrieval_feedback import current_memory


def discover_source_memories(store, eligible, passages, *, include_sensitive=False, offset=None):
    """Reuse the caller's eligible records and pinned evidence snapshot.

    Exact source identities prevent matching copied text or other versions.
    current_memory revalidates every recorded reference before exposing a route
    that fetch could otherwise reject. No text or copied quotes are returned.
    Optional offset pages the source-order / memory-ID ranking in groups of eight.
    """
    if offset is not None and (type(offset) is not int or offset < 0):
        raise ValueError("offset must be a nonnegative integer")
    if not store._sqlite.in_transaction:
        raise ValueError("Memory discovery requires an evidence read snapshot")
    sources = {}
    for passage in passages:
        identity = (passage.get("document_id"), passage.get("chunk_id"))
        if all(isinstance(value, str) and value for value in identity) and passage.get("citation"):
            sources.setdefault(identity, passage)
    source_order = {identity: rank for rank, identity in enumerate(sources)}
    candidates = []
    for memory in eligible if sources else ():
        linked = set()
        for ref in _source_refs(memory):
            identity = (ref.get("document_id"), ref.get("chunk_id"))
            if not all(isinstance(value, str) for value in identity) or identity not in sources:
                continue
            quote = ref.get("quote")
            if quote is not None and (not isinstance(quote, str) or not quote.strip()
                                      or quote not in sources[identity].get("quote", "")):
                continue
            linked.add(identity)
        if not linked or current_memory(store, memory.id, include_sensitive, memory=memory) is None:
            continue
        ordered = sorted(linked, key=source_order.__getitem__)
        sources_for_memory = [{key: sources[identity][key] for key in ("citation", "version", "is_latest")
                               if key in sources[identity]} for identity in ordered[:4]]
        candidates.append((source_order[ordered[0]], memory.id, {
            "citation": f"[mem:{memory.id}]", "sources": sources_for_memory,
            "kind": _kind(_normal_category(memory.metadata.get("category")), memory=memory),
            "verification": _verification(memory), "omitted_sources": max(0, len(ordered) - 4),
        }))
    candidates.sort(key=lambda item: (item[0], item[1]))
    start = offset or 0
    page = candidates[start:start + 8]
    result = {
        "executed": True, "scope": "retrieved_passages_only",
        "relation": "memory_cites_source", "claim_support": "not_determined",
        "next_step": "Fetch memory citations and inspect their sources. Links are not query relevance or claim support.",
        "candidates": [item[2] for item in page],
        "matching_memories": len(candidates), "omitted_candidates": len(candidates) - len(page),
    }
    if offset is not None:
        next_offset = offset + len(page)
        has_more = next_offset < len(candidates)
        result.update(offset=offset, next_offset=next_offset if has_more else None,
                      has_more=has_more, total=len(candidates))
    return result
