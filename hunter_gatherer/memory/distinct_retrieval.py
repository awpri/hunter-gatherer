"""Opt-in exact-copy grouping inside a bounded, authorized candidate pool.

This is source diversity, not semantic filtering or corroboration assessment.
No source text, score or citation is rewritten. Distinct original bytes and
distinct positions in an original always remain separate candidates.
"""
import json
import re

from hunter_gatherer.memory.library_health import _read_snapshot
from hunter_gatherer.memory.retrieval_assessment import assess_retrieval

_CANDIDATE_LIMIT = 30
_HASH = re.compile(r"[0-9a-f]{64}")
_POLICY = (
    "Duplicate sources share the stored original hash and exact excerpt/position. "
    "Their quote_equals points only to identical quote text; source labels, warnings "
    "and citations remain separate. This proves neither source truth nor independent corroboration. "
    "Selection covers only the returned candidate pool, not all distinct archive passages."
)


def search_distinct(archive, query, top_k=5, include_sensitive=False, document_ids=None,
                    response_format="full", explain=False, *, candidate_budget=None):
    if response_format not in ("full", "compact"):
        raise ValueError("response_format must be full or compact")
    if type(include_sensitive) is not bool:
        raise ValueError("include_sensitive must be boolean")
    if type(explain) is not bool:
        raise ValueError("explain must be a boolean")
    limit = max(1, min(int(top_k), 30))
    # Normal archive search validates scope/options and retains its existing
    # candidate-channel policy. Compact candidates avoid fetching 30 contexts.
    with _read_snapshot(archive.store) as db:
        baseline = archive.search(query, top_k=_CANDIDATE_LIMIT,
            include_sensitive=include_sensitive, document_ids=document_ids,
            response_format="compact", explain=True,
            **({"candidate_budget": candidate_budget} if candidate_budget is not None else {}))
        candidates = baseline["hits"]
        similarities = {hit["citation"]: hit.get("explanation", {}).get("semantic_similarity")
                        for hit in candidates}
        if not explain:
            for hit in candidates:
                hit.pop("explanation", None)
        coordinates = {}
        if candidates:
            rows = db.execute(
                "SELECT c.id,c.document_id,c.ordinal,s.ordinal AS section_ordinal,"
                "CASE WHEN typeof(d.content_hash)='text' AND length(CAST(d.content_hash AS BLOB))=64 "
                "THEN d.content_hash ELSE NULL END AS content_hash "
                "FROM archive_chunks c JOIN archive_documents d ON d.id=c.document_id "
                "JOIN archive_sections s ON s.id=c.section_id AND s.document_id=c.document_id "
                "WHERE c.id IN (" + ",".join("?" for _ in candidates) + ")",
                [h["chunk_id"] for h in candidates],
            ).fetchall()
            coordinates = {row["id"]: row for row in rows}
        groups, ordered = {}, []
        for rank, hit in enumerate(candidates, 1):
            row = coordinates.get(hit["chunk_id"])
            # Unusable grouping metadata must not discard an otherwise valid
            # retrieved passage or infer that unrelated originals are copies.
            key = ("citation", hit["citation"])
            if (row is not None and row["document_id"] == hit["document_id"]
                    and isinstance(row["content_hash"], str) and _HASH.fullmatch(row["content_hash"])
                    and type(row["ordinal"]) is int and type(row["section_ordinal"]) is int):
                key = (row["content_hash"], row["ordinal"], row["section_ordinal"],
                       json.dumps(hit["location"], sort_keys=True), hit["quote"])
            if key not in groups:
                item = {**hit, "candidate_rank": rank}
                groups[key] = item
                ordered.append(item)
            else:
                representative = groups[key]
                alternative = {key: value for key, value in hit.items()
                               if key not in {"quote", "context", "context_location"}}
                alternative.update(candidate_rank=rank, quote_equals=representative["citation"])
                representative.setdefault("duplicate_sources", []).append(alternative)
        selected = ordered[:limit]
        if response_format == "full":
            for item in selected:
                full = archive.get_chunk(item["chunk_id"], include_sensitive=include_sensitive)
                if full.get("error") or full.get("quote") != item["quote"]:
                    raise ValueError("Selected source is unavailable; retry distinct search")
                item["context"], item["context_location"] = full["context"], full["context_location"]
        result = {**baseline, "hits": selected, "retrieval_strategy": "distinct",
                  "duplicate_selection": {
                      "method": "same_original_hash_exact_text_and_position",
                      "candidate_limit": _CANDIDATE_LIMIT, "candidate_count": len(candidates),
                      "distinct_candidates": len(ordered), "requested_top_k": limit,
                      "collapsed_passages": len(candidates) - len(ordered),
                      "distinct_candidates_omitted": max(0, len(ordered) - limit),
                      "scope": "returned_candidate_pool_only", "content_policy": _POLICY,
                  },
                  "retrieval_assessment": assess_retrieval(query, [
                      {"text": hit["quote"], "citation": hit["citation"],
                       "semantic_similarity": similarities.get(hit["citation"])} for hit in selected])}
        return result
