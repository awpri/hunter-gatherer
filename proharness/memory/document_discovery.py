"""Bounded catalog discovery, independent of extracted text and claim support.

Only latest visible catalog metadata is matched. A match is a navigation lead,
never evidence that the original contains an answer. No original access,
extraction, embeddings, schema writes, or transaction ownership occurs here.
"""
from __future__ import annotations

import heapq
import re
import unicodedata

from proharness.memory.document_handoff import _HASH, _ID, _LABEL_LIMITS, _MEDIA_TYPE
from proharness.memory.document_visibility import visible_document_clause

_POLICY = (
    "Catalog labels are untrusted navigation metadata, not quoted evidence or claim support. "
    "Original bytes, page locations, and renderer availability are unchecked. "
    "Stored chunk presence does not establish extraction completeness or validity."
)
_LIMITS = {**_LABEL_LIMITS, "source_filename": 255, "media_type": 120, "extraction_status": 64}
_MATCH_FIELDS = (*_LABEL_LIMITS, "source_filename")
_METHOD = "unicode_exact_metadata_tokens"


def _tokens(text):
    # Share the evidence stop list without its semantic query expansion.
    from proharness.memory.evidence import _STOP_WORDS

    normalized = unicodedata.normalize("NFC", text).casefold()
    return frozenset(token for token in re.findall(r"\w+", normalized)
                     if len(token) >= 2 and token not in _STOP_WORDS)


def _validate_queries(queries):
    if type(queries) is not list or not 1 <= len(queries) <= 5:
        raise ValueError("queries must contain 1 to 5 strings")
    if any(type(query) is not str or not query.strip() or len(query) > 4000 for query in queries):
        raise ValueError("each discovery query must contain 1 to 4000 characters")
    return list(dict.fromkeys(queries))


def _validate_sensitive(include_sensitive):
    if type(include_sensitive) is not bool:
        raise ValueError("include_sensitive must be a boolean")


def _label_projection(field, limit):
    # Read at most four bytes per Unicode code point, then validate character
    # bounds in Python. SQLite length(text) stops at NUL, so bound by bytes first.
    return (f"CASE WHEN typeof(d.{field})='text' AND length(CAST(d.{field} AS BLOB))<={limit * 4} "
            f"THEN d.{field} ELSE NULL END AS {field}")


def _catalog_rows(store, *, include_sensitive, ordered=False, without_passages=False,
                  row_ids=None, all_records=False):
    projections = [_label_projection(field, limit) for field, limit in _LIMITS.items()
                   if field != "source_filename"]
    # rtrim removes the set of non-separator characters from the end, leaving
    # the directory prefix. Only its final basename crosses the SQL boundary.
    # Bound and reject NUL before string operations; never read original_path.
    normalized = "replace(d.source_path,char(92),'/')"
    basename = f"substr({normalized},length(rtrim({normalized},replace({normalized},'/','')))+1)"
    projections.append(
        "CASE WHEN typeof(d.source_path)='text' AND length(CAST(d.source_path AS BLOB))<=16384 "
        f"AND instr(d.source_path,char(0))=0 THEN CASE WHEN length(CAST({basename} AS BLOB))<=1020 "
        f"THEN {basename} ELSE NULL END ELSE NULL END AS source_filename")
    projections += ["d.rowid AS catalog_rowid", _label_projection("id", 36), _label_projection("content_hash", 64), "d.version",
                    "EXISTS (SELECT 1 FROM archive_chunks c WHERE c.document_id=d.id) AS has_stored_chunks"]
    clause = "1" if all_records else visible_document_clause(include_sensitive)
    parameters = []
    if row_ids is not None:
        clause += " AND d.rowid IN (SELECT value FROM json_each(?))"
        import json
        parameters.append(json.dumps(row_ids))
    if without_passages:
        clause += " AND NOT EXISTS (SELECT 1 FROM archive_chunks c WHERE c.document_id=d.id)"
    sql = "SELECT " + ",".join(projections) + " FROM archive_documents d WHERE " + clause
    if ordered:
        sql += " ORDER BY d.created_at DESC,d.id"
    cursor = store._sqlite.execute(sql, parameters)
    try:
        while batch := cursor.fetchmany(64):
            yield from batch
    finally:
        cursor.close()


