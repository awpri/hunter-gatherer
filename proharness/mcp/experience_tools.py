"""MCP resource families for scoped procedure advice and outcome reports."""

from proharness.memory.experience import procedure_card, procedure_outcome


def procedure(harness, action: str, workspace_id: str, **kwargs):
    return procedure_card(harness.store, action, workspace_id, **kwargs)


def outcome(harness, action: str, workspace_id: str, procedure_id: str, **kwargs):
    return procedure_outcome(harness.store, action, workspace_id, procedure_id, **kwargs)


_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_REFS = {
    "type": "array",
    "maxItems": 20,
    "items": {
        "type": "object",
        "properties": {"memory_id": _ID, "chunk_id": _ID},
        "minProperties": 1,
        "maxProperties": 1,
        "additionalProperties": False,
    },
}
_SCALARS = {
    "type": "object",
    "maxProperties": 64,
    "additionalProperties": {"type": ["string", "number", "boolean"]},
}
_OBSERVATIONS = {
    "type": "object",
    "maxProperties": 64,
    "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
}
_CARD = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "trigger": {"type": "string", "minLength": 1, "maxLength": 2000},
        "preconditions": _SCALARS,
        "environment": _SCALARS,
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        "verification": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        "known_failures": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        "evidence_refs": _REFS,
    },
    "required": [
        "title",
        "trigger",
        "preconditions",
        "environment",
        "steps",
        "verification",
        "known_failures",
    ],
    "additionalProperties": False,
}
_COMMON = {
    "workspace_id": {
        **_ID,
        "description": "Explicit trusted-local scope; unregistered labels/global are supported, and known registered workspace privacy is inherited. Labels are not authentication.",
    },
    "version": {
        "type": "integer",
        "minimum": 1,
        "description": "Specific immutable card content version. Required for outcome recording.",
    },
    "actor_label": {
        **_ID,
        "description": "Required for mutations; a self-reported client label, not authenticated identity.",
    },
    "idempotency_key": {
        **_ID,
        "description": "Required for mutations. Stable retry key per workspace/actor; changed payload is refused.",
    },
    "sensitive": {"type": "boolean", "default": False},
    "include_sensitive": {
        "type": "boolean",
        "default": False,
        "description": "Explicit private-source opt-in; removed source content is never restored by this flag.",
    },
    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
    "offset": {"type": "integer", "minimum": 0, "default": 0},
}
SCHEMAS = {
    "proharness_procedure": {
        "type": "object",
        "properties": {
            **_COMMON,
            "action": {
                "type": "string",
                "enum": ["create", "revise", "quarantine", "deprecate", "get", "list", "recommend"],
            },
            "procedure_id": _ID,
            "card": _CARD,
            "expected_revision": {
                "type": "integer",
                "minimum": 1,
                "description": "Required for revise/quarantine/deprecate; prevents concurrent stale updates.",
            },
            "reason": {
                "type": "string",
                "maxLength": 2000,
                "description": "Required explicit reason for quarantine/deprecate.",
            },
            "query": {"type": "string", "maxLength": 2000, "default": ""},
            "environment": {
                **_OBSERVATIONS,
                "description": "Caller-observed environment values, compared explicitly with each card signature.",
            },
            "observations": {
                **_OBSERVATIONS,
                "description": "Caller-observed precondition values; missing/null observations remain unverified.",
            },
            "status": {
                "type": "string",
                "enum": ["", "active", "quarantined", "deprecated"],
                "default": "",
                "description": "Optional list filter; recommend always omits quarantined/deprecated versions.",
            },
        },
        "required": ["action", "workspace_id"],
        "additionalProperties": False,
    },
    "proharness_procedure_outcome": {
        "type": "object",
        "properties": {
            **_COMMON,
            "action": {"type": "string", "enum": ["record", "list"]},
            "procedure_id": _ID,
            "outcome": {
                "type": "string",
                "enum": ["success", "failure", "unknown"],
                "default": "unknown",
            },
            "evidence_refs": _REFS,
            "verification_receipt": {
                "type": "string",
                "maxLength": 4000,
                "default": "",
                "description": "Self-reported external verification notes. Not fetched, executed or treated as independent proof.",
            },
            "task_id": {
                "type": "string",
                "maxLength": 128,
                "default": "",
                "description": "Work-record descriptor; not presently a validated task reference.",
            },
        },
        "required": ["action", "workspace_id", "procedure_id"],
        "additionalProperties": False,
    },
}
DESCRIPTIONS = {
    "proharness_procedure": "Create, revise, quarantine, deprecate or retrieve scoped procedure cards. Cards require trigger, explicit preconditions/environment signature, steps, verification and known failures. Mutations require actor_label and idempotency_key; updates require expected_revision. Recommend checks caller observations and returns compatible advice separately from unverified/incompatible candidates. No automatic execution, authorization or promotion. Evidence and privacy are rechecked on all reads and retries.",
    "proharness_procedure_outcome": "Record/list self-reported success, failure or unknown for a specific procedure version. Record requires actor_label, idempotency_key and version. Existing visible memory/chunk references can establish current evidence availability, never task truth. External verification_receipt/task_id are descriptors. Repeated failures cannot promote procedures; removed/private sources are redacted on reads and retries.",
}
HANDLERS = {"proharness_procedure": procedure, "proharness_procedure_outcome": outcome}
