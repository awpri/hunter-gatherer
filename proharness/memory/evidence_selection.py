"""Opt-in requirement coverage and source redundancy; never a support verdict."""
from __future__ import annotations

import hashlib
import re


def _quote_key(item):
    return re.sub(r"\s+", " ", item.get("quote", "")).strip()


def equivalent_requirement_candidates(packet, passages):
    """Route authorized candidate excerpts to delivered normalized text only.

    This establishes neither source equivalence, claim support, nor independent
    corroboration. Empty excerpts never establish text equivalence.
    """
    delivered = {}
    delivered_citations = {passage["citation"] for passage in passages}
    for passage in passages:
        normalized = _quote_key(passage)
        if normalized:
            delivered.setdefault(normalized, passage["citation"])
    result = []
    for candidate in packet.get("supporting_passages", [])[:2]:
        citation = delivered.get(_quote_key(candidate))
        if citation and candidate["citation"] not in delivered_citations:
            result.append({"candidate_citation": candidate["citation"],
                           "delivered_citation": citation,
                           "equivalence": "whitespace_normalized_quote"})
    return result


def select_requirements(main, requirements, key, limit):
    queues = [list(packet.get(key, [])) for packet in requirements]
    selected, seen = [], set()

    def add(item):
        identity = (_quote_key(item) or item["citation"]) if key == "supporting_passages" else item["memory_id"]
        if identity not in seen and len(selected) < limit:
            seen.add(identity)
            selected.append(item)

    # One lead per requirement per round, then fill unused capacity with the
    # original relevance-ranked candidates. Exact duplicate excerpts occupy one slot.
    while any(queues) and len(selected) < limit:
        for queue in queues:
            if queue:
                add(queue.pop(0))
    for item in main:
        add(item)
    return selected


def source_families(store, passages):
    """Group shared lineage or identical original bytes among returned sources."""
    documents = {}
    with store._lock:
        for passage in passages:
            identity = passage["document_id"]
            row = store._sqlite.execute("SELECT source_identity,content_hash FROM archive_documents WHERE id=?", (identity,)).fetchone()
            if row:
                documents[identity] = tuple(row)
    parents = {identity: identity for identity in documents}

    def root(identity):
        while parents[identity] != identity:
            identity = parents[identity]
        return identity

    ids = list(documents)
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            if any(a == b for a, b in zip(documents[left], documents[right], strict=True)):
                parents[root(right)] = root(left)
    groups = {}
    for passage in passages:
        identity = passage["document_id"]
        if identity in documents:
            family = groups.setdefault(root(identity), {"document_ids": [], "citations": [], "quote_keys": set()})
            if identity not in family["document_ids"]:
                family["document_ids"].append(identity)
            family["citations"].append(passage["citation"])
            family["quote_keys"].add(_quote_key(passage))
    result = []
    for group in groups.values():
        quotes = group.pop("quote_keys")
        group["family_id"] = hashlib.sha256("|".join(sorted(group["document_ids"])).encode()).hexdigest()[:16]
        group["duplicate_excerpt_count"] = len(group["citations"]) - len(quotes)
        group["independent_corroboration"] = "not_determined"
        result.append(group)
    return result