def _schema_available(store):
    rows = store._sqlite.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('archive_documents','archive_chunks')")
    return len(rows.fetchall()) == 2


def _candidate(row, *, include_sensitive):
    doc, digest, version = row["id"], row["content_hash"], row["version"]
    if (type(doc) is not str or not _ID.fullmatch(doc)
            or type(digest) is not str or not _HASH.fullmatch(digest)
            or type(version) is not int or version < 1):
        return None
    labels, warnings = {}, []
    for field, limit in _LIMITS.items():
        value = row[field]
        if type(value) is not str or len(value) > limit or "\x00" in value:
            value = None
            warnings.append(f"{field}_unavailable")
        labels[field] = value
    if labels["media_type"] is not None and not _MEDIA_TYPE.fullmatch(labels["media_type"]):
        labels["media_type"] = None
        warnings.append("media_type_unavailable")
    arguments = {"document_id": doc}
    if include_sensitive:
        arguments["include_sensitive"] = True
    result = {
        "kind": "document_catalog", "document_id": doc, "version": version,
        "content_hash": digest, **labels, "has_stored_chunks": bool(row["has_stored_chunks"]),
        "claim_support": "not_determined",
        "followups": [{"tool": "proharness_document_outline", "arguments": dict(arguments)},
                      {"tool": "proharness_get_document", "arguments": {**arguments, "max_chars": 2000}}],
    }
    from proharness.memory.original_file_reference import original_file_citation

    result["source_reference"] = {"document_id": doc, "version": version, "original_content_hash": digest}
    result["original_citation"] = original_file_citation(result["source_reference"])
    if labels["media_type"] and labels["media_type"].casefold() == "application/pdf":
        result["followups"].append({
            "tool": "proharness_document_page_image", "arguments": dict(arguments),
            "required_arguments": ["page"], "page_location": "unknown", "availability": "unchecked",
            "expected_document": {"document_id": doc, "version": version, "original_content_hash": digest},
        })
    if warnings:
        result["source_metadata_warnings"] = warnings
    return result


def _match(candidate, token_queries):
    fields = {name: _tokens(candidate[name] or "") for name in _MATCH_FIELDS}
    all_terms = frozenset().union(*fields.values())
    matches = [terms & all_terms for terms in token_queries]
    if not any(matches):
        return None
    # Deduplicate query token sets for ranking: repetition never boosts a lead.
    unique_matches = [terms & all_terms for terms in set(token_queries)]
    rank = (max(map(len, unique_matches)), len(frozenset().union(*unique_matches)))
    candidate["matched_queries"] = [i for i, match in enumerate(matches) if match]
    candidate["matched_fields"] = [name for name, terms in fields.items()
                                   if any(terms & query for query in token_queries)]
    # Tokens themselves are omitted: matching remains bounded for long queries.
    return rank


def _discovery_paging(offset, limit, total, returned):
    next_offset = offset + returned
    has_more = next_offset < total
    return {"offset": offset, "next_offset": next_offset if has_more else None,
            "has_more": has_more, "total": total}


