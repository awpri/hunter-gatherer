---
name: research
description: Deep multi-source research on a question. Spawns parallel sub-agents to gather information from the web, then synthesizes a cited answer. Use when the user asks to "research", "investigate", "find out about", or asks a factual question spanning multiple sources.
trigger:
  - research
  - investigate
  - look up
  - find sources on
  - what's the latest on
  - compare
---

# Research Skill

You are performing research. Follow this orchestrator-worker pattern:

## 1. Decompose
Break the question into 3–5 independent sub-questions that, together, cover
the whole topic. Prefer breadth over depth at this stage — each sub-question
should be answerable from a small number of sources.

## 2. Delegate (parallel)
For each sub-question, call `spawn_agent` with:
  - a clear, focused objective (one sentence),
  - the tools it may use (`web_fetch`, `read_file`),
  - an output format ("return 3–5 key findings with source URLs, ≤200 words").

Sub-agents run with isolated context windows, so give each enough to be
self-sufficient. Do NOT relay their raw output back through you verbatim —
ask for condensed findings.

## 3. Synthesize
Once all sub-agents return:
  - Identify points of agreement and disagreement across sources.
  - Resolve conflicts by weighting primary/recent sources higher.
  - Write a single coherent answer with inline citations.
  - Call `memory_store` for any durable facts worth keeping for next time.

## 4. Stop conditions
If a sub-agent fails or returns low-signal output, do one focused retry with a
re-worded query rather than spawning more agents. Never spawn more than 5
sub-agents for a single question — diminishing returns dominate past that.

## When NOT to use this skill
- A single fact lookup → use `web_fetch` directly, no sub-agents.
- An opinion or creative task → just answer.
- A code task → use the coding tools directly.
