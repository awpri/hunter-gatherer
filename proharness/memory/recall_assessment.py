"""Cheap, non-filtering wording checks on the memories actually delivered."""

from proharness.memory.retrieval_assessment import assess_retrieval


def assess_memory_matches(query, hits, *, include_sensitive=False):
    # Recall relevance may be a hybrid/backend score, not cosine similarity.
    # Never feed it into a semantic threshold or infer confidence from it.
    result = assess_retrieval(query, [
        {"text": hit.memory.text, "citation": f"[mem:{hit.memory.id}]"}
        for hit in hits
    ])
    result.update(
        method="query_wording_overlap",
        filtering_applied=False,
        limitations=("Checks only returned memory wording. Paraphrases may have weak overlap; "
                     "no semantic relevance, claim support or library-wide absence is established."),
    )
    if result["status"] in {"weak_matches", "no_candidates"}:
        if len(query) <= 4000:
            arguments = {
                "query": query, "top_k": 5, "source_discovery": True,
                "response_format": "bounded", "max_tokens": 2048,
            }
            if include_sensitive:
                arguments["include_sensitive"] = True
            result["followups"] = [{
                "tool": "proharness_evidence", "arguments": arguments,
                "purpose": "Search passages and file metadata as well as memories; inspect candidate sources before answering.",
            }]
        else:
            result["followups_omitted"] = "query_exceeds_evidence_discovery_limit"
    return result
