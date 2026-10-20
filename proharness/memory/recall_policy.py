"""Opt-in, locally calibrated relevance review of current visible memories.

Raw cross-encoder logits are candidate relevance signals, never confidence,
truth, answerability, or evidence that a corpus contains no answer. The trusted
local manifest binds the tested runtime contract to one store and calibration.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath

from proharness.memory.retrieval_feedback import current_memory
from proharness.memory.schemas import RecallHit

MODEL_REPO = "cross-encoder/ms-marco-MiniLM-L6-v2"
MODEL_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
MODEL_FILES = (
    ("config.json", 794, "380e02c93f431831be65d99a4e7e5f67c133985bf2e77d9d4eba46847190bacc"),
    ("model.safetensors", 90870598, "821d1aa69520101d6e0737f78a042ae25b19e5cb9160701909d10434f4aeb0ae"),
    ("special_tokens_map.json", 132, "3c3507f36dff57bce437223db3b3081d1e2b52ec3e56ee55438193ecb2c94dd6"),
    ("tokenizer.json", 711396, "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"),
    ("tokenizer_config.json", 1330, "a5c2e5a7b1a29a0702cd28c08a399b5ecc110c263009d17f7e3b415f25905fd8"),
    ("vocab.txt", 231508, "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3"),
    ("README.md", 3673, "7c0ec39941d0d1d766ccde671f592971d82fe45b7eea550a80d1ea864ebc3baa"),
)
PREPROCESSING = {"max_length": 512, "batch_size": 16, "threads": 2,
                 "activation": "Identity", "text_field": "memory.text"}
_KEYS = {"version", "id", "backend", "embedder_fingerprint", "store_id",
         "candidate_budget", "max_results", "include_sensitive", "model",
         "preprocessing", "threshold", "calibration"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WORKER_CODES = {"busy": "worker_busy", "unavailable": "worker_unavailable",
                 "timeout": "worker_timeout", "invalid_output": "worker_invalid_output",
                 "model_mismatch": "worker_model_mismatch"}
_WORKER_REASONS = {"worker_busy", "worker_timeout", "worker_unavailable", "worker_failed",
                   "worker_protocol", "worker_model_invalid", "worker_input_limit",
                   "worker_dependency_missing", "worker_shutdown", "worker_launch_failed",
                   "worker_invalid_result", "worker_invalid_output", "worker_model_mismatch",
                   "worker_model_missing", "input_truncated"}


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate policy field")
        result[key] = value
    return result


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _load_policy(path):
    """Read a small trusted manifest; no model code or source text is read here."""
    with Path(path).expanduser().open("rb") as handle:
        encoded = handle.read(65537)
    if len(encoded) > 65536:
        raise ValueError("Policy too large")
    policy = json.loads(encoded, object_pairs_hook=_strict_object)
    if not isinstance(policy, dict) or set(policy) != _KEYS:
        raise ValueError("Invalid policy fields")
    if (type(policy["version"]) is not int or policy["version"] != 1 or
            not isinstance(policy["id"], str) or
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", policy["id"]) or
            policy["backend"] != "SqliteVecBackend" or
            policy["embedder_fingerprint"] != "st:BAAI/bge-small-en-v1.5" or
            not isinstance(policy["store_id"], str) or
            not re.fullmatch(r"[0-9a-f]{16}", policy["store_id"]) or
            type(policy["candidate_budget"]) is not int or policy["candidate_budget"] != 20 or
            type(policy["max_results"]) is not int or policy["max_results"] != 5 or
            policy["include_sensitive"] is not False or not _finite(policy["threshold"])):
        raise ValueError("Policy contract mismatch")
    preprocessing = policy["preprocessing"]
    if (not isinstance(preprocessing, dict) or preprocessing != PREPROCESSING or
            any(type(preprocessing[k]) is not int for k in ("max_length", "batch_size", "threads"))):
        raise ValueError("Preprocessing mismatch")
    model = policy["model"]
    if (not isinstance(model, dict) or set(model) != {"repo", "revision", "files"} or
            model["repo"] != MODEL_REPO or model["revision"] != MODEL_REVISION or
            not isinstance(model["files"], list) or len(model["files"]) != len(MODEL_FILES)):
        raise ValueError("Model mismatch")
    files = []
    for entry in model["files"]:
        if (not isinstance(entry, dict) or set(entry) != {"file", "bytes", "sha256"} or
                not isinstance(entry["file"], str) or type(entry["bytes"]) is not int or
                entry["bytes"] <= 0 or not isinstance(entry["sha256"], str) or
                not _SHA256.fullmatch(entry["sha256"])):
            raise ValueError("Invalid artifact")
        file = PurePosixPath(entry["file"])
        if file.is_absolute() or ".." in file.parts or "\\" in entry["file"]:
            raise ValueError("Unsafe artifact path")
        files.append((entry["file"], entry["bytes"], entry["sha256"]))
    if len(set(files)) != len(files) or set(files) != set(MODEL_FILES):
        raise ValueError("Artifact contract mismatch")
    calibration = policy["calibration"]
    if (not isinstance(calibration, dict) or
            set(calibration) != {"study", "split", "corpus_sha256", "labels_sha256", "policy_sha256"} or
            not isinstance(calibration["study"], str) or not calibration["study"].strip() or
            len(calibration["study"]) > 256 or calibration["split"] != "development" or
            any(not isinstance(calibration[key], str) or not _SHA256.fullmatch(calibration[key])
                for key in ("corpus_sha256", "labels_sha256", "policy_sha256"))):
        raise ValueError("Invalid calibration provenance")
    return policy, hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"),
                                            allow_nan=False).encode()).hexdigest()


def _snapshot(memory):
    # Access receipts may update last_accessed/access_count independently. All
    # returned source fields and references must still match what was scored.
    return json.dumps({"id": memory.id, "text": memory.text, "source": memory.source,
                       "type": memory.type, "metadata": memory.metadata, "tags": memory.tags,
                       "importance": memory.importance, "created_at": memory.created_at},
                      sort_keys=True, allow_nan=False)


def _current_hits(harness, hits, include_sensitive):
    visible, changed = [], False
    for hit in hits:
        fresh = current_memory(harness.store, hit.memory.id, include_sensitive)
        if fresh is None or _snapshot(fresh) != _snapshot(hit.memory):
            changed = True
            continue
        visible.append(replace(hit, memory=fresh))
    return visible, changed


def apply_local_relevance_check(harness, query: str, hits: list[RecallHit], *,
                                top_k: int, include_sensitive: bool) -> tuple[list[RecallHit], dict]:
    """Filter <=20 supplied visible candidates, fail open only to live sources."""
    if type(top_k) is not int or not 1 <= top_k <= 5:
        raise ValueError("local relevance review requires top_k from 1 to 5")
    if not isinstance(query, str) or type(include_sensitive) is not bool:
        raise ValueError("Invalid relevance review arguments")
    if len(hits) > 20 or len({hit.memory.id for hit in hits}) != len(hits):
        raise ValueError("Expected at most 20 unique visible candidates")
    hits, initial_change = _current_hits(harness, hits, include_sensitive)
    assessment = {"applied": False, "status": "not_applied", "scope": "current_visible_memories",
                  "bounded": True, "visible_candidates": len(hits), "returned": 0,
                  "filtered_count": 0, "policy_id": None,
                  "filter_score_semantics": "raw_logit_candidate_relevance",
                  "hit_score_semantics": "original_retrieval_score", "ordering": "original_retrieval",
                  "interpretation": "Candidate relevance only; not confidence or answerability."}

    def fallback(reason):
        live, changed = _current_hits(harness, hits, include_sensitive)
        chosen = live[:top_k]
        assessment.update(applied=False, status="not_applied", reason=reason,
                          fallback="unfiltered_relevance", returned=len(chosen))
        if initial_change or changed or reason == "search_changed":
            assessment.update(status="search_changed", reason="search_changed", incomplete=True)
        return chosen, assessment

    if initial_change:
        return fallback("search_changed")
    settings = harness.settings
    model_path = getattr(settings, "recall_reranker_model_path", "")
    policy_path = getattr(settings, "recall_reranker_policy_path", "")
    if not model_path or not policy_path:
        return fallback("missing_config")
    try:
        policy, digest = _load_policy(policy_path)
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        return fallback("policy_invalid")
    assessment.update(policy_id=policy["id"], policy_digest=digest)
    from proharness.memory.vec_sqlite import SqliteVecBackend

    if type(harness.store._backend) is not SqliteVecBackend:
        return fallback("backend_mismatch")
    try:
        fingerprint = harness.store.embedder.fingerprint()
        stored_fingerprint = harness.store.get_meta("embedder_fingerprint")
    except Exception:
        return fallback("embedder_mismatch")
    if fingerprint != policy["embedder_fingerprint"] or stored_fingerprint != fingerprint:
        return fallback("embedder_mismatch")
    from proharness.mcp.tools import _store_identity

    if _store_identity(harness)["id"] != policy["store_id"]:
        return fallback("store_mismatch")
    if include_sensitive:
        return fallback("sensitive_scope")
    if not hits:
        assessment.update(status="no_visible_candidates", reason="empty_candidate_pool",
                          message="Retrieval returned no visible memory candidates; the local model was not run.")
        return [], assessment
    try:
        from proharness.memory.recall_reranker_runtime import runtime

        result = runtime.score(model_path, policy["model"]["files"], query,
                               [hit.memory.text for hit in hits],
                               timeout_seconds=getattr(settings, "recall_reranker_timeout_seconds", 15),
                               idle_seconds=getattr(settings, "recall_reranker_idle_seconds", 60))
    except Exception as exc:
        code = getattr(exc, "code", "worker_unavailable")
        if not isinstance(code, str):
            code = "worker_unavailable"
        code = _WORKER_CODES.get(code, code)
        return fallback(code if code in _WORKER_REASONS else "worker_unavailable")
    # Empty scores are valid only for an explicit, bounded truncation skip.
    # They must never become a successful bounded no-match.
    if (not isinstance(result, dict) or not isinstance(result.get("scores"), list) or
            type(result.get("truncated_count")) is not int or
            not 0 <= result["truncated_count"] <= len(hits)):
        return fallback("worker_invalid_result")
    if result["truncated_count"]:
        if result["scores"] or result.get("skipped_reason") != "input_truncated":
            return fallback("worker_invalid_result")
        return fallback("input_truncated")
    if ("skipped_reason" in result or len(result["scores"]) != len(hits) or
            any(not _finite(score) for score in result["scores"])):
        return fallback("worker_invalid_result")
    # Revalidate all scored candidates, including those rejected: disappearance
    # must not turn an incomplete search into a trustworthy bounded no-match.
    live, changed = _current_hits(harness, hits, include_sensitive)
    if changed:
        return fallback("search_changed")
    ranked = sorted(zip(live, result["scores"], strict=True), key=lambda pair: -pair[1])
    passing = [hit for hit, score in ranked if score >= policy["threshold"]]
    chosen = passing[:top_k]
    assessment.update(applied=True, status="candidates_for_review" if chosen else "no_candidates_passed",
                      returned=len(chosen), filtered_count=len(hits) - len(passing),
                      threshold=policy["threshold"], ordering="local_reranker")
    if not chosen:
        assessment["message"] = "No visible memory candidates passed this relevance filter."
    return chosen, assessment
