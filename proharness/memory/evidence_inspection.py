"""Bounded original-page planning from already authorized evidence candidates.

This module does not search, read originals, render, assess support, or record
inspection. Its callers own the authorization snapshot for all supplied hits.
"""
from __future__ import annotations

from copy import deepcopy
import re

from proharness.memory.document_handoff import document_handoff_descriptor
from proharness.memory.original_page_reference import original_page_citation, valid_original_page_reference

_CHECK_FIELDS = {"id", "requirement", "detail", "critical_numeric"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SOURCE_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
_MEMORY_ID = re.compile(r"[^\s:\[\]\x00]{1,100}")
_MAX_CANDIDATES = 8
_POLICY = (
    "Metadata-only suggestions from supplied checks and returned candidates. "
    "A candidate-page link does not establish that the page answers the detail. "
    "Selection, rendering and retrieval never record inspection. Every check "
    "remains uninspected, meaning no inspection is recorded by this plan; outside "
    "inspection history is unknown. critical_numeric is caller-declared and an "
    "absence of extraction warnings never credits inspection. Fetch and inspect "
    "exact pages and compare their returned "
    "source_reference with expected_source_reference. The page budget bounds this "
    "plan only, not subsequent tool calls or source availability."
)


def validate_inspection_plan(value, query, requirements):
    """Validate explicit planning inputs before retrieval; None preserves defaults."""
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"page_budget", "checks"}:
        raise ValueError("inspection_plan requires only page_budget and checks")
    budget, checks = value["page_budget"], value["checks"]
    if type(budget) is not int or not 0 <= budget <= 8:
        raise ValueError("inspection_plan page_budget must be an integer from 0 to 8")
    if type(checks) is not list or not 1 <= len(checks) <= 12:
        raise ValueError("inspection_plan checks must contain 1 to 12 checks")
    allowed = {question.strip().casefold(): question.strip() for question in [query, *requirements]}
    normalized, seen = [], set()
    for check in checks:
        if (type(check) is not dict or not {"id", "requirement", "detail"} <= set(check)
                or not set(check) <= _CHECK_FIELDS):
            raise ValueError("inspection_plan checks require id, requirement, detail and optional critical_numeric")
        identity = check["id"]
        if type(identity) is not str or not _ID.fullmatch(identity) or identity in seen:
            raise ValueError("inspection_plan check IDs must be unique ASCII identifiers of 1 to 64 characters")
        question, detail = check["requirement"], check["detail"]
        if type(question) is not str or not 1 <= len(question) <= 4000 or not question.strip():
            raise ValueError("inspection_plan requirement must contain 1 to 4000 characters")
        if question.strip().casefold() not in allowed:
            raise ValueError("inspection_plan requirement must match the main query or a supplied requirement")
        if type(detail) is not str or not 1 <= len(detail) <= 240 or not detail.strip():
            raise ValueError("inspection_plan detail must contain 1 to 240 characters")
        critical = check.get("critical_numeric", False)
        if type(critical) is not bool:
            raise ValueError("inspection_plan critical_numeric must be boolean")
        seen.add(identity)
        normalized.append({"id": identity, "requirement": allowed[question.strip().casefold()],
                           "detail": detail.strip(), "critical_numeric": critical})
    return {"page_budget": budget, "checks": normalized}


def _candidates(packet):
    """Yield direct passages, then linked passages, then source-less memories."""
    if type(packet) is not dict:
        return
    passages = packet.get("supporting_passages", [])
    if type(passages) is list:
        yield from passages
    memories = packet.get("relevant_memories", [])
    if type(memories) is not list:
        return
    source_less = []
    for memory in memories:
        if type(memory) is not dict:
            continue
        provenance = memory.get("provenance", {})
        sources = provenance.get("sources", []) if type(provenance) is dict else []
        if type(sources) is list and sources:
            yield from sources
        else:
            source_less.append(memory)
    yield from source_less


