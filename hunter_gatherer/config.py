"""Central configuration: pydantic-settings loads from env / .env.

Everything the harness needs to run lives here so the rest of the codebase
takes a single `Settings` object rather than reading env vars ad hoc.

Env-var conventions
-------------------
  • Project settings use a `HG_` prefix (e.g. `HG_ROUTER_ALPHA`,
    `HG_SQLITE_PATH`) — this is what `.env.example` documents and what
    `env_prefix="hg_"` reads.
  • Third-party provider keys keep their conventional non-prefixed names
    (`OPENAI_API_KEY`, `LLAMA_BASE_URL`, `QDRANT_*`) so they interoperate with
    other tools; each such field lists its conventional name as a
    `validation_alias` in addition to the prefixed form.

  • Anthropic-Messages-compatible endpoints (Anthropic itself, Z.ai/Zhipu GLM,
    Amazon Bedrock, etc.) are configured via the vendor-neutral
    `MESSAGES_API_*` knobs (`HG_MESSAGES_API_BASE_URL`,
    `HG_MESSAGES_API_KEY`, `HG_MESSAGES_API_MODELS`). The
    legacy `ANTHROPIC_*` names still work as aliases. The internal client class
    is still named `AnthropicClient` because it speaks the Anthropic Messages
    *protocol* — vendor-neutral like "HTTP server".
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. All fields have defaults so the harness runs
    fully local with no cloud keys and no Qdrant (falls back to local-only)."""

    model_config = SettingsConfigDict(
        env_prefix="hg_",
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    def __init__(self, **values):
        # A checkout's .env may belong to another application. Only load h/g's
        # own configuration unless the caller explicitly selects a file.
        if "_env_file" not in values:
            values["_env_file"] = os.environ.get("HG_CONFIG_FILE") or Path.home() / ".hunter-gatherer" / "config.env"
        super().__init__(**values)

    # ── Cloud providers (conventional non-prefixed env names) ───────────────
    openai_api_key: str = Field(
        default="", validation_alias=AliasChoices("openai_api_key", "hg_openai_api_key")
    )
    anthropic_api_key: str = Field(
        default="", validation_alias=AliasChoices("anthropic_api_key", "hg_anthropic_api_key")
    )

    # ── Anthropic-Messages-compatible endpoints (vendor-neutral) ────────────
    # Any provider that speaks the Anthropic Messages API: Anthropic itself,
    # Z.ai/Zhipu GLM (https://api.z.ai/api/anthropic), Amazon Bedrock, etc.
    # `messages_api_key` is the authoritative field; `anthropic_api_key` above
    # is kept as a legacy alias and the two are unioned in `has_anthropic`.
    messages_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "messages_api_key", "hg_messages_api_key",
            "anthropic_api_key", "hg_anthropic_api_key",
        ),
    )
    messages_api_base_url: str = Field(
        default="https://api.anthropic.com/v1",
        validation_alias=AliasChoices(
            "messages_api_base_url", "hg_messages_api_base_url",
            "anthropic_base_url", "hg_anthropic_base_url",
        ),
    )
    # Comma-separated model ids that route through the Messages client (e.g.
    # "GLM-5.2,glm-5-turbo,claude-3-5-sonnet-20241022"). Replaces the old
    # claude- prefix heuristic; the prefix still works as a back-compat fallback.
    messages_api_models: str = Field(
        default="",
        validation_alias=AliasChoices(
            "messages_api_models", "hg_messages_api_models",
        ),
    )

    # ── Local LLM (llama.cpp server, OpenAI-compatible) ─────────────────────
    llama_base_url: str = Field(
        default="http://localhost:8080/v1",
        validation_alias=AliasChoices("llama_base_url", "hg_llama_base_url"),
    )

    # ── Model registry: which model id each tier uses ───────────────────────
    local_model: str = "local"
    cheap_model: str = "gpt-4o-mini"
    cloud_model: str = "gpt-4o"

    # ── Router ───────────────────────────────────────────────────────────────
    # RouteLLM-style α. Lower → cloud used more aggressively.
    router_alpha: float = 0.55
    router_trivial_max_chars: int = 120
    # When True, never escalate to cloud even if the router wants to.
    local_only: bool = False
    # Closed-loop self-tuning α (idea #3, 2026-07-31): after reflection, if
    # enough new router decisions have accumulated, re-run the sweep and
    # auto-apply the recommended α within these guardrails.
    router_autotune_enabled: bool = True
    router_autotune_min_decisions: int = 100  # need ≥ this many scored rows
    router_alpha_min: float = 0.2   # never auto-tune below this
    router_alpha_max: float = 0.85  # never auto-tune above this
    router_alpha_max_step: float = 0.05  # max Δα per adjustment

    # ── Memory: Qdrant (NAS) — conventional non-prefixed QDRANT_* names ─────
    qdrant_url: str = Field(
        default="http://localhost:6333",
        validation_alias="hg_qdrant_url",
    )
    qdrant_collection: str = Field(
        default="hg_memories",
        validation_alias="hg_qdrant_collection",
    )
    qdrant_api_key: str = Field(
        default="",
        validation_alias="hg_qdrant_api_key",
    )

    # ── Memory: SQLite source-of-truth ──────────────────────────────────────
    sqlite_path: str = "library.db"

    # ── Embeddings ──────────────────────────────────────────────────────────
    embed_model: str = "hashing"
    embed_local_files_only: bool = False

    # ── Recall (Park scoring) ───────────────────────────────────────────────
    recency_weight: float = 1.0
    importance_weight: float = 1.0
    relevance_weight: float = 1.0
    recall_topk: int = 8

    # ── Consolidation ───────────────────────────────────────────────────────
    dedup_cosine: float = 0.90

    # ── Reflection / continual learning (ideas #1, #2) ───────────────────────
    # Park 2023 fires reflection when cumulative recent importance crosses a
    # threshold (~150 in the paper, on a 1-10 importance scale → ~15 inserts).
    # When the running counter (store.pending_importance) crosses this, a
    # reflection pass fires. 0 disables signal-triggering (cron-only fallback).
    reflect_importance_threshold: float = 30.0
    # Rehearsal window (idea #3 — GEM/Chaudhry): N most-recent episodic
    # memories always prepended to the recall block each turn. 0 disables.
    rehearsal_window: int = 3

    # ── Voice ───────────────────────────────────────────────────────────────
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute: str = "int8"
    tts: str = "none"

    # ── Cost (USD per 1M tokens) ────────────────────────────────────────────
    price_local: float = 0.0
    price_gpt4o_mini_in: float = 0.15
    price_gpt4o_mini_out: float = 0.60
    price_gpt4o_in: float = 2.50
    price_gpt4o_out: float = 10.00

    # ── Paths ───────────────────────────────────────────────────────────────
    skills_dir: str = Field(
        default="", description="If empty, defaults to <pkg>/skills/examples plus ~/.hunter-gatherer/skills"
    )

    # ── UI server ───────────────────────────────────────────────────────────
    # Desktop shell (localhost) bypasses auth; LAN mode (0.0.0.0) requires it.
    ui_host: str = "127.0.0.1"   # "0.0.0.0" to expose on the LAN
    ui_port: int = 8765
    ui_token: str = ""           # empty + LAN → generated + logged on startup
    ui_lan_mode: bool = False    # True → bind 0.0.0.0 + enforce token
    ui_mount_mcp: bool = True    # expose the MCP tool interface at /mcp
    # Discovery surface only; this does not grant or restrict data access.
    mcp_tool_profile: Literal["full", "library"] = "library"
    library_maintenance_enabled: bool = True
    library_maintenance_interval_seconds: int = Field(default=300, ge=1, le=86400)
    library_maintenance_batch_size: int = Field(default=32, ge=1, le=128)

    # ── Pipelines ───────────────────────────────────────────────────────────
    # Default per-run token budget for pipelines without an explicit
    # token_budget (2026-08-12 #2). 0 = unlimited. Past 80% consumed,
    # model.invoke tiers degrade one step (cloud→cheap→local, honoring each
    # node's tier_floor) to preserve completion instead of a hard stop.
    pipeline_default_budget: int = 0

    # ── Self-growing router exemplars (2026-08-11 #1) ────────────────────────
    # Cap on the persisted exemplar set (FIFO eviction biased toward
    # low-match-count), and the cosine floor above which a candidate query is
    # considered a near-duplicate (bump, not add).
    router_exemplar_max: int = 200
    router_exemplar_promote_cosine: float = 0.90

    # ── Trained router classifier (2026-08-19 #1) ────────────────────────────
    # The fitted logistic head is consulted before the kNN layer only when it
    # was trained on ≥ min_rows labeled decisions AND |p − 0.5| ≥ the
    # confidence floor; otherwise _decide falls back to the exemplar-cosine
    # threshold — the upgrade can only help or no-op.
    router_classifier_min_rows: int = 50
    router_classifier_confidence_floor: float = 0.15

    # ── Recall-usage autotune (2026-08-14 #1) ────────────────────────────────
    # After reflection passes, if enough recall-usage rows have accumulated,
    # sweep the Park weights and auto-apply within guardrails (max step per
    # adjustment, weights clamped to [0, 2]).
    recall_autotune_enabled: bool = True
    recall_autotune_min_rows: int = 200
    recall_weight_max_step: float = 0.25

    # ── Embedder-swap safety (2026-08-13 #3) ─────────────────────────────────
    # On fingerprint mismatch (store built with a different embedder):
    # "warn" logs loudly and continues; "error" refuses to serve. Production /
    # NAS deployments should set "error". Fix path: `hunt memory
    # rebuild-vectors`.
    embedder_mismatch: str = "warn"  # "warn" | "error"
    rebuild_batch: int = 64

    # ── Working-memory eviction (2026-08-11 #3) ──────────────────────────────
    # When True, turns evicted past the session's rolling window are summarized
    # (one-line local-model call) and paged into long-term memory as episodic
    # `working-evict` entries before being dropped — the Letta eviction policy.
    # False keeps pure FIFO (cost-purist mode).
    session_summarize_on_evict: bool = True

    # ── Procedural→pipeline promotion (2026-08-13 #2) ────────────────────────
    # When a mined procedural memory's recurrence (occurrences) crosses this
    # threshold, graduate it into an authored, MCP-exposed pipeline. 0 disables.
    procedural_promote_at: int = 5

    # ── Best-of-N sampling (2026-08-14 #3) ──────────────────────────────────
    # Sample N candidate answers on objective final-answer turns and return the
    # argmax under a cheap external scorer (local-tier rubric judge — Huang
    # et al.: the scorer must not be the model critiquing itself). 0 = off
    # (default); 2-3 recommended. Hard-capped at 3 in the agent. Only fires on
    # non-local routes and while the turn's token spend is under
    # best_of_n_budget.
    best_of_n: int = 0
    best_of_n_budget: int = 6000

    # ── Vector backend selection (2026-08-12 #3) ─────────────────────────────
    # "memory" (default, local in-process vectors) | "sqlite_vec" (local-only
    # single-file, needs the [local] extra) | "qdrant" (optional remote index).
    vector_backend: str = "memory"

    # ── Hands-free voice (2026-08-14 #2) ─────────────────────────────────────
    # Wake-word model (openWakeWord; empty = "hey_jarvis"), barge-in on/off,
    # and the echo/refractory window after activation or playback end (ms).
    wake_word_model: str = ""
    barge_in: bool = True
    wake_refractory_ms: int = 800

    # ── A-MEM associative links (2026-08-19 #2) ─────────────────────────────
    # Top-m similar neighbors become each memory's outgoing links at store
    # time (captured from the dedup search), and recall expands one hop along
    # them to fill remaining k slots at seed × expansion_factor. 0 disables.
    link_top_m: int = 3
    link_expansion_factor: float = 0.8

    # ── Streaming STT prefetch (2026-08-19 #3) ──────────────────────────────
    # Speculatively warm route (+recall) on stable STT partials so the
    # embed+decide latency hides behind the user's own speech. Partials
    # shorter than min_chars never trigger embedding work.
    stt_prefetch: bool = True
    stt_prefetch_min_chars: int = 12

    # ── Temporal belief revision (2026-08-20 #1) ────────────────────────────
    # Reflection-time contradiction sweep: pairs semantic memories against
    # neighbors found at a LOWER cosine than the insert-time dedup threshold
    # (paraphrased contradictions land ~0.77, under dedup_cosine=0.90), judges
    # each pair, and marks supersession (metadata.superseded_by + valid_from/
    # valid_to) instead of deleting — preserving the audit trail. Superseded
    # memories are filtered (or downranked) out of the recall block.
    contradiction_cosine: float = 0.72
    contradiction_max_pairs: int = 20
    recall_superseded_mode: str = "filter"  # "filter" | "downrank"

    # ── Tool-result condensation (2026-08-23 #1) ────────────────────────────
    # Results over this estimated-token budget are compressed to a local-tier
    # digest; the raw payload is parked in a bounded sidecar and retrievable
    # on demand via the always-on tool_result_fetch tool. 0 disables.
    # Error results never condense (the revision gate needs them verbatim).
    tool_result_max_tokens: int = 400

    # ── Write-time fact extraction (2026-08-28 #1) ──────────────────────────
    # One local-tier structured call per non-trivial turn extracts ≤ N atomic
    # facts, each LLM-scored 1-10 (Park's rubric), fed through the Consolidator.
    # False keeps the legacy first-person-cue heuristic as the write path.
    extract_facts: bool = True
    extract_max_facts: int = 4

    # ── MMR recall selection (2026-08-28 #3) ────────────────────────────────
    # Diversity re-ranking of the fused candidate pool before the final top-k
    # slice: iteratively pick argmax λ·park_score − (1−λ)·max-similarity-to-
    # already-selected, so paraphrase variants stop spending k slots. 1.0
    # reproduces greedy top-k exactly; 0 disables diversity entirely (pure
    # novelty). Classic default 0.7.
    recall_mmr_lambda: float = 0.7

    # ── Usage-fitted forgetting curve (2026-08-31 #1) ───────────────────────
    # recency = 2**(−Δt / (68.97h · 2**(θ·x))) where x = (log access_count,
    # importance, is-episodic, is-consolidated). θ = 0 (default) reproduces
    # the flat 0.99**hours curve EXACTLY; the fitted vector persists through
    # learned_params ("recall_halflife_theta", a JSON 4-list). Half-Life
    # Regression (Settles & Meeder, KDD 2016).
    recall_halflife_theta: list[float] = [0.0, 0.0, 0.0, 0.0]

    # ── Adaptive retrieval gating (2026-08-31 #2) ───────────────────────────
    # Drop recall candidates whose relevance sub-score falls below this floor
    # before Park scoring / MMR — nothing relevant → grounded abstention (no
    # memory block) instead of k marginal distractors framed as relevant.
    # 0.0 = off; fitted by `memory tune` (floor sweep), not guessed.
    recall_relevance_floor: float = 0.0

    # Explicit per-call local relevance checking. Empty paths disable setup;
    # profile, ordinary recall and the internal context pipeline stay unchanged.
    recall_reranker_model_path: str = ""
    recall_reranker_policy_path: str = ""
    recall_reranker_timeout_seconds: float = Field(default=15, ge=1, le=60)
    recall_reranker_idle_seconds: float = Field(default=60, ge=1, le=300)

    # ── Explicit feedback (2026-08-31 #3) ───────────────────────────────────
    # Human gold labels weight N× proxy labels in the tuning sweeps.
    feedback_gold_weight: int = 5

    # ── Semantic answer cache (2026-09-01 #1) ───────────────────────────────
    # A zero-token "cached" tier below local: repeated/similar queries skip
    # routing, recall, tool selection, AND the model call. Entries record the
    # [mem:…] ids they cited; memory mutations (consolidation UPDATE/DELETE,
    # supersession, decay) invalidate dependent entries so a cached answer
    # can never outlive the belief it was grounded in. sim floor per
    # GPTCache's semi-precise-cache finding; TTL + LRU cap as backstops.
    answer_cache_enabled: bool = True
    answer_cache_sim: float = 0.95
    answer_cache_ttl_hours: float = 168.0  # 1 week
    answer_cache_max: int = 256

    # Explicitly pinned standing facts; token count is an approximation.
    core_memory_max_tokens: int = Field(default=512, ge=0, le=4096)
    # Exact archived passages can prime the main agent and workers. Zero
    # disables automatic priming; read-only archive tools remain available.
    document_context_max_tokens: int = Field(default=768, ge=0, le=4096)
    document_context_top_k: int = Field(default=3, ge=1, le=20)
    # Exhaustive preserves current archive recall; bounded caps passage hydration.
    # Reuse matching live archive chunk vectors; disables only import optimization.
    archive_embedding_reuse: bool = True
    archive_search_policy: Literal["exhaustive", "bounded"] = "exhaustive"
    archive_candidate_budget: int = Field(default=128, ge=30, le=2048)
    # Opt-in source discovery: one best passage per document before candidate limits.
    archive_source_routing_policy: Literal["passage_pool", "document_heads"] = "passage_pool"
    archive_catalog_index: bool = False
    # Evidence packets historically expand/sort topic terms for archive search.
    # Original preserves the question wording for semantic document retrieval.
    evidence_document_query_strategy: Literal["expanded", "original"] = "expanded"
    # Experimental Find delivery: metadata-only hits remain exact Open references.
    # Does not discard retrieval candidates or change answerability diagnostics.
    library_find_delivery_policy: Literal["all", "body_first", "focused"] = "all"
    library_find_compact: bool = False
    # Opt-in exact-source graph navigation. No writing/reranking model call.
    library_find_relationships: bool = False
    # Opt-in Find trials: original preserves the question; coverage also changes
    # ranking and quote allocation. No extra model pass. Gates retain legacy.
    library_find_relevance_policy: Literal["legacy", "original", "coverage"] = "legacy"
    # Citation validity is always checked; semantic support is one optional
    # local-model call and is reported as unverified when disabled/unavailable.
    attribution_judge: bool = False

    # ── Delegate-finding harvest (2026-09-01 #2) ────────────────────────────
    # Harvest the turn's full textual yield (final answer + condensed
    # spawn_agent findings) through the extractor, with provenance metadata.
    harvest_delegate: bool = True

    # ── Within-tier model selection (2026-09-01 #3) ─────────────────────────
    # Per-tier candidate lists (comma-separated). Singleton lists reproduce
    # today's one-model-per-tier behavior exactly; multiple entries enable
    # the UCB1 bandit (reward = outcome − λ·normalized cost).
    cheap_models: str = ""  # e.g. "gpt-4o-mini,gpt-4.1-mini"
    cloud_models: str = ""  # e.g. "gpt-4o,claude-3-5-sonnet-20241022"
    bandit_cost_lambda: float = 0.2
    bandit_exploration: bool = True

    # ── Inbound MCP client (2026-09-17 #1) ──────────────────────────────────
    # External MCP servers whose tools become first-class agent tools
    # (allowlist-first: nothing connects unless configured). JSON list:
    #   [{"name": "weather", "command": "/path/to/server", "args": ["--flag"]}]
    # or URL form: [{"name": "svc", "url": "http://host:port/mcp"}]
    mcp_servers: str = ""
    mcp_refresh_minutes: float = 10.0

    # ── Conversational query rewriting (2026-09-17 #2) ─────────────────────
    # Fold recent history into a self-contained rewrite for short/pronominal
    # follow-ups BEFORE recall; fed as an extra_queries variant (widening
    # only). Local tier, per-turn cache, trivial-route skip.
    recall_rewrite: bool = True

    # ── Judge calibration (2026-09-17 #3) ───────────────────────────────────
    # eval --judges sweeps these hand-set cosine bands against the golden
    # adjudication pairs and (with --apply) persists fitted values through
    # learned_params.
    judges_apply_thresholds: bool = False

    # ── Intra-step tool concurrency (2026-09-07 #3) ─────────────────────────
    # When the model emits several tool calls in one step, run them through
    # this many workers (step costs max(latency) not sum). 1 = sequential,
    # exactly the old loop.
    tool_concurrency: int = 3

    # ── Session-aware routing (2026-09-09 #1) ──────────────────────────────
    # Mid-session escalation must cross α by this margin (tier-flap
    # hysteresis; 0 = off, the blind per-query default). Short follow-ups
    # stay escalated automatically when the previous tier was cloud/cheap.
    router_switch_margin: float = 0.0

    # ── Spend governor (2026-09-09 #2) ────────────────────────────────────
    # Daily ceilings (0 = off). Trending over → α+offset (cheaper routes);
    # overrun → tier cap at cheap; exhausted + unattended → local. Visible
    # in the governor WS event / decision reasons, never silent.
    daily_usd_budget: float = 0.0
    daily_token_budget: int = 0

    # ── HyDE recall expansion (2026-08-23 #2) ───────────────────────────────
    # Before recall, the local tier drafts a 1-2 sentence hypothetical answer
    # to the query; it is embedded alongside the query and the candidate pools
    # fuse (relevance = max over variants). The hypothetical can only widen
    # the pool — never hide existing candidates. Skipped on trivial routes.
    recall_hyde: bool = True

    # ── Tier-aware context budget (2026-08-20 #2) ───────────────────────────
    # Per-tier context windows (defaults: local 4k / cheap 32k / cloud 200k)
    # with an output reserve; the loop composes each turn within the remaining
    # budget — history trims oldest-first, recall shrinks its top-k. Override
    # with "tier=tokens,..." (a special tier "all" sets every tier).
    context_windows: str = ""
    context_output_reserve: int = 2048

    # ── Helpers ─────────────────────────────────────────────────────────────
    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_anthropic(self) -> bool:
        """True if any Messages-API-compatible credential is configured.
        Covers both the vendor-neutral `messages_api_key` and the legacy
        `anthropic_api_key` (kept for back-compat)."""
        return bool(self.messages_api_key or self.anthropic_api_key)

    @property
    def messages_api_model_set(self) -> set[str]:
        """Lowercased model ids explicitly routed through the Messages client."""
        return {m.strip().lower() for m in self.messages_api_models.split(",") if m.strip()}

    def sqlite_uri(self) -> str:
        # Resolve RELATIVE paths against ~/.hunter-gatherer (not the cwd): an
        # unpinned invocation (an agent running `hunter_gatherer` from another
        # project) previously dropped library.db into that project's root,
        # confusing every tool that greps the tree. Absolute paths (the .env
        # and every client config pin one) are untouched.
        p = Path(self.sqlite_path)
        if not p.is_absolute():
            p = Path.home() / ".hunter-gatherer" / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p.resolve().as_uri()

    def price_for(self, model: str, direction: str) -> float:
        """USD per 1M tokens for a given model + 'in'|'out'.

        Both the model id and the price-field slugs are normalized by
        stripping separators ('gpt-4o-mini' → 'gpt4omini' matches the field
        price_gpt4o_mini_*), so naming inconsistency between the two can't
        silently zero out a lookup. History: an earlier version replaced '-'
        with '_' only in the model id, producing 'gpt_4o' against
        'price_gpt4o_in' and silently returning $0 for every gpt-4o /
        gpt-4o-mini call — caught by the A/B harness (2026-08-13 #1)."""
        norm = model.lower().replace("-", "").replace(".", "").replace("_", "")
        # Direct hit first (common case).
        direct = getattr(self, f"price_{norm}_{direction}", None)
        if direct is not None:
            return float(direct)
        # Fallback: match against every price field, separator-insensitively.
        for field in self.model_fields:
            if not field.startswith("price_") or not field.endswith(f"_{direction}"):
                continue
            slug = field[len("price_"): -len(f"_{direction}")].replace("_", "")
            if slug == norm:
                return float(getattr(self, field))
        return 0.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton settings."""
    return Settings()