def discover_documents(store, *, queries, limit=8, include_sensitive=False, offset=None):
    """Return up to eight metadata leads from one streaming, visible catalog scan.

    Rank by largest distinct-token intersection for one query, then union of
    matched query terms, then document ID ascending. Output size is bounded;
    scan work is linear in eligible documents, not capped to recent imports.
    An optional offset pages the same stable ranking beyond the initial eight;
    skipped candidates retain numeric ranking identities rather than payloads.
    """
    queries = _validate_queries(queries)
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError("limit must be an integer from 1 to 8")
    _validate_sensitive(include_sensitive)
    if offset is not None and (type(offset) is not int or offset < 0):
        raise ValueError("offset must be a nonnegative integer")
    result = {"executed": True, "queries": queries, "method": _METHOD, "scope": "latest_visible_documents",
              "content_policy": _POLICY, "limit": limit, "candidates": [], "scanned_documents": 0,
              "matching_documents": 0, "omitted_documents": 0, "invalid_identity_documents": 0,
              "documents_with_omitted_metadata": 0, "matching_scope": "bounded_catalog_labels"}
    if offset is not None:
        result.update(_discovery_paging(offset, limit, 0, 0))
    token_queries = [_tokens(query) for query in queries]
    if not any(token_queries):
        result.update(executed=False, reason="no_searchable_metadata_terms")
        return result
    if getattr(store.settings, "archive_catalog_index", False):
        from proharness.memory.catalog_index import indexed_discovery
        indexed = indexed_discovery(store, token_queries, limit, include_sensitive, offset=offset)
        if indexed is not None:
            return {**result, **indexed}
    heap = []
    with store._lock:
        if not _schema_available(store):
            result.update(executed=False, reason="archive_unavailable")
            return result
        for row in _catalog_rows(store, include_sensitive=include_sensitive):
            result["scanned_documents"] += 1
            candidate = _candidate(row, include_sensitive=include_sensitive)
            if candidate is None:
                result["invalid_identity_documents"] += 1
                continue
            if candidate.get("source_metadata_warnings"):
                result["documents_with_omitted_metadata"] += 1
            rank = _match(candidate, token_queries)
            if rank is None:
                continue
            result["matching_documents"] += 1
            # Reverse lexical ID for heap eviction; final output orders it forward.
            key = (*rank, tuple(-ord(char) for char in candidate["document_id"]))
            item = (key, candidate if offset is None else row["catalog_rowid"])
            retained_limit = limit if offset is None else offset + limit
            if len(heap) < retained_limit:
                heapq.heappush(heap, item)
            elif key > heap[0][0]:
                heapq.heapreplace(heap, item)
        if offset is not None:
            # Retain only numeric ranking keys and row identities for skipped
            # pages, never label payloads or text. Hydrate the bounded page.
            page = sorted(heap, key=lambda item: item[0], reverse=True)[offset:offset + limit]
            rows = {row["catalog_rowid"]: row for row in _catalog_rows(
                store, include_sensitive=include_sensitive, row_ids=[item[1] for item in page])} if page else {}
            for _, rowid in page:
                candidate = _candidate(rows[rowid], include_sensitive=include_sensitive)
                _match(candidate, token_queries)
                result["candidates"].append(candidate)
    if offset is None:
        result["candidates"] = [item[1] for item in sorted(heap, key=lambda item: item[0], reverse=True)]
    else:
        result.update(_discovery_paging(offset, limit, result["matching_documents"], len(result["candidates"])))
    result["omitted_documents"] = result["matching_documents"] - len(result["candidates"])
    return result


def browse_document_catalog(store, *, query="", without_passages=False, limit=20, offset=0,
                            include_sensitive=False):
    """Metadata-filtered listing, newest first, preserving bounded pagination."""
    if type(query) is not str or len(query) > 4000:
        raise ValueError("query must be a string of at most 4000 characters")
    if type(without_passages) is not bool:
        raise ValueError("without_passages must be a boolean")
    _validate_sensitive(include_sensitive)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError("limit must be 1 to 100 and offset must be nonnegative integers")
    result = {"documents": [], "total": 0, "next_offset": None, "method": _METHOD,
              "content_policy": _POLICY, "without_passages": without_passages,
              "scanned_documents": 0, "invalid_identity_documents": 0,
              "documents_with_omitted_metadata": 0, "matching_scope": "bounded_catalog_labels"}
    tokens = _tokens(query)
    if query.strip() and not tokens:
        return result
    if getattr(store.settings, "archive_catalog_index", False):
        from proharness.memory.catalog_index import indexed_browse
        indexed = indexed_browse(store, tokens, limit, offset, include_sensitive, without_passages)
        if indexed is not None:
            return {**result, **indexed}
    with store._lock:
        if not _schema_available(store):
            result["reason"] = "archive_unavailable"
            return result
        for row in _catalog_rows(store, include_sensitive=include_sensitive, ordered=True,
                                 without_passages=without_passages):
            result["scanned_documents"] += 1
            candidate = _candidate(row, include_sensitive=include_sensitive)
            if candidate is None:
                result["invalid_identity_documents"] += 1
                continue
            if candidate.get("source_metadata_warnings"):
                result["documents_with_omitted_metadata"] += 1
            if tokens and _match(candidate, [tokens]) is None:
                continue
            index = result["total"]
            result["total"] += 1
            if offset <= index < offset + limit:
                result["documents"].append(candidate)
    if offset + limit < result["total"]:
        result["next_offset"] = offset + limit
    return result
