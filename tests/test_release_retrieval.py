"""Guard the release evaluation's data contracts, not its retrieval scores."""
import importlib.util
from pathlib import Path


def evaluation_module():
    path = Path(__file__).resolve().parents[1] / "scripts/evaluate_release_retrieval.py"
    spec = importlib.util.spec_from_file_location("release_evaluation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_cases_have_exact_source_sentences_and_version_pair():
    data = evaluation_module().load_cases()
    assert len(data["cases"]) == 12
    assert {case["category"] for case in data["cases"]} >= {"literal", "paraphrase", "negation", "qualification", "versions", "missing"}
    by_id = {document["id"]: document for document in data["documents"]}
    assert by_id["cedar-v1"]["path"] == by_id["cedar-v2"]["path"]
    assert by_id["cedar-v1"]["text"] != by_id["cedar-v2"]["text"]
    assert any(case["expected_document"] is None for case in data["cases"])


def test_release_evaluation_uses_isolated_settings_and_exact_reopen(tmp_path, monkeypatch):
    from hunter_gatherer.config import Settings

    def forbid_settings(*args, **kwargs):
        raise AssertionError("Evaluation loaded ambient settings")

    monkeypatch.setattr(Settings, "__init__", forbid_settings)
    module = evaluation_module()
    result = module.evaluate_mode(module.load_cases(), "hashing", tmp_path / "evaluation")
    assert result["summary"]["fidelity_errors"] == 0
    assert result["summary"]["cases"] == 12
    assert all(case["answerability"] == "not_determined" for case in result["cases"])
    assert result["qdrant"] is False
