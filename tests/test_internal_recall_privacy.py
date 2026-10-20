"""The internal recall tool follows the same explicit privacy boundary."""
import json

from proharness.memory.schemas import Memory
from proharness.tools.base import ToolContext
from proharness.tools.builtin import MemoryRecallTool


def test_internal_recall_sensitive_opt_in_is_explicit_boolean(store):
    memory = store.add(Memory(text="Private Zephyr funding note.", metadata={"sensitive": True}))
    tool = MemoryRecallTool()
    ctx = ToolContext(store=store, settings=store.settings)
    default = tool.safe_run(json.dumps({"query": "Zephyr funding"}), ctx)
    assert memory.text not in default.content
    opt_in = tool.safe_run(json.dumps({"query": "Zephyr funding", "include_sensitive": True}), ctx)
    assert memory.text in opt_in.content
    invalid = tool.safe_run(json.dumps({"query": "Zephyr funding", "include_sensitive": "false"}), ctx)
    assert invalid.error
    assert memory.text not in invalid.content


def test_internal_recall_rechecks_privacy_after_retrieval(store, monkeypatch):
    from proharness.memory.recall import recall

    memory = store.add(Memory(text="Zephyr material budget."))
    old_hits = recall(store, "Zephyr", touch=False)
    current = store.get(memory.id)
    current.metadata["sensitive"] = True
    store.update(current)
    monkeypatch.setattr("proharness.memory.recall.recall", lambda *a, **k: old_hits)
    result = MemoryRecallTool().run({"query": "Zephyr"}, ToolContext(store=store, settings=store.settings))
    assert memory.text not in result.content
