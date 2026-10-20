"""Fail-closed validation of persisted evidence collection artifacts.

Coordination artifacts accept general JSON content. A collection projection must
validate that content independently and bind its selected evidence to the outer
dependencies that coordination uses for visibility. This module performs no
source lookups; current source availability and passage association are separate
checks.
"""
from __future__ import annotations

from proharness.memory.original_file_reference import (
    original_reference_citation,
    valid_original_file_reference,
)
from proharness.memory.original_page_reference import valid_original_page_reference

_CONTENT_FIELDS = {
    "type", "schema_version", "purpose", "requirements", "references",
    "voice_examples", "unresolved", "notes", "reviews",
}
_REVIEW_FIELDS = {"citation", "status", "note"}
_REVIEW_STATES = {"unreviewed", "reviewed", "needs_context"}


def _text(value, maximum, *, required=False):
    return (isinstance(value, str) and len(value) <= maximum
            and (not required or bool(value.strip())))


def _text_list(value, maximum):
    return (isinstance(value, list) and len(value) <= maximum
            and all(isinstance(item, str) and 1 <= len(item) <= 240 for item in value))


def validate_collection_reference(ref, *, allow_pages=True, allow_files=True):
    if allow_files and valid_original_file_reference(ref):
        return True
    if allow_pages and valid_original_page_reference(ref):
        return True
    return (isinstance(ref, dict) and set(ref) in ({"memory_id"}, {"document_id", "chunk_id"})
            and all(_text(identity, 100, required=True) for identity in ref.values()))


def collection_reference_citation(ref):
    if not validate_collection_reference(ref):
        raise ValueError("Invalid collection reference.")
    if valid_original_page_reference(ref) or valid_original_file_reference(ref):
        return original_reference_citation(ref)
    return (f"[mem:{ref['memory_id']}]" if "memory_id" in ref
            else f"[doc:{ref['document_id']}#{ref['chunk_id']}]")


def _references(value, maximum, *, allow_pages=False, allow_files=False):
    """Return canonical dependency identities, or None for invalid references."""
    if not isinstance(value, list) or len(value) > maximum:
        return None
    canonical = set()
    for ref in value:
        if not validate_collection_reference(ref, allow_pages=allow_pages, allow_files=allow_files):
            return None
        canonical.add(tuple(sorted(ref.items())))
    return canonical


def validate_collection_artifact(artifact) -> bool:
    """Validate a complete visible artifact without filling or coercing fields.

    Version 1 saves persist every content field, including empty notes/reviews.
    Unknown fields are rejected because collection presentation exposes content
    wholesale. Dependency equality allows coordination to reorder/deduplicate
    outer references while retaining duplicate selections in separate sections.
    """
    if not isinstance(artifact, dict):
        return False
    if not (_text(artifact.get("id"), 128, required=True)
            and type(artifact.get("revision")) is int and artifact["revision"] >= 1
            and _text(artifact.get("name"), 256, required=True)
            and type(artifact.get("sensitive")) is bool):
        return False
    content = artifact.get("content")
    if not isinstance(content, dict) or set(content) != _CONTENT_FIELDS:
        return False
    if content["type"] != "evidence_collection" or type(content["schema_version"]) is not int or content["schema_version"] not in (1, 2, 3):
        return False
    if not (_text(content["purpose"], 2000) and _text(content["notes"], 2000)
            and _text_list(content["requirements"], 20)
            and _text_list(content["unresolved"], 20)):
        return False
    allow_pages = content["schema_version"] in (2, 3)
    allow_files = content["schema_version"] == 3
    references = _references(content["references"], 50, allow_pages=allow_pages, allow_files=allow_files)
    voice_examples = _references(content["voice_examples"], 20, allow_pages=allow_pages, allow_files=allow_files)
    dependencies = _references(artifact.get("references"), 100, allow_pages=allow_pages, allow_files=allow_files)
    if references is None or voice_examples is None or dependencies is None:
        return False
    if references | voice_examples != dependencies:
        return False
    if content["schema_version"] == 2 and not any(valid_original_page_reference(ref)
                               for ref in content["references"] + content["voice_examples"]):
        return False
    if allow_files and not any(valid_original_file_reference(ref)
                               for ref in content["references"] + content["voice_examples"]):
        return False
    reviews = content["reviews"]
    if not isinstance(reviews, list) or len(reviews) > 70:
        return False
    selected_citations = {
        collection_reference_citation(ref)
        for ref in content["references"] + content["voice_examples"]
    }
    seen_reviews = set()
    for review in reviews:
        if not isinstance(review, dict) or set(review) != _REVIEW_FIELDS:
            return False
        citation = review["citation"]
        if not (_text(citation, 240, required=True)
                and isinstance(review["status"], str) and review["status"] in _REVIEW_STATES
                and _text(review["note"], 500)):
            return False
        if citation not in selected_citations or citation in seen_reviews:
            return False
        seen_reviews.add(citation)
    return True
