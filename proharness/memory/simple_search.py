"""One bounded library discovery operation, composed from existing evidence reads.

No writing, reranking, query-rewrite or answerability model is called. Existing
configured embeddings may be used by the evidence machinery. Source links
discover records with different wording; they do not establish query relevance.
"""
from __future__ import annotations

import hashlib
import math
import re
from copy import deepcopy
from pathlib import Path

from proharness.memory import find_cursor
from proharness.memory.evidence import _evidence_requirements, build_evidence_packet
from proharness.memory.evidence_bounded import bounded_evidence_packet
from proharness.memory.output_budget import budget_report, output_bytes, validate_budget


def _subject_references(packet, queries):
    """Defer weak off-subject text when a named subject is actually located.

    Capitalization is a conservative clue, not entity recognition. Never require
    a name on every passage: titles, sections, strong semantic matches, explicit
    source links, and requirement candidates can all retain useful context.
    Deferred sources keep exact Open references; this is not an absence verdict.
    """
    from proharness.memory.retrieval_assessment import _terms

    candidates = [*packet["supporting_passages"], *packet["relevant_memories"]]
    terms = set().union(*(_terms(query) for query in queries))
    names = {word.casefold() for index, word in enumerate(re.findall(r"[^\W\d]\w*", queries[0]))
             if word[0].isupper() and len(word) > 2 and word.casefold() in terms
             # A sentence-initial common noun is not a reliable named subject.
             and (index > 0 or any(letter.isupper() for letter in word[1:]))}
    searchable = {item["citation"]: _terms(" ".join((
        item.get("quote", item.get("text", "")), str(item.get("title", "")),
        str(item.get("location", {}).get("section") or "")))) for item in candidates}
    located = names & set().union(*searchable.values()) if searchable else set()
    if not located:
        return frozenset()
    required = {citation for check in packet.get("requirement_checks", [])
                for citation in check.get("selected_citations", [])}
    linked = {source.get("citation") for memory in packet["relevant_memories"]
              if located & searchable[memory["citation"]]
              for source in memory.get("provenance", {}).get("sources", [])}
    deferred = set()
    for item in candidates:
        citation = item["citation"]
        if citation in required or citation in linked or located & searchable[citation]:
            continue
        # Multiple literal clues can supply useful context without repeating a
        # name. This rule only defers thin incidental overlap.
        if len(terms & searchable[citation]) >= 2:
            continue
        semantic = (item.get("explanation", {}).get("semantic_similarity")
                    if "quote" in item else item.get("semantic_relevance"))
        if (isinstance(semantic, (int, float)) and not isinstance(semantic, bool)
                and math.isfinite(semantic) and .35 <= semantic <= 1):
            continue
        deferred.add(citation)
    return frozenset(deferred)


def _coverage_priorities(packet, queries, *, statistics_candidates=None):
    """Allocate across memories and passages using the actual question.

    Weighted literal coverage favors specific clues over incidental common words.
    Existing semantic signals preserve a route for paraphrases. These are ordering
    heuristics, never answerability judgments or reasons to discard a candidate.
    Optional statistics_candidates freezes ordinary retrieval IDFs before graph
    navigation is appended; every candidate is scored against those same weights.
    """
    from collections import Counter

    from proharness.memory.query_focus import query_idfs, query_terms, weighted_coverage

    candidates = [*packet["supporting_passages"], *packet["relevant_memories"]]
    texts = [set(query_terms(item.get("quote", item.get("text", "")))) for item in candidates]
    metadata = [set(query_terms(" ".join((str(item.get("title", "")),
                 str(item.get("location", {}).get("section") or ""))))) for item in candidates]
    if statistics_candidates is None:
        statistics_terms = [body | meta for body, meta in zip(texts, metadata, strict=True)]
    else:
        statistics_terms = [query_terms(item.get("quote", item.get("text", ""))) |
            query_terms(" ".join((str(item.get("title", "")),
                str(item.get("location", {}).get("section") or ""))))
            for item in statistics_candidates]
    frequencies = Counter(term for present in statistics_terms for term in present)
    weights = [query_idfs(query_terms(query), frequencies, len(statistics_terms)) for query in queries]
    priorities = {}
    for item, body, meta in zip(candidates, texts, metadata, strict=True):
        semantic = item.get("semantic_relevance")
        if "quote" in item:
            semantic = item.get("explanation", {}).get("semantic_similarity")
        if (not isinstance(semantic, (int, float)) or isinstance(semantic, bool)
                or not math.isfinite(semantic) or not 0 <= semantic <= 1):
            semantic = 0.0
        score = max((weighted_coverage(weight, body) + .25 * weighted_coverage(weight, meta)
                     for weight in weights), default=0.0) + .45 * semantic
        priorities[item["citation"]] = score
    return priorities


