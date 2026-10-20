"""Small temporal-memory MCP families, with explicit scope and evidence."""
from proharness.memory.temporal import TemporalMemory


def claim(harness, action, scope, **kwargs):
    # The SDK may supply absent optional properties as None. Only pass fields
    # belonging to the selected action; meaningful values for others are errors.
    allowed = {
        "record": {"subject", "predicate", "idempotency_key", "expected_revision", "value",
                   "evidence_ids", "valid_from", "valid_to", "kind", "replaces", "include_sensitive"},
        "resolve": {"subject", "predicate", "idempotency_key", "expected_revision",
                    "retained_ids", "reason", "include_sensitive"},
        "query": {"subject", "predicate", "as_of", "known_at", "include_sensitive", "limit"},
        "history": {"subject", "predicate", "as_of", "known_at", "include_sensitive", "limit"},
    }.get(action)
    if allowed is None:
        raise ValueError("action must be record, query, resolve or history")
    if any(v is not None and k not in allowed for k, v in kwargs.items()):
        raise ValueError("Argument does not apply to selected action")
    kwargs = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    service = TemporalMemory(harness.store)
    if action in {"record", "resolve"}:
        return service.mutate(action, scope=scope, **kwargs)
    if action in {"query", "history"}:
        return service.query(scope=scope, history=action == "history", **kwargs)
    raise ValueError("action must be record, query, resolve or history")


def memory_history(harness, memory_id, include_sensitive=False, limit=30):
    return TemporalMemory(harness.store).history(memory_id, include_sensitive=include_sensitive, limit=limit)


HANDLERS = {"proharness_claim": claim, "proharness_memory_history": memory_history}
_TIMESTAMP = {"type": ["number", "string", "null"], "description": "Finite epoch seconds or timezone-aware ISO timestamp; null means unknown."}
SCHEMAS = {
    "proharness_claim": {"type": "object", "required": ["action", "scope"], "additionalProperties": False,
                        "properties": {
                            "action": {"type": "string", "enum": ["record", "query", "resolve", "history"]},
                            "scope": {"type": "string", "description": "Explicit organizational scope; not authentication."},
                            "subject": {"type": "string"}, "predicate": {"type": "string"},
                            "value": {}, "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50},
                            "kind": {"type": "string", "enum": ["assertion", "change", "correction"]},
                            "replaces": {"type": "string"}, "valid_from": _TIMESTAMP, "valid_to": _TIMESTAMP,
                            "as_of": _TIMESTAMP, "known_at": _TIMESTAMP,
                            "idempotency_key": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 0},
                            "retained_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50},
                            "reason": {"type": "string"}, "include_sensitive": {"type": "boolean", "default": False},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 200}}},
    "proharness_memory_history": {"type": "object", "required": ["memory_id"], "additionalProperties": False,
                                  "properties": {"memory_id": {"type": "string"},
                                                 "include_sensitive": {"type": "boolean", "default": False},
                                                 "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30}}},
}
DESCRIPTIONS = {
    "proharness_claim": "Record/query/resolve/audit scoped structured claims linked to existing memories. Mutations require subject, predicate, idempotency_key and expected_revision (initial 0). Record requires value and evidence_ids. Change closes predecessor at valid_from; correction preserves its interval and changes what is known after recording. Unknown dates remain unknown. Resolve requires retained_ids and reason. Query supports as_of and known_at; available means visible evidence, not verified truth. Sensitivity and deleted sources are rechecked on every delivery and retry.",
    "proharness_memory_history": "Inspect a bounded explicit memory replacement chain and source provenance. Historical access does not bypass source deletion or sensitivity. Hidden/deleted links stop traversal. No execution or authorization is implied.",
}
