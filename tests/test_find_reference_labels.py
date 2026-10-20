"""Public omitted-source labels enrich navigation without competing for evidence."""
from copy import deepcopy

import pytest

from proharness.memory.evidence_bounded import bounded_evidence_packet
from proharness.memory.output_budget import output_bytes


def packet():
    return {"include_sensitive": False, "supporting_passages": [
        {"citation": "[doc:d#c]", "document_id": "d", "chunk_id": "c",
         "quote": "Exact source body.", "title": "日本語😀" * 100,
         "location": {"page": 7, "section": "Section 日本語" * 30, "start": 1, "end": 19},
         "version": 1, "is_latest": False, "latest_version": 2,
         "warnings": ["Older version"], "sensitive": False},
        {"citation": "[doc:e#f]", "document_id": "e", "chunk_id": "f",
         "quote": "Useful retained evidence."}], "relevant_memories": [
        {"citation": "[mem:m]", "memory_id": "m", "text": "A remembered project: " + "exact detail " * 50,
         "provenance": {"sources": []}, "known_details": {"private_extra": "DO NOT LABEL"}}]}


def project(source, tokens=4096, **kwargs):
    return bounded_evidence_packet(source, tokens, public_followups=True,
                                   compact_public=True,
                                   reference_only=frozenset({"[doc:d#c]", "[mem:m]"}), **kwargs)


def test_bounded_verbatim_labels_preserve_identity_privacy_and_packet():
    source = packet()
    before = deepcopy(source)
    result = project(source)
    refs = {ref["citation"]: ref for ref in result["omitted_references"]}
    doc = refs["[doc:d#c]"]
    assert doc["navigation_labels"] == {
        "title": source["supporting_passages"][0]["title"][:160],
        "title_truncated": True, "section": source["supporting_passages"][0]["location"]["section"][:120],
        "section_truncated": True, "page": 7}
    assert doc["document_id"] == "d" and doc["chunk_id"] == "c"
    assert doc["version"] == 1 and doc["latest_version"] == 2
    assert doc["warnings"] == ["Older version"]
    for ref in refs.values():
        assert ref["followups"][0]["arguments"]["refs"] == [ref["citation"]]
        assert ref["followups"][0]["arguments"]["include_sensitive"] is False
    memory = refs["[mem:m]"]
    assert memory["navigation_labels"]["text_prefix"] == source["relevant_memories"][0]["text"][:120]
    assert memory["navigation_labels"]["text_prefix_truncated"] is True
    assert "DO NOT LABEL" not in str(result)
    assert source == before
    assert result["supporting_passages"][0]["quote"] == "Useful retained evidence."


@pytest.mark.parametrize("tokens", [512, 1024, 2048, 4096])
def test_labels_do_not_displace_core_or_change_continuation(tokens, monkeypatch):
    source = packet()
    framing = {"continuation": {"next_cursor": "exact-opaque-cursor", "has_more": True}}
    labeled = project(source, tokens, envelope=framing)
    monkeypatch.setattr("proharness.memory.evidence_bounded._reference_labels", lambda *_: {})
    unlabeled = project(source, tokens, envelope=framing)
    stripped = deepcopy(labeled)
    for ref in stripped["omitted_references"]:
        ref.pop("navigation_labels", None)
    stripped.pop("budget")
    unlabeled.pop("budget")
    assert stripped == unlabeled
    assert labeled["continuation"] == framing["continuation"]
    assert output_bytes(labeled) == labeled["budget"]["serialized_bytes"] <= tokens * 4


def test_labels_are_public_only_and_never_borrow_excluded_candidate_data():
    source = packet()
    legacy = bounded_evidence_packet(source, reference_only=frozenset({"[doc:d#c]", "[mem:m]"}))
    assert all("navigation_labels" not in ref for ref in legacy["omitted_references"])
    source["supporting_passages"][0].pop("title")
    source["supporting_passages"][0].pop("location")
    result = project(source)
    doc = next(ref for ref in result["omitted_references"] if ref["citation"] == "[doc:d#c]")
    assert "navigation_labels" not in doc