def _body_first_references(packet, queries):
    """Conservative presentation rule, never a relevance or absence verdict.

    Keep any literal body match, any returned semantic match, and every selected
    requirement citation. Other candidates retain references instead of quotes.
    No query expansion, new embedding, source read or answer model is performed.
    """
    from proharness.memory.retrieval_assessment import _terms

    terms = set().union(*(_terms(query) for query in queries))
    if not terms:
        return frozenset()
    required = {citation for check in packet.get("requirement_checks", [])
                for citation in check.get("selected_citations", [])}
    deferred = set()
    for item in [*packet["supporting_passages"], *packet["relevant_memories"]]:
        citation = item.get("citation")
        if not citation or citation in required:
            continue
        body = item.get("quote", item.get("text", ""))
        semantic = item.get("semantic_relevance")
        if "quote" in item:
            semantic = item.get("explanation", {}).get("semantic_similarity")
        if (isinstance(semantic, (int, float)) and not isinstance(semantic, bool)
                and math.isfinite(semantic) and .35 <= semantic <= 1):
            continue
        if not terms.intersection(_terms(body)):
            deferred.add(citation)
    return frozenset(deferred)


def _focused_references(packet, queries):
    """Defer thin matches to exact Open leads, without removing candidates.

    This opt-in presentation heuristic never declares corpus absence. A shared
    capitalized subject alone is weak evidence for a requested detail. Real
    semantic matches can preserve paraphrases; these thresholds are review
    heuristics, not probabilities. No extra embedding/model calls are made.
    """
    from proharness.memory.retrieval_assessment import _terms

    generic = {"name", "named", "date", "year", "know", "known", "next", "his", "her"}
    focuses = []
    for query in queries:
        terms = _terms(query)
        names = {word.casefold() for index, word in enumerate(re.findall(r"[^\W\d]\w*", query))
                 if word[0].isupper() and word.casefold() in terms
                 and (index > 0 or any(letter.isupper() for letter in word[1:]))}
        focus = terms - names - generic
        focuses.append((focus or terms, bool(names and focus)))
    required = {citation for check in packet.get("requirement_checks", [])
                for citation in check.get("selected_citations", [])}
    deferred = {}
    for item in [*packet["supporting_passages"], *packet["relevant_memories"]]:
        citation = item["citation"]
        if citation in required:
            continue
        body = item.get("quote", item.get("text", ""))
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if "quote" in item and lines and all(re.match(r"^#{1,6}\s+", line) for line in lines):
            deferred[citation] = "heading_only_navigation"
            continue
        semantic = (item.get("explanation", {}).get("semantic_similarity")
                    if "quote" in item else item.get("semantic_relevance"))
        if (isinstance(semantic, bool) or not isinstance(semantic, (int, float))
                or not math.isfinite(semantic) or not 0 <= semantic <= 1):
            semantic = 0.0
        body_terms = _terms(body)
        if any(focus & body_terms or semantic >= (.70 if named else .60)
               for focus, named in focuses):
            continue
        deferred[citation] = "weak_query_body_match"
    return deferred


def _open(ref, include_sensitive, view="text"):
    return {"tool": "proharness_open", "arguments": {
        "refs": [ref], "view": view, "max_tokens": 2048,
        "include_sensitive": include_sensitive,
    }}


