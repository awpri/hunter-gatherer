"""Read-only source availability, never an answerability or completeness verdict."""
import sqlite3

from proharness.memory.document_visibility import visible_document_clause

_FIELDS = ("status", "scope", "document_count", "with_stored_passages",
           "without_stored_passages", "needs_extraction_review", "completeness", "reason")


def compact_coverage(value):
    """Keep coverage counts in bounded replies; optional navigation can yield."""
    # Counts concern latest visible documents in every format. The checked
    # status and with-passages complement are redundant when counts exist.
    omitted = {"with_stored_passages", "scope"}
    if value.get("status") == "checked":
        omitted.add("status")
    result = {key: value[key] for key in _FIELDS if key in value and key not in omitted}
    if "followups" in value:
        result["followups_omitted"] = "output_budget"
    return result


def archive_coverage(store, *, include_sensitive=False, include_global=True, query=""):
    """Caller owns the read snapshot; no prose, originals, models or writes.

    Counts use the same latest-version/visibility closure as document search.
    Chunk existence does not validate its text, extraction coverage or relevance.
    """
    result = {"status": "not_checked", "scope": "latest_visible_documents",
              "completeness": "not_established"}
    if not include_global:
        return {**result, "reason": "global_archive_excluded"}
    try:
        row = store._sqlite.execute(
            "SELECT count(*) total,coalesce(sum(EXISTS (SELECT 1 FROM archive_chunks c "
            "WHERE c.document_id=d.id)),0) with_chunks,"
            "coalesce(sum(d.extraction_status='needs_review'),0) needs_review "
            "FROM archive_documents d WHERE " + visible_document_clause(include_sensitive)
        ).fetchone()
    except sqlite3.DatabaseError:
        return {**result, "reason": "archive_unavailable"}
    result.update(status="checked", document_count=row[0], with_stored_passages=row[1],
                  without_stored_passages=row[0] - row[1], needs_extraction_review=row[2])
    if result["without_stored_passages"]:
        arguments = {"without_passages": True, "limit": 10}
        if include_sensitive:
            arguments["include_sensitive"] = True
        # Do not clip a long question into a different query. The caller may
        # add a focused query to the metadata-only browse operation.
        if isinstance(query, str) and query.strip() and len(query) <= 240:
            arguments["query"] = query
        result["followups"] = [{"tool": "proharness_list_documents", "arguments": arguments,
                              "purpose": "Inspect file metadata; missing passages are not evidence of absence."}]
    return result
