"""Experimental heading routing with an unchanged global-retrieval fallback."""
from __future__ import annotations

import math

from proharness.memory.documents import _tokens
from proharness.memory.retrieval_assessment import assess_retrieval


def search_sections(archive, query, top_k=5, include_sensitive=False, document_ids=None,
                    response_format="full", explain=False, *, candidate_budget=None):
    # The established path validates every public option and retains global
    # candidates in case a heading does not describe the needed source detail.
    limit = max(1, min(int(top_k), 30))
    baseline = archive.search(query, limit, include_sensitive, document_ids, response_format, explain,
                              **({"candidate_budget": candidate_budget} if candidate_budget is not None else {}))
    if not query.strip() or document_ids == []:
        return {**baseline, "retrieval_strategy": "sections", "routed_sections": []}
    clause, params = archive._visible(include_sensitive), []
    if document_ids is not None:
        clause += " AND d.id IN (" + ",".join("?" for _ in document_ids) + ")"
        params.extend(document_ids)
    terms = set(_tokens(query))
    with archive.store._lock:
        rows = archive.store._sqlite.execute("SELECT s.id,s.document_id,s.section,s.page FROM archive_sections s JOIN archive_documents d ON d.id=s.document_id WHERE " + clause, params).fetchall()
    ranked = []
    for row in rows:
        heading = set(_tokens(row["section"]))
        overlap = terms & heading
        if overlap:
            ranked.append((len(overlap) / math.sqrt(max(1, len(heading))), row))
    ranked.sort(key=lambda entry: (-entry[0], entry[1]["document_id"], entry[1]["id"]))
    routes = [dict(row) for _, row in ranked[:min(12, max(4, limit))]]
    if not routes:
        return {**baseline, "retrieval_strategy": "sections", "routed_sections": [], "routing_fallback": "no_matching_heading"}
    routed = archive._search_weighted(query, limit, include_sensitive, document_ids, response_format,
                                      explain, native=True, section_ids=[r["id"] for r in routes])
    if "candidate_selection" in baseline:
        # Routed passages have a separate exhaustive pool. Do not describe the
        # combined result as fully covered by the bounded global candidate IDs.
        baseline["candidate_selection"] = {**baseline["candidate_selection"],
                                            "scope": "global_channel_only",
                                            "section_channel": "exhaustive_scoped"}
    queues = [list(routed["hits"]), list(baseline["hits"])]
    selected, seen = [], set()
    while any(queues) and len(selected) < limit:
        for queue in queues:
            if not queue or len(selected) >= limit:
                continue
            hit = queue.pop(0)
            if hit["citation"] not in seen:
                selected.append(hit)
                seen.add(hit["citation"])
    return {**baseline, "hits": selected, "retrieval_strategy": "sections", "routed_sections": routes,
            "total": max(baseline["total"], routed["total"]),
            "warnings": list(dict.fromkeys(baseline["warnings"] + routed["warnings"])) + ["Experimental heading routing interleaves exact passages with global matches. Scores belong to their retrieval channel; headings do not establish claim support."],
            "retrieval_assessment": assess_retrieval(query, [{"text": h["quote"], "citation": h["citation"]} for h in selected])}
