"""Measure document discovery and excerpt emphasis separately on fixed synthetic cases.

Run from a checkout with h/g installed, optionally with its semantic extra and
cached BGE model. No model downloads, user settings, Qdrant or live library are
used. Scores are diagnostic observations, not pass thresholds or general quality
claims. --output must name a new file; scratch libraries stay beside the report.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import tempfile
import time
from types import SimpleNamespace


FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/release_retrieval/cases.json"
BGE_MODEL = "BAAI/bge-small-en-v1.5"


def load_cases(path: Path = FIXTURE) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    documents = {item["id"]: item for item in data["documents"]}
    if len(documents) != len(data["documents"]):
        raise ValueError("Duplicate document IDs")
    if len({item["id"] for item in data["cases"]}) != len(data["cases"]):
        raise ValueError("Duplicate case IDs")
    for item in data["documents"]:
        if Path(item["path"]).name != item["path"]:
            raise ValueError("Fixture paths must be simple file names")
    for case in data["cases"]:
        expected = case["expected_document"]
        if expected is not None and case["expected_sentence"] not in documents[expected]["text"]:
            raise ValueError("Expected sentences must be exact source text")
    return data


def evaluate_mode(data: dict, mode: str, directory: Path) -> dict:
    # Offline flags are set before importing the optional model runtime. They
    # deliberately override the caller's environment; missing cache is an error.
    for variable in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ[variable] = "1"
    from hunter_gatherer.config import Settings
    from hunter_gatherer.memory.documents import DocumentArchive
    from hunter_gatherer.memory.embeddings import HashingEmbedder, SentenceTransformerEmbedder
    from hunter_gatherer.memory.simple_open import open_sources
    from hunter_gatherer.memory.simple_search import find
    from hunter_gatherer.memory.store import MemoryStore
    from hunter_gatherer.terminal import passage_excerpt

    directory.mkdir()
    settings = Settings.model_construct(
        sqlite_path=str(directory / "evaluation.db"), vector_backend="memory",
        embed_model="hashing" if mode == "hashing" else BGE_MODEL, local_only=True,
        openai_api_key="", anthropic_api_key="", messages_api_key="",
        qdrant_url="", qdrant_api_key="", library_maintenance_enabled=False,
        recall_reranker_enabled=False, archive_search_policy="exhaustive",
        library_find_relevance_policy="coverage",
    )
    started = time.perf_counter()
    embedder = HashingEmbedder() if mode == "hashing" else SentenceTransformerEmbedder(BGE_MODEL)
    result = {"mode": mode, "model": settings.embed_model, "qdrant": False,
              "max_tokens": 4096, "search_pages": 1,
              "library_find_relevance_policy": settings.library_find_relevance_policy, "cases": []}
    with MemoryStore(settings, embedder=embedder) as store:
        archive = DocumentArchive(store)
        doc_ids = {}
        for item in data["documents"]:
            source = directory / item["path"]
            source.write_text(item["text"], encoding="utf-8")
            doc_ids[item["id"]] = archive.ingest(str(source), title=item["title"], category="release-evaluation")["document_id"]
        result["initialization_and_ingest_seconds"] = round(time.perf_counter() - started, 3)
        labels = {value: key for key, value in doc_ids.items()}
        original_text = {item["id"]: item["text"] for item in data["documents"]}
        result["embedding_fingerprint"] = embedder.fingerprint()
        harness = SimpleNamespace(store=store, settings=settings)
        for case in data["cases"]:
            started = time.perf_counter()
            packet = find(harness, case["question"], max_tokens=4096)
            elapsed = time.perf_counter() - started
            passages = packet.get("supporting_passages", [])
            ranked = list(dict.fromkeys(p["document_id"] for p in passages))
            expected_id = doc_ids.get(case["expected_document"])
            rank = ranked.index(expected_id) + 1 if expected_id in ranked else None
            focus_hit = context_hit = False
            displayed = []
            fidelity_errors = []
            for passage in passages:
                exact = archive.get_chunk(passage["chunk_id"])
                if (exact.get("quote") != passage["quote"] or exact.get("citation") != passage["citation"]
                        or exact.get("document_id") != passage["document_id"]):
                    fidelity_errors.append("delivered passage differs from exact stored chunk")
                if passage["quote"] not in original_text[labels[passage["document_id"]]]:
                    fidelity_errors.append("delivered quote is not exact original fixture text")
                opened = open_sources(harness, [passage["citation"]], max_tokens=4096)
                if (opened["statuses"] != ["available"] or len(opened["sources"]) != 1
                        or opened["sources"][0].get("quote") != passage["quote"]):
                    fidelity_errors.append("citation did not reopen to the same exact quote")
                excerpt = passage_excerpt(passage["quote"], case["question"])
                focus = excerpt.text[excerpt.focus_start:excerpt.focus_end]
                if excerpt.text not in passage["quote"] or focus not in excerpt.text:
                    fidelity_errors.append("display excerpt is not contiguous source text")
                is_expected = passage["document_id"] == expected_id
                if is_expected:
                    focus_hit |= case["expected_sentence"] in focus
                    context_hit |= case["expected_sentence"] in excerpt.text
                displayed.append({"document": labels[passage["document_id"]],
                                  "citation": passage["citation"], "version": passage["version"],
                                  "is_latest": passage["is_latest"], "focus": focus,
                                  "excerpt": excerpt.text})
            result["cases"].append({
                **case, "expected_document_rank": rank,
                "expected_sentence_in_focus": focus_hit if expected_id else None,
                "expected_sentence_in_context": context_hit if expected_id else None,
                "returned_documents": [labels[value] for value in ranked],
                "has_more": bool(packet.get("continuation", {}).get("next_cursor")),
                "answerability": packet.get("retrieval_assessment", {}).get("answerability"),
                "fidelity_errors": fidelity_errors, "displayed": displayed,
                "search_ms": round(elapsed * 1000, 2),
            })
    scored = [case for case in result["cases"] if case["expected_document"]]
    unknown = [case for case in result["cases"] if case.get("no_supported_fact")]
    result["summary"] = {
        "cases": len(result["cases"]), "cases_with_expected_source": len(scored),
        "expected_document_top1": sum(case["expected_document_rank"] == 1 for case in scored),
        "expected_document_top3": sum(0 < (case["expected_document_rank"] or 99) <= 3 for case in scored),
        "expected_document_any_delivered": sum(case["expected_document_rank"] is not None for case in scored),
        "expected_sentence_in_focus": sum(case["expected_sentence_in_focus"] for case in scored),
        "expected_sentence_in_context": sum(case["expected_sentence_in_context"] for case in scored),
        "no_supported_fact_cases": len(unknown),
        "no_supported_fact_with_not_determined": sum(case["answerability"] == "not_determined" for case in unknown),
        "fidelity_errors": sum(len(case["fidelity_errors"]) for case in result["cases"]),
        "search_median_ms": round(statistics.median(case["search_ms"] for case in result["cases"]), 2),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("hashing", "semantic", "both"), default="hashing")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error("--output already exists; select a new report path")
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="retrieval-evaluation-", dir=output.parent))
    data = load_cases()
    versions = {}
    for dependency in ("sentence-transformers", "torch", "transformers", "numpy"):
        try:
            versions[dependency] = importlib.metadata.version(dependency)
        except importlib.metadata.PackageNotFoundError:
            versions[dependency] = None
    report = {
        "schema_version": 1, "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "notice": data["notice"], "python": platform.python_version(), "platform": platform.platform(),
        "optional_dependency_versions": versions,
        "scratch_directory": str(scratch),
        "interpretation": "First-page discovery and terminal excerpt metrics. Context hit is not correct emphasis. Unknown-case not_determined is a metadata safety contract, not demonstrated abstention or reasoning. No Qdrant was used; semantic mode changes the document embedding model. No numerical quality threshold gates release.",
        "results": [],
    }
    for mode in ("hashing", "semantic") if args.mode == "both" else (args.mode,):
        report["results"].append(evaluate_mode(data, mode, scratch / mode))
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "results": [{"mode": item["mode"], **item["summary"]} for item in report["results"]]}, indent=2))
    return int(any(result["summary"]["fidelity_errors"] for result in report["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