def build_inspection_plan(store, normalized, candidates_by_query, include_sensitive=False):
    """Project at most 96 distinct pages without fetching or crediting inspection.

    Only the first eight distinct citation candidates per check receive metadata
    work. Remaining candidate identities are counted, never looked up or emitted.
    Document handoffs are cached by exact document/chunk identity within this call.
    Allocation is critical_detail_coverage_v1: greedily prefer the most new
    critical check IDs, then most new check IDs, then total critical IDs, then
    total check IDs; exact page citation ascending breaks remaining ties. Once
    all IDs have a suggested page, additional pages may still fill the budget.
    Coverage counts declared checks, not all requested details or claim support.
    """
    if type(include_sensitive) is not bool:
        raise ValueError("include_sensitive must be boolean")
    pages, checks, handoffs = {}, [], {}
    for declared in normalized["checks"]:
        result = {**deepcopy(declared), "candidate_citations": [], "page_citations": [],
                  "no_page_candidates": [], "omitted_candidate_count": 0}
        seen = set()
        packet = candidates_by_query.get(declared["requirement"].casefold(), {})
        for candidate in _candidates(packet):
            if type(candidate) is not dict:
                continue
            citation = candidate.get("citation")
            if type(citation) is not str or not 1 <= len(citation) <= 240 or citation in seen:
                continue
            if "memory_id" in candidate:
                identity = candidate["memory_id"]
                if (type(identity) is not str or not _MEMORY_ID.fullmatch(identity)
                        or citation != f"[mem:{identity}]"):
                    continue
            else:
                identities = (candidate.get("document_id"), candidate.get("chunk_id"))
                if (not all(type(identity) is str and _SOURCE_ID.fullmatch(identity) for identity in identities)
                        or citation != f"[doc:{identities[0]}#{identities[1]}]"):
                    continue
            seen.add(citation)
            if len(result["candidate_citations"]) >= _MAX_CANDIDATES:
                result["omitted_candidate_count"] += 1
                continue
            result["candidate_citations"].append(citation)
            if "memory_id" in candidate:
                result["no_page_candidates"].append({"citation": citation, "reason": "memory_without_original_page"})
                continue
            key = (candidate["document_id"], candidate["chunk_id"])
            if key not in handoffs:
                handoffs[key] = document_handoff_descriptor(store, candidate, include_sensitive=include_sensitive)
            descriptor = handoffs[key]
            hint = descriptor.get("original_page") if descriptor else None
            ref = hint.get("expected_source_reference") if hint else None
            if not valid_original_page_reference(ref):
                reason = (descriptor.get("original_page_omitted", {}).get("reason", "no_original_page_metadata")
                          if descriptor else "invalid_source_metadata")
                result["no_page_candidates"].append({"citation": citation, "reason": reason})
                continue
            page_citation = original_page_citation(ref)
            if page_citation not in result["page_citations"]:
                result["page_citations"].append(page_citation)
            if page_citation not in pages:
                followup = deepcopy(hint["followup"])
                pages[page_citation] = {"citation": page_citation, "source_reference": deepcopy(ref),
                    "expected_source_reference": deepcopy(ref), "followup": followup,
                    "availability": "unchecked", "check_ids": [], "critical_check_ids": [],
                    "source_candidate_citations": [], "content_policy": hint["content_policy"]}
            page = pages[page_citation]
            if declared["id"] not in page["check_ids"]:
                page["check_ids"].append(declared["id"])
                if declared["critical_numeric"]:
                    page["critical_check_ids"].append(declared["id"])
            if citation not in page["source_candidate_citations"]:
                page["source_candidate_citations"].append(citation)
        checks.append(result)

    selected, covered = [], set()
    while len(selected) < normalized["page_budget"] and len(selected) < len(pages):
        remaining = [page for citation, page in pages.items() if citation not in selected]
        def priority(page):
            return (-len(set(page["critical_check_ids"]) - covered),
                    -len(set(page["check_ids"]) - covered),
                    -len(page["critical_check_ids"]), -len(page["check_ids"]), page["citation"])
        page = min(remaining, key=priority)
        selected.append(page["citation"])
        covered.update(page["check_ids"])
    for check in checks:
        check["selected_page_citations"] = [citation for citation in check["page_citations"] if citation in selected]
        check["unselected_page_citations"] = [citation for citation in check["page_citations"] if citation not in selected]
        if not check["candidate_citations"]:
            status = "no_candidates"
        elif not check["page_citations"]:
            status = "no_original_page_candidates"
        elif not check["selected_page_citations"]:
            status = "excluded_by_page_budget"
        elif check["unselected_page_citations"] or check["omitted_candidate_count"] or check["no_page_candidates"]:
            status = "partially_planned"
        else:
            status = "planned"
        check.update(planning_status=status, inspection_status="uninspected", claim_support="not_determined")
    return {"page_budget": normalized["page_budget"], "checks": checks,
            "candidate_pages": [pages[citation] for citation in sorted(pages)],
            "selected_page_citations": selected,
            "uninspected_check_ids": [check["id"] for check in checks],
            "claim_support": "not_determined", "inspection_history": "not_tracked",
            "planning_policy": "critical_detail_coverage_v1",
            "coverage_scope": "supplied_checks_and_returned_candidates",
            "coverage_unit": "declared_checks_not_all_requested_details", "content_policy": _POLICY}