def test_near_cap_labels_take_priority_over_accounted_optional_metadata(monkeypatch):
    source = packet()
    source["supporting_passages"][0]["title"] = "Family research notes"
    source["supporting_passages"][0]["location"]["section"] = "Timeline"
    source["relevant_memories"][0]["text"] = "Marathon training plan for 2027"
    source["relevant_memories"].append({
        "citation": "[mem:retained]", "memory_id": "retained", "text": "Required retained claim.",
        "provenance": {"sources": []},
        "known_details": {str(i): "Optional detail " * 3 for i in range(200)}})
    source["requirement_checks"] = [{
        "query": "Required claim", "selected_citations": ["[mem:retained]"],
        "retrieval_assessment": {"status": "review_required", "missing_query_literals": ["unknown name"]}}]
    source["missing_information"] = [{"field": "name", "question": "Unknown name"}]
    framing = {"continuation": {"next_cursor": "exact-cursor", "has_more": True}}
    labeled = project(source, 2048, envelope=framing)
    monkeypatch.setattr("proharness.memory.evidence_bounded._reference_labels", lambda *_: {})
    unlabeled = project(source, 2048, envelope=framing)
    retained = next(item for item in unlabeled["relevant_memories"]
                    if item["citation"] == "[mem:retained]")
    original_retained = next(item for item in source["relevant_memories"]
                             if item["citation"] == "[mem:retained]")
    assert 0 < len(retained["known_details"]) < len(original_retained["known_details"])
    assert output_bytes(labeled) <= 8192
    assert all(ref.get("navigation_labels") for ref in labeled["omitted_references"])
    assert labeled["continuation"] == unlabeled["continuation"]
    assert labeled["requirement_checks"] == unlabeled["requirement_checks"]
    assert labeled["missing_information"] == unlabeled["missing_information"]
    assert labeled["supporting_passages"] == unlabeled["supporting_passages"]
    assert [(m["citation"], m["text"]) for m in labeled["relevant_memories"]] == [
        (m["citation"], m["text"]) for m in unlabeled["relevant_memories"]]
    assert [{k: v for k, v in ref.items() if k != "navigation_labels"}
            for ref in labeled["omitted_references"]] == unlabeled["omitted_references"]
    current = labeled["relevant_memories"][0]
    baseline = unlabeled["relevant_memories"][0]
    assert len(current["known_details"]) < len(baseline["known_details"])
    assert current["omitted_metadata_fields"] - baseline["omitted_metadata_fields"] == (
        len(baseline["known_details"]) - len(current["known_details"]))


def test_labels_precede_repeated_diagnostics_without_losing_gap_or_requirement_refs(monkeypatch):
    source = packet()
    source["supporting_passages"][0]["title"] = "Family notes"
    source["supporting_passages"][0]["location"] = {"page": 7}
    source["relevant_memories"][0]["text"] = "Marathon plan"
    candidates = [f"[mem:diagnostic-{i:04d}]" for i in range(150)]
    source["retrieval_assessment"] = {
        "status": "review_required", "candidate_citations": candidates,
        "matched_query_terms": ["shared_name"] * 100,
        "missing_query_literals": ["grandmother"], "recommended_actions": ["Review sources"] * 100}
    source["requirement_checks"] = [{
        "selected_citations": ["[doc:e#f]"],
        "retrieval_assessment": {"candidate_citations": ["[doc:e#f]"],
                                 "missing_query_literals": ["name"]}}]
    labeled = project(source, 2048)
    monkeypatch.setattr("proharness.memory.evidence_bounded._reference_labels", lambda *_: {})
    baseline = project(source, 2048)
    assert all(ref.get("navigation_labels") for ref in labeled["omitted_references"])
    assert labeled["supporting_passages"] == baseline["supporting_passages"]
    assert labeled["requirement_checks"] == baseline["requirement_checks"]
    assert labeled["retrieval_assessment"]["missing_query_literals"] == ["grandmother"]
    assert labeled["requirement_checks"][0]["selected_citations"] == ["[doc:e#f]"]
    assessment = labeled["retrieval_assessment"]
    for key in ("candidate_citations", "matched_query_terms", "recommended_actions"):
        assert len(assessment.get(key, [])) + assessment["omitted_list_counts"][key] == len(
            source["retrieval_assessment"][key])
    assert sum(len(assessment.get(key, [])) for key in (
        "candidate_citations", "matched_query_terms", "recommended_actions")) < sum(
            len(baseline["retrieval_assessment"].get(key, [])) for key in (
                "candidate_citations", "matched_query_terms", "recommended_actions"))
