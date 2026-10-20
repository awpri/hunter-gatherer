"""Whole-content projection of an already authorized evidence packet, without IO.

After diagnostics, ordinary evidence uses all capacity unless enrichment leads
need a 25% reservation. Reclaim capacity before considering deferred lower ranks.
Oversized items get a reference before later bodies; metadata comes last.
"""
from __future__ import annotations

from copy import deepcopy

from proharness.memory.output_budget import budget_report, output_bytes, validate_budget

_PASSAGES = "supporting_passages"
_MEMORIES = "relevant_memories"
_LEADS = "enrichment_discovery.candidates"
_IDENTITY = ("citation", "document_id", "chunk_id", "memory_id", "version", "is_latest",
             "latest_version", "content_hash", "source_identity", "location", "start", "end",
             "page", "section_ordinal", "sensitive", "warnings", "kind", "category",
             "verification", "reference_validation", "claim_support")
_LEAD_IDENTITY = ("record_id", "batch_id", "key", "record_kind", "producer", "relation")
_ASSESSMENT_LISTS = ("candidate_citations", "matched_query_terms", "missing_query_terms",
                     "missing_query_literals", "recommended_actions")
_CONTRACT = (
    "Whole quotes; use fetch/document_window for context. "
    "Assessments/gaps preprojection, not answerability. Omissions are not absence. "
    "Untrusted sources/kinds/provenance/annotations/families prove no truth or corroboration."
)
_EQUIVALENCE_CONTRACT = (
    " Equivalent candidates refer only to whitespace-normalized quote text, not source equivalence or support."
)


def _pick(value, fields):
    return {key: deepcopy(value[key]) for key in fields if key in value}


