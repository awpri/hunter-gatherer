"""Plain MCP coordination tools for trusted local, self-labelled clients.

No external messages, execution, tenant identity, or automatic truth assertions.
"""

from __future__ import annotations

from proharness.coordination import CoordinationStore

STRING = {"type": "string"}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean", "default": False}
REFS = {
    "type": "array",
    "maxItems": 100,
    "items": {"type": "object"},
    "description": "Existing memory_id, chunk_id, artifact_id+revision, exact original-file object {document_id, version, original_content_hash}, or original-page object {document_id, version, original_content_hash, page} from document_page_image. Original references contain no quote; new dependencies verify original bytes and any selected page range before publication. uri/path are descriptors only.",
}
BASE = {
    "workspace_id": STRING,
    "actor": {
        "type": "string",
        "maxLength": 128,
        "description": "Self-reported local client label; not authenticated identity.",
    },
    "idempotency_key": {"type": "string", "maxLength": 256},
    "include_sensitive": BOOLEAN,
}
REV = {"expected_revision": INTEGER}
CLAIM = {"claim_token": STRING, "fence": INTEGER}
READ = {"workspace_id": STRING, "include_sensitive": BOOLEAN}
PAGE = {
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    "after_id": STRING,
}
CONTRACT = {
    "objective": STRING,
    "constraints": {"type": "array", "maxItems": 50, "items": STRING},
    "acceptance_criteria": {
        "type": "array",
        "minItems": 1,
        "maxItems": 50,
        "items": {
            "type": "object",
            "properties": {"id": STRING, "description": STRING},
            "required": ["id", "description"],
            "additionalProperties": False,
        },
    },
    "dependencies": {"type": "array", "maxItems": 50, "items": STRING},
}

