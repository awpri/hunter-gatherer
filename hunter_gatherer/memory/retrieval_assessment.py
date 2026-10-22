"""Bounded candidate diagnostics; never an answerability or corpus-absence test.

Frozen heuristic: exact meaningful-token coverage of quote text. Less than half the terms in every quote
signals weak matches unless a real cosine >= 0.60 warrants quote review.
These thresholds are inspection heuristics, not calibrated confidence scores.
Callers must omit semantic_similarity for hashing embeddings or unavailable data.
"""

from __future__ import annotations

import math
import re

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "document",
        "documents",
        "explain",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "show",
        "tell",
        "that",
        "the",
        "their",
        "them",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "about",
        "find",
        "search",
        "information",
    ]
)
_MAX_TERMS = 64
_MAX_CITATIONS = 30
_MAX_TERM_CHARS = 128
_MAX_LITERAL_CHARS = 240
_MAX_CITATION_CHARS = 160
_MIN_LEXICAL_COVERAGE = 0.5
_SEMANTIC_REVIEW_THRESHOLD = 0.60


def _terms(text: str) -> set[str]:
    return {
        term
        for term in re.findall(r"[\w]+", text.casefold())
        if term not in _STOPWORDS and (len(term) > 1 or term.isdigit())
    }


def assess_retrieval(query: str, candidates: list[dict]) -> dict:
    """Assess only supplied quotes, without filtering, ranking, or inference.

    Status ``not_checked`` denotes a blank query; ``no_candidates`` means no
    returned candidates for a nonblank query. All candidate statuses require
    actual quote review. Output term/citation lists are bounded and say when
    truncated; counts and decisions use all supplied terms and candidates.
    Display strings are capped; oversized citations are omitted rather than
    shortened into misleading references.
    """
    terms = _terms(query)
    found = set().union(*(_terms(c.get("text", "")) for c in candidates)) if candidates else set()
    matched, missing = sorted(terms & found), sorted(terms - found)
    coverage = (
        max((len(terms & _terms(c.get("text", ""))) / len(terms) for c in candidates), default=0)
        if terms
        else 0
    )
    literals = set(re.findall(r'"([^"]+)"', query))
    literals.update(t for t in re.findall(r"[\w-]+", query) if any(ch.isdigit() for ch in t))
    missing_literals = sorted(
        literal
        for literal in literals
        if not any(
            re.search(
                r"(?<!\w)" + re.escape(literal.casefold()) + r"(?!\w)", c.get("text", "").casefold()
            )
            for c in candidates
        )
    )
    semantic = any(
        isinstance(c.get("semantic_similarity"), (int, float))
        and not isinstance(c.get("semantic_similarity"), bool)
        and math.isfinite(c["semantic_similarity"])
        and _SEMANTIC_REVIEW_THRESHOLD <= c["semantic_similarity"] <= 1
        for c in candidates
    )
    reasons = []
    if not query.strip():
        status = "not_checked"
        reasons.append("empty_query")
    elif not candidates:
        status = "no_candidates"
        reasons.append("no_returned_candidates")
    elif not terms:
        status = "review_required"
        reasons.append("no_meaningful_query_terms")
    elif missing_literals or (coverage < _MIN_LEXICAL_COVERAGE and not semantic):
        status = "weak_matches"
        reasons.append(
            "query_literal_not_observed" if missing_literals else "limited_query_term_coverage"
        )
    else:
        status = "review_required"
        reasons.append(
            "lexical_overlap_requires_review"
            if coverage >= _MIN_LEXICAL_COVERAGE
            else "semantic_signal_requires_review"
        )
    if missing_literals and "query_literal_not_observed" not in reasons:
        reasons.append("query_literal_not_observed")
    if candidates:
        reasons.append("quote_review_required")
    if missing:
        reasons.append("missing_terms_not_corpus_absence")
    citations = list(
        dict.fromkeys(c["citation"] for c in candidates if isinstance(c.get("citation"), str))
    )
    safe_citations = [citation for citation in citations if len(citation) <= _MAX_CITATION_CHARS]
    actions = ["review_returned_quotes"] if candidates else []
    if candidates and (missing or missing_literals):
        actions.append("inspect_missing_query_details")
    if status in {"no_candidates", "weak_matches"}:
        actions.append("broaden_query_or_sources")
    return {
        "status": status,
        "answerability": "not_determined",
        "scope": "returned_candidates_only",
        "reason_codes": reasons,
        "matched_query_terms": [term[:_MAX_TERM_CHARS] for term in matched[:_MAX_TERMS]],
        "missing_query_terms": [term[:_MAX_TERM_CHARS] for term in missing[:_MAX_TERMS]],
        "missing_query_literals": [
            literal[:_MAX_LITERAL_CHARS] for literal in missing_literals[:_MAX_TERMS]
        ],
        "literals_truncated": len(missing_literals) > _MAX_TERMS
        or any(len(literal) > _MAX_LITERAL_CHARS for literal in missing_literals),
        "query_term_count": len(terms),
        "terms_truncated": len(matched) > _MAX_TERMS
        or len(missing) > _MAX_TERMS
        or any(len(term) > _MAX_TERM_CHARS for term in matched + missing),
        "candidate_count": len(candidates),
        "candidate_citations": safe_citations[:_MAX_CITATIONS],
        "citations_truncated": len(safe_citations) > _MAX_CITATIONS
        or len(safe_citations) != len(citations),
        "recommended_actions": actions,
    }
