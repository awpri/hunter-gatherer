"""Write-time fact extraction (2026-08-28 #1) — Mem0's missing first half.

The main write path into long-term memory was a keyword heuristic: whole raw
messages behind a first-person-cue gate at a pinned importance=4.0 — so the
Park β term was a constant on the main path, the importance-threshold
reflection trigger accumulated exactly 4×count, and Mem0 reconciliation
judged whole multi-fact utterances instead of atomic facts.

This module implements the documented two-stage pipeline's first half: one
local-tier structured call per non-trivial turn extracts ≤ N atomic facts,
each LLM-scored 1-10 with Park et al. 2023's rubric and fed through the
existing ``Consolidator.store_with_consolidation`` (the second half). Guards
per house pattern: local tier only, trivial-route skip, fact cap, off-switch
— with the original cue heuristic kept as the no-LLM fallback.
"""
from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

_TYPES = ("semantic", "episodic", "procedural")


@dataclass
class ExtractedFact:
    text: str
    type: str = "semantic"
    importance: float = 5.0


def _parse_facts(raw: str, cap: int) -> list[ExtractedFact]:
    """Robustly pull the JSON array out of the model reply (models wrap it in
    prose/fences often enough to matter). Returns [] on any failure."""
    raw = (raw or "").strip()
    with contextlib.suppress(Exception):
        start, end = raw.index("["), raw.rindex("]") + 1
        items = json.loads(raw[start:end])
        out: list[ExtractedFact] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = str(item.get("fact", "") or item.get("text", "") or "").strip()
            if not text:
                continue
            ftype = str(item.get("type", "semantic")).strip().lower()
            if ftype not in _TYPES:
                ftype = "semantic"
            try:
                importance = max(1.0, min(10.0, float(item.get("importance", 5.0))))
            except (TypeError, ValueError):
                importance = 5.0
            out.append(ExtractedFact(text=text, type=ftype, importance=importance))
            if len(out) >= cap:
                break
        return out
    return []


def make_extractor(llm, *, max_facts: int = 4):
    """Build a local-tier fact extractor: ``extract(text) -> [ExtractedFact]``.
    Failure-tolerant — an unusable model returns [], and the caller falls back
    to the legacy cue heuristic."""

    def extract(text: str) -> list[ExtractedFact]:
        from proharness.agents.prompts import EXTRACTION_PROMPT
        from proharness.models.base import Message

        try:
            result = llm.chat(
                [Message(role="user", content=EXTRACTION_PROMPT.format(text=text[:4000]))],
                temperature=0.0, max_tokens=500,
            )
            return _parse_facts(result.message.content, max_facts)
        except Exception as e:
            log.debug("fact extraction failed: %s", e)
            return []

    return extract
