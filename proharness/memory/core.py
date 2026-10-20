"""Explicit, bounded core memory assembled from active stored facts.

Pinning is a user decision: importance and past recall frequency never promote
records implicitly. Facts are copied verbatim, with the same citation labels
as ordinary recall. Token budgets use the harness's character-based estimate;
they are not a promise about a particular model's tokenizer.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from proharness.context import estimate_tokens
from proharness.memory.evidence import is_sensitive_memory

if TYPE_CHECKING:
    from proharness.memory.schemas import Memory
    from proharness.memory.store import MemoryStore


def set_core_memory(store: MemoryStore, memory_id: str, pinned: bool = True) -> Memory:
    """Pin/unpin an active fact and return its updated record.

    A pin makes retention permanent. Unpinning only removes the core flag;
    changing retention is a separate decision. Metadata-only writes avoid
    treating a pin as a new observation or re-embedding unchanged fact text.
    """
    if not isinstance(pinned, bool):
        raise ValueError("pinned must be a boolean")
    with store._lock:
        memory = store.get(memory_id)
        if memory is None or not memory.is_retrievable():
            raise ValueError("Active memory not found")
        metadata = dict(memory.metadata)
        if pinned:
            metadata.update(core=True, retention="permanent")
        else:
            metadata.pop("core", None)
        if metadata != memory.metadata:
            store._sqlite.execute(
                "UPDATE memories SET metadata = ? WHERE id = ?",
                (json.dumps(metadata), memory.id),
            )
            store._invalidate_cached_memory(memory.id)
            store._sqlite.commit()
            memory.metadata = metadata
        return memory


def core_memory_block(
    store: MemoryStore, max_tokens: int = 512, include_sensitive: bool = False,
) -> dict:
    """Return complete visible pinned facts fitting the estimated token budget.

    ``memories`` contains JSON-friendly records for exactly the facts in
    ``text``. ``tokens`` includes citation/line overhead. Omission counts and
    token estimates describe budget omissions only: private, deleted and
    superseded records never appear in the report or its counts. Oversized
    facts are omitted whole, allowing smaller later facts to use the budget.
    Sorting is descending importance, then creation time and ID for stability.
    """
    if (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
            or not 0 <= max_tokens <= 4096):
        raise ValueError("max_tokens must be an integer between 0 and 4096")
    with store._lock:
        memories = [
            memory for memory in store.iter_all()
            if memory.metadata.get("core") is True and memory.is_retrievable()
            and (include_sensitive or not is_sensitive_memory(memory, store))
        ]
    memories.sort(key=lambda memory: (-memory.importance, memory.created_at, memory.id))

    lines: list[str] = []
    included: list[dict] = []
    omitted_count = omitted_tokens = 0
    for memory in memories:
        citation = f"[mem:{memory.id[:6]}]"
        line = f"- {citation} {memory.text}"
        candidate = "\n".join([*lines, line])
        if estimate_tokens(candidate) > max_tokens:
            omitted_count += 1
            omitted_tokens += estimate_tokens(line)
            continue
        lines.append(line)
        included.append({
            "id": memory.id,
            "text": memory.text,
            "citation": citation,
            "type": memory.type,
            "importance": memory.importance,
            "source": memory.source,
        })
    text = "\n".join(lines)
    return {
        "text": text,
        "memories": included,
        "tokens": estimate_tokens(text) if text else 0,
        "omitted_count": omitted_count,
        "omitted_tokens": omitted_tokens,
    }
