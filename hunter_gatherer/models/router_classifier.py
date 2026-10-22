"""The trained router classifier (2026-08-19 #1).

The documented "train a real router" extension point, realized: a cheap
logistic-regression head fit on accumulated ``router_decisions`` rows, using
only features computable *before* any model call:

    [exemplar-cosine score, query length, rule-layer trivial flag,
     hour-of-day (sin/cos), tool-kind one-hot (lexical inference)]

Hand-rolled softmax + batch gradient descent (L2) — no scikit-learn
dependency, matching the project's "simple primitives first" rule.

Labels come from *observed outcomes*, never self-assessment:
    escalate=1  — good cloud/cheap turns, and LOCAL turns that hit
                  max_steps/revised (the mis-route correction signal)
    escalate=0  — good local turns
    dropped     — repeat outcomes, and bad cloud turns (ambiguous causes)

Safety: ``_decide`` consults the classifier only when it exists, was trained
on enough rows, and is confident (``|p − 0.5| ≥ floor``); otherwise it falls
back to the kNN exemplar-cosine layer — the upgrade can only help or no-op.
Promotion is empirical: the ``trained_vs_knn`` A/B decision must show the
advantage before the classifier is trusted.
"""
from __future__ import annotations

import contextlib
import json
import math
import time
from dataclasses import dataclass, field

# Lexical tool-kind buckets (deterministic, selector-free so the router needs
# no new wiring; mirrors the builtin registry's kinds).
_TOOL_KINDS = ["shell", "read_file", "write_file", "web_fetch", "memory", "spawn_agent", "model", "none"]
_TOOL_KEYWORDS = {
    "shell": ("run ", "execute ", "command", "test", "build", "pytest", "shell", "script"),
    "read_file": ("read ", "open ", "show me the file", "look at "),
    "write_file": ("write ", "save ", "create a file", "output to"),
    "web_fetch": ("fetch ", "http", "url", "website", "https://"),
    "memory": ("remember", "recall", "memory", "what do you know", "forget"),
    "spawn_agent": ("research", "in depth", "across many", "break this down", "plan a"),
    "model": ("summarize", "translate", "explain", "draft", "write a"),
}
_N_FEATURES = 5 + len(_TOOL_KINDS)
_BLOB_KEY = "router_classifier"


def infer_tool_kind(query: str) -> str:
    q = f" {query.lower()} "
    best, best_hits = "none", 0
    for kind, kws in _TOOL_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw in q)
        if hits > best_hits:
            best, best_hits = kind, hits
    return best


def extract_features(query: str, cloud_score: float | None, ts: float | None = None) -> list[float]:
    """Features computable before any model call. ``cloud_score`` is the
    exemplar-cosine score (None → 0.0, e.g. rule-layer routes)."""
    ts = ts if ts is not None else time.time()
    hour = time.localtime(ts).tm_hour
    kind = infer_tool_kind(query)
    onehot = [1.0 if kind == k else 0.0 for k in _TOOL_KINDS]
    stripped = query.strip()
    trivial = 1.0 if len(stripped) <= 20 and stripped.lower() in (
        "hi", "hello", "hey", "ok", "thanks", "yes", "no") else 0.0
    return [
        float(cloud_score or 0.0),
        min(len(query) / 1000.0, 1.0),
        trivial,
        math.sin(2 * math.pi * hour / 24.0),
        math.cos(2 * math.pi * hour / 24.0),
        *onehot,
    ]


def label_for(decision) -> int | None:
    """1 = should escalate, 0 = should stay local, None = drop (ambiguous)."""
    outcome = getattr(decision, "outcome", "good")
    tier = getattr(decision, "tier", "local")
    if outcome in ("repeat",):
        return None
    if outcome in ("max_steps", "revised"):
        return 1 if tier == "local" else None
    # good
    return 0 if tier == "local" else 1


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