def _public_routes(value, include_sensitive):
    """Translate only routes carrying an exact executable reference.

    Do not reconstruct a citation from a document ID. Original-file citations
    carry version/hash identity; existing source citations retain their version.
    The bounded formatter creates some legacy routes, so run this after it too.
    """
    if isinstance(value, list):
        for item in value:
            _public_routes(item, include_sensitive)
    elif isinstance(value, dict):
        for key, item in list(value.items()):
            if key not in {"followups", "followup"}:
                _public_routes(item, include_sensitive)
        for key in ("followups", "followup"):
            if key not in value:
                continue
            supplied = value[key] if key == "followups" else [value[key]]
            routes = []
            for route in supplied:
                if not isinstance(route, dict):
                    continue
                tool = route.get("tool")
                if tool in {"proharness_find", "proharness_open", "proharness_memory",
                            "proharness_collection", "proharness_import_documents", "proharness_status"}:
                    routes.append(route)
                    continue
                arguments = route.get("arguments", {})
                refs = arguments.get("refs")
                citation = value.get("original_citation") or value.get("citation")
                if tool == "proharness_fetch" and refs:
                    routes.append({"tool": "proharness_open", "arguments": {
                        "refs": refs, "view": "text", "max_tokens": 2048,
                        "include_sensitive": include_sensitive}})
                elif citation and tool in {"proharness_document_outline", "proharness_get_document"}:
                    routes.append(_open(citation, include_sensitive, "outline"))
                elif citation and tool == "proharness_document_window":
                    routes.append(_open(citation, include_sensitive, "context"))
                elif tool == "proharness_document_page_image":
                    from proharness.memory.original_page_reference import (
                        original_page_citation,
                        parse_original_page_citation,
                        valid_original_page_reference,
                    )
                    expected = route.get("expected_source_reference", value.get("expected_source_reference"))
                    if valid_original_page_reference(expected):
                        routes.append(_open(original_page_citation(expected), include_sensitive, "page"))
                    elif parse_original_page_citation(citation):
                        routes.append(_open(citation, include_sensitive, "page"))
                # Unknown locations and unreferenced browse routes are not
                # executable public open calls; they deliberately disappear.
            routes = [route for i, route in enumerate(routes) if route not in routes[:i]]
            if key == "followups":
                value[key] = routes
            elif routes:
                value[key] = routes[0]
            else:
                value.pop(key)


def public_followups(value, *, include_sensitive=False):
    """Return a copy with structural followups mapped to exact public open calls.

    Source text/quotes are never modified, even when they mention legacy tools.
    Routes without an executable exact citation are dropped, not fabricated.
    """
    if type(include_sensitive) is not bool:
        raise ValueError("include_sensitive must be a boolean")
    result = deepcopy(value)
    _public_routes(result, include_sensitive)
    return result


def find(harness, query: str | None = None, requirements: list[str] | None = None,
         max_tokens: int = 2048, include_sensitive: bool = False,
         cursor: str | None = None, constraints: dict | None = None,
         contextual_requirements: bool | None = None) -> dict:
    """One bounded search page, or revision-pinned continuation of that search.

    Continuations retain only query/options and offsets, never evidence bodies.
    The existing retrieval model and thresholds determine the candidate scope;
    reaching its end does not establish that an answer is absent from the library.
    """
    validate_budget(max_tokens)
    if type(include_sensitive) is not bool:
        raise ValueError("include_sensitive must be a boolean")
    if contextual_requirements is not None and type(contextual_requirements) is not bool:
        raise ValueError("contextual_requirements must be a boolean")
    if constraints is not None:
        from proharness.memory.relationship_constraints import validate_constraints
        constraints = validate_constraints(constraints)
        if constraints and not getattr(harness.settings, "library_find_relationships", False):
            raise ValueError("Explicit graph constraints require library_find_relationships enabled; use ordinary Find without constraints otherwise.")
    with harness.store._lock:
        before = find_cursor.revision(harness.store)
        if cursor is not None:
            state = find_cursor.resume(harness.store, cursor, include_sensitive, query, requirements, constraints,
                                       contextual_requirements)
        else:
            if type(query) is not str or not query.strip() or len(query) > 4000:
                raise ValueError("query must contain 1 to 4000 characters (or supply a Find cursor)")
            state = {"query": query.strip(), "requirements": _evidence_requirements(requirements),
                     "include_sensitive": include_sensitive, "offsets": {}, "pending": {}}
            if constraints:
                state["constraints"] = constraints
            if contextual_requirements:
                state["contextual_requirements"] = True
        from proharness.memory.retrieval_context import retrieval_context
        # Relationship navigation reuses the ordinary query vector. Enabling it
        # must not also change the semantic input of the baseline search.
        with retrieval_context(state['query']):
            result, next_state = _find_page(harness, state, max_tokens, continuation=cursor is not None)
        after = find_cursor.revision(harness.store)
        # Own index preparation may write before the read snapshot. External
        # commits during assembly cannot be attached to the newer revision.
        if before[1] != after[1]:
            raise ValueError("Library changed during Find; restart Find.")
        if next_state is not None:
            find_cursor.issue(harness.store, next_state, after,
                              token=result["continuation"]["next_cursor"])
        return result


