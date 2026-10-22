"""Account for complete MCP JSON output, including escaped Unicode and framing."""
from __future__ import annotations

import json
import math


def serialize_json(value):
    """Lossless MCP text JSON; preserve default escaping, order and str fallback."""
    return json.dumps(value, separators=(",", ":"), default=str)


def output_bytes(value):
    return len(serialize_json(value).encode("utf-8"))


def budget_report(value, max_tokens):
    """Fixed-point accounting includes the report itself; tokens are approximate."""
    value["budget"] = {"max_tokens": max_tokens, "estimated_tokens": 0,
                       "serialized_bytes": 0, "method": "serialized UTF-8 bytes / 4; not a tokenizer"}
    for _ in range(8):
        size = output_bytes(value)
        if value["budget"]["serialized_bytes"] == size:
            break
        value["budget"].update(serialized_bytes=size, estimated_tokens=math.ceil(size / 4))
    return value


def fits(value, max_tokens, reserve=128):
    return output_bytes(budget_report(value, max_tokens)) + reserve <= max_tokens * 4


def validate_budget(max_tokens):
    if type(max_tokens) is not int or not 512 <= max_tokens <= 16384:
        raise ValueError("max_tokens must be an integer from 512 to 16384")
