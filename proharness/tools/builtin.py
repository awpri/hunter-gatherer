"""Built-in tools: shell, file I/O, web fetch, and first-class memory + agent
delegation tools.

memory_recall / memory_store / spawn_agent are first-class so the model can
page memories in/out of context (MemGPT/Letta pattern) and delegate subtasks
(Anthropic orchestrator-worker pattern) by calling tools — not by the loop
hard-coding those behaviors.
"""
from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import Any

import httpx

from proharness.tools.base import Registry, Tool, ToolContext, ToolResult


class ShellTool(Tool):
    name = "shell"
    description = (
        "Run a shell command in the working directory and return stdout+stderr. "
        "Use for filesystem operations, git, build/test runs, and any CLI task. "
        "Prefer read_file/write_file for simple file edits."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {"type": "number", "description": "Max seconds (default 60).", "default": 60},
        },
        "required": ["command"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        cmd = args["command"]
        timeout = float(args.get("timeout", 60))
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=ctx.workdir, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return ToolResult.err(f"command timed out after {timeout}s")
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        content = out.strip() or "(no output)"
        failed = proc.returncode != 0
        if failed:
            # The agent only carries content/error forward to the model, not
            # metadata. Keep the actual exit status in its external evidence.
            content = f"[exit code: {proc.returncode}]\n{content}"
        return ToolResult(content=content, error=failed, metadata={"exit_code": proc.returncode})


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a text file from the working directory and return its contents."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative or absolute path to the file."},
            "max_lines": {"type": "integer", "description": "Optional cap on lines returned.", "default": 2000},
        },
        "required": ["path"],
    }

    def run(self, args, ctx) -> ToolResult:
        p = Path(ctx.workdir) / args["path"] if not Path(args["path"]).is_absolute() else Path(args["path"])
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ToolResult.err(f"file not found: {p}")
        lines = text.splitlines()
        max_lines = int(args.get("max_lines", 2000))
        if len(lines) > max_lines:
            text = "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines truncated)"
        return ToolResult.ok(text)


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write text to a file (overwriting). Use for creating or fully replacing files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    def run(self, args, ctx) -> ToolResult:
        p = Path(ctx.workdir) / args["path"] if not Path(args["path"]).is_absolute() else Path(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
        return ToolResult.ok(f"wrote {len(args['content'])} chars to {p}")


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "Fetch a URL and return its text content (HTML stripped to text). "
        "Use for retrieving documentation, articles, or API responses."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "default": 8000},
        },
        "required": ["url"],
    }

    def run(self, args, ctx) -> ToolResult:
        url = args["url"]
        max_chars = int(args.get("max_chars", 8000))
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=30.0, headers={"User-Agent": "proharness/0.1"})
            resp.raise_for_status()
        except Exception as e:
            return ToolResult.err(f"fetch failed: {e}")
        text = _html_to_text(resp.text)
        return ToolResult.ok(text[:max_chars])


class MemoryRecallTool(Tool):
    """Pages relevant memories into context. MemGPT/Letta pattern: the agent
    itself decides when to retrieve, rather than the loop hard-injecting."""

    name = "memory_recall"
    description = (
        "Retrieve relevant memories (facts, past events, preferences) from the "
        "agent's long-term store. Call this when you need context about the user "
        "or prior interactions. Returns Park-scored memories (recency + "
        "importance + relevance). Sensitive records require include_sensitive=true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What you want to remember."},
            "top_k": {"type": "integer", "default": 5},
            "include_sensitive": {"type": "boolean", "default": False},
        },
        "required": ["query"],
    }

    def run(self, args, ctx) -> ToolResult:
        if ctx.store is None:
            return ToolResult.err("memory store not configured")
        from dataclasses import replace

        from proharness.memory.evidence import is_sensitive_memory
        from proharness.memory.recall import format_recall, recall

        include_sensitive = args.get("include_sensitive", False)
        if not isinstance(include_sensitive, bool):
            return ToolResult.err("include_sensitive must be a boolean")
        # In "downrank" mode superseded memories can still surface on explicit
        # asks; log them as stale_requested (2026-08-20 #1) so a wrongly-
        # superseded fact that keeps being asked for is measurable, not silent.
        mode = getattr(ctx.settings, "recall_superseded_mode", "filter")
        hits = recall(ctx.store, args["query"], top_k=int(args.get("top_k", 5)), settings=ctx.settings)
        visible = []
        for hit in hits:
            current = ctx.store.get(hit.memory.id)
            if (current is not None and current.is_retrievable(include_superseded=mode == "downrank")
                    and (include_sensitive or not is_sensitive_memory(current, ctx.store))):
                visible.append(replace(hit, memory=current))
        hits = visible
        if not hits:
            return ToolResult.ok("(no relevant memories)")
        stale = [h for h in hits if h.memory.is_superseded]
        if mode == "downrank" and stale:
            with contextlib.suppress(Exception):
                ctx.store.log_recall_usage([
                    (args["query"][:200], h.memory.id, 0, h.recency, h.importance, h.relevance, 0, 1)
                    for h in stale
                ])
        return ToolResult.ok(format_recall(hits))