@dataclass
class LogisticRouter:
    """A fitted logistic-regression escalate/not-escalate head."""

    weights: list[float] = field(default_factory=lambda: [0.0] * _N_FEATURES)
    bias: float = 0.0
    means: list[float] = field(default_factory=lambda: [0.0] * _N_FEATURES)
    stds: list[float] = field(default_factory=lambda: [1.0] * _N_FEATURES)
    n_rows: int = 0
    trained_at: float = 0.0
    train_accuracy: float = 0.0

    # ── training ───────────────────────────────────────────────────────────
    def fit(self, X: list[list[float]], y: list[int], *, epochs: int = 300, lr: float = 0.1,
            l2: float = 1e-3) -> None:
        assert len(X) == len(y) and X
        n = len(X)
        d = len(X[0])
        self.means = [sum(row[j] for row in X) / n for j in range(d)]
        self.stds = [
            (math.sqrt(sum((row[j] - self.means[j]) ** 2 for row in X) / n) or 1.0)
            for j in range(d)
        ]
        Z = [self._standardize(row) for row in X]
        w = [0.0] * d
        b = 0.0
        for _ in range(epochs):
            gw = [0.0] * d
            gb = 0.0
            for zi, yi in zip(Z, y, strict=True):
                p = _sigmoid(sum(wj * zj for wj, zj in zip(w, zi, strict=True)) + b)
                err = p - yi
                gb += err
                for j in range(d):
                    gw[j] += err * zi[j]
            for j in range(d):
                w[j] -= lr * (gw[j] / n + l2 * w[j])
            b -= lr * gb / n
        self.weights, self.bias = w, b
        self.n_rows = n
        self.trained_at = time.time()
        self.train_accuracy = self._accuracy(Z, y)

    def _standardize(self, row: list[float]) -> list[float]:
        return [
            (v - m) / s for v, m, s in zip(row, self.means, self.stds, strict=True)
        ]

    def _accuracy(self, Z: list[list[float]], y: list[int]) -> float:
        if not Z:
            return 0.0
        correct = sum(
            1 for zi, yi in zip(Z, y, strict=True)
            if (self.prob_standardized(zi) >= 0.5) == bool(yi)
        )
        return correct / len(Z)

    # ── inference ──────────────────────────────────────────────────────────
    def prob_standardized(self, z: list[float]) -> float:
        return _sigmoid(sum(wj * zj for wj, zj in zip(self.weights, z, strict=True)) + self.bias)

    def predict(self, features: list[float]) -> float:
        """P(escalate | features), standardized with the training stats."""
        return self.prob_standardized(self._standardize(features))

    # ── persistence ────────────────────────────────────────────────────────
    def to_blob(self) -> str:
        return json.dumps({
            "weights": self.weights, "bias": self.bias,
            "means": self.means, "stds": self.stds,
            "n_rows": self.n_rows, "trained_at": self.trained_at,
            "train_accuracy": self.train_accuracy,
        })

    @classmethod
    def from_blob(cls, blob: str) -> LogisticRouter:
        d = json.loads(blob)
        return cls(
            weights=d["weights"], bias=d["bias"], means=d["means"], stds=d["stds"],
            n_rows=d.get("n_rows", 0), trained_at=d.get("trained_at", 0.0),
            train_accuracy=d.get("train_accuracy", 0.0),
        )


def save_classifier(store, classifier: LogisticRouter) -> None:
    store.set_meta(_BLOB_KEY, classifier.to_blob())


def load_classifier(store, *, min_rows: int = 50) -> LogisticRouter | None:
    """Load the persisted head, or None when absent / trained on too few rows."""
    blob = store.get_meta(_BLOB_KEY)
    if not blob:
        return None
    with contextlib.suppress(Exception):
        clf = LogisticRouter.from_blob(blob)
        if clf.n_rows >= min_rows:
            return clf
    return None


def train_from_decisions(decisions, *, score_fn=None) -> tuple[LogisticRouter | None, dict]:
    """Fit the head on logged decisions. ``score_fn(query) -> float`` recomputes
    the exemplar-cosine score for rows whose score is None (rule-layer routes).
    Returns (classifier | None, report-dict). None when fewer than 2 usable
    labeled rows of each class exist."""
    X: list[list[float]] = []
    y: list[int] = []
    for d in decisions:
        lab = label_for(d)
        if lab is None:
            continue
        score = d.score
        if score is None and score_fn is not None:
            with contextlib.suppress(Exception):
                score = score_fn(d.query)
        X.append(extract_features(d.query, score, getattr(d, "ts", None)))
        y.append(lab)
    n_pos = sum(y)
    n_neg = len(y) - n_pos
    report = {"rows": len(y), "positive": n_pos, "negative": n_neg}
    if n_pos < 2 or n_neg < 2:
        report["note"] = "insufficient labeled rows (need ≥2 of each class)"
        return None, report
    clf = LogisticRouter()
    clf.fit(X, y)
    report["train_accuracy"] = round(clf.train_accuracy, 4)
    return clf, report


def heldout_comparison(decisions, *, score_fn=None, seed: int = 7) -> dict | None:
    """80/20 split: classifier accuracy vs the kNN-threshold baseline
    (escalate iff logged score ≥ logged α). Returns the comparison dict, or
    None when there isn't enough data. Deterministic (fixed-seed shuffle)."""
    import random

    labeled = []
    for d in decisions:
        lab = label_for(d)
        if lab is None or d.score is None:
            continue
        labeled.append((d, lab))
    n_pos = sum(1 for _, lab in labeled if lab == 1)
    if len(labeled) < 20 or n_pos < 5 or (len(labeled) - n_pos) < 5:
        return None
    rng = random.Random(seed)
    rng.shuffle(labeled)
    cut = int(len(labeled) * 0.8)
    train, test = labeled[:cut], labeled[cut:]
    clf = LogisticRouter()
    clf.fit(
        [extract_features(d.query, d.score, d.ts) for d, _ in train],
        [lab for _, lab in train],
    )
    # Classifier accuracy on held-out.
    correct = sum(
        1 for d, lab in test
        if (clf.predict(extract_features(d.query, d.score, d.ts)) >= 0.5) == bool(lab)
    )
    clf_acc = correct / len(test)
    # kNN baseline: escalate iff score ≥ the row's logged α.
    correct = sum(
        1 for d, lab in test
        if (d.score >= (d.alpha if d.alpha is not None else 0.55)) == bool(lab)
    )
    knn_acc = correct / len(test)
    return {"classifier": round(clf_acc, 4), "knn": round(knn_acc, 4), "heldout": len(test)}
