"""Source-first navigation over one authorized passage-candidate pool.

This groups by document identity, not inferred topic, quality or corroboration.
It performs no follow-up searches and emits no source prose as evidence.
"""

from copy import deepcopy

from proharness.memory.document_handoff import _ID, _LABEL_LIMITS
from proharness.memory.extraction_warnings import warning_summary
from proharness.memory.library_health import _read_snapshot
from proharness.memory.retrieval_assessment import assess_retrieval

_CANDIDATES = 30
_REFERENCES = 3
_POLICY = (
    "Document routes are untrusted discovery metadata, not quoted evidence or claim support. "
    "Only the returned passage-candidate pool is grouped; unreturned documents may be relevant. "
    "Inspect a scoped-search or fetch result before answering. Distinct document IDs do not "
    "establish independent corroboration. Follow-ups recheck current source visibility."
)


def _descriptor(hit):
    result = {
        key: deepcopy(hit.get(key))
        for key in ("document_id", "version", "is_latest", "latest_version", "source_uri")
    }
    warnings = []
    for key, limit in _LABEL_LIMITS.items():
        value = hit.get(key)
        if type(value) is not str or len(value) > limit or "\x00" in value:
            value = None
            warnings.append(f"{key}_unavailable")
        result[key] = value
    if warnings:
        result["source_metadata_warnings"] = warnings
    result.update(warning_summary(hit.get("warnings")))
    return result


def search_sources(
    archive,
    query,
    top_k=5,
    include_sensitive=False,
    document_ids=None,
    response_format="full",
    explain=False,
    *,
    candidate_budget=None,
):
    """Return document routes; full/compact both omit bodies in this strategy."""
    if type(query) is not str or not query.strip() or len(query) > 4000:
        raise ValueError("sources query must contain 1–4000 characters")
    if type(top_k) is not int or not 1 <= top_k <= 30:
        raise ValueError("sources top_k must be an integer from 1 to 30")
    if type(include_sensitive) is not bool or type(explain) is not bool:
        raise ValueError("include_sensitive and explain must be booleans")
    if response_format not in {"full", "compact"}:
        raise ValueError("response_format must be full or compact")
    policy = archive.store.settings.archive_source_routing_policy
    with _read_snapshot(archive.store):
        base = archive.search(
            query,
            top_k=_CANDIDATES,
            include_sensitive=include_sensitive,
            document_ids=document_ids,
            response_format="compact",
            explain=explain,
            **({"document_heads": True} if policy == "document_heads" else {}),
            **({"candidate_budget": candidate_budget} if candidate_budget is not None else {}),
        )
        groups = {}
        for rank, hit in enumerate(base["hits"], 1):
            document, chunk = hit.get("document_id"), hit.get("chunk_id")
            if (
                type(document) is not str
                or not _ID.fullmatch(document)
                or type(chunk) is not str
                or not _ID.fullmatch(chunk)
                or hit.get("citation") != f"[doc:{document}#{chunk}]"
            ):
                raise ValueError("Invalid source identity; retry source search")
            if document not in groups:
                groups[document] = {
                    **_descriptor(hit),
                    "best_candidate_rank": rank,
                    "matched_passages_in_pool": 0,
                    "passage_references": [],
                    "passage_references_omitted": 0,
                }
            route = groups[document]
            route["matched_passages_in_pool"] += 1
            if len(route["passage_references"]) < _REFERENCES:
                reference = {"citation": hit["citation"], "candidate_rank": rank}
                if explain and "explanation" in hit:
                    reference["explanation"] = deepcopy(hit["explanation"])
                route["passage_references"].append(reference)
            else:
                route["passage_references_omitted"] += 1
        selected = list(groups.values())[:top_k]
        for route in selected:
            if explain:
                # Reuse only this source's already-loaded candidate quotes. No
                # semantic/confidence conclusion, extra read, or filtering.
                assessment = assess_retrieval(query, [
                    {"text": hit["quote"]} for hit in base["hits"]
                    if hit["document_id"] == route["document_id"]
                ])
                route["query_match"] = {
                    "basis": "literal_terms_in_retrieved_passages",
                    "scope": "this_document_candidates_only",
                    **{key: assessment[key] for key in (
                        "matched_query_terms", "missing_query_terms",
                        "missing_query_literals", "query_term_count",
                        "terms_truncated", "literals_truncated", "candidate_count",
                    )},
                    "meaning": "Term presence is not claim support; missing terms are not document absence.",
                }
            private = {"include_sensitive": True} if include_sensitive else {}
            arguments = {
                "query": query,
                "document_ids": [route["document_id"]],
                "top_k": 5,
                "response_format": "compact",
                "retrieval_strategy": "flat",
                **private,
            }
            if candidate_budget is not None:
                arguments["candidate_budget"] = candidate_budget
            route["followups"] = [
                {"tool": "proharness_search_documents", "arguments": arguments},
                {
                    "tool": "proharness_fetch",
                    "arguments": {
                        "refs": [ref["citation"] for ref in route["passage_references"]],
                        **private,
                    },
                },
            ]
        result = {key: value for key, value in base.items() if key not in {"hits", "total"}}
        if isinstance(result.get("retrieval_assessment"), dict):
            result["retrieval_assessment"] = {
                **result["retrieval_assessment"],
                "scope": "retrieved_passage_candidates_before_document_grouping",
            }
        result.update(
            documents=selected,
            retrieval_strategy="sources",
            total_passage_matches=base.get("total"),
            content_policy=_POLICY,
            source_selection={
                "candidate_policy": policy,
                "candidate_limit": _CANDIDATES,
                "candidate_passages": len(base["hits"]),
                "documents_in_pool": len(groups),
                "documents_selected": len(selected),
                "documents_omitted_by_top_k": max(0, len(groups) - len(selected)),
                "scope": "returned_candidate_pool_only",
                "ordering": "first_passage_rank",
                "answerability": "not_determined",
            },
        )
        return result