class MemoryStoreTool(Tool):
    name = "memory_store"
    description = (
        "Store a durable fact, preference, or event in long-term memory for "
        "future recall. Use for things worth remembering across sessions (user "
        "preferences, decisions, project context)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The fact/event to remember."},
            "type": {
                "type": "string",
                "enum": ["semantic", "episodic", "procedural"],
                "default": "semantic",
            },
            "importance": {"type": "number", "description": "1..10 salience", "default": 5},
            "tags": {"type": "array", "items": {"type": "string"}, "default": []},
        },
        "required": ["text"],
    }

    def run(self, args, ctx) -> ToolResult:
        if ctx.store is None:
            return ToolResult.err("memory store not configured")
        from proharness.memory.consolidate import Consolidator
        from proharness.memory.schemas import Memory

        mem = Memory(
            text=args["text"],
            type=args.get("type", "semantic"),
            importance=float(args.get("importance", 5.0)),
            tags=args.get("tags", []),
            source="agent",
        )
        cons = Consolidator(store=ctx.store, settings=ctx.settings)
        stored = cons.store_with_consolidation(mem)
        if stored is None:
            return ToolResult.ok("(redundant — already in memory)")
        return ToolResult.ok(f"stored memory {mem.id}: {mem.text}")


class SpawnAgentTool(Tool):
    """Orchestrator-worker delegation. Anthropic finding: spawn sub-agents for
    breadth-first independent subtasks or compression; the worker returns a
    condensed result, not the full raw dump."""

    name = "spawn_agent"
    description = (
        "Delegate a self-contained subtask to a sub-agent with its own context "
        "window. Use for independent sub-parts of a larger task that can run in "
        "parallel, or when you need to compress a large amount of research into "
        "a focused answer. Returns the sub-agent's final answer."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Clear objective for the sub-agent."},
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tool names the sub-agent may use.",
            },
        },
        "required": ["task"],
    }

    def run(self, args, ctx) -> ToolResult:
        if ctx.spawn_agent is None:
            return ToolResult.err("sub-agent spawning not configured")
        result = ctx.spawn_agent(task=args["task"], allowed_tools=args.get("tools"))
        return ToolResult.ok(result)


class ToolResultFetchTool(Tool):
    """Retrieve a parked raw tool payload (2026-08-23 #1).

    The main loop condenses over-budget tool results to a digest and parks the
    raw text in the sidecar; this always-on tool fetches the full payload on
    demand — the agent escalates to the raw dump only when the digest provably
    wasn't enough."""

    name = "tool_result_fetch"
    description = (
        "Retrieve the full raw payload of a previously condensed tool result. "
        "Use when a digest in the conversation is insufficient and you need the "
        "complete output (full file contents, complete command output)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "call_id": {"type": "string", "description": "The call_id from the digest's pointer line."},
            "max_chars": {"type": "integer", "default": 20000},
        },
        "required": ["call_id"],
    }

    def run(self, args, ctx) -> ToolResult:
        if ctx.store is None:
            return ToolResult.err("sidecar store not configured")
        from proharness.tools.results import ResultSidecar

        payload = ResultSidecar(ctx.store).fetch(
            args["call_id"], max_chars=int(args.get("max_chars", 20_000))
        )
        if payload is None:
            return ToolResult.err(f"no parked result for call_id={args['call_id']} "
                                  "(it may have been pruned or the result was never condensed)")
        return ToolResult.ok(payload)


def default_registry() -> Registry:
    from proharness.tools.archive import archive_tools

    """The standard tool set. Caller adds the spawn_agent hook separately if
    sub-agent delegation is enabled."""
    r = Registry()
    for t in (
        ShellTool(),
        ReadFileTool(),
        WriteFileTool(),
        WebFetchTool(),
        MemoryRecallTool(),
        MemoryStoreTool(),
        SpawnAgentTool(),
        ToolResultFetchTool(),
        *archive_tools(),
    ):
        r.register(t)
    return r


def _html_to_text(html: str) -> str:
    """Cheap HTML → text. Good enough for tool output; for serious extraction
    swap in a proper parser (beautifulsoup4)."""
    import re

    # Drop scripts/styles.
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    # Tags → spaces.
    text = re.sub(r"<[^>]+>", " ", html)
    # Entities (common ones).
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text