def _find_page(harness, state, max_tokens, *, continuation):
    query, questions = state["query"], state["requirements"]
    include_sensitive = state["include_sensitive"]
    policy = harness.settings.library_find_delivery_policy
    relevance = harness.settings.library_find_relevance_policy
    focused = relevance == "coverage"
    packet = build_evidence_packet(
        harness.store, query.strip(), top_k=5, include_sensitive=include_sensitive,
        evidence_requirements=questions,
        **({"contextual_requirements": True} if state.get("contextual_requirements") else {}),
        selection_strategy="requirements" if questions else "relevance",
        source_discovery=True, memory_discovery=True, include_context=False,
        retrieval_offsets={key: value for key, value in state["offsets"].items()
                           if key in {"memories", "passages", "catalog"}}, retrieval_continuation=continuation,
        **({"explain_document_matches": True} if policy in {"body_first", "focused"} or focused else {}),
        **({"document_query_strategy": "original"} if relevance != "legacy" else {}),
        **({"document_relevance_policy": "coverage"} if focused else {}),
    )
    paging = packet.pop("retrieval_page")
    graph_items, graph_metadata = [], None
    graph_seen = set(state["pending"].get("relationship_seen", []))
    graph_bodies = set(state["pending"].get("relationship_bodies", []))
    graph_seeds = list(state["pending"].get("relationship_seeds", []))
    if getattr(harness.settings, "library_find_relationships", False):
        from proharness.memory.relationship_retrieval import relationship_candidates
        if not continuation:
            graph_seeds = [item["citation"] for item in packet["supporting_passages"][:4]]
        graph_items, graph_metadata = relationship_candidates(
            harness.store, query, graph_seeds, include_sensitive=include_sensitive,
            constraints=state.get("constraints"))
    graph_order = state['pending'].get('relationship_order')
    if graph_order is not None:
        validated = {hit['citation']: (hit, path) for hit, path in graph_items}
        if set(validated) != set(graph_order):
            raise ValueError('Relationship candidates changed during Find; restart Find.')
        # The source revision is pinned, but embedding availability may change.
        # Retain IDs/order only; every resumed path and source is revalidated.
        graph_items = [validated[citation] for citation in graph_order]
    else:
        graph_order = [hit['citation'] for hit, _ in graph_items]
    graph_all = {hit["citation"] for hit, _ in graph_items}
    graph_offset = state["offsets"].get("relationships", 0)
    # All graph candidates are already bounded to 32. Give the allocator the
    # complete remaining pool so a target's exact witness cannot fall just past
    # a five-item slice. The output budget, not a source-count cap, controls bodies.
    graph_page = graph_items[graph_offset:]
    graph_base = [hit["citation"] for hit, _ in graph_page]
    # Never repeat a quote. An earlier graph lead may be promoted to a body if
    # ordinary retrieval independently reaches it on a later page.
    for hit in packet["supporting_passages"]:
        if hit["citation"] in graph_bodies:
            state = deepcopy(state)
            state["pending"]["passages"] = sorted(
                set(state["pending"].get("passages", [])) | {hit["citation"]})
    requirement_refs = {citation for check in packet["requirement_checks"]
                        for citation in check["retrieval_assessment"].get("candidate_citations", [])}
    required_seen = set(state["pending"].get("requirements", []))
    streams = {"memories": packet["relevant_memories"], "passages": packet["supporting_passages"],
               "catalog": packet["source_discovery"]["candidates"]}
    for name, values in streams.items():
        seen = set(state["pending"].get(name, [])) | required_seen
        values[:] = [item for item in values if item.get("citation", item.get("original_citation")) not in seen]
    baseline_passages = list(streams["passages"])
    baseline_citations = {hit["citation"] for hit in baseline_passages}
    ordinary_statistics = [*baseline_passages, *streams["memories"]]
    for hit, _ in graph_page:
        if hit["citation"] not in graph_seen and hit["citation"] not in baseline_citations:
            packet["supporting_passages"].append(hit)
    # Baseline paging accounts only for its own admitted stream. Graph chunks
    # use their independently bounded offset, including shared exact citations.
    streams["passages"] = baseline_passages
    # Keep the transport's store identity inside the serialized cap.
    path = str(Path(harness.settings.sqlite_path).expanduser().resolve())
    packet["store"] = {"id": hashlib.sha256(path.encode()).hexdigest()[:16]}
    linked = packet["memory_discovery"]
    linked["next_step"] = "Open memory citations and inspect sources; links establish neither relevance nor support."
    for candidate in linked["candidates"]:
        candidate["followups"] = [_open(candidate["citation"], include_sensitive)]
    _public_routes(packet, include_sensitive)
    reference_only = _body_first_references(packet, [query, *questions]) if policy == "body_first" else frozenset()
    reference_reasons = {}
    if focused:
        subject_references = _subject_references(packet, [query, *questions])
        reference_reasons = {citation: "named_subject_not_located_in_candidate" for citation in subject_references - reference_only}
        reference_only |= subject_references
    if policy == "focused":
        focused_reasons = _focused_references(packet, [query, *questions])
        reference_reasons.update(focused_reasons)
        reference_only |= frozenset(focused_reasons)
    if state.get("constraints"):
        mismatched = {hit["citation"] for hit, path in graph_page
            if path.get("applicability", {}).get("status") == "mismatch"
            or any(step.get("applicability", {}).get("status") == "mismatch" for step in path["steps"])}
        # Keep counter-evidence and partial sources inspectable. Comparison of
        # authored fields changes delivery, never deletes broader candidates.
        reference_only |= mismatched
        reference_reasons.update({citation: "recorded_constraint_mismatch_inspect_source" for citation in mismatched})
    bundles = None
    if graph_metadata is not None:
        from proharness.memory.relationship_selection import evidence_bundles, bound_source_matches
        bundles, bundle_reasons = evidence_bundles(graph_items, baseline_citations,
            seen=graph_seen, delivered=graph_bodies)
        graph_only = {hit['citation'] for hit, _ in graph_page} - baseline_citations - graph_seen
        protected = {citation for citation, reason in reference_reasons.items()
                     if reason in {'recorded_constraint_mismatch_inspect_source', 'heading_only_navigation'}}
        primary = (bound_source_matches(graph_page) & graph_only) - protected - set(bundle_reasons)
        # An explicit property binding is useful even when the exact body says
        # "700 readings" instead of repeating the key "capacity". Bring only its
        # validated witness closure, not all sources associated with an entity.
        promoted, pending = set(primary), list(primary)
        while pending:
            for witness in bundles.get(pending.pop(), []):
                if witness not in promoted:
                    promoted.add(witness)
                    pending.append(witness)
        promoted -= protected | set(bundle_reasons)
        reference_only -= promoted
        for citation in promoted:
            reference_reasons.pop(citation, None)
        leads = graph_only - promoted
        reference_only |= leads
        reference_reasons.update({citation: 'connected_source_lead_inspect_before_use'
                                  for citation in leads - protected})
        # Prior references remain navigable, but cannot satisfy a body dependency.
        reference_only |= set(bundle_reasons)
        reference_reasons.update({citation: reason for citation, reason in bundle_reasons.items()
                                  if citation not in protected})
    priorities = (_coverage_priorities(packet, [query, *questions],
        **({"statistics_candidates": ordinary_statistics} if graph_metadata is not None else {}))
        if focused or graph_metadata is not None else None)
    if policy in {"body_first", "focused"} or focused:
        # Match explanations were requested only to preserve semantic candidates.
        # Do not let their extra IDs/diagnostics displace evidence in the packet.
        for passage in packet["supporting_passages"]:
            passage.pop("explanation", None)
        searches = [packet.get("retrieval", {}).get("document_search", {}),
                    *(check.get("document_search", {}) for check in packet.get("requirement_checks", []))]
        for search in searches:
            selection = search.get("candidate_selection")
            if isinstance(selection, dict):
                selection.pop("candidate_ids", None)
    # Internal scan counters and candidate IDs are operational diagnostics, not
    # source evidence. Keep scope/completeness/fallback warnings in public Find.
    for search in [packet.get("retrieval", {}).get("document_search", {}),
                   *(check.get("document_search", {}) for check in packet.get("requirement_checks", []))]:
        selection = search.get("candidate_selection")
        if isinstance(selection, dict):
            keep = {"policy", "ranking_complete", "exhaustive_fallback", "reason", "document_selection"}
            search["candidate_selection"] = {key: value for key, value in selection.items() if key in keep}
            search["candidate_selection"]["omitted_metadata_fields"] = len(set(selection) - keep)
    trace = {
        "retrieval_passes": 1,
        "stages": ["memory_and_passage_search", *(["requirement_subqueries"] if questions else []),
                   "exact_source_to_memory_discovery", "catalog_source_discovery", "bounded_projection"],
        "requirement_queries": len(questions),
        "fallback": "not_run; source and memory discovery composed in the first pass",
    }
    if graph_metadata is not None:
        trace["stages"].insert(-1, "bounded_relationship_navigation")
        trace["retrieval_passes"] = 2
        trace["graph_pass"] = "typed links then scoped lexical passages; no extra model"
    if relevance != "legacy":
        trace["relevance_policy"] = relevance
    token = find_cursor.new_token()
    navigation = {"next_cursor": token,
                  "scope": "Admitted memory/passage/catalog matches; not exhaustive library answerability."}
    envelope = {"work_trace": trace, "continuation": navigation}
    from proharness.memory.visual_regions import discover_regions
    region_pool = discover_regions(harness.store, [query, *questions], include_sensitive, limit=256)
    region_offset = state["offsets"].get("visual_regions", 0)
    region_page = region_pool[region_offset:]
    if region_pool:
        envelope["visual_region_discovery"] = {
            "scope": "At most 256 FTS description matches, ranked lexically; not exhaustive image understanding.",
            "candidate_limit_reached": len(region_pool) == 256,
            "remaining": len(region_page), "description_policy": "Agent-authored, unverified discovery aids; not source quotations."}
    if region_page:
        # Reserve an inspectable exact reference before ordinary output allocation.
        # Long annotations never prevent their source pixels from being reachable.
        envelope["visual_regions"] = [{**region_page[0], "description": "", "description_omitted": True}]
    if state.get("constraints"):
        envelope["constraint_check"] = {"requested": state["constraints"],
            "scope": "Exact authored graph fields only; ordinary search remains unfiltered. Missing fields stay unknown; matching is not verified compatibility.",
            "status": "inspect_path_results" if graph_metadata else "no_applicable_graph_records"}
    if graph_metadata is not None:
        # Evidence gets the packet first. Detailed routes are optional and only
        # use remaining bytes; a long route must never displace an exact quote.
        envelope["retrieval_paths"] = {
            "status": graph_metadata["status"], "paths": [],
            "scope": "Navigation hints, not claim support. Missing or bounded paths do not establish absence.",
            "truncated": graph_metadata["truncated"], "omitted_paths": len(graph_page)}
    projected = bounded_evidence_packet(
        packet, max_tokens=max_tokens, public_followups=True,
        compact_public=harness.settings.library_find_compact,
        reference_only=reference_only, candidate_priorities=priorities,
        candidate_bundles=bundles,
        reference_reasons=reference_reasons,
        envelope=envelope)
    region_consumed = int(bool(region_page))
    if region_page:
        projected["visual_regions"][0] = region_page[0]
        if output_bytes(budget_report(projected, max_tokens)) > max_tokens * 4:
            projected["visual_regions"][0] = envelope["visual_regions"][0]
        for item in region_page[1:]:
            projected["visual_regions"].append(item)
            if output_bytes(budget_report(projected, max_tokens)) > max_tokens * 4:
                projected["visual_regions"].pop()
                break
            region_consumed += 1
        projected["visual_region_discovery"]["remaining"] = len(region_page) - region_consumed
        budget_report(projected, max_tokens)
    returned = {
        "memories": {item["citation"] for item in projected["relevant_memories"]},
        "passages": {item["citation"] for item in projected["supporting_passages"]},
        "catalog": {item["original_citation"] for item in projected["source_discovery"]["candidates"]},
    }
    for item in projected["omitted_references"]:
        name = {"relevant_memories": "memories", "supporting_passages": "passages"}.get(item["stream"])
        if name and item.get("citation"):
            returned[name].add(item["citation"])
    represented_passages = set(returned["passages"])
    graph_returned = represented_passages & graph_all
    if graph_metadata is not None:
        detail = projected["retrieval_paths"]
        retained = [path for path in detail["paths"]
                    if path["target"]["citation"] in represented_passages]
        detail["paths"] = retained
        detail["omitted_paths"] = len(graph_page) - len(retained)
        for hit, path in graph_page:
            if path in retained:
                continue
            if hit["citation"] not in represented_passages:
                continue
            detail["paths"].append(path)
            detail["omitted_paths"] -= 1
            budget_report(projected, max_tokens)
            if output_bytes(projected) > max_tokens * 4:
                detail["paths"].pop()
                detail["omitted_paths"] += 1
        # Recompute after rejected additions; text/refs were never truncated.
        budget_report(projected, max_tokens)
    # Keep graph IDs out of the ordinary cursor's bounded pending set.
    returned["passages"] -= graph_returned - baseline_citations
    next_state = deepcopy(state)
    if graph_metadata is not None:
        next_state["pending"]["relationship_seeds"] = graph_seeds
        next_state["pending"]["relationship_seen"] = sorted(graph_seen | graph_returned)
        next_state["pending"]["relationship_bodies"] = sorted(graph_bodies | {
            item['citation'] for item in projected['supporting_passages']
            if item['citation'] in graph_all})
        next_state['pending']['relationship_order'] = graph_order
    next_state["pending"]["requirements"] = sorted(
        required_seen | (set().union(*returned.values()) & requirement_refs))
    next_state["offsets"]["visual_regions"] = region_offset + region_consumed
    more = region_offset + region_consumed < len(region_pool)
    for name, page in paging.items():
        seen = set(state["pending"].get(name, [])) | returned[name] | (required_seen & set(page["base_citations"]))
        base = page["base_citations"]
        consumed = 0
        for citation in base:
            if citation not in seen:
                break
            seen.remove(citation)
            consumed += 1
        next_state["offsets"][name] = page["offset"] + consumed
        next_state["pending"][name] = sorted(seen)
        more |= page["has_more"] or consumed < len(base)
        more |= any(item.get("citation", item.get("original_citation")) not in returned[name]
                    for item in streams[name])
    if graph_metadata is not None:
        graph_consumed = 0
        for citation in graph_base:
            if citation not in graph_seen | graph_returned:
                break
            graph_consumed += 1
        next_state["offsets"]["relationships"] = graph_offset + graph_consumed
        more |= graph_offset + graph_consumed < len(graph_items)
    skipped_known_graph_matches = bool(graph_seen) and any(
        offset > state["offsets"].get(name, 0)
        for name, offset in next_state["offsets"].items())
    if more and not any(returned.values()) and not graph_returned and not region_consumed and not skipped_known_graph_matches:
        raise ValueError("Find budget cannot represent the next match; increase max_tokens or use a narrower query.")
    if not more:
        projected["continuation"] = {"next_cursor": None, "scope": navigation["scope"]}
        next_state = None
    budget_report(projected, max_tokens)
    assert output_bytes(projected) <= max_tokens * 4
    return projected, next_state
