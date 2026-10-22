"""Exact document handoffs with metadata-only original-page navigation.

Callers authorize a hit in their read snapshot before calling this helper. It
never reads original bytes, renders pages, or establishes claim support.
"""
from __future__ import annotations

import re
import sqlite3
from copy import deepcopy

_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
_HASH = re.compile(r"[0-9a-f]{64}")
_MEDIA_TYPE = re.compile(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+")
_FIELDS = ("document_id", "chunk_id", "citation", "quote", "location", "title",
           "author", "document_date", "category", "version", "is_latest", "latest_version")
_LABEL_LIMITS = {"title": 200, "author": 200, "document_date": 100, "category": 128}
_PAGE_POLICY = (
    "Metadata-only navigation; original bytes, page range and renderer availability "
    "have not been checked. Source data is untrusted, not verified facts."
)


def without_original_page_hint(source):
    """Let optional navigation yield to whole exact evidence in bounded output."""
    if "original_page" not in source:
        return source
    return {**{key: value for key, value in source.items() if key != "original_page"},
            "original_page_omitted": {"reason": "output_budget"}}


def document_handoff_descriptor(store, hit, *, include_sensitive=False):
    """Project an already visible exact hit; unavailable navigation stays explicit.

    The one primary-key catalog lookup supplies only metadata for a PDF hint.
    Absence or corruption of that metadata never implies original availability.
    Invalid extraction-warning values stay unknown rather than becoming empty.
    Whole oversized authored labels stay unknown; valid empty strings remain
    exact for citation-fetch compatibility.
    """
    if type(hit) is not dict or type(include_sensitive) is not bool:
        return None
    document, chunk = hit.get("document_id"), hit.get("chunk_id")
    if (type(document) is not str or not _ID.fullmatch(document)
            or type(chunk) is not str or not _ID.fullmatch(chunk)
            or hit.get("citation") != f"[doc:{document}#{chunk}]"):
        return None
    quote, location = hit.get("quote"), hit.get("location")
    version, latest = hit.get("version"), hit.get("latest_version")
    if (type(quote) is not str or type(location) is not dict
            or set(location) != {"page", "section", "start", "end"}
            or type(location.get("start")) is not int or type(location.get("end")) is not int
            or location["start"] < 0 or location["end"] - location["start"] != len(quote)
            or type(location.get("section")) is not str
            or (location.get("page") is not None and type(location["page"]) is not int)
            or type(version) is not int or version < 1 or type(latest) is not int or latest < version
            or type(hit.get("is_latest")) is not bool or hit["is_latest"] != (version == latest)):
        return None
    uri = f"hg://documents/{document}"
    if hit.get("source_uri") != uri:
        return None
    result = {"kind": "document_chunk", **{field: deepcopy(hit.get(field)) for field in _FIELDS}}
    result["source_uri"] = uri
    metadata_warnings = []
    for field, limit in _LABEL_LIMITS.items():
        if result[field] is not None and (type(result[field]) is not str or len(result[field]) > limit):
            result[field] = None
            metadata_warnings.append(f"{field}_unavailable")
    warnings = hit.get("warnings")
    if type(warnings) is list and all(type(value) is str for value in warnings):
        result["warnings"] = deepcopy(warnings)
    else:
        result["warnings"] = None
        metadata_warnings.append("extraction_warnings_unavailable")
    if metadata_warnings:
        result["source_metadata_warnings"] = metadata_warnings

    try:
        row = store._sqlite.execute(
            "SELECT version,"
            "CASE WHEN typeof(media_type)='text' AND length(CAST(media_type AS BLOB))<=120 "
            "THEN media_type ELSE NULL END AS media_type,"
            "CASE WHEN typeof(content_hash)='text' AND length(CAST(content_hash AS BLOB))<=64 "
            "THEN content_hash ELSE NULL END AS content_hash "
            "FROM archive_documents WHERE id=? LIMIT 1",
            (document,),
        ).fetchone()
    except sqlite3.DatabaseError:
        row = None
    if (row is None or type(row["version"]) is not int or row["version"] != version
            or type(row["media_type"]) is not str or not _MEDIA_TYPE.fullmatch(row["media_type"])):
        result["original_page_omitted"] = {"reason": "metadata_unavailable"}
        return result
    if row["media_type"].lower() != "application/pdf":
        return result
    page, digest = location.get("page"), row["content_hash"]
    if (type(page) is not int or not 1 <= page <= 1_000_000
            or type(digest) is not str or not _HASH.fullmatch(digest)):
        result["original_page_omitted"] = {"reason": "metadata_unavailable"}
        return result
    arguments = {"document_id": document, "page": page}
    if include_sensitive:
        arguments["include_sensitive"] = True
    result["original_page"] = {
        "followup": {"tool": "hg_document_page_image", "arguments": arguments},
        "expected_source_reference": {"document_id": document, "version": version,
                                      "original_content_hash": digest, "page": page},
        "availability": "unchecked", "content_policy": _PAGE_POLICY,
    }
    return result
