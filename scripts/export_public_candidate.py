#!/usr/bin/env python3
"""Prepare a bounded release source candidate, never publish or export history.

Dry run prints a hashed manifest. --destination copies only the manifest into a
new or empty outside-repository directory after bounded hygiene triage passes.
Only explicitly reviewed synthetic tests and public docs are included.
Evaluation artifacts, historical reports and local configuration are excluded.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

from check_publication_hygiene import MAX_FILE, MAX_TOTAL, PATTERNS

ROOT = Path(__file__).resolve().parents[1]

# Finite review boundary: no recursive tests/docs/evaluation inclusion.
PUBLIC_TESTS = (
    "tests/conftest.py", "tests/test_config_env.py", "tests/test_memory_store.py",
    "tests/test_backup.py", "tests/test_demo.py", "tests/test_demo_cli.py",
    "tests/test_public_candidate.py", "tests/test_find_reference_labels.py",
    "tests/test_find_focused_delivery.py",
    "tests/test_import_index_readiness.py",
    # Synthetic product contracts: six tools, continuation, API/privacy, exact sources.
    "tests/test_simple_library_workflow.py", "tests/test_simple_search.py",
    "tests/test_ask_cited_context.py",
    "tests/test_find_continuation.py", "tests/test_library_api.py",
    "tests/test_library_sources_collections.py", "tests/test_collection_source_privacy.py",
    "tests/test_legacy_security_boundary.py",
    "tests/test_internal_recall_privacy.py", "tests/test_navigation_source_info.py",
    "tests/test_navigation_source_info_interfaces.py", "tests/test_reference_mcp.py",
    # Synthetic PDF fixtures and shared helpers; real render tests require document-view.
    "tests/test_document_page_image.py", "tests/test_document_page_image_transport.py",
    "tests/test_page_image_fit_backend.py", "tests/test_page_image_fit_interfaces.py",
    "tests/test_page_image_fit_review.py", "tests/test_page_image_fit_worker.py",
    "tests/test_original_page_mcp_workflow.py", "tests/test_visual_regions.py",
    "tests/test_original_page_collections.py",
    "tests/test_secure_files.py",
    "tests/test_optional_dependencies.py", "tests/test_setup.py", "tests/test_terminal.py",
    "tests/test_namespace_compatibility.py", "tests/test_personal_library.py", "tests/test_release_retrieval.py",
)
PUBLIC_FIXTURES = ("tests/fixtures/find_subject_v1.json", "tests/fixtures/release_retrieval/cases.json")
PUBLIC_DOCS = (
    "docs/public-release-guide.md", "docs/release-provenance-20261014.md", "docs/visual-regions.md",
    "docs/index.md", "docs/getting-started.md", "docs/usage.md", "docs/mcp.md",
    "docs/cli.md", "docs/retrieval-evaluation-v0.1.0a1.md", "CHANGELOG.md",
    "docs/assets/alpha-library.png",
    "docs/faq.md", "docs/roadmap.md", "docs/assets/wordmark.svg", "CONTRIBUTING.md", "SECURITY.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml", ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml", ".github/pull_request_template.md",
)


def export_bytes(name):
    """Preserve reviewed source bytes, including the public repository README."""
    return (ROOT / name).read_bytes()


def selected_paths():
    selected = set(PUBLIC_TESTS + PUBLIC_DOCS + PUBLIC_FIXTURES)
    for path in (ROOT / "hunter_gatherer").rglob("*.py"):
        relative = path.relative_to(ROOT)
        if "eval" not in relative.parts and "__pycache__" not in relative.parts:
            selected.add(relative.as_posix())
    for tree in ("frontend/src", "frontend/public"):
        for path in (ROOT / tree).rglob("*"):
            if path.is_file():
                selected.add(path.relative_to(ROOT).as_posix())
    selected.update([
        "README.md", "pyproject.toml", ".env.example", ".gitignore",
        "frontend/index.html", "frontend/package.json", "frontend/package-lock.json",
        "frontend/tsconfig.json", "frontend/vite.config.ts", "frontend/postcss.config.js",
        "frontend/tailwind.config.js", "hunter_gatherer/coordination/README.md",
        "hunter_gatherer/skills/examples/research/SKILL.md",
        "scripts/check_publication_hygiene.py", "scripts/export_public_candidate.py",
        "scripts/smoke_packaged_demo.py", "scripts/smoke_personal_library.py", "scripts/evaluate_release_retrieval.py",
        ".github/workflows/packaged-demo.yml", "docs/demo.md",
    ])
    selected.update(path.relative_to(ROOT).as_posix()
                    for path in (ROOT / "hunter_gatherer/demo_data").glob("*.md"))
    # Include only directly referenced static assets plus explicit third-party notices.
    for tree in ("hunter_gatherer/server/static", "hunter_gatherer/server/library_static"):
        directory = ROOT / tree
        index = directory / "index.html"
        selected.add(index.relative_to(ROOT).as_posix())
        for reference in re.findall(r'(?:src|href)=["\']/?([^"\']+)["\']', index.read_text()):
            if ":" in reference or reference.startswith("//"):
                raise ValueError("external asset reference requires review")
            relative = Path(tree) / reference
            if ".." in relative.parts:
                raise ValueError("unsafe asset reference")
            selected.add(relative.as_posix())
        for name in ("fonts/Geist-Variable.woff2", "fonts/GeistMono-Variable.woff2", "fonts/OFL.txt", "licenses/Lucide-LICENSE.txt", "brand/hunter-gatherer-lockup.svg", "brand/hg-compact.svg"):
            if (directory / name).exists():
                selected.add((Path(tree) / name).as_posix())
    for notice in ("LICENSE", "NOTICE"):
        if (ROOT / notice).is_file():
            selected.add(notice)
    return sorted(selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    entries = []
    categories = {}
    total = 0
    try:
        for name in selected_paths():
            path = ROOT / name
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):
                raise ValueError("selected file missing or unsafe")
            data = export_bytes(name)
            size = len(data)
            total += size
            if size > MAX_FILE or total > MAX_TOTAL:
                raise ValueError("bounded review budget exceeded")
            for category, pattern in PATTERNS.items():
                if re.search(pattern, data):
                    categories[category] = categories.get(category, 0) + 1
            entries.append({"path": name, "bytes": size, "sha256": hashlib.sha256(data).hexdigest()})
        manifest = {
            "purpose": "release source candidate; validation recorded separately",
            "excluded": ["Git history", "agent state", "local MCP configuration", "unreviewed tests", "eval artifacts", "historical docs"],
            "validation_required": ["exact exported artifact build/install/restore verification", "cross-platform matrix execution", "publication contents and history review"],
            "readme_source": "README.md",
            "reviewed_tests": list(PUBLIC_TESTS),
            "license_file_present": (ROOT / "LICENSE").is_file(),
            "hygiene_review_categories": categories,
            "files": entries,
        }
        if args.destination:
            if categories:
                raise ValueError("hygiene findings require review; no export created")
            destination = args.destination.expanduser().absolute()
            if destination.is_symlink() or destination.resolve().is_relative_to(ROOT):
                raise ValueError("destination must be outside repository")
            if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
                raise ValueError("destination must be new or empty")
            # Recheck bytes against manifest during copying; do not publish or delete.
            destination.mkdir(parents=True, exist_ok=True)
            for entry in entries:
                data = export_bytes(entry["path"])
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise ValueError("source changed during export; partial candidate requires review")
                target = destination / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            (destination / "PUBLIC_CANDIDATE_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
            print(json.dumps({"files": len(entries), "bytes": total, "status": manifest["purpose"]}))
        else:
            print(json.dumps(manifest, indent=2))
        return 1 if categories else 0
    except (OSError, ValueError) as error:
        print("Candidate preparation incomplete:", str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
