"""Add bounded recovery hints after collection source selection is complete.

This module never reads sources or grants access. Its caller must validate every
collection dependency in the same read snapshot before supplying occurrences.
"""
from __future__ import annotations

import json
from copy import deepcopy

from hunter_gatherer.memory.output_budget import budget_report, output_bytes


def add_collection_recovery(packet, omitted, *, identity, revision, max_tokens, public_tools=False, include_sensitive=False):
    """Use spare bytes only; never remove or shorten selected evidence or notes.

If even the minimal hint cannot fit, the existing collection ID/revision remain
the recovery address. The artifact tool description documents that fallback.
"""
    if not omitted:
        return packet
    original = packet["content"]

    def candidate(pointer):
        result = {**packet}
        if packet["format"] == "json":
            result["content"] = {**original, "recovery": pointer}
        else:
            result["content"] = original + "\n\n## Recover omitted sources\n\n" + json.dumps(
                pointer, ensure_ascii=False, separators=(",", ":"))
        budget_report(result, max_tokens)
        return result if output_bytes(result) <= max_tokens * 4 else None

    minimal = {"tool": "hg_artifact", "action": "collection_refs"}
    if public_tools:
        minimal = {"tool": "hg_collection", "arguments": {"action": "refs",
            "collection_id": identity, "revision": revision, "include_sensitive": include_sensitive}}
    pointer = {**minimal, "artifact_id": identity, "revision": revision,
               "omitted": [], "unlisted_omitted": len(omitted)}
    best = candidate(pointer)
    if best is None:
        # Markdown's minimal metadata-only packet may not contain the identity
        # in its body. Keep a self-contained address whenever a pointer is added.
        if packet["format"] == "markdown" and not public_tools:
            minimal.update(artifact_id=identity, revision=revision)
        return candidate(minimal) or packet

    # The list is ordered by collection occurrence, not deduplicated source.
    # Original-page occurrences include their exact identity and image followup;
    # retain those fields together, just as text occurrences retain citations.
    # Binary search avoids repeatedly serializing all growing body prefixes.
    low, high = 0, len(omitted)
    while low < high:
        count = (low + high + 1) // 2
        attempt = candidate({**pointer, "omitted": omitted[:count],
                             "unlisted_omitted": len(omitted) - count})
        if attempt is None:
            high = count - 1
        else:
            low, best = count, attempt
    return deepcopy(best)
