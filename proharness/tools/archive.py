"""Read-only archive tools for the internal agent and its workers.

The MCP and internal agent surfaces intentionally share their input contracts
and retrieval implementations. The internal tools do not create another
Harness, router, or store. Outputs remain structured source material so exact
quotations and page locations survive the round trip to the model.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

from proharness.tools.base import Tool, ToolContext, ToolResult, estimate_tokens

ARCHIVE_TOOL_NAMES = frozenset({
    "evidence", "search_documents", "get_document", "get_document_chunk", "list_documents",
    "document_outline", "document_window", "fetch",
})


def _without_parent_context(value):
    """Drop duplicate surrounding text, never edit exact quotes or facts."""
    if isinstance(value, list):
        return [_without_parent_context(item) for item in value]
    if not isinstance(value, dict):
        return value
    passage = isinstance(value.get("quote"), str) and value.get("document_id") and value.get("chunk_id")
    return {
        key: _without_parent_context(item) for key, item in value.items()
        if not (passage and key in {"context", "context_location"})
    }


def _source_candidates(payload: dict) -> list[dict]:
    candidates = [payload]
    for key in ("hits", "supporting_passages", "source_leads", "documents", "chunks", "sources"):
        values = payload.get(key)
        if isinstance(values, list):
            candidates.extend(item for item in values if isinstance(item, dict))
    discovery = payload.get("source_discovery")
    if isinstance(discovery, dict) and isinstance(discovery.get("candidates"), list):
        candidates.extend(item for item in discovery["candidates"] if isinstance(item, dict))
    for memory in payload.get("relevant_memories", []):
        if isinstance(memory, dict) and isinstance(memory.get("provenance"), dict):
            candidates.extend(item for item in memory["provenance"].get("sources", []) if isinstance(item, dict))
    return candidates


def _retry_reference(candidate: dict, store, max_tokens: int, include_sensitive: bool) -> dict | None:
    """Resolve a visible source and translate section-local to parent offsets."""
    from proharness.memory.documents import DocumentArchive

    document_id = candidate.get("document_id")
    chunk_id = candidate.get("chunk_id")
    if store is None or not isinstance(document_id, str):
        return None
    if candidate.get("kind") == "document_catalog":
        from proharness.memory.document_handoff import _HASH, _ID

        version, digest = candidate.get("version"), candidate.get("content_hash")
        if (not _ID.fullmatch(document_id) or type(version) is not int or version < 1
                or type(digest) is not str or not _HASH.fullmatch(digest)):
            return None
        with store._lock:
            visible = DocumentArchive._visible(include_sensitive)
            valid = store._sqlite.execute(
                f"SELECT 1 FROM archive_documents d WHERE d.id=? AND d.version=? AND d.content_hash=? AND {visible}",
                (document_id, version, digest),
            ).fetchone()
        if valid is None:
            return None
        args = {"document_id": document_id}
        if include_sensitive:
            args["include_sensitive"] = True
        return {"kind": "document_catalog", "document_id": document_id, "version": version,
                "claim_support": "not_determined",
                "retry": {"tool": "document_outline", "arguments": args},
                "inspection_note": "Catalog lead only. Empty extraction may require an image-capable assistant to inspect the original; page is unknown."}
    with store._lock:
        visible = DocumentArchive._visible(include_sensitive, latest=False)
        doc = store._sqlite.execute(
            f"SELECT d.id FROM archive_documents d WHERE d.id=? AND {visible}", (document_id,),
        ).fetchone()
        if doc is None:
            return None
        if chunk_id:
            chunk = store._sqlite.execute(
                "SELECT c.start,s.ordinal,s.page FROM archive_chunks c "
                "JOIN archive_sections s ON s.id=c.section_id "
                "WHERE c.id=? AND c.document_id=?", (chunk_id, document_id),
            ).fetchone()
            if chunk is None:
                return None
            page = chunk["page"]
            prior = store._sqlite.execute(
                "SELECT coalesce(sum(length(text)),0) FROM archive_sections "
                "WHERE document_id=? AND ordinal<?" + (" AND page=?" if page is not None else ""),
                (document_id, chunk["ordinal"], page) if page is not None else (document_id, chunk["ordinal"]),
            ).fetchone()[0]
            start = prior + chunk["start"]
        else:
            page = candidate.get("page")
            start = candidate.get("start", 0)
            page = page if type(page) is int else None
            start = start if type(start) is int and start >= 0 else 0
    args = {"document_id": document_id, "start": start, "max_chars": min(512, max_tokens)}
    if page is not None:
        args["page"] = page
    if include_sensitive:
        args["include_sensitive"] = True
    ref = {"document_id": document_id, "retry": {"tool": "get_document", "arguments": args}}
    if chunk_id:
        ref["chunk_id"] = chunk_id
    return ref


def _internal_followups(value):
    if isinstance(value, list):
        for item in value:
            _internal_followups(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            candidates = item if key == "followups" and isinstance(item, list) else (
                [item] if key in {"followup", "retry"} and isinstance(item, dict) else [])
            for followup in candidates:
                if isinstance(followup, dict):
                    tool = followup.get("tool", "")
                    if isinstance(tool, str) and tool.startswith("proharness_") and tool[11:] in ARCHIVE_TOOL_NAMES:
                        followup["tool"] = tool[11:]
            if key not in {"known_details", "provenance"}:
                _internal_followups(item)


def _bound_evidence_result(payload, max_tokens, store, include_sensitive):
    """Keep requirement routing and search diagnostics at the prompt boundary.

    Use the shared projector when it fits; the smaller internal default keeps
    diagnostics plus reauthorized citation retries. Neither path infers support.
    """
    from proharness.memory.evidence_bounded import bounded_evidence_packet
    from proharness.memory.evidence_compact import expand_evidence_packet

    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    if "compact_format" in payload:
        payload = expand_evidence_packet(payload)
    if max_tokens >= 512 and "bounded_format" not in payload:
        try:
            # Fits the stricter byte budget too; no source is rewritten.
            projected = bounded_evidence_packet(payload, max_tokens=min(max_tokens, 16384))
            _internal_followups(projected)
            from proharness.memory.output_budget import budget_report
            budget_report(projected, min(max_tokens, 16384))
            return json.dumps(projected, indent=2)
        except ValueError:
            pass  # The diagnostic envelope may need the smaller retry form.

    def assessment(value):
        return {key: value[key] for key in ("status", "scope", "candidate_count") if key in value}

    result = {"truncated": True, "answerability": "not_determined",
              "guidance": "Text omitted for budget, not absent. Use fetch with candidate_citations; inspect exact sources before answering.",
              "retrieval_assessment": assessment(payload.get("retrieval_assessment", {})),
              "requirement_checks": []}
    retrieval = payload.get("retrieval", {})
    search = retrieval.get("document_search", {})
    main = payload.get("retrieval_assessment", {})
    def missing_refs(value):
        return len(value.get("candidate_citations", [])) + value.get("omitted_list_counts", {}).get("candidate_citations", 0)

    result["retrieval_assessment"].update(candidate_citations=[], candidate_references_omitted=missing_refs(main))
    result["retrieval"] = {key: search[key] for key in ("executed", "scope") if key in search}
    if "archive_coverage" in payload:
        from proharness.memory.archive_coverage import compact_coverage
        result["archive_coverage"] = compact_coverage(payload["archive_coverage"])
    warnings = list(retrieval.get("warnings", []))
    checks = payload.get("requirement_checks", [])
    for check in checks:
        entry = {"query": check["query"], **assessment(check.get("retrieval_assessment", {})),
                 "candidate_citations": [],
                 "candidate_references_omitted": missing_refs(check.get("retrieval_assessment", {}))}
        result["requirement_checks"].append(entry)
        warnings.extend(check.get("warnings", []))
    if warnings:
        result["warnings"] = list(dict.fromkeys(warnings))
    for name in ("source_discovery", "enrichment_discovery"):
        if name in payload:
            discovery = payload[name]
            result[name] = {key: discovery[key] for key in ("executed", "reason", "scope", "warnings") if key in discovery}
            result[name]["candidates_omitted"] = len(discovery.get("candidates", []))
    if estimate_tokens(encode(result)) > max_tokens:
        error = {"error": "Evidence diagnostics exceed budget; narrow requirements or increase tool_result_max_tokens.",
                 "truncated": True, "answerability": "not_determined"}
        return encode(error) if estimate_tokens(encode(error)) <= max_tokens else None

    # Recheck current access before putting a citation back into the prompt.
    assessments = [*(check.get("retrieval_assessment", {}) for check in checks), main]
    destinations = [*result["requirement_checks"], result["retrieval_assessment"]]
    refs = list(dict.fromkeys(c for check in assessments for c in check.get("candidate_citations", [])))[:32]
    if refs and store is not None:
        from proharness.memory.reference_fetch import fetch_references

        resolved = fetch_references(store, refs, max_tokens=512, include_sensitive=include_sensitive)
        allowed = {ref for ref, status in zip(refs, resolved["statuses"], strict=True) if status in {"available", "omitted"}}
        for original, entry in zip(assessments, destinations, strict=True):
            for citation in original.get("candidate_citations", []):
                if citation not in allowed:
                    continue
                entry["candidate_citations"].append(citation)
                entry["candidate_references_omitted"] -= 1
                if estimate_tokens(encode(result)) > max_tokens:
                    entry["candidate_citations"].pop()
                    entry["candidate_references_omitted"] += 1
    for candidate in payload.get("source_discovery", {}).get("candidates", []):
        reference = _retry_reference(candidate, store, max_tokens, include_sensitive)
        if reference is None:
            continue
        proposal = {**result, "references": [*result.get("references", []), reference]}
        if estimate_tokens(encode(proposal)) <= max_tokens:
            result = proposal
        if len(result.get("references", [])) >= 3:
            break
    return encode(result)


def bound_archive_result(raw: str, *, max_tokens: int, store=None, include_sensitive: bool = False) -> str:
    """Keep authoritative results exact or explicitly replace them with a retry.

    This is an internal prompt boundary, not a change to the MCP retrieval APIs.
    No model rewrites source text. Small results and disabled limits retain their
    original bytes; over-budget source text is never silently clipped.
    """
    if max_tokens <= 0 or estimate_tokens(raw) <= max_tokens:
        return raw

    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if payload.get("retrieval_strategy") == "sources" and isinstance(payload.get("documents"), list):
        # Source discovery remains metadata-only even at the prompt boundary.
        # Never use the generic fallback that may hydrate an excerpt instead.
        result = {**payload, "documents": [], "output_omissions": {
            "documents": len(payload["documents"]), "reason": "tool_result_budget"}}
        if estimate_tokens(encode(result)) > max_tokens:
            return encode({"error": "Source-route diagnostics exceed output budget; increase tool_result_max_tokens.",
                           "retrieval_strategy": "sources", "documents": [],
                           "retrieval_metadata_omitted": True,
                           "output_omissions": {"documents": len(payload["documents"]), "reason": "tool_result_budget"}})
        for document in payload["documents"]:
            proposal = {**result, "documents": [*result["documents"], document],
                        "output_omissions": {"documents": result["output_omissions"]["documents"] - 1,
                                             "reason": "tool_result_budget"}}
            if estimate_tokens(encode(proposal)) <= max_tokens:
                result = proposal
        return encode(result)
    if "requirement_checks" in payload and "retrieval_assessment" in payload:
        evidence = _bound_evidence_result(payload, max_tokens, store, include_sensitive is True)
        if evidence is not None:
            return evidence
    reduced = _without_parent_context(payload)
    if reduced != payload:
        reduced["parent_context_omitted"] = True
        rendered = encode(reduced)
        if estimate_tokens(rendered) <= max_tokens:
            return rendered

    # A small requested document page can still carry a large catalog envelope
    # (paths, author, title, etc.). Keep every returned passage and all paging
    # coordinates unchanged while explicitly omitting that optional metadata.
    if isinstance(payload.get("passages"), list) and isinstance(payload.get("document_id"), str):
        page = {key: payload[key] for key in (
            "document_id", "source_uri", "version", "is_latest", "latest_version",
            "page", "start", "next_start", "total_chars", "truncated", "passages",
            "warnings", "content_policy", "citation_map",
        ) if key in payload}
        page["metadata_omitted"] = True
        rendered = encode(page)
        if estimate_tokens(rendered) <= max_tokens:
            return rendered
        # Keep exact source text and continuation coordinates if citation
        # metadata alone prevents this page from fitting. Never silently drop
        # citations: callers must know the returned text has no attached map.
        if "citation_map" in page:
            page.pop("citation_map")
            page["citation_map_omitted"] = "response_budget"
            rendered = encode(page)
            if estimate_tokens(rendered) <= max_tokens:
                return rendered

    fallback = {
        "error": "Archive result exceeds the token budget. Source text was omitted, not summarized.",
        "truncated": True,
        "guidance": "Read a smaller get_document page using start/max_chars and follow next_start; or narrow the search with top_k=1.",
        "references": [],
    }
    selection = payload.get("candidate_selection")
    if isinstance(selection, dict):
        fallback["candidate_selection"] = {key: selection[key] for key in (
            "policy", "candidate_budget", "candidate_count", "ranking_complete",
            "suggested_candidate_budget", "exhaustive_fallback", "reason", "scope", "section_channel",
        ) if key in selection}
        if selection.get("ranking_complete") is False:
            fallback["guidance"] = (
                "Read references for omitted text. Retrieval was also incomplete: retry search_documents "
                "with suggested_candidate_budget, or narrow document_ids. Neither omission proves absence.")
    if estimate_tokens(encode(fallback)) <= max_tokens:
        seen = set()
        for candidate in _source_candidates(payload):
            key = (candidate.get("document_id"), candidate.get("chunk_id"))
            if key in seen:
                continue
            seen.add(key)
            try:
                reference = _retry_reference(candidate, store, max_tokens, include_sensitive is True)
            except Exception:
                reference = None  # Missing catalog or malformed metadata cannot expose a source.
            if reference is None:
                continue
            proposal = {**fallback, "references": [*fallback["references"], reference]}
            if estimate_tokens(encode(proposal)) <= max_tokens:
                fallback = proposal
            if len(fallback["references"]) >= 3:
                break
        return encode(fallback)
    for message in (
        '{"error":"Archive result too large; retry get_document with smaller max_chars","truncated":true}',
        "ERROR: archive result too large; retry with a smaller page.",
        "ERROR: result too large",
        "ERROR",
    ):
        if estimate_tokens(message) <= max_tokens:
            return message
    return "ERROR"  # The token estimator charges one token; positive budgets fit.


class _ArchiveReadTool(Tool):
    def __init__(self, name: str):
        # Import the contract only; constructing these tools never starts MCP,
        # instantiates a Harness, or opens the database.
        from proharness.mcp.tools import get_description, get_schema

        self.name = name
        self.description = get_description(f"proharness_{name}")
        self.parameters = copy.deepcopy(get_schema(f"proharness_{name}"))

    def _validate(self, args: dict[str, Any]) -> None:
        """Tool.safe_run parses JSON but does not enforce the declared schema.

        In particular, the string "false" must never opt into sensitive data.
        Bounds and domain-specific fields are validated/clamped by the shared
        retrieval APIs, preserving the same pagination behavior as MCP.
        """
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        properties = self.parameters["properties"]
        unknown = args.keys() - properties.keys()
        if unknown:
            raise ValueError(f"unknown arguments: {', '.join(sorted(unknown))}")
        for key in self.parameters.get("required", []):
            if key not in args:
                raise ValueError(f"missing required argument: {key}")
        types = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}
        for key, value in args.items():
            if value is None and key in {"page", "required_fields", "document_ids"}:
                continue
            expected = properties[key]["type"]
            if isinstance(expected, list):
                if value is None and "null" in expected:
                    continue
                expected = next((kind for kind in expected if kind != "null"), "null")
            if not isinstance(value, types[expected]) or (
                expected == "integer" and isinstance(value, bool)
            ):
                raise ValueError(f"{key} must be a {expected}")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if ctx.store is None:
            return ToolResult.err("memory store not configured")
        self._validate(args)
        if self.name == "evidence":
            from proharness.mcp.tools import evidence

            payload = evidence(SimpleNamespace(store=ctx.store, settings=ctx.store.settings), **args)
        elif self.name == "search_documents":
            from proharness.mcp.tools import search_documents

            payload = search_documents(SimpleNamespace(store=ctx.store), **args)
        elif self.name == "fetch":
            from proharness.memory.reference_fetch import fetch_references

            payload = fetch_references(ctx.store, **args)
        elif self.name in {"document_outline", "document_window"}:
            from proharness.memory.document_navigation import document_outline, document_window

            method = {"document_outline": document_outline, "document_window": document_window}[self.name]
            payload = method(ctx.store, **args)
        else:
            from proharness.memory.documents import DocumentArchive

            filtered_catalog = False
            if self.name == "list_documents":
                from proharness.memory.documents import _validate_catalog_filters

                _validate_catalog_filters(args.get("query", ""), args.get("without_passages", False))
                filtered_catalog = bool(args.get("query") or args.get("without_passages"))
            archive = DocumentArchive(ctx.store, initialize_search=not filtered_catalog)
            method = {
                "get_document": archive.get_document,
                "get_document_chunk": archive.get_chunk,
                "list_documents": archive.list_documents,
            }[self.name]
            payload = method(**args)
        _internal_followups(payload)
        if "budget" in payload:
            from proharness.memory.output_budget import budget_report

            budget_report(payload, payload["budget"]["max_tokens"])
        return ToolResult(
            content=json.dumps(payload, indent=2) if "bounded_format" in payload else json.dumps(payload, indent=2, ensure_ascii=False),
            error=bool(payload.get("error")),
            metadata={"authoritative": True},
        )


def archive_tools() -> list[Tool]:
    """Fresh tools bound to the invoking ToolContext's existing memory store."""
    return [_ArchiveReadTool(name) for name in (
        "evidence", "search_documents", "get_document", "get_document_chunk", "list_documents",
        "document_outline", "document_window", "fetch",
    )]