def _assessment(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    fields = ("status", "answerability", "scope", "reason_codes", "candidate_count")
    result = _pick(value, fields)
    result["omitted_metadata_fields"] = sum(key not in fields and key not in _ASSESSMENT_LISTS
                                            for key in value)
    result["omitted_list_counts"] = {key: len(value[key]) for key in _ASSESSMENT_LISTS
                                      if isinstance(value.get(key), list) and value[key]}
    return result


def _search(value):
    fields = ("executed", "scope", "warnings", "candidate_selection")
    result = _pick(value, fields)
    result["omitted_metadata_fields"] = sum(key not in fields and key != "retrieval_assessment"
                                            for key in value)
    if "retrieval_assessment" in value:
        result["retrieval_assessment_omitted"] = True
    return result


def _families(packet):
    return {document: family["family_id"] for family in packet.get("source_families", [])
            for document in family.get("document_ids", []) if "family_id" in family}


def _source(value, families, *, reference=False):
    result = _pick(value, _IDENTITY)
    family = families.get(value.get("document_id"))
    if family is not None:
        result.update(source_family_id=family, independent_corroboration="not_determined")
    if reference:
        result["content_status"] = "omitted"
    elif "quote" in value:
        result["quote"] = deepcopy(value["quote"])
    if "context" in value or value.get("context_omitted") is True:
        result["context_omitted"] = True
    return result


def _item(value, stream, rank, families, *, reference=False):
    result = _source(value, families, reference=reference)
    result["rank"] = rank
    if stream == _MEMORIES:
        if not reference and "text" in value:
            result["text"] = deepcopy(value["text"])
        provenance = value.get("provenance", {})
        if reference:
            result["provenance_omitted_counts"] = {
                "sources": len(provenance.get("sources", [])),
                "unresolved_sources": len(provenance.get("unresolved_sources", []))}
        else:
            result["provenance"] = _pick(provenance, ("origin", "recorded_at", "unresolved_sources"))
            result["provenance"]["sources"] = [
                _source(source, families, reference=True) for source in provenance.get("sources", [])]
        if not reference:
            result.update(known_details={}, omitted_metadata_fields=len(value.get("known_details", {})))
            if "missing_details" in value:
                result["missing_details"] = deepcopy(value["missing_details"])
    elif stream == _LEADS:
        result.update(_pick(value, _LEAD_IDENTITY))
        result["annotation_status"] = "derived_discovery_aid"
        if not reference and "derived_text" in value:
            result["derived_text"] = deepcopy(value["derived_text"])
        # Anchors can cite different exact subsets of the same chunk: never intern by citation.
        result["source_references"] = [
            _source(source, families, reference=True) if reference else deepcopy(source)
            for source in value.get("source_references", [])]
    optional = {key: item for key, item in value.items()
                if key not in result and key not in {"context", "context_location", "context_omitted", "followups",
                                                    "quote", "text", "derived_text"}}
    if stream == _MEMORIES:
        optional.pop("known_details", None)
    if optional:
        result["omitted_metadata_fields"] = result.get("omitted_metadata_fields", 0) + len(optional)
    return result


def _followups(value, include_sensitive, public=False):
    if public:
        citation = value.get("citation")
        if not citation:
            citation = next((ref.get("citation") for ref in value.get("source_references", [])
                             if ref.get("citation")), None)
        if not citation:
            return []
        views = ["text", "context"] if value.get("chunk_id") or value.get("source_references") else ["text"]
        return [{"tool": "proharness_open", "arguments": {
            "refs": [citation], "view": view, "max_tokens": 2048,
            "include_sensitive": bool(include_sensitive)}} for view in views]

    extra = {"include_sensitive": True} if include_sensitive else {}
    result = []
    if value.get("citation"):
        result.append({"tool": "proharness_fetch", "arguments": {
            "refs": [value["citation"]], **extra}})
    chunk = value.get("chunk_id")
    if chunk is None and value.get("source_references"):
        chunk = value["source_references"][0].get("chunk_id")
    if chunk:
        result.append({"tool": "proharness_document_window", "arguments": {
            "chunk_id": chunk, **extra}})
    return result


def _lean_reference(value, stream, rank, families, include_sensitive, public=False):
    """Keep routing identities when bulky provenance cannot fit a reference."""
    fields = ("citation", "document_id", "chunk_id", "memory_id", "version", "is_latest",
              "latest_version", "content_hash", "kind", "verification", "warnings")
    result = _pick(value, fields)
    result.update(stream=stream, rank=rank, content_status="omitted")
    if value.get("document_id") in families:
        result.update(source_family_id=families[value["document_id"]],
                      independent_corroboration="not_determined")
    if stream == _LEADS:
        result["source_references"] = [_pick(ref, ("citation", "document_id", "chunk_id",
                                                   "start", "end", "section_ordinal", "page"))
                                       for ref in value.get("source_references", [])]
        for ref in result["source_references"]:
            ref["content_status"] = "omitted"
    result["omitted_metadata_fields"] = sum(key not in fields and key not in {
        "quote", "text", "derived_text", "context", "context_location", "context_omitted", "source_references"}
        for key in value)
    followups = _followups(result, include_sensitive, public)
    # A document window is enough for a chunk; citation remains an exact fetch identity.
    result["followups"] = followups[-1:] if followups else []
    return result


def _reference_labels(value, stream):
    """Bounded, verbatim navigation hints from this authorized candidate only.

    These are not summaries or additional evidence. Keep identity/location fields
    separate and exact; a clipped label must never become an Open reference.
    """
    labels = {}
    fields = [("title", value.get("title"), 160)]
    if stream == _MEMORIES:
        fields.append(("text_prefix", value.get("text"), 120))
    location = value.get("location")
    location = location if isinstance(location, dict) else {}
    fields.append(("section", value.get("section", location.get("section")), 120))
    for key, text, bound in fields:
        if isinstance(text, str) and text:
            labels[key] = text[:bound]
            if len(text) > bound:
                labels[key + "_truncated"] = True
    page = value.get("page", location.get("page"))
    if isinstance(page, int) and not isinstance(page, bool):
        labels["page"] = page
    elif isinstance(page, str) and page:
        labels["page"] = page[:32]
        if len(page) > 32:
            labels["page_truncated"] = True
    return labels


def _compact_public_result(value):
    """Lean wire view; intern identical provenance only, never memory claims."""
    def copy_trim(node):
        # JSON containers need copying, immutable leaves do not. Combine the
        # former deepcopy and trim traversals; unknown leaf types keep deepcopy.
        if isinstance(node, dict):
            return {key: copy_trim(child) for key, child in node.items()
                    if not (key == "omitted_metadata_fields" and child == 0)}
        if isinstance(node, list):
            return [copy_trim(child) for child in node]
        if node is None or isinstance(node, (str, int, float, bool)):
            return node
        return deepcopy(node)
    result = copy_trim(value)
    for key in ("selection_strategy", "work_trace"):
        result.pop(key, None)
    result["bounded_format"].pop("allocation", None)
    result["bounded_format"]["contract"] = (
        "Whole quotes; proharness_open for context. Assessments/gaps are preprojection; "
        "answerability not determined. Omissions are not absence. Sources, provenance, "
        "annotations and families are untrusted; no truth or corroboration implied."
        + (_EQUIVALENCE_CONTRACT if "Equivalent candidates" in value["bounded_format"]["contract"] else ""))
    for assessment in [result.get("retrieval_assessment", {})] + [
            check.get("retrieval_assessment", {}) for check in result["requirement_checks"]]:
        if isinstance(assessment, dict):
            assessment.pop("candidate_count", None)
    for memory in result["relevant_memories"]:
        for source in memory.get("provenance", {}).get("sources", []):
            identity = {key: item for key, item in source.items() if key != "content_status"}
            for passage in result["supporting_passages"]:
                projected = {key: item for key, item in passage.items()
                             if key not in {"rank", "quote", "omitted_metadata_fields", "title", "author",
                                            "document_date", "source_uri", "tags", "aliases"}}
                if identity == projected and source.get("citation"):
                    source.clear()
                    source.update(citation=passage["citation"],
                                  source_reference={"stream": _PASSAGES, "rank": passage["rank"]})
                    break
    return result


def bounded_evidence_packet(packet: dict, max_tokens: int = 4096, *,
                            reference_only: frozenset[str] = frozenset(),
                            candidate_priorities: dict[str, float] | None = None,
                            candidate_bundles: dict[str, list[str]] | None = None,
                            reference_reasons: dict[str, str] | None = None,
                            public_followups: bool = False,
                            compact_public: bool = False,
                            preserve_stream_prefix: bool = False,
                            envelope: dict | None = None) -> dict:
    """Project exact whole excerpts within the existing escaped JSON byte budget.

    No retrieval, privacy filtering, model call or source lookup occurs. Optional
    caller priorities order allocation without calculating new retrieval scores.
    compact_public opts into a lean public envelope and identical-provenance rank
    references; it requires public_followups and preserves whole memory claims.
    Public followups are generated before allocation; envelope fields participate
    in every byte check and must not replace projector-owned fields. Callers must
    supply already-public routes in input metadata when enabling public mode.
    preserve_stream_prefix stops a stream at its first unrepresented candidate
    and uses original stream order instead of caller priority ordering.
    candidate_bundles optionally maps targets to up to eight authorized ordinary
    witness citations; target bodies require the complete transitive bundle to fit.
    Internal reference_only citations retain exact navigation without body text.
    If critical diagnostics cannot fit, fail instead of deleting warnings.
    """
    validate_budget(max_tokens)
    if type(public_followups) is not bool:
        raise ValueError("public_followups must be a boolean")
    if type(compact_public) is not bool:
        raise ValueError("compact_public must be a boolean")
    if compact_public and not public_followups:
        raise ValueError("compact_public requires public_followups")
    if type(preserve_stream_prefix) is not bool:
        raise ValueError("preserve_stream_prefix must be a boolean")
    if envelope is not None and not isinstance(envelope, dict):
        raise ValueError("envelope must be a dictionary")
    if not isinstance(packet, dict) or any(key in packet for key in (
            "compact_format", "content_registry", "bounded_format")):
        raise ValueError("Expected an unprojected authorized evidence packet")
    streams = {_PASSAGES: packet.get(_PASSAGES, []), _MEMORIES: packet.get(_MEMORIES, []),
               _LEADS: packet.get("enrichment_discovery", {}).get("candidates", [])}
    if any(not isinstance(values, list) or any(not isinstance(v, dict) for v in values)
           for values in streams.values()):
        raise ValueError("Invalid evidence candidates")
    bundle_candidates = {}
    bundles = {}
    if candidate_bundles is not None:
        if type(candidate_bundles) is not dict or len(candidate_bundles) > 32:
            raise ValueError("candidate_bundles must map at most 32 targets to witnesses")
        for stream in (_PASSAGES, _MEMORIES):
            for rank, value in enumerate(streams[stream], 1):
                citation = value.get('citation')
                if isinstance(citation, str):
                    if citation in bundle_candidates:
                        raise ValueError("Bundle candidate citations must be unique")
                    bundle_candidates[citation] = (stream, rank, value)
        for citation, members in candidate_bundles.items():
            if (not isinstance(citation, str) or citation not in bundle_candidates
                    or type(members) is not list or len(members) > 8
                    or any(not isinstance(m, str) or m not in bundle_candidates or m == citation for m in members)
                    or len(set(members)) != len(members)):
                raise ValueError("Bundles require exact authorized target/witness citations")
        # Citation cycles describe co-delivery requirements, not identity or
        # support. Bounded reachability handles them without dropping an edge.
        for citation in candidate_bundles:
            members = []
            reached = set()
            pending_members = [citation]
            while pending_members:
                member = pending_members.pop()
                if member in reached:
                    continue
                reached.add(member)
                if len(reached) > 32:
                    raise ValueError("A candidate bundle may reach at most 32 exact citations")
                members.append(member)
                pending_members.extend(reversed(candidate_bundles.get(member, [])))
            bundles[citation] = members
    result = _pick(packet, ("include_sensitive", "required_fields", "selection_strategy", "warnings", "store"))
    if reference_only:
        result["delivery_policy"] = {
            "policy": "query_focus" if reference_reasons else "body_first",
            "reference_only_candidates": len(reference_only),
            "reason": ("Weak off-subject candidates are references; inspect per-reference omission reasons. No absence or support judgment."
                       if reference_reasons else
                       "No literal query terms in body and no available semantic match. Open references may still be relevant; not an absence or support judgment."),
        }
    if "archive_coverage" in packet:
        from proharness.memory.archive_coverage import compact_coverage
        result["archive_coverage"] = compact_coverage(packet["archive_coverage"])
    query = packet.get("query")
    if query is not None and output_bytes(query) <= 1024:
        result["query"] = deepcopy(query)
    elif query is not None:
        result["query_omitted"] = True
    result.update(supporting_passages=[], relevant_memories=[], source_leads=[],
                  requirement_checks=[], omitted_references=[])
    if "retrieval_assessment" in packet:
        result["retrieval_assessment"] = _assessment(packet["retrieval_assessment"])
    retrieval = packet.get("retrieval", {})
    if retrieval:
        result["retrieval"] = _pick(retrieval, ("warnings", "document_query_strategy", "lexical_only"))
        if "document_search" in retrieval:
            result["retrieval"]["document_search"] = _search(retrieval["document_search"])
    for check in packet.get("requirement_checks", []):
        projected = _pick(check, ("query", "search_query", "warnings", "support", "document_query_strategy"))
        projected["retrieval_assessment"] = _assessment(check.get("retrieval_assessment", {}))
        if "document_search" in check:
            projected["document_search"] = _search(check["document_search"])
        projected.update(selected_citations=[], selected_preprojection_count=len(check.get("selected_citations", [])))
        if check.get("selected_equivalent_candidates"):
            projected.update(selected_equivalent_candidates=[],
                             selected_equivalent_candidates_omitted=len(check["selected_equivalent_candidates"]))
        result["requirement_checks"].append(projected)
    for section in ("source_discovery", "enrichment_discovery"):
        if section in packet:
            result[section] = _pick(packet[section], (
                "executed", "reason", "scope", "warnings", "matching_records", "omitted_documents",
                "omitted_search_terms"))
            result[section]["candidates"] = []
    memory_discovery = packet.get("memory_discovery")
    if memory_discovery is not None:
        result["memory_discovery"] = _pick(memory_discovery, (
            "executed", "scope", "relation", "claim_support", "next_step", "matching_memories"))
        result["memory_discovery"].update(candidates=[], omitted_candidates=(
            memory_discovery.get("omitted_candidates", 0) + len(memory_discovery.get("candidates", []))))
    projection = {
        "omitted_counts": {stream: len(values) for stream, values in streams.items()},
        "source_lead_bodies_omitted": len(packet.get("source_leads", [])),
        "context_occurrences_omitted": sum("context" in p or p.get("context_omitted") is True for p in streams[_PASSAGES]) + sum(
            "context" in p or p.get("context_omitted") is True for m in streams[_MEMORIES]
            for p in m.get("provenance", {}).get("sources", [])),
        "missing_information_omitted": len(packet.get("missing_information", [])),
        "source_families_omitted": len(packet.get("source_families", [])),
        "source_discovery_candidates_omitted": len(packet.get("source_discovery", {}).get("candidates", [])),
        "source_inspection_omitted": "source_inspection" in packet,
        "omitted_reference_count": 0,
    }
    # Absent/empty optional sections need no omission counters in the minimum envelope.
    projection = {key: value for key, value in projection.items()
                  if value or key in {"omitted_counts", "omitted_reference_count"}}
    result["projection"] = projection
    result["bounded_format"] = {
        "version": "proharness_evidence_bounded_v1", "contract": _CONTRACT + (
            _EQUIVALENCE_CONTRACT if any(check.get("selected_equivalent_candidates")
                                         for check in packet.get("requirement_checks", [])) else ""),
        "allocation": "75/25 ordinary/enrichment" if streams[_LEADS] else "ordinary (no leads)",
    }
    if public_followups:
        result["bounded_format"]["contract"] = result["bounded_format"]["contract"].replace(
            "fetch/document_window", "proharness_open")
    if envelope:
        collisions = set(envelope).intersection(result) | (set(envelope) & {
            "budget", "missing_information", "source_families", "source_discovery",
            "source_inspection", "enrichment_discovery", "memory_discovery",
            "archive_coverage", "retrieval", "retrieval_assessment", "query", "query_omitted",
            "include_sensitive", "required_fields", "selection_strategy", "warnings", "store",
            "delivery_policy"})
        if collisions:
            raise ValueError("envelope cannot replace projector fields: " + ", ".join(sorted(collisions)))
        result.update(deepcopy(envelope))
    # Include prospective omission accounting before allocation, so even zero-content output is truthful.
    projection["omitted_reference_count"] = sum(map(len, streams.values()))
    limit = max_tokens * 4

    compact_report = None

    def fits(ceiling=limit):
        nonlocal compact_report
        if compact_public:
            wire = _compact_public_result(result)
            if compact_report is None:
                budget_report(wire, max_tokens)
            else:
                wire["budget"] = dict(compact_report)
                for _ in range(8):
                    size = output_bytes(wire)
                    if wire["budget"]["serialized_bytes"] == size:
                        break
                    wire["budget"].update(serialized_bytes=size, estimated_tokens=(size + 3) // 4)
            compact_report = wire["budget"]
            return compact_report["serialized_bytes"] <= ceiling
        if not public_followups:
            return output_bytes(budget_report(result, max_tokens)) <= ceiling
        # Public allocation probes repeatedly mutate a few fields of the same
        # envelope. Keep the prior fixed-point report instead of resetting its
        # counters to zero and encoding the unchanged framing again. Updating
        # a report's number of digits is the only reason a second probe is
        # needed; ordinary mutations settle after one update/check pair.
        if "budget" not in result:
            budget_report(result, max_tokens)
        else:
            report = result["budget"]
            for _ in range(8):
                size = output_bytes(result)
                if report["serialized_bytes"] == size:
                    break
                report.update(serialized_bytes=size, estimated_tokens=(size + 3) // 4)
        return result["budget"]["serialized_bytes"] <= ceiling

    if not fits():
        raise ValueError(f"Minimum truthful evidence envelope exceeds max_tokens; framing requires {(output_bytes(result) + 3) // 4} budget units")
    linked_retained = set()

    def add_memory_links(ceiling=limit):
        if memory_discovery is None:
            return
        target = result["memory_discovery"]
        for index, candidate in enumerate(memory_discovery.get("candidates", [])):
            if index in linked_retained:
                continue
            target["candidates"].append(deepcopy(candidate))
            target["omitted_candidates"] -= 1
            if fits(ceiling):
                linked_retained.add(index)
            else:
                target["candidates"].pop()
                target["omitted_candidates"] += 1
        target["candidates"] = [deepcopy(memory_discovery["candidates"][i]) for i in sorted(linked_retained)]

    assessments = [(packet.get("retrieval_assessment"), result.get("retrieval_assessment"))]
    assessments.extend((original.get("retrieval_assessment"), check["retrieval_assessment"])
                       for original, check in zip(packet.get("requirement_checks", []),
                                                  result["requirement_checks"], strict=True))
    retained_lists = {}

    def add_assessment_value(original, projected, key, index, ceiling=limit):
        retained = retained_lists.setdefault((id(projected), key), set())
        if index in retained:
            return
        projected.setdefault(key, []).append(deepcopy(original[key][index]))
        projected["omitted_list_counts"][key] -= 1
        if fits(ceiling):
            retained.add(index)
        else:
            projected[key].pop()
            projected["omitted_list_counts"][key] += 1
            if not projected[key]:
                projected.pop(key)

    # A review_required label must not crowd out the actual gaps it qualifies.
    # Reserve at most 512 bytes / one eighth of remaining capacity; exact
    # missing literals precede terms, round-robin across question assessments.
    # These are lexical observations, never proof of corpus absence.
    framing = output_bytes(result)
    gap_ceiling = framing + min(512, (limit - framing) // 8)
    for key in ("missing_query_literals", "missing_query_terms"):
        eligible = [(original, projected) for original, projected in assessments
                    if isinstance(original, dict) and isinstance(projected, dict)
                    and isinstance(original.get(key), list)]
        for index in range(max((len(original[key]) for original, _ in eligible), default=0)):
            for original, projected in eligible:
                if index < len(original[key]):
                    add_assessment_value(original, projected, key, index, gap_ceiling)
    # Independent requirement references precede content allocation, in caller order.
    for original, check in zip(packet.get("requirement_checks", []), result["requirement_checks"], strict=True):
        assessment = check["retrieval_assessment"]
        original_assessment = original.get("retrieval_assessment", {})
        for index in range(len(original_assessment.get("candidate_citations", []))):
            add_assessment_value(original_assessment, assessment, "candidate_citations", index)
    # An explicit discovery request gets a small reference allowance before
    # bodies; never let metadata consume an unbounded share of source content.
    link_start = output_bytes(result)
    add_memory_links(link_start + min(1024, max(0, limit - link_start) // 8))
    base = output_bytes(result)
    families = _families(packet)
    accepted = {stream: set() for stream in streams}
    delivered = {stream: [] for stream in streams}
    references = {}
    ordinary = [(stream, rank + 1, streams[stream][rank])
                for rank in range(max(len(streams[_PASSAGES]), len(streams[_MEMORIES])))
                for stream in (_PASSAGES, _MEMORIES) if rank < len(streams[stream])]
    if candidate_priorities and not preserve_stream_prefix:
        # Caller-supplied ranking signals only change allocation order. Stable
        # ties retain interleaving; all candidates remain available as references.
        ordinary.sort(key=lambda item: -candidate_priorities.get(item[2].get("citation"), 0.0))
    leads = [(_LEADS, rank, item) for rank, item in enumerate(streams[_LEADS], 1)]

    def target(stream):
        return result.get("enrichment_discovery", {}).get("candidates", []) if stream == _LEADS else result[stream]

    blocked_streams = set()

    def add_bundle(citation, ceiling):
        members = [bundle_candidates[c] for c in bundles[citation]]
        if preserve_stream_prefix and any(
                any(earlier not in accepted[member_stream] and (member_stream, earlier) not in references
                    and not any(s == member_stream and r == earlier for s, r, _ in members)
                    for earlier in range(1, member_rank))
                for member_stream, member_rank, _ in members):
            return False, False
        # An explicitly reference-only witness cannot become primary evidence.
        if any(value.get('citation') in reference_only for _, _, value in members):
            return False, False
        additions = []
        removed = []
        for member_stream, member_rank, member_value in members:
            if member_rank in accepted[member_stream]:
                continue
            key = (member_stream, member_rank)
            previous = references.get(key)
            if previous is not None:
                result['omitted_references'].remove(previous)
                projection['omitted_reference_count'] += 1
                removed.append((key, previous))
            item = _item(member_value, member_stream, member_rank, families)
            target(member_stream).append(item)
            projection['omitted_counts'][member_stream] -= 1
            projection['omitted_reference_count'] -= 1
            additions.append((member_stream, member_rank, member_value, item))
        admitted = fits(ceiling)
        measured = compact_report['serialized_bytes'] if compact_public else result['budget']['serialized_bytes']
        deferred = not admitted and ceiling < limit and measured <= limit
        if admitted:
            for member_stream, member_rank, member_value, item in additions:
                accepted[member_stream].add(member_rank)
                delivered[member_stream].append((member_rank, member_value, item))
                references.pop((member_stream, member_rank), None)
        else:
            for member_stream, _, _, item in reversed(additions):
                target(member_stream).remove(item)
                projection['omitted_counts'][member_stream] += 1
                projection['omitted_reference_count'] += 1
            for key, previous in removed:
                result['omitted_references'].append(previous)
                projection['omitted_reference_count'] -= 1
        return admitted, deferred

    def add_candidates(queue, ceiling):
        for stream, rank, value in queue:
            if stream in blocked_streams or rank in accepted[stream]:
                continue
            bundle_failed = False
            citation = value.get('citation')
            if citation in bundles:
                admitted, deferred = add_bundle(citation, ceiling)
                if admitted:
                    continue
                if deferred:
                    return
                bundle_failed = True
            key = (stream, rank)
            previous = references.get(key)
            if previous is not None:
                result["omitted_references"].remove(previous)
                projection["omitted_reference_count"] += 1
            item = _item(value, stream, rank, families)
            target(stream).append(item)
            projection["omitted_counts"][stream] -= 1
            projection["omitted_reference_count"] -= 1
            defer_body = value.get("citation") in reference_only or bundle_failed
            if defer_body or not fits(ceiling):
                # A reservation must not let smaller, lower-ranked bodies prevent
                # this item from fitting when unused capacity is reclaimed.
                # fits(ceiling) already settled this unchanged trial's exact
                # wire size. Comparing another ceiling requires no projection.
                measured = (compact_report['serialized_bytes'] if compact_public
                            else result['budget']['serialized_bytes'])
                deferred = not defer_body and ceiling < limit and measured <= limit
                target(stream).pop()
                projection["omitted_counts"][stream] += 1
                projection["omitted_reference_count"] += 1
                if previous is not None:
                    result["omitted_references"].append(previous)
                    projection["omitted_reference_count"] -= 1
                if deferred:
                    return
                if previous is not None:
                    continue
                # Reserve an executable path to this item before lower ranks can
                # consume the remaining space. Never shorten its source text.
                ref = _item(value, stream, rank, families, reference=True)
                ref.update(stream=stream, followups=_followups(ref, packet.get("include_sensitive", False), public_followups))
                lean = _lean_reference(value, stream, rank, families, packet.get("include_sensitive", False), public_followups)
                deferred_reference = False
                for candidate in ((lean,) if defer_body else (ref, lean)):
                    if defer_body:
                        candidate["omission_reason"] = (reference_reasons or {}).get(
                            value.get("citation"), "bundle_witnesses_not_delivered" if bundle_failed else "no_literal_body_match")
                    result["omitted_references"].append(candidate)
                    projection["omitted_reference_count"] -= 1
                    if fits(ceiling):
                        references[key] = candidate
                        break
                    measured = (compact_report['serialized_bytes'] if compact_public
                                else result['budget']['serialized_bytes'])
                    deferred_reference |= ceiling < limit and measured <= limit
                    result["omitted_references"].pop()
                    projection["omitted_reference_count"] += 1
                else:
                    if deferred_reference:
                        return
                    if preserve_stream_prefix:
                        blocked_streams.add(stream)
            else:
                accepted[stream].add(rank)
                delivered[stream].append((rank, value, item))
                references.pop(key, None)

    if leads:
        add_candidates(ordinary, base + (limit - base) * 3 // 4)
        lead_start = output_bytes(result)
        add_candidates(leads, min(limit, lead_start + (limit - base) // 4))
        add_candidates(ordinary, limit)
        add_candidates(leads, limit)
    else:
        add_candidates(ordinary, limit)
    # Reclaimed earlier items must precede later items already accepted under reservations.
    for stream in streams:
        target(stream).sort(key=lambda item: item["rank"])

    order = {key: index for index, key in enumerate(streams)}
    result["omitted_references"].sort(key=lambda ref: (order[ref["stream"]], ref["rank"]))

    for original, projected in assessments:
        if not isinstance(original, dict) or not isinstance(projected, dict):
            continue
        for key in _ASSESSMENT_LISTS:
            if public_followups and key in {"matched_query_terms", "recommended_actions"}:
                continue
            if public_followups and key == "candidate_citations" and projected is result.get("retrieval_assessment"):
                continue
            values = original.get(key, [])
            if not isinstance(values, list) or not values:
                continue
            for index in range(len(values)):
                add_assessment_value(original, projected, key, index)
            if key in projected:
                # Early gap allocation can skip a large entry; preserve source
                # ordering when it fits later, without duplication or lost counts.
                projected[key] = [deepcopy(values[i]) for i in sorted(retained_lists[(id(projected), key)])]

    # Requirement citations remain exact discovery references even when outside main candidates.
    citations = {item.get("citation") for stream in streams for item in target(stream)
                 if "quote" in item or "text" in item}
    for original, check in zip(packet.get("requirement_checks", []), result["requirement_checks"], strict=True):
        selected = [c for c in original.get("selected_citations", []) if c in citations]
        check["selected_citations"] = selected
        if not fits():
            check["selected_citations"] = []
        for equivalent in original.get("selected_equivalent_candidates", []):
            if equivalent.get("delivered_citation") not in citations:
                continue
            check["selected_equivalent_candidates"].append(deepcopy(equivalent))
            check["selected_equivalent_candidates_omitted"] -= 1
            if not fits():
                check["selected_equivalent_candidates"].pop()
                check["selected_equivalent_candidates_omitted"] += 1
    # Retry undelivered links using leftover space without duplicates or lost counts.
    add_memory_links()
    # Explicit gaps retain priority over optional navigation and metadata.
    if public_followups and packet.get("missing_information"):
        result["missing_information"] = deepcopy(packet["missing_information"])
        old = projection.get("missing_information_omitted", 0)
        projection["missing_information_omitted"] = 0
        if not fits():
            result.pop("missing_information")
            projection["missing_information_omitted"] = old
    # Navigation hints precede optional metadata, never core evidence, exact
    # references, requirements or gaps. Core-full packets may have no label room.
    # Labels are verbatim prefixes under the bounded format's untrusted contract.
    if public_followups:
        for reference in result["omitted_references"]:
            stream, rank = reference["stream"], reference["rank"]
            labels = _reference_labels(streams[stream][rank - 1], stream)
            if labels:
                reference["navigation_labels"] = labels
                if not fits():
                    reference.pop("navigation_labels")
    # Repeated top-level candidate IDs and lexical/action diagnostics follow
    # labels. Requirement references and missing-term observations stayed above.
    # Their existing omitted_list_counts continue to account for every deferred
    # diagnostic entry; no retrieval or source candidate is removed.
    if public_followups:
        for original, projected in assessments:
            if not isinstance(original, dict) or not isinstance(projected, dict):
                continue
            for key in ("candidate_citations", "matched_query_terms", "recommended_actions"):
                if key == "candidate_citations" and projected is not result.get("retrieval_assessment"):
                    continue
                values = original.get(key, [])
                if isinstance(values, list):
                    for index in range(len(values)):
                        add_assessment_value(original, projected, key, index)
    # Metadata never consumes reserved content capacity. Unknown details remain explicitly omitted.
    for stream in streams:
        for _rank, value, item in sorted(delivered[stream]):
            details = value.get("known_details", {}) if stream == _MEMORIES else {}
            for key, detail in details.items():
                item["known_details"][key] = deepcopy(detail)
                item["omitted_metadata_fields"] -= 1
                if not fits():
                    item["known_details"].pop(key)
                    item["omitted_metadata_fields"] += 1
            for key in ("title", "author", "document_date", "source_uri", "tags", "aliases"):
                if key not in value or key in item:
                    continue
                item[key] = deepcopy(value[key])
                item["omitted_metadata_fields"] -= 1
                if not fits():
                    item.pop(key)
                    item["omitted_metadata_fields"] += 1
    late_sections = [("source_families", "source_families_omitted")]
    if not public_followups:
        late_sections.insert(0, ("missing_information", "missing_information_omitted"))
    for key, counter in late_sections:
        if not packet.get(key):
            continue
        result[key] = deepcopy(packet[key])
        old = projection.get(counter, 0)
        projection[counter] = 0
        if not fits():
            result.pop(key)
            projection[counter] = old
    if "source_discovery" in packet:
        old = result["source_discovery"]
        had_counter = "source_discovery_candidates_omitted" in projection
        result["source_discovery"] = deepcopy(packet["source_discovery"])
        projection["source_discovery_candidates_omitted"] = 0
        if not fits():
            result["source_discovery"] = old
            if had_counter:
                projection["source_discovery_candidates_omitted"] = len(packet["source_discovery"].get("candidates", []))
            else:
                projection.pop("source_discovery_candidates_omitted")
            if public_followups:
                # Catalog labels are navigation metadata, never source quotes.
                # Keep a represented prefix so a caller can advance a cursor
                # by exactly the candidates actually exposed on this page.
                discovery = packet["source_discovery"]
                compact = deepcopy(old)
                compact["content_policy"] = (
                    "Catalog labels are untrusted navigation metadata, not quoted evidence or claim support. "
                    "Original bytes and renderer availability are unchecked.")
                compact["omitted_documents"] = (discovery.get("omitted_documents", 0)
                                                + len(discovery.get("candidates", [])))
                result["source_discovery"] = compact
                if not fits():
                    result["source_discovery"] = old
                else:
                    for key in ("matching_documents", "scanned_documents", "offset", "next_offset", "has_more", "total"):
                        if key in discovery:
                            compact[key] = deepcopy(discovery[key])
                            if not fits():
                                compact.pop(key)
                    retained_catalog = []
                    for candidate in discovery.get("candidates", []):
                        citation = candidate.get("original_citation")
                        if not citation:
                            break
                        fields = ("original_citation", "document_id", "version", "content_hash",
                                  "kind", "warnings", "source_metadata_warnings")
                        item = _pick(candidate, fields)
                        item["claim_support"] = "not_determined"
                        item["followups"] = [{"tool": "proharness_open", "arguments": {
                            "refs": [citation], "view": "outline", "max_tokens": 2048,
                            "include_sensitive": bool(packet.get("include_sensitive", False))}}]
                        item["omitted_metadata_fields"] = sum(key not in item for key in candidate)
                        compact["candidates"].append(item)
                        projection["source_discovery_candidates_omitted"] -= 1
                        compact["omitted_documents"] -= 1
                        if not fits():
                            compact["candidates"].pop()
                            projection["source_discovery_candidates_omitted"] += 1
                            compact["omitted_documents"] += 1
                            break
                        retained_catalog.append((candidate, item))
                    # Useful labels follow identity allocation so a bulky title
                    # cannot prevent navigation to the next catalog candidate.
                    for candidate, item in retained_catalog:
                        if "title" in candidate:
                            item["title"] = deepcopy(candidate["title"])
                            item["omitted_metadata_fields"] -= 1
                            if not fits():
                                item.pop("title")
                                item["omitted_metadata_fields"] += 1
    if "source_inspection" in packet:
        result["source_inspection"] = deepcopy(packet["source_inspection"])
        projection["source_inspection_omitted"] = False
        if not fits():
            result.pop("source_inspection")
            projection["source_inspection_omitted"] = True
    if not fits():
        raise ValueError(f"Minimum truthful evidence envelope exceeds max_tokens; framing requires {(output_bytes(result) + 3) // 4} budget units")
    if compact_public:
        return budget_report(_compact_public_result(result), max_tokens)
    return result
