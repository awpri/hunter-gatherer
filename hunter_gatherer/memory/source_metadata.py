"""Bounded source descriptors, preserving authored labels without inferring truth.

These fields are untrusted provenance, not a claim-support assessment. Missing
or malformed labels stay unknown; a record is never promoted to a fact by this
projection. Entire oversized labels are omitted rather than changing their
meaning by retaining only a prefix.
"""
from __future__ import annotations

import math
from collections.abc import Mapping


def _text(value, field, limit, warnings):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > limit:
        warnings.append(f"{field}_unavailable")
        return None
    return value


def memory_source_descriptor(metadata, *, memory_type, source, recorded_at=None):
    """Expose only bounded, explicitly stored memory labels and provenance."""
    warnings = []
    if not isinstance(metadata, Mapping):
        metadata = {}
        warnings.append("metadata_unavailable")
    result = {
        "type": _text(memory_type, "type", 32, warnings),
        "source_kind": _text(metadata.get("kind"), "source_kind", 128, warnings),
        "category": _text(metadata.get("category"), "category", 128, warnings),
        "verification": _text(metadata.get("verification"), "verification", 100, warnings) or "unverified",
        "source": _text(source, "source", 200, warnings),
        "recorded_at": None,
    }
    if recorded_at is not None:
        try:
            finite = type(recorded_at) in (int, float) and math.isfinite(recorded_at)
        except OverflowError:
            finite = False
        if finite:
            result["recorded_at"] = recorded_at
        else:
            warnings.append("recorded_at_unavailable")
    if warnings:
        result["source_metadata_warnings"] = warnings
    return result


def document_source_descriptor(*, category, author, document_date):
    """Expose document labels, not an inferred event date or factual status."""
    warnings = []
    result = {
        "category": _text(category, "category", 128, warnings),
        "author": _text(author, "author", 200, warnings),
        "document_date": _text(document_date, "document_date", 100, warnings),
    }
    if warnings:
        result["source_metadata_warnings"] = warnings
    return result
