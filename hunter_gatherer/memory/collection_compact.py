"""Lossless JSON collection-export projection; never fetches or authorizes sources."""
from __future__ import annotations

import json
import math
from copy import deepcopy

from hunter_gatherer.memory.original_file_reference import (
    original_reference_citation,
    valid_original_file_reference,
)
from hunter_gatherer.memory.original_page_reference import valid_original_page_reference
from hunter_gatherer.memory.output_budget import budget_report, output_bytes, validate_budget

_VERSION = "hg_collection_export_compact_v1"
_EXPORT_FIELDS = ("collection_id", "revision", "truncated", "omitted_references", "content_policy")
_FULL_FIELDS = {"format", "content", "unresolved", "budget", *_EXPORT_FIELDS}
_COMPACT_FIELDS = {"format", "response_format", "content", "budget"}
_DEFAULTS = {"purpose": "", "requirements": [], "evidence": [], "voice_examples": [],
             "actor_notes": "", "actor_reviews": [], "unresolved": []}
_COLLECTION_FIELDS = {"name", "review_policy", "export", *_DEFAULTS}


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("Collection export requires finite JSON values and string object keys")


def _same(left, right):
    return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
        right, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _budget(value):
    report = value.get("budget")
    if not isinstance(report, dict):
        raise ValueError("Collection export requires a budget report")
    maximum = report.get("max_tokens")
    validate_budget(maximum)
    size = output_bytes(value)
    expected = {"max_tokens": maximum, "estimated_tokens": math.ceil(size / 4),
                "serialized_bytes": size, "method": "serialized UTF-8 bytes / 4; not a tokenizer"}
    if not _same(report, expected):
        raise ValueError("Collection export budget does not describe its serialized response")
    return maximum


def _content(content, shape):
    if type(content) is not dict or type(content.get("export")) is not dict:
        raise ValueError("Invalid collection export content")
    metadata = content["export"]
    if set(metadata) != set(_EXPORT_FIELDS):
        raise ValueError("Invalid collection export metadata")
    if (type(metadata["collection_id"]) is not str or not metadata["collection_id"]
            or type(metadata["revision"]) is not int or metadata["revision"] < 1
            or type(metadata["truncated"]) is not bool
            or type(metadata["omitted_references"]) is not int or metadata["omitted_references"] < 0
            or type(metadata["content_policy"]) is not str):
        raise ValueError("Invalid collection export metadata types")
    if type(content.get("unresolved")) is not list or any(type(x) is not str for x in content["unresolved"]):
        raise ValueError("Invalid collection unresolved items")
    if shape == "minimal":
        if "name" in content or set(content) & (_COLLECTION_FIELDS - {"export", "unresolved"}):
            raise ValueError("Minimal export contains collection fields")
        return
    if shape != "collection" or not set(content) >= _COLLECTION_FIELDS:
        raise ValueError("Invalid collection export shape")
    for field in ("name", "purpose", "actor_notes", "review_policy"):
        if type(content[field]) is not str:
            raise ValueError("Invalid collection text field")
    if type(content["requirements"]) is not list or any(type(x) is not str for x in content["requirements"]):
        raise ValueError("Invalid collection requirements")
    for field in ("evidence", "voice_examples"):
        if type(content[field]) is not list:
            raise ValueError("Invalid collection source section")
        for item in content[field]:
            if type(item) is not dict or type(item.get("citation")) is not str or not item["citation"]:
                raise ValueError("Invalid inline collection source")
            if item.get("kind") in {"original_page", "original_file"}:
                is_file = item["kind"] == "original_file"
                valid_reference = valid_original_file_reference if is_file else valid_original_page_reference
                reference = item.get("source_reference")
                followup = item.get("followup")
                if (not valid_reference(reference)
                        or item["citation"] != original_reference_citation(reference)
                        or ("expected_source_reference" in item and
                            not _same(item["expected_source_reference"], reference))
                        or "quote" in item or item.get("availability") != "unchecked"
                        or type(followup) is not dict or set(followup) != {"tool", "arguments"}
                        or followup["tool"] not in (
                            "hg_open", "hg_get_document" if is_file else "hg_document_page_image")
                        or type(followup["arguments"]) is not dict):
                    raise ValueError("Invalid original file source" if is_file else "Invalid original page source")
                arguments = followup["arguments"]
                if followup["tool"] == "hg_open":
                    if (set(arguments) != {"refs", "view", "max_tokens", "include_sensitive"}
                            or arguments["refs"] != [item["citation"]]
                            or arguments["view"] != ("outline" if is_file else "page")
                            or type(arguments["include_sensitive"]) is not bool
                            or type(arguments["max_tokens"]) is not int or not 512 <= arguments["max_tokens"] <= 16384):
                        raise ValueError("Invalid public original source followup")
                    continue
                coordinate = "max_chars" if is_file else "page"
                expected = 2000 if is_file else reference["page"]
                if (set(arguments) not in ({"document_id", coordinate}, {"document_id", coordinate, "include_sensitive"})
                        or arguments["document_id"] != reference["document_id"]
                        or type(arguments[coordinate]) is not int or arguments[coordinate] != expected
                        or ("include_sensitive" in arguments and arguments["include_sensitive"] is not True)):
                    raise ValueError("Invalid original file followup" if is_file else "Invalid original page followup")
            elif type(item.get("quote")) is not str or item.get("kind") not in ("memory", "document_chunk"):
                raise ValueError("Invalid inline collection source")
    if type(content["actor_reviews"]) is not list:
        raise ValueError("Invalid actor reviews")
    for review in content["actor_reviews"]:
        if (type(review) is not dict or type(review.get("citation")) is not str or not review["citation"]
                or review.get("status") not in ("unreviewed", "reviewed", "needs_context")
                or type(review.get("note")) is not str):
            raise ValueError("Invalid actor review fields")


