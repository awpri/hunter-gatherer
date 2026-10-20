"""Offline fictional onboarding data; never opens the configured user store."""
from __future__ import annotations

import hashlib
import json
import shlex
from importlib.resources import files
from pathlib import Path

from proharness.config import Settings
from proharness.memory.documents import DocumentArchive
from proharness.memory.embeddings import HashingEmbedder
from proharness.memory.schemas import Memory
from proharness.memory.store import MemoryStore

DEMO_SCHEMA = 1


def _local_settings(target: str | Path) -> Settings:
    """Construct local settings without reading environment variables or .env."""
    return Settings.model_construct(
        sqlite_path=str(Path(target).expanduser().resolve() / "demo.db"),
        vector_backend="memory", embed_model="hashing", local_only=True,
        openai_api_key="", anthropic_api_key="", messages_api_key="",
        qdrant_url="", qdrant_api_key="", library_maintenance_enabled=False,
        recall_reranker_enabled=False,
    )


def demo_settings(target: str | Path) -> Settings:
    """Validate a previously created demo and return environment-free settings."""
    target = Path(target).expanduser().resolve()
    try:
        manifest = json.loads((target / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("Not an initialized demo directory") from exc
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != DEMO_SCHEMA or manifest.get("fictional") is not True
            or manifest.get("embedding") != "hashing:384"
            or manifest.get("database") != str(target / "demo.db")
            or not (target / "demo.db").is_file() or (target / "demo.db").is_symlink()):
        raise ValueError("Invalid isolated demo manifest or database")
    return _local_settings(target)


def create_demo(target: str | Path) -> dict:
    """Create an isolated demo in an explicit absent or empty directory.

    Partial failures deliberately leave their files for inspection; a retry must
    use a new empty directory. Existing data is never replaced or cleaned up.
    """
    if not target or not str(target).strip():
        raise ValueError("An explicit demo target directory is required")
    requested = Path(target).expanduser()
    if requested.is_symlink():
        raise ValueError("Demo target must not be a symlink")
    target = requested.resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("Demo target must be absent or an empty directory")
    target.mkdir(parents=True, exist_ok=True)
    sources = target / "sources"
    sources.mkdir()
    entries = []
    with MemoryStore(_local_settings(target), embedder=HashingEmbedder()) as store:
        archive = DocumentArchive(store)
        for name, logical_name, date in (
            ("pilot-plan-v1.md", "pilot-plan.md", "2030-04-01"),
            ("pilot-plan-v2.md", "pilot-plan.md", "2030-04-08"),
            ("pilot-review.md", "pilot-review.md", "2030-04-15"),
        ):
            data = files("proharness").joinpath("demo_data", name).read_bytes()
            # Retain convenient human-readable copies of every input revision.
            (sources / name).write_bytes(data)
            logical = sources / logical_name
            logical.write_bytes(data)
            document = archive.ingest(str(logical), title="Fictional Lantern pilot " + name,
                                      author="Fictional Northstar Workshop", document_date=date,
                                      category="demo")
            entries.append({"input": str(sources / name), "sha256": hashlib.sha256(data).hexdigest(),
                            **document})
        owner_refs = []
        for entry in entries[:2]:
            chunk = store._sqlite.execute(
                "SELECT id FROM archive_chunks WHERE document_id=? AND text LIKE ? ORDER BY ordinal LIMIT 1",
                (entry["document_id"], "%pilot owner%"),
            ).fetchone()
            hit = archive.get_chunk(chunk[0])
            owner_refs.append([{key: hit[key] for key in ("document_id", "chunk_id", "quote")}])
        old = store.add(Memory(text="Fictional Lantern pilot owner is Mira Vale.",
                               tags=["demo", "fictional"], source="demo:pilot-plan-v1",
                               metadata={"evidence_refs": owner_refs[0], "valid_from": "2030-04-01"}, importance=5))
        new = store.supersede(old.id, Memory(text="Fictional Lantern pilot owner is Rowan Finch as of 2030-04-08.",
                               tags=["demo", "fictional"], source="demo:pilot-plan-v2",
                               metadata={"evidence_refs": owner_refs[1], "valid_from": "2030-04-08"}, importance=5))
    manifest = {
        "schema_version": DEMO_SCHEMA, "fictional": True,
        "database": str(target / "demo.db"), "embedding": "hashing:384",
        "documents": entries, "memories": {"original_owner": old.id, "current_owner": new.id},
        "questions": [
            {"question": "What is the Lantern pilot's weekly review schedule?", "expected": "Tuesday at 10:00 UTC; cite the plan."},
            {"question": "Who owns the Lantern pilot now, and who owned it before?", "expected": "Rowan Finch replaced Mira Vale on 2030-04-08; cite both versions."},
            {"question": "What measured percentage did the pilot improve completion time by?", "expected": "Unknown. No measured completion-time outcome is recorded; do not infer it from the target."},
        ],
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (target / "START-HERE.md").write_text(
        "# Fictional offline ProHarness demo\n\nAll people, organizations and events are invented.\n"
        "See manifest.json for source IDs, preserved originals and practice questions.\n"
        f"Restart: `proharness demo serve {shlex.quote(str(target))} --port 8767`\n"
        "Connect an MCP client to http://127.0.0.1:8767/mcp . Ordinary startup reads user\n"
        "configuration and is not the isolated demo launcher. See docs/demo.md.\n"
        "Hashing embeddings exercise evidence workflows; they are not a semantic quality benchmark.\n"
    )
    return manifest
