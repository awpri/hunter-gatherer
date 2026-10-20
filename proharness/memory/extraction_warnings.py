"""Whole extraction warnings for bounded navigation and attribution.

Warnings are untrusted data. Omission is explicit, never silently interpreted as
a clean extraction. Platform-wide cautions take priority over individual issues.
"""
import json

from proharness.memory.agent_extraction import PARTIAL_WARNING, WARNING

_PRIORITY = (WARNING, PARTIAL_WARNING)
_MAX_BYTES = 1200
_MAX_ITEMS = 10


def warning_summary(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            value = None
    if not isinstance(value, list) or any(not isinstance(w, str) for w in value):
        return {"warnings": [], "extraction_warnings_unavailable": True}
    ordered = [w for w in value if w in _PRIORITY] + [w for w in value if w not in _PRIORITY]
    selected, used = [], 0
    for warning in ordered:
        # Match escaped JSON budgeting; retain whole warnings, including Unicode.
        cost = len(json.dumps(warning).encode("utf-8")) + 1
        if len(selected) < _MAX_ITEMS and used + cost <= _MAX_BYTES:
            selected.append(warning)
            used += cost
    result = {"warnings": selected}
    if len(selected) != len(value):
        result["extraction_warnings_omitted"] = len(value) - len(selected)
    return result
