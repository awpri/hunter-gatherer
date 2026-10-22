"""A single exact-reference reader for text, context, outlines and PDF pages."""
from __future__ import annotations

from hunter_gatherer.memory.document_navigation import document_outline, document_window
from hunter_gatherer.memory.library_health import _read_snapshot
from hunter_gatherer.memory.original_file_reference import parse_original_file_citation
from hunter_gatherer.memory.original_page_reference import parse_original_page_citation
from hunter_gatherer.memory.output_budget import budget_report, output_bytes, validate_budget
from hunter_gatherer.memory.reference_fetch import _DOC, fetch_references
from hunter_gatherer.memory.simple_search import public_followups


def open_sources(harness, refs, view="text", offset=0, max_tokens=2048, include_sensitive=False):
    validate_budget(max_tokens)
    if type(include_sensitive) is not bool or type(offset) is not int or offset < 0:
        raise ValueError("include_sensitive must be boolean; offset must be nonnegative integer")
    if not isinstance(refs, list) or not 1 <= len(refs) <= 32:
        raise ValueError("refs requires 1–32 exact citations")
    if view not in {"text", "context", "outline", "page", "region"}:
        raise ValueError("view must be text, context, outline or page")
    if (view != "text" and len(refs) != 1) or (offset and view not in {"outline", "page", "region"}):
        raise ValueError("Context/outline/page require one reference; offset is for outline or file pages")
    from hunter_gatherer.memory.visual_regions import parse_region_citation, region_image
    if len(refs) == 1 and parse_region_citation(refs[0]):
        if view not in {"region", "page"} or offset:
            raise ValueError("Region references require region or page view without offset")
        result = region_image(harness.store, parse_region_citation(refs[0]), include_sensitive=include_sensitive, view=view)
        # Native PNG bytes are a separate image content block. Only the public
        # text metadata participates in the serialized text budget.
        metadata = {key: value for key, value in result.items() if not key.startswith("_")}
        budget_report(metadata, max_tokens)
        if output_bytes(metadata) > max_tokens * 4:
            metadata.pop("description", None)
            metadata["description_omitted"] = True
            budget_report(metadata, max_tokens)
        if output_bytes(metadata) > max_tokens * 4:
            raise ValueError("Budget too small for region image metadata; increase max_tokens")
        return {**{key: value for key, value in result.items() if key.startswith("_")}, **metadata}
    from hunter_gatherer.mcp.tools import _store_identity

    identity = {"id": _store_identity(harness)["id"]}
    # Keep public navigation inside the same advertised byte cap.
    inner_budget = max(512, max_tokens - 256)
    fetched = fetch_references(harness.store, refs, max_tokens=inner_budget,
                               include_sensitive=include_sensitive, store_identity=identity)
    if view == "text":
        result = public_followups(fetched, include_sensitive=include_sensitive)
        while output_bytes(budget_report(result, max_tokens)) > 4 * max_tokens and result["sources"]:
            removed = result["sources"].pop()
            for index in removed["indices"]:
                result["statuses"][index] = "omitted"
    else:
        # Resolve catalog identity and visibility first. A hidden source and a
        # missing source have the same result; page rendering rechecks access.
        # Omitted means visible but too large for this text budget; it must
        # not prevent metadata or surrounding-context navigation.
        if fetched["statuses"][0] not in {"available", "omitted"}:
            return budget_report(fetched, max_tokens)
        ref = refs[0]
        page_ref = parse_original_page_citation(ref)
        file_ref = parse_original_file_citation(ref)
        chunk_ref = _DOC.fullmatch(ref) if isinstance(ref, str) else None
        if view == "page":
            if not page_ref and file_ref:
                page_ref = {**file_ref, "page": offset + 1}
            elif offset:
                raise ValueError("Exact page citations already select a page; omit offset")
            if not page_ref:
                raise ValueError("Page view requires an exact page or original-file citation")
            from hunter_gatherer.memory.document_page_image import document_page_image

            result = document_page_image(harness.store, document_id=page_ref["document_id"],
                page=page_ref["page"], include_sensitive=include_sensitive)
            if not result.get("error") and result.get("source_reference") != page_ref:
                # Never deliver pixels from a different version/hash/page.
                result = {"error": "Original source identity changed or is unavailable.",
                          "error_code": "source_identity_mismatch"}
            result["store"] = identity
            metadata = {k: v for k, v in result.items() if not k.startswith("_")}
            if output_bytes(metadata) > 4 * max_tokens:
                raise ValueError("Budget too small for original-page metadata")
            return result
        if view == "context":
            if not chunk_ref:
                raise ValueError("Context view requires an exact document passage citation")
            result = document_window(harness.store, chunk_ref[2], before=1, after=1,
                max_tokens=inner_budget, include_sensitive=include_sensitive, store_identity=identity)
            result = public_followups(result, include_sensitive=include_sensitive)
        else:
            document_id = (file_ref or page_ref or {}).get("document_id") or (chunk_ref[1] if chunk_ref else None)
            if document_id is None:
                raise ValueError("Outline view requires a document passage or original-file/page citation")
            # One snapshot covers outline visibility and exact section→chunk links.
            with _read_snapshot(harness.store) as db:
                result = document_outline(harness.store, document_id, offset=offset, limit=20,
                    max_tokens=max(512, max_tokens - 512), include_sensitive=include_sensitive,
                    store_identity=identity)
                for section in result.get("sections", []):
                    section.pop("followup", None)
                    row = db.execute("SELECT id FROM archive_chunks WHERE document_id=? AND section_id=? ORDER BY start,id LIMIT 1",
                                     (document_id, section["section_id"])).fetchone()
                    if row:
                        section["followup"] = {"tool": "hg_open", "arguments": {
                            "refs": [f"[doc:{document_id}#{row[0]}]"], "view": "context",
                            "include_sensitive": include_sensitive}}
                while output_bytes(budget_report(result, max_tokens)) > max_tokens * 4 and result.get("sections"):
                    result["sections"].pop()
                    result["next_offset"] = offset + len(result["sections"])
                    result["omitted_reason"] = "output_budget"
            if result.get("next_offset") is not None:
                result["followup"] = {"tool": "hg_open", "arguments": {
                    "refs": refs, "view": "outline", "offset": result["next_offset"],
                    "max_tokens": max_tokens, "include_sensitive": include_sensitive}}
    if view in {"context", "outline"} and not result.get("error"):
        # Reuse the originating exact citation's already-authorized metadata.
        # Optional page navigation never displaces evidence or reads pixels.
        source = fetched["sources"][0] if fetched["sources"] else {}
        hint = source.get("original_page")
        if hint:
            result["original_page"] = public_followups(hint, include_sensitive=include_sensitive)
            budget_report(result, max_tokens)
            if output_bytes(result) > max_tokens * 4:
                result.pop("original_page")
                result["original_page_omitted"] = {"reason": "output_budget"}
                if output_bytes(budget_report(result, max_tokens)) > max_tokens * 4:
                    result.pop("original_page_omitted")
        elif source.get("original_page_omitted"):
            result["original_page_omitted"] = source["original_page_omitted"]
            if output_bytes(budget_report(result, max_tokens)) > max_tokens * 4:
                result.pop("original_page_omitted")
    budget_report(result, max_tokens)
    if output_bytes(result) > max_tokens * 4:
        raise ValueError("Budget too small for source navigation; increase max_tokens")
    return result
