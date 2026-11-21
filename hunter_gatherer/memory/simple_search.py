"""One bounded library discovery operation, composed from existing evidence reads.

No writing, reranking, query-rewrite or answerability model is called. Existing
configured embeddings may be used by the evidence machinery. Source links
discover records with different wording; they do not establish query relevance.
"""
from __future__ import annotations

import hashlib
import math
import re
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

from hunter_gatherer.memory import find_cursor
from hunter_gatherer.memory.embeddings import HashingEmbedder
from hunter_gatherer.memory.evidence import _evidence_requirements, build_evidence_packet
from hunter_gatherer.memory.evidence_bounded import bounded_evidence_packet
from hunter_gatherer.memory.output_budget import budget_report, output_bytes, validate_budget

# Result kinds a first page ranks and admits for its continuations.
_RANKED = ("memories", "passages", "catalog")
_PREFIX = {"memories": "[mem:", "passages": "[doc:", "catalog": "[file:"}


def _subject_references(packet, queries):
    """Defer weak off-subject text when a named subject is actually located.

    Capitalization is a conservative clue, not entity recognition. Never require
    a name on every passage: titles, sections, strong semantic matches, explicit
    source links, and requirement candidates can all retain useful context.
    Deferred sources keep exact Open references; this is not an absence verdict.
    """
    from hunter_gatherer.memory.query_focus import question_terms, query_terms
    from hunter_gatherer.memory.lexical import normalize_word

    candidates = [*packet["supporting_passages"], *packet["relevant_memories"]]
    terms = set().union(*(question_terms(query) for query in queries))
    names = {normalize_word(word) for index, word in enumerate(re.findall(r"[^\W\d]\w*", queries[0]))
             if word[0].isupper() and len(word) > 2 and normalize_word(word) in terms
             # A sentence-initial common noun is not a reliable named subject.
             and (index > 0 or any(letter.isupper() for letter in word[1:]))}
    searchable = {item["citation"]: query_terms(" ".join((
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


def _quantity_priority(item, queries):
    from hunter_gatherer.memory.quantity_focus import quantity_match_score

    sentences = re.split(r"(?<=[.!?])\s+|\n+", item.get("quote", item.get("text", "")))
    return max((quantity_match_score(sentence, query)
                for query in queries for sentence in sentences), default=0)


def _coverage_priorities(packet, queries, *, statistics_candidates=None):
    """Allocate across memories and passages using the actual question.

    Weighted literal coverage favors specific clues over incidental common words.
    Existing semantic signals preserve a route for paraphrases. These are ordering
    heuristics, never answerability judgments or reasons to discard a candidate.
    Optional statistics_candidates freezes ordinary retrieval IDFs before graph
    navigation is appended; every candidate is scored against those same weights.
    """
    from collections import Counter

    from hunter_gatherer.memory.query_focus import question_terms, query_idfs, query_terms, weighted_coverage

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
    weights = [query_idfs(question_terms(query), frequencies, len(statistics_terms)) for query in queries]
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
        # A requested count next to its noun is a useful ordering clue. Keep
        # every other candidate and preserve the exact quote, including caveats.
        # This does not assess whether a number is true, current or sufficient.
        score += .25 * _quantity_priority(item, queries)
        priorities[item["citation"]] = score
    return priorities


def _body_first_references(packet, queries):
    """Conservative presentation rule, never a relevance or absence verdict.

    Keep any literal body match, any returned semantic match, and every selected
    requirement citation. Other candidates retain references instead of quotes.
    No query expansion, new embedding, source read or answer model is performed.
    """
    from hunter_gatherer.memory.query_focus import question_terms, query_terms

    terms = set().union(*(question_terms(query) for query in queries))
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
        if not terms.intersection(query_terms(body)):
            deferred.add(citation)
    return frozenset(deferred)


def _focused_references(packet, queries):
    """Defer thin matches to exact Open leads, without removing candidates.

    This opt-in presentation heuristic never declares corpus absence. A shared
    capitalized subject alone is weak evidence for a requested detail. Real
    semantic matches can preserve paraphrases; these thresholds are review
    heuristics, not probabilities. No extra embedding/model calls are made.
    """
    from hunter_gatherer.memory.query_focus import question_terms, query_terms
    from hunter_gatherer.memory.lexical import normalize_word

    generic = {"name", "named", "date", "year", "know", "known", "next", "his", "her"}
    focuses = []
    for query in queries:
        terms = question_terms(query)
        names = {normalize_word(word) for index, word in enumerate(re.findall(r"[^\W\d]\w*", query))
                 if word[0].isupper() and normalize_word(word) in terms
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
        body_terms = query_terms(body)
        if any(focus & body_terms or semantic >= (.70 if named else .60)
               for focus, named in focuses):
            continue
        deferred[citation] = "weak_query_body_match"
    return deferred


def _open(ref, include_sensitive, view="text"):
    return {"tool": "hg_open", "arguments": {
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
                if tool in {"hg_find", "hg_open", "hg_memory",
                            "hg_collection", "hg_import_documents", "hg_status"}:
                    routes.append(route)
                    continue
                arguments = route.get("arguments", {})
                refs = arguments.get("refs")
                citation = value.get("original_citation") or value.get("citation")
                if tool == "hg_fetch" and refs:
                    routes.append({"tool": "hg_open", "arguments": {
                        "refs": refs, "view": "text", "max_tokens": 2048,
                        "include_sensitive": include_sensitive}})
                elif citation and tool in {"hg_document_outline", "hg_get_document"}:
                    routes.append(_open(citation, include_sensitive, "outline"))
                elif citation and tool == "hg_document_window":
                    routes.append(_open(citation, include_sensitive, "context"))
                elif tool == "hg_document_page_image":
                    from hunter_gatherer.memory.original_page_reference import (
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
    """One bounded search page, or a continuation of that search.

    The first page admits up to ``find_cursor.ADMITTED`` ranked matches of
    each kind. Continuations deliver the rest of those, rechecked as they are
    delivered, and retain only query/options and citations, never evidence
    bodies. The existing retrieval model and thresholds determine the candidate
    scope; reaching its end does not establish that an answer is absent.
    """
    validate_budget(max_tokens)
    if type(include_sensitive) is not bool:
        raise ValueError("include_sensitive must be a boolean")
    if contextual_requirements is not None and type(contextual_requirements) is not bool:
        raise ValueError("contextual_requirements must be a boolean")
    if constraints is not None:
        from hunter_gatherer.memory.relationship_constraints import validate_constraints
        constraints = validate_constraints(constraints)
        if constraints and not getattr(harness.settings, "library_find_relationships", False):
            raise ValueError("Explicit graph constraints require library_find_relationships enabled; use ordinary Find without constraints otherwise.")
    from hunter_gatherer.memory.evidence import prepare_evidence_search
    from hunter_gatherer.memory.library_health import _read_snapshot
    from hunter_gatherer.memory.retrieval_context import retrieval_context

    store = harness.store
    if cursor is None:
        if type(query) is not str or not query.strip() or len(query) > 4000:
            raise ValueError("query must contain 1 to 4000 characters (or supply a Find cursor)")
        _embed_before_locking(store, _search_texts(query, requirements, bool(contextual_requirements)))
        # Other writers may queue deliveries, even when using a different backend.
        # Complete due repairs before pinning the page snapshot.
        store._retry_pending_index()
        if not isinstance(store.embedder, HashingEmbedder):
            # Catch up before taking the page lock. Semantic search rechecks
            # the database version inside its snapshot and reports failures.
            with suppress(Exception):
                store._refresh_index_from_other_writers()
    with store._lock:
        if store._sqlite.in_transaction:
            raise ValueError("Find continuation requires no active transaction; restart Find.")
        # Index upkeep may write, so it happens first. Everything after it reads
        # one snapshot: the whole page, and on a continuation the check of every
        # admitted record it delivers. A commit by another process during the
        # search therefore cannot disturb this page.
        archive = prepare_evidence_search(store, source_discovery=True)
        with _read_snapshot(store):
            if cursor is not None:
                state = find_cursor.resume(store, cursor, include_sensitive, query, requirements, constraints,
                                           contextual_requirements)
            else:
                state = {"query": query.strip(), "requirements": _evidence_requirements(requirements),
                         "include_sensitive": include_sensitive, "offsets": {}, "pending": {}}
                if constraints:
                    state["constraints"] = constraints
                if contextual_requirements:
                    state["contextual_requirements"] = True
            # Relationship navigation reuses the ordinary query vector. Enabling it
            # must not also change the semantic input of the baseline search.
            with retrieval_context(state['query']):
                result, next_state = _find_page(harness, state, max_tokens, continuation=cursor is not None,
                                                archive=archive)
            if next_state is not None:
                find_cursor.issue(store, next_state, token=result["continuation"]["next_cursor"])
            return result


def _search_texts(query, requirements, contextual: bool) -> list[str]:
    """The question and detail texts the search will embed, worded as it words them.

    A detail searched with the question as context is embedded as
    "question\\ndetail", a different text from the detail alone. Invalid input
    yields only what is valid; the search itself reports the rest.
    """
    if not isinstance(query, str):
        return []
    question = query.strip()
    try:
        details = _evidence_requirements(requirements)
    except ValueError:
        details = []
    return [question, *(f"{question}\n{detail}" if contextual else detail for detail in details)]


def _embed_before_locking(store, texts) -> None:
    """Run the embedding model for the question and its details before the store lock is held.

    A search holds the store lock from start to finish, so every other client
    waits for whatever it does, including a model call. An embedder that caches
    by text lets that call happen here instead; the search then finds the
    vector already computed. Purely an optimization: any failure is left for
    the search itself to report.
    """
    embedder = getattr(store, "embedder", None)
    if not getattr(embedder, "caches_by_text", False):
        return
    wanted = [text.strip() for text in texts if isinstance(text, str) and 0 < len(text.strip()) <= 4000]
    if wanted:
        try:
            embedder.embed_batch(wanted[:5])
        except Exception:  # noqa: BLE001 — reported by the search, with its own handling
            return


def _find_page(harness, state, max_tokens, *, continuation, archive=None):
    query, questions = state["query"], state["requirements"]
    include_sensitive = state["include_sensitive"]
    policy = harness.settings.library_find_delivery_policy
    relevance = harness.settings.library_find_relevance_policy
    focused = relevance == "coverage"
    graph_items, graph_metadata = [], None
    graph_seen = set(state["pending"].get("relationship_seen", []))
    graph_bodies = set(state["pending"].get("relationship_bodies", []))
    graph_seeds = list(state["pending"].get("relationship_seeds", []))
    admitted = None
    if continuation:
        # Never repeat a quote: a passage already delivered whole as a
        # connected-source lead leaves the ordinary list.
        admitted = {name: [citation for citation in state["admitted"][name] if citation not in graph_bodies]
                    for name in _RANKED}
    packet = build_evidence_packet(
        harness.store, query.strip(), top_k=5, include_sensitive=include_sensitive,
        evidence_requirements=questions,
        **({"contextual_requirements": True} if state.get("contextual_requirements") else {}),
        selection_strategy="requirements" if questions else "relevance",
        source_discovery=True, memory_discovery=True, include_context=False,
        prepared_archive=archive, retrieval_continuation=continuation,
        **({"admitted": admitted} if continuation
           else {"retrieval_offsets": {}, "admitted_limit": find_cursor.ADMITTED}),
        **({"explain_document_matches": True} if policy in {"body_first", "focused"} or focused else {}),
        **({"document_query_strategy": "original"} if relevance != "legacy" else {}),
        **({"document_relevance_policy": "coverage"} if focused else {}),
    )
    paging = packet.pop("retrieval_page")
    if getattr(harness.settings, "library_find_relationships", False):
        from hunter_gatherer.memory.relationship_retrieval import relationship_candidates
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
        # Connected leads are navigated again on every page. While their set is
        # unchanged its first order keeps the offset exact; a changed set means
        # the leads themselves changed. Retain IDs/order only; every resumed
        # path and source is revalidated.
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
    # An earlier graph reference may still be promoted to a body if ordinary
    # retrieval independently reaches it on a later page.
    streams = {"memories": packet["relevant_memories"], "passages": packet["supporting_passages"],
               "catalog": packet["source_discovery"]["candidates"]}
    # What this page offers, in retrieval order. A first page admits these,
    # then its requirement leads, then the rest of each ranking.
    offered = {name: [item.get("citation", item.get("original_citation")) for item in values]
               for name, values in streams.items()}
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
        from hunter_gatherer.memory.relationship_selection import evidence_bundles, bound_source_matches
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
    if focused:
        # Present the same question-focused order used for budget allocation.
        # The projector otherwise restores input ranks when all quotes fit.
        # Cursor offsets still use retrieval_page.base_citations, and consumed
        # candidates are tracked by ID, so this page-local ordering cannot skip
        # or repeat a source when a smaller budget requires continuation.
        for name in ("supporting_passages", "relevant_memories"):
            # An explicit count wins a tie with the same noun in a heading.
            # Stable allocation sorting below preserves this tie order too.
            packet[name].sort(key=lambda item: (-priorities.get(item["citation"], 0.0),
                                               -_quantity_priority(item, [query, *questions])))
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
    from hunter_gatherer.memory.visual_regions import admitted_regions, discover_regions
    if continuation:
        regions_admitted = "visual_regions" in state["admitted"]
        region_page, region_withdrawn = admitted_regions(
            harness.store, state["admitted"].get("visual_regions", []), include_sensitive)
        limit_reached = list(state["limit_reached"])
    else:
        region_page, region_withdrawn = discover_regions(
            harness.store, [query, *questions], include_sensitive, limit=find_cursor.REGIONS), []
        regions_admitted = bool(region_page)
        limit_reached = [name for name in _RANKED if not paging[name]["ranked_complete"]]
        if len(region_page) == find_cursor.REGIONS:
            limit_reached.append("visual_regions")
    token = find_cursor.new_token()
    navigation = {"next_cursor": token,
                  "scope": "Admitted memory/passage/catalog matches; not exhaustive library answerability."}
    if limit_reached:
        # Each kind admits its first ADMITTED ranked matches; a narrower
        # question reaches the rest.
        navigation["admitted_limit_reached"] = True
    if continuation:
        withdrawn = sum(len(paging[name]["withdrawn"]) for name in _RANKED) + len(region_withdrawn)
        if withdrawn:
            # Admitted records that are no longer available are never delivered.
            navigation["withdrawn"] = withdrawn
        if find_cursor.added_since(harness.store, state):
            navigation["added_since_first_page"] = "Not included; search again to include them."
    envelope = {"work_trace": trace, "continuation": navigation}
    if regions_admitted:
        envelope["visual_region_discovery"] = {
            "scope": "At most 256 FTS description matches, ranked lexically; not exhaustive image understanding.",
            "candidate_limit_reached": "visual_regions" in limit_reached,
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
    # Graph-only leads are tracked by the relationship bookkeeping below.
    returned["passages"] -= graph_returned - baseline_citations
    next_state = {key: deepcopy(value) for key, value in state.items() if key not in {"offsets", "pending"}}
    next_state.update(offsets={}, pending={}, limit_reached=limit_reached)
    if graph_metadata is not None:
        next_state["pending"]["relationship_seeds"] = graph_seeds
        next_state["pending"]["relationship_seen"] = sorted(graph_seen | graph_returned)
        next_state["pending"]["relationship_bodies"] = sorted(graph_bodies | {
            item['citation'] for item in projected['supporting_passages']
            if item['citation'] in graph_all})
        next_state['pending']['relationship_order'] = graph_order
    # Admitted citations leave their list once represented on a page (as a body
    # or an exact reference, in any order) or withdrawn as no longer available.
    if continuation:
        remaining = {name: [citation for citation in admitted[name]
                            if citation not in returned[name] and citation not in paging[name]["withdrawn"]]
                     for name in _RANKED}
    else:
        leads = [citation for check in packet["requirement_checks"]
                 for citation in check["retrieval_assessment"].get("candidate_citations", [])]
        remaining = {}
        for name in _RANKED:
            ordered = [*offered[name], *(citation for citation in leads if citation.startswith(_PREFIX[name])),
                       *paging[name]["ranked"]]
            remaining[name] = [citation for citation in dict.fromkeys(ordered) if citation not in returned[name]]
        next_state["watermark"] = find_cursor.watermark(harness.store)
    if regions_admitted:
        remaining["visual_regions"] = [item["citation"] for item in region_page[region_consumed:]]
    next_state["admitted"] = remaining
    more = any(remaining.values())
    progressed = continuation and (sum(map(len, remaining.values()))
                                   < sum(map(len, state["admitted"].values())))
    if graph_metadata is not None:
        graph_consumed = 0
        for citation in graph_base:
            if citation not in graph_seen | graph_returned:
                break
            graph_consumed += 1
        next_state["offsets"]["relationships"] = graph_offset + graph_consumed
        more |= graph_offset + graph_consumed < len(graph_items)
        progressed |= graph_consumed > 0
    if more and not any(returned.values()) and not graph_returned and not region_consumed and not progressed:
        raise ValueError("Find budget cannot represent the next match; increase max_tokens or use a narrower query.")
    if not more:
        projected["continuation"]["next_cursor"] = None
        next_state = None
    budget_report(projected, max_tokens)
    assert output_bytes(projected) <= max_tokens * 4
    return projected, next_state
