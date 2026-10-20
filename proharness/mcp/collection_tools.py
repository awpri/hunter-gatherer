"""Personal-library actions in the existing artifact family; shared UI rules."""
from __future__ import annotations

from copy import deepcopy

from proharness.coordination import CoordinationError
from proharness.memory.collection_input import CollectionInput


def _collection_schema():
    schema = CollectionInput.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return inline(deepcopy(definitions[value["$ref"].removeprefix("#/$defs/")]))
        return {key: inline(item) for key, item in value.items() if key != "title"}

    schema = inline(schema)
    for key in ("include_sensitive", "idempotency_key", "expected_revision"):
        schema["properties"].pop(key)
    schema["required"] = ["name"]
    schema["description"] = (
        "Library collection content. Store only references: {memory_id}, {document_id,chunk_id}, "
        "exact original-file {document_id,version,original_content_hash}, or original-page {document_id,version,original_content_hash,page}. Keep voice_examples "
        "separate from evidence. Notes/reviews are actor assessments, never verified claims. "
        "Pass idempotency_key, expected_revision and include_sensitive at the top level."
    )
    return schema


_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_PRIVATE = {"include_sensitive": {"type": "boolean"}}
_REVISION = {"revision": {"type": "integer", "minimum": 1}}
OPERATIONS = {
    "artifact_collection_save": (
        {"collection": _collection_schema(), "artifact_id": _ID,
         "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
         "expected_revision": {"type": "integer", "minimum": 1}, **_PRIVATE},
        ["collection", "idempotency_key"],
        "Save a reference-only investigation in Library; no workspace_id or actor needed. "
        "collection requires name; optional purpose, requirements, references, voice_examples, "
        "unresolved, notes and reviews. Update with artifact_id and expected_revision; retry with "
        "the same idempotency_key and identical arguments. Uses the web library's live-source checks.", True,
    ),
    "artifact_collection_get": (
        {"artifact_id": _ID, **_REVISION, **_PRIVATE}, ["artifact_id"],
        "Reopen a Library collection, optionally at an immutable revision; no workspace_id needed. "
        "Returns found=false when absent or a redacted record when its dependencies are unavailable.", False,
    ),
    "artifact_collection_list": (
        {"limit": {"type": "integer", "minimum": 1, "maximum": 100},
         "after_id": {"type": "string"}, **_PRIVATE}, [],
        "List saved Library collections; follow next_cursor using after_id. No workspace_id needed.", False,
    ),
    "artifact_collection_export": (
        {"artifact_id": _ID, **_REVISION, **_PRIVATE,
         "format": {"type": "string", "enum": ["json", "markdown"]},
         "response_format": {"type": "string", "enum": ["full", "compact"]},
         "max_tokens": {"type": "integer", "minimum": 512, "maximum": 16384}},
        ["artifact_id"],
        "Prepare a bounded cited assistant handoff. Defaults to compact JSON and max_tokens=4096 "
        "(UTF-8 bytes/4 estimate, not actual tokens); markdown defaults to full. Preserves warnings, "
        "unresolved questions and separate voice examples, and rechecks source access. Use collection_refs "
        "at the returned revision to recover omitted references. No workspace_id needed.", False,
    ),
}


def dispatch(store, operation, arguments, *, public_tools=False):
    from proharness.memory import library_collections as library

    args = dict(arguments)
    identity = args.pop("artifact_id", None)
    try:
        if operation == "artifact_collection_save":
            content = args.pop("collection")
            # These controls have exactly one location in the MCP request.
            if set(content) & {"include_sensitive", "idempotency_key", "expected_revision"}:
                raise ValueError("Collection write controls must be supplied at the top level")
            body = CollectionInput.model_validate({**content, **args})
            result = library.save_collection(store, body, identity)
        elif operation == "artifact_collection_get":
            result = library.get_collection(store, identity, **args)
        elif operation == "artifact_collection_list":
            result = library.list_collections(store, **args)
        elif operation == "artifact_collection_export":
            args.setdefault("format", "json")
            args.setdefault("response_format", "compact" if args["format"] == "json" else "full")
            result = library.export_collection(store, identity, public_tools=public_tools, **args)
            # MCP adds store identity to its response. Keep the canonical,
            # independently bounded export inside an unchanged envelope.
            return {"export": result} if result is not None else {"found": False}
        else:
            raise ValueError("Unsupported collection action")
    except CoordinationError as exc:
        return {"error": str(exc), "error_code": exc.code}
    return result if result is not None else {"found": False}
