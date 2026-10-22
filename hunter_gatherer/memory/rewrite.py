"""Conversational query rewriting (2026-09-17 #2) — contextualize follow-ups
before recall.

Session-aware routing gave the *router* the conversation dimension; recall
still embeds the raw message, so "and the second one?" retrieves on
context-free noise — the memory block on follow-up turns is distractor-laden
or (since the relevance floor) correctly empty, leaving the agent ungrounded
mid-task even when the store holds exactly the right fact.

The rewrite is one short local-tier call folding the last few turns into a
self-contained query. Generated-not-trusted, HyDE-style conservatism: it
feeds ``recall(extra_queries=[...])`` as an additional variant (fused by id,
relevance = max over variants — widening only, never hiding a hit).

Research link: Ma et al., "Query Rewriting for Retrieval-Augmented Large
Language Models" (EMNLP 2024, arXiv:2305.14283) + TREC CAsT conversational
query reformulation, composed with Park 2023.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Cheap follow-up detector: short queries, or pronominal/ellipsis cues that
# only resolve against history. Mirrors the repeat-intent detector's shape.
_PRONOMINAL = re.compile(
    r"\b(why|how about|what about|and|also|it|that|this|those|these|the second|"
    r"the first|the other|the last|the previous|more|again|go deeper|"
    r"explain (that|it|this)|tell me more)\b",
    re.I,
)
_MAX_QUERY_CHARS = 120  # longer queries are self-contained


def needs_rewrite(query: str, history_texts: list[str]) -> bool:
    """True when the query is short or pronominal AND there is history to
    resolve it against (≥ 2 prior turns — the same gate the idea specifies)."""
    if len(history_texts) < 2:
        return False
    q = (query or "").strip()
    if not q or len(q) > _MAX_QUERY_CHARS:
        return False
    return bool(_PRONOMINAL.search(q)) or len(q) < 40


_REWRITE_PROMPT = (
    "Rewrite this follow-up question as a self-contained question that makes "
    "sense with no other context, using the recent conversation to resolve "
    "pronouns and ellipsis. Keep the user's intent exactly; reply with the "
    "rewritten question only.\n\nRecent conversation:\n{history}\n\n"
    "Follow-up: {query}\n\nSelf-contained rewrite:"
)


def make_rewriter(llm):
    """Build a local-tier rewriter: ``rewrite(query, history_texts) -> str``
    (empty string on any failure — the caller then skips the variant)."""

    def rewrite(query: str, history_texts: list[str]) -> str:
        from hunter_gatherer.models.base import Message

        history = "\n".join(f"- {t[:300]}" for t in history_texts[-6:])
        try:
            result = llm.chat(
                [Message(role="user", content=_REWRITE_PROMPT.format(
                    history=history, query=query[:1000]))],
                temperature=0.0, max_tokens=200,
            )
            return (result.message.content or "").strip().strip('"')
        except Exception as e:
            log.debug("query rewrite failed: %s", e)
            return ""

    return rewrite
