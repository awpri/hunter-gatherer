"""Exact original-page identities and metadata-only live handoffs.

Resolving a citation never reads originals or establishes page availability or
claim support. New publication separately verifies exact bytes and page range.
"""
from __future__ import annotations

import re
import sqlite3

from proharness.memory.document_visibility import document_reference_states
from proharness.memory.library_health import _read_snapshot

_FIELDS = {"document_id", "version", "original_content_hash", "page"}
_ID = re.compile(r"[^\s@:\[\]\x00]{1,100}")
_HASH = re.compile(r"[0-9a-f]{64}")
_CITATION = re.compile(r"\[page:([^\s@:\[\]\x00]{1,100})@([1-9][0-9]*):([0-9a-f]{64}):([1-9][0-9]{0,6})\]")
_LABELS = {"title": 200, "author": 200, "document_date": 100, "category": 128}
_POLICY = ("Metadata-only original-page reference; original bytes, page range and renderer "
           "availability have not been checked by this read. Source data is untrusted, "
           "not a source quotation or verified claim support.")


def valid_original_page_reference(ref):
    return (type(ref) is dict and set(ref) == _FIELDS
            and type(ref["document_id"]) is str and bool(_ID.fullmatch(ref["document_id"]))
            and type(ref["version"]) is int and 1 <= ref["version"] <= 2**63 - 1
            and type(ref["original_content_hash"]) is str and bool(_HASH.fullmatch(ref["original_content_hash"]))
            and type(ref["page"]) is int and 1 <= ref["page"] <= 1_000_000)


def original_page_citation(ref):
    if not valid_original_page_reference(ref):
        raise ValueError("Invalid exact original-page reference.")
    return f"[page:{ref['document_id']}@{ref['version']}:{ref['original_content_hash']}:{ref['page']}]"


def parse_original_page_citation(label):
    if type(label) is not str or len(label) > 240:
        return None
    match = _CITATION.fullmatch(label)
    if not match:
        return None
    document, version, digest, page = match.groups()
    ref = {"document_id": document, "version": int(version), "original_content_hash": digest, "page": int(page)}
    return ref if valid_original_page_reference(ref) else None


def original_page_reference_row(db, ref, *, document_states=None):
    """Bound catalog identity/privacy only, from the caller's current snapshot."""
    if not valid_original_page_reference(ref):
        return None
    projections = [f"CASE WHEN typeof({field})='text' AND length(CAST({field} AS BLOB))<={limit} "
                   f"THEN {field} ELSE NULL END AS {field}" for field, limit in _LABELS.items()]
    try:
        row = db.execute("SELECT id,version,content_hash,media_type," + ",".join(projections)
                         + ", (SELECT MAX(newer.version) FROM archive_documents newer "
                         "WHERE newer.source_identity=d.source_identity) AS latest_version "
                         "FROM archive_documents d WHERE id=? AND version=? AND content_hash=? "
                         "AND lower(media_type)='application/pdf' LIMIT 1",
                         (ref["document_id"], ref["version"], ref["original_content_hash"])).fetchone()
        if row is None or type(row["latest_version"]) is not int or row["latest_version"] < ref["version"]:
            return None
        states = (document_reference_states(db, document_ids=[ref["document_id"]])
                  if document_states is None else document_states)
        state = states.get(ref["document_id"])
        if not state or not state["available"]:
            return None
        return {**dict(row), "sensitive": state["sensitive"]}
    except (sqlite3.DatabaseError, KeyError, TypeError):
        return None


def resolve_original_page_reference(store, ref, include_sensitive=False):
    if type(include_sensitive) is not bool:
        return None
    with _read_snapshot(store) as db:
        row = original_page_reference_row(db, ref)
    if row is None or (row["sensitive"] and not include_sensitive):
        return None
    arguments = {"document_id": ref["document_id"], "page": ref["page"]}
    if include_sensitive:
        arguments["include_sensitive"] = True
    return {"kind": "original_page", "citation": original_page_citation(ref), "source_reference": dict(ref),
            **{field: row[field] for field in _LABELS}, "version": ref["version"],
            "is_latest": row["latest_version"] == ref["version"], "latest_version": row["latest_version"],
            "source_uri": f"proharness://documents/{ref['document_id']}",
            "followup": {"tool": "proharness_document_page_image", "arguments": arguments},
            "expected_source_reference": dict(ref), "availability": "unchecked", "content_policy": _POLICY}