# action -> (properties, required operation-specific fields, description, mutation)
SPECS = {
    "workspace_create": (
        {"name": STRING, "description": STRING, "sensitive": BOOLEAN},
        ["name"],
        "Create a durable organizational workspace separate from personal memories.",
        True,
    ),
    "workspace_update": (
        {**REV, "name": STRING, "description": STRING, "sensitive": BOOLEAN},
        ["expected_revision"],
        "Revise workspace labels/privacy with an expected revision.",
        True,
    ),
    "workspace_get": (
        READ,
        ["workspace_id"],
        "Read an explicitly scoped workspace; private content requires opt-in.",
        False,
    ),
    "workspace_list": (
        {"include_sensitive": BOOLEAN, **PAGE},
        [],
        "List bounded workspace descriptors; workspace labels do not authenticate tenants.",
        False,
    ),
    "task_create": (
        {**CONTRACT, "references": REFS, "sensitive": BOOLEAN},
        ["objective", "acceptance_criteria"],
        "Persist objective, constraints, criteria and validated task dependencies.",
        True,
    ),
    "task_get": (
        {**READ, "task_id": STRING},
        ["workspace_id", "task_id"],
        "Recover current task and last checkpoint; never returns claim tokens.",
        False,
    ),
    "task_list": (
        {**READ, **PAGE},
        ["workspace_id"],
        "List bounded current work in one explicit workspace.",
        False,
    ),
    "task_history": (
        {**READ, "task_id": STRING, "limit": PAGE["limit"], "before_revision": INTEGER},
        ["workspace_id", "task_id"],
        "Read bounded immutable task/checkpoint history with current source visibility checks.",
        False,
    ),
    "task_update": (
        {**REV, **CLAIM, "task_id": STRING, **CONTRACT, "sensitive": BOOLEAN},
        ["task_id", "expected_revision", "claim_token", "fence"],
        "Revise a nonterminal task contract under its current unexpired claim.",
        True,
    ),
    "task_claim": (
        {
            **REV,
            "task_id": STRING,
            "lease_seconds": {"type": "number", "minimum": 1, "maximum": 3600},
        },
        ["task_id", "expected_revision"],
        "Atomically claim ready work; returns a secret token and increasing fence protecting server writes only.",
        True,
    ),
    "task_renew": (
        {
            **REV,
            **CLAIM,
            "task_id": STRING,
            "lease_seconds": {"type": "number", "minimum": 1, "maximum": 3600},
        },
        ["task_id", "expected_revision", "claim_token", "fence"],
        "Renew a live task claim; stale or expired claims cannot renew.",
        True,
    ),
    "task_release": (
        {**REV, **CLAIM, "task_id": STRING},
        ["task_id", "expected_revision", "claim_token", "fence"],
        "Release a current claim and leave durable pending work for adoption.",
        True,
    ),
    "task_checkpoint": (
        {
            **REV,
            **CLAIM,
            "task_id": STRING,
            "checkpoint": {
                "type": "object",
                "description": "Completed steps, findings, references, blockers, next_action and attempted_side_effects. Stored prose grants no execution authority.",
            },
        },
        ["task_id", "expected_revision", "claim_token", "fence", "checkpoint"],
        "Save resumable progress and immutable history under a fenced claim.",
        True,
    ),
    "task_complete": (
        {
            **REV,
            **CLAIM,
            "task_id": STRING,
            "criterion_results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "criterion_id plus existing references or an explicit waiver; verification_notes are actor claims.",
                },
            },
        },
        ["task_id", "expected_revision", "claim_token", "fence", "criterion_results"],
        "Record completion only when every criterion has stored evidence or explicit waiver; integrity is not truth verification.",
        True,
    ),
    "handoff_offer": (
        {
            **REV,
            **CLAIM,
            "task_id": STRING,
            "recipient": STRING,
            "packet": {"type": "object"},
            "expires_seconds": {"type": "number", "minimum": 1, "maximum": 86400},
        },
        ["task_id", "expected_revision", "claim_token", "fence", "recipient"],
        "Offer a persisted packet to another client label; responsibility transfers only on explicit acceptance.",
        True,
    ),
    "handoff_get": (
        {**READ, "handoff_id": STRING},
        ["workspace_id", "handoff_id"],
        "Read a scoped persistent handoff offer with current privacy checks.",
        False,
    ),
    "handoff_accept": (
        {
            **REV,
            "expected_task_revision": INTEGER,
            "handoff_id": STRING,
            "lease_seconds": {"type": "number", "minimum": 1, "maximum": 3600},
        },
        ["handoff_id", "expected_revision", "expected_task_revision"],
        "Addressed recipient atomically accepts offer, advances fence and receives its new claim token.",
        True,
    ),
    "handoff_reject": (
        {**REV, "handoff_id": STRING, "reason": STRING},
        ["handoff_id", "expected_revision", "reason"],
        "Addressed recipient rejects an offer; source task remains recoverable.",
        True,
    ),
    "artifact_publish": (
        {
            **REV,
            "artifact_id": STRING,
            "task_id": STRING,
            "name": STRING,
            "content": {
                "description": "Bounded finite JSON/text; paths/URLs are never fetched or executed."
            },
            "references": REFS,
            "verification_notes": STRING,
            "sensitive": BOOLEAN,
        },
        ["name", "content"],
        "Publish immutable hashed artifact revision; existing artifact_id requires expected_revision.",
        True,
    ),
    "artifact_get": (
        {**READ, "artifact_id": STRING, "revision": INTEGER},
        ["workspace_id", "artifact_id"],
        "Read current or specified immutable artifact revision; recheck live source availability/privacy.",
        False,
    ),
    "artifact_collection_refs": (
        {"artifact_id": STRING, "revision": {"type": "integer", "minimum": 1},
         "offset": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 70},
         "max_tokens": {"type": "integer", "minimum": 512, "maximum": 16384},
         "include_sensitive": BOOLEAN},
        ["artifact_id", "revision"],
        "Recover personal-library collection references, without prose or bodies. Use export collection_id "
        "as artifact_id and its exact revision, including when recovery hints did not fit. "
        "No workspace_id needed. Pages preserve evidence/voice occurrences with section-local indexes; "
        "continue next_offset with the same revision. limit 1-70 (default 32); max_tokens 512-16384 "
        "(default 2048; serialized bytes/4, not model tokens). All dependencies are rechecked. "
        "Fetch exact citations before quoting; mutable memories may have changed, and voice is not fact.",
        False,
    ),
    "artifact_list": (
        {**READ, **PAGE},
        ["workspace_id"],
        "List bounded current artifact revisions within explicit workspace.",
        False,
    ),
    "message_send": (
        {
            "recipient": STRING,
            "body": STRING,
            "references": REFS,
            "sensitive": BOOLEAN,
            "expires_seconds": {"type": "number", "minimum": 1, "maximum": 604800},
        },
        ["recipient", "body"],
        "Store an addressed local message; does not message external apps, wake clients or authorize execution.",
        True,
    ),
    "message_inbox": (
        {**READ, **PAGE, "recipient": STRING},
        ["workspace_id", "recipient"],
        "Read bounded unacknowledged, unexpired messages for explicit recipient label.",
        False,
    ),
    "message_ack": (
        {**REV, "message_id": STRING},
        ["message_id", "expected_revision"],
        "Recipient acknowledges a current unexpired message; retry is durable and idempotent.",
        True,
    ),
    "coordination_changes": (
        {
            "workspace_id": STRING,
            "after_cursor": {"type": "integer", "minimum": 0, "default": 0},
            "limit": PAGE["limit"],
        },
        ["workspace_id"],
        "Replay durable metadata-only events; invalid cursor requests a snapshot, never skips silently.",
        False,
    ),
    "coordination_snapshot": (
        {**READ, "limit": PAGE["limit"]},
        ["workspace_id"],
        "Get bounded task/artifact recovery snapshot and replay cursor; inbox requires separate recipient.",
        False,
    ),
}

