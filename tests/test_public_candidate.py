"""Exporter integrity and boundary checks use only synthetic temporary files."""
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def exporter(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("public_export_test", REPO / "scripts/export_public_candidate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selected_boundary_and_local_markdown_links(exporter):
    selected = set(exporter.selected_paths())
    assert set(exporter.PUBLIC_TESTS) <= selected
    assert {"LICENSE", "NOTICE", "docs/public-release-guide.md"} <= selected
    assert not any("eval" in Path(name).parts for name in selected)
    assert not any(".git" in Path(name).parts for name in selected)
    for name in selected:
        if not name.endswith(".md"):
            continue
        content = exporter.export_bytes(name).decode()
        targets = re.findall(r"\]\(([^)]+)\)", content)
        targets += re.findall(r'(?:src|href)=["\x27]([^"\x27]+)["\x27]', content)
        for target in targets:
            if ":" in target or target.startswith("#"):
                continue
            relative = str(Path(name).parent / target.split("#")[0])
            normalized = (REPO / relative).resolve().relative_to(REPO).as_posix()
            assert normalized in selected or (
                (REPO / normalized).is_dir()
                and any(path.startswith(normalized.rstrip("/") + "/") for path in selected)
            ), (name, target)


def test_manifest_hashes_actual_readme_and_refuses_reuse(exporter, monkeypatch, tmp_path, capsys):
    root = tmp_path / "source"
    root.mkdir()
    (root / "README.md").write_bytes(b"Public project overview\n[License](LICENSE)")
    (root / "docs").mkdir()
    (root / "docs/public-release-guide.md").write_text("[License](../LICENSE)")
    (root / "LICENSE").write_text("Synthetic license marker")
    monkeypatch.setattr(exporter, "ROOT", root)
    monkeypatch.setattr(exporter, "selected_paths", lambda: ["README.md", "LICENSE"])
    destination = tmp_path / "candidate"
    monkeypatch.setattr(sys, "argv", ["export", "--destination", str(destination)])
    assert exporter.main() == 0
    readme = (destination / "README.md").read_bytes()
    assert readme == b"Public project overview\n[License](LICENSE)"
    manifest = json.loads((destination / "PUBLIC_CANDIDATE_MANIFEST.json").read_text())
    assert manifest["readme_source"] == "README.md"
    entry = next(e for e in manifest["files"] if e["path"] == "README.md")
    assert entry["bytes"] == len(readme)
    assert entry["sha256"] == hashlib.sha256(readme).hexdigest()
    assert exporter.main() == 2
    assert (destination / "README.md").read_bytes() == readme
    capsys.readouterr()


def test_export_rejects_selected_symlink_and_pattern_findings(exporter, monkeypatch, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("safe synthetic bytes")
    (root / "selected.txt").symlink_to(outside)
    monkeypatch.setattr(exporter, "ROOT", root)
    monkeypatch.setattr(exporter, "selected_paths", lambda: ["selected.txt"])
    destination = tmp_path / "candidate"
    monkeypatch.setattr(sys, "argv", ["export", "--destination", str(destination)])
    assert exporter.main() == 2
    assert not destination.exists()
    (root / "selected.txt").unlink()
    # Construct a scanner pattern without placing a home-directory literal in source.
    (root / "selected.txt").write_text("/" + "Users" + "/synthetic-review")
    assert exporter.main() == 2
    assert not destination.exists()


def test_product_contract_suites_are_in_review_boundary(exporter):
    selected = set(exporter.PUBLIC_TESTS)
    assert {
        "tests/test_simple_library_workflow.py", "tests/test_find_continuation.py",
        "tests/test_library_api.py", "tests/test_collection_source_privacy.py",
        "tests/test_legacy_security_boundary.py", "tests/test_visual_regions.py",
        "tests/test_internal_recall_privacy.py", "tests/test_navigation_source_info.py",
        "tests/test_document_page_image.py", "tests/test_document_page_image_transport.py",
        "tests/test_page_image_fit_backend.py", "tests/test_original_page_mcp_workflow.py",
        "tests/test_secure_files.py",
    } <= selected