def compact_collection_export(export: dict) -> dict:
    """Remove repeated framing and exact empty defaults, retaining standalone content.

    The delivered budget measures compact bytes. Sources, source ordering, user
    assessments, policy wording and visibility decisions are never changed.
    """
    _json_value(export)
    if type(export) is not dict or set(export) != _FULL_FIELDS or export.get("format") != "json":
        raise ValueError("Expected a full JSON collection export")
    maximum = _budget(export)
    content = export.get("content")
    if type(content) is not dict or "compact_format" in content:
        raise ValueError("Expected unprojected collection content")
    shape = "collection" if "name" in content else "minimal"
    _content(content, shape)
    for field in _EXPORT_FIELDS:
        if not _same(export[field], content["export"][field]):
            raise ValueError("Conflicting collection envelope metadata")
    if not _same(export["unresolved"], content["unresolved"]):
        raise ValueError("Conflicting collection unresolved items")
    result = {"format": "json", "response_format": "compact", "content": deepcopy(content)}
    defaults = _DEFAULTS if shape == "collection" else {"unresolved": []}
    for field, default in defaults.items():
        if field in result["content"] and _same(result["content"][field], default):
            result["content"].pop(field)
    result["content"]["compact_format"] = {"version": _VERSION, "shape": shape}
    return budget_report(result, maximum)


def expand_collection_export(export: dict) -> dict:
    """Restore full framing and account for expanded bytes; expanded bytes may exceed cap."""
    _json_value(export)
    if (type(export) is not dict or set(export) != _COMPACT_FIELDS
            or export.get("format") != "json" or export.get("response_format") != "compact"):
        raise ValueError("Expected a compact JSON collection export")
    maximum = _budget(export)
    content = deepcopy(export.get("content"))
    if type(content) is not dict:
        raise ValueError("Invalid compact collection content")
    marker = content.pop("compact_format", None)
    if (type(marker) is not dict or set(marker) != {"version", "shape"}
            or marker.get("version") != _VERSION or marker.get("shape") not in ("collection", "minimal")):
        raise ValueError("Unsupported compact collection format")
    shape = marker["shape"]
    defaults = _DEFAULTS if shape == "collection" else {"unresolved": []}
    for field, default in defaults.items():
        if field not in content:
            content[field] = deepcopy(default)
    _content(content, shape)
    result = {"format": "json", **deepcopy(content["export"]), "content": content,
              "unresolved": deepcopy(content["unresolved"])}
    return budget_report(result, maximum)