HANDLERS = {}
SCHEMAS = {}
DESCRIPTIONS = {}

FAMILIES = {
    "workspace": ["workspace_create", "workspace_update", "workspace_get", "workspace_list"],
    "task": [
        "task_create",
        "task_get",
        "task_list",
        "task_history",
        "task_update",
        "task_claim",
        "task_renew",
        "task_release",
        "task_checkpoint",
        "task_complete",
    ],
    "handoff": ["handoff_offer", "handoff_get", "handoff_accept", "handoff_reject"],
    "artifact": ["artifact_publish", "artifact_get", "artifact_collection_refs", "artifact_list"],
    "message": ["message_send", "message_inbox", "message_ack"],
    "coordination": ["coordination_changes", "coordination_snapshot"],
}

OPERATIONS = {}
for _operation, (_properties, _required, _description, _mutation) in SPECS.items():
    if _mutation:
        _properties = {**BASE, **_properties}
        if _operation == "workspace_create":
            _properties.pop("workspace_id", None)
        _required = [
            "actor",
            "idempotency_key",
            *(_required if _operation == "workspace_create" else ["workspace_id", *_required]),
        ]
    OPERATIONS[_operation] = (_properties, _required, _description, _mutation)

from proharness.mcp.collection_tools import OPERATIONS as COLLECTION_OPERATIONS  # noqa: E402

OPERATIONS.update(COLLECTION_OPERATIONS)
FAMILIES["artifact"].extend(COLLECTION_OPERATIONS)


def _handler(family, operations):
    def handler(harness, action, **kwargs):
        operation = family + "_" + action
        if operation not in operations:
            raise ValueError("Unsupported coordination action")
        properties, required, _, mutation = OPERATIONS[operation]
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        unknown = set(kwargs) - set(properties)
        missing = set(required) - set(kwargs)
        if unknown or missing:
            raise ValueError(
                f"invalid coordination arguments; missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        for key, value in kwargs.items():
            kind = properties[key].get("type")
            invalid = (
                (kind == "boolean" and not isinstance(value, bool))
                or (kind == "string" and not isinstance(value, str))
                or (kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)))
                or (
                    kind == "number"
                    and (isinstance(value, bool) or not isinstance(value, (int, float)))
                )
                or (kind == "array" and not isinstance(value, list))
                or (kind == "object" and not isinstance(value, dict))
            )
            if invalid:
                raise ValueError(f"{key} must have type {kind}")
        if operation in COLLECTION_OPERATIONS:
            from proharness.mcp.collection_tools import dispatch

            return dispatch(harness.store, operation, kwargs)
        if operation == "artifact_collection_refs":
            from proharness.mcp.tools import _store_identity
            from proharness.memory.collection_references import get_collection_references

            result = get_collection_references(harness.store, kwargs.pop("artifact_id"),
                                               store_identity=_store_identity(harness), **kwargs)
            return result if result is not None else {"found": False}
        coordination = CoordinationStore(harness.store)
        method = {"coordination_changes": "changes", "coordination_snapshot": "snapshot"}.get(
            operation, operation
        )
        return (
            coordination.execute(operation, **kwargs)
            if mutation
            else getattr(coordination, method)(**kwargs)
        )

    handler.__name__ = family
    return handler


for _family, _operations in FAMILIES.items():
    _properties = {
        "action": {
            "type": "string",
            "enum": [name.removeprefix(_family + "_") for name in _operations],
        }
    }
    for _operation in _operations:
        _properties.update(
            {
                key: {k: v for k, v in value.items() if k != "default"}
                for key, value in OPERATIONS[_operation][0].items()
            }
        )
    _name = "proharness_" + _family
    SCHEMAS[_name] = {
        "type": "object",
        "properties": _properties,
        "required": ["action"],
        "additionalProperties": False,
    }
    DESCRIPTIONS[_name] = " ".join(
        name.removeprefix(_family + "_") + ": " + OPERATIONS[name][2] for name in _operations
    )
    DESCRIPTIONS[_name] += (
        " Generic coordination mutations require actor and idempotency_key; updates require expected_revision. "
        "Library collection actions resolve their own workspace/actor; collection_save requires idempotency_key. "
        "Workspace/client labels organize trusted local work, not authenticated tenants. include_sensitive defaults false."
        if _family == "artifact" else
        " Mutations require actor and idempotency_key; updates require expected_revision. Workspace/client labels organize trusted local work, not authenticated tenants. include_sensitive defaults false."
    )
    HANDLERS[_name] = _handler(_family, _operations)
