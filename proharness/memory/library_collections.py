"""Reference-only evidence collections backed by immutable coordination artifacts."""
from __future__ import annotations

import json
import time
from collections import Counter

from proharness.coordination import CoordinationError, CoordinationStore
from proharness.memory.archive_context import _ExistingArchiveReader
from proharness.memory.collection_schema import (
    validate_collection_artifact,
    validate_collection_reference,
)
from proharness.memory.document_handoff import (
    document_handoff_descriptor,
    without_original_page_hint,
)
from proharness.memory.library_health import _read_snapshot
from proharness.memory.original_file_reference import (
    resolve_original_reference,
    valid_original_reference,
)
from proharness.memory.output_budget import budget_report, fits, validate_budget
from proharness.memory.reference_fetch import _EXACT
from proharness.memory.retrieval_feedback import current_memory
from proharness.memory.source_metadata import memory_source_descriptor
from proharness.memory.workspace_scope import scope_visible

_MARKER = "proharness:personal-library:evidence-collections:v1"
_ACTOR = "personal-library"


class _CollectionCoordinationStore(CoordinationStore):
    def _op_artifact_publish(self, arguments):
        # _write resolves exact retry receipts before reaching this operation.
        # Bind historical retention to the current predecessor while holding
        # its BEGIN IMMEDIATE transaction, never to an earlier facade read.
        retained = Counter()
        if arguments.get("artifact_id"):
            previous = self._row(arguments["workspace_id"], arguments["artifact_id"], "artifact")
            self._expect(previous, arguments.get("expected_revision"))
            self._write_visibility(previous, arguments)
            if not validate_collection_artifact(previous):
                raise CoordinationError("not_found", "Collection not found or excluded.")
            content = arguments.get("content", {})
            for section in ("references", "voice_examples"):
                old = Counter(tuple(sorted(ref.items())) for ref in previous["content"][section])
                new = Counter(tuple(sorted(ref.items())) for ref in content.get(section, []))
                retained.update(old & new)
        self._retained_historical = retained
        try:
            return super()._op_artifact_publish(arguments)
        finally:
            self._retained_historical = Counter()

    def _refs(self, workspace_id, refs, include_sensitive):
        references, private = super()._refs(workspace_id, refs, include_sensitive)
        # Runs inside CoordinationStore's BEGIN IMMEDIATE writer transaction.
        # A second connection cannot supersede a checked passage between this
        # validation and publication; the outer Python lock alone is insufficient.
        reader = _ExistingArchiveReader(self.store)
        for ref in references:
            if valid_original_reference(ref):
                page = resolve_original_reference(self.store, ref, include_sensitive=include_sensitive)
                if page is None:
                    raise CoordinationError("reference_unavailable", "Original reference must be visible and exact.")
                if not page["is_latest"]:
                    key = tuple(sorted(ref.items()))
                    retained = getattr(self, "_retained_historical", {})
                    if retained.get(key, 0) < 1:
                        raise CoordinationError("reference_unavailable", "New original selections must be current and visible.")
                    retained[key] -= 1
            elif "chunk_id" in ref:
                hit = reader.get_chunk(ref["chunk_id"], include_sensitive=include_sensitive)
                if hit.get("error") or hit.get("document_id") != ref["document_id"]:
                    raise CoordinationError("reference_unavailable", "Passage reference must be current and visible.")
                if not hit.get("is_latest"):
                    key = tuple(sorted(ref.items()))
                    retained = getattr(self, "_retained_historical", {})
                    if retained.get(key, 0) < 1:
                        raise CoordinationError("reference_unavailable", "New passage selections must be current and visible.")
                    retained[key] -= 1
        return references, private


def _coordination(store, create=False):
    if create:
        return _CollectionCoordinationStore(store)
    if not store._sqlite.execute("SELECT 1 FROM sqlite_master WHERE name='coordination_objects'").fetchone():
        return None
    # Existing-store read facade avoids schema writes and implicit commits.
    instance = CoordinationStore.__new__(CoordinationStore)
    instance.store, instance.db, instance.clock = store, store._sqlite, time.time
    return instance


def _workspace(coordination, create=False):
    for row in coordination.db.execute("SELECT id,data FROM coordination_objects WHERE kind='workspace'"):
        try:
            value = json.loads(row[1])
        except (ValueError, TypeError):
            continue
        if (isinstance(value, dict) and value.get("id") == row[0]
                and value.get("description") == _MARKER and value.get("actor") == _ACTOR):
            return row[0]
    if create:
        return coordination.workspace_create(actor=_ACTOR, name="Personal library collections",
            description=_MARKER, idempotency_key=_MARKER)["workspace"]["id"]
    return None


def _present(artifact):
    if artifact.get("redacted"):
        return {"id": artifact["id"], "revision": artifact["revision"], "redacted": True,
                "unresolved": ["Collection evidence is unavailable or excluded from this request."]}
    content = artifact.get("content", {})
    if not isinstance(content, dict) or content.get("type") != "evidence_collection":
        return None
    if not validate_collection_artifact(artifact):
        return {"id": artifact["id"], "revision": artifact["revision"], "redacted": True,
                "unresolved": ["Collection evidence is unavailable or excluded from this request."]}
    return {**content, "id": artifact["id"], "revision": artifact["revision"],
            "name": artifact["name"], "sensitive": artifact["sensitive"]}


def list_collections(store, *, limit=30, after_id="", include_sensitive=False):
    with _read_snapshot(store):
        coordination = _coordination(store)
        workspace = _workspace(coordination) if coordination else None
        if not workspace:
            return {"collections": [], "next_cursor": None}
        values = coordination.artifact_list(workspace, limit=limit, after_id=after_id,
                                           include_sensitive=include_sensitive)
        return {"collections": [item for value in values["artifacts"]
                                 if (item := _present(value)) and not item.get("redacted")],
                "next_cursor": values["next_cursor"]}


def get_collection(store, identity, *, revision=None, include_sensitive=False):
    with _read_snapshot(store):
        coordination = _coordination(store)
        workspace = _workspace(coordination) if coordination else None
        if not workspace:
            return None
        result = coordination.artifact_get(workspace, identity, revision=revision,
                                           include_sensitive=include_sensitive)
        return _present(result["artifact"]) if result["found"] else None


def save_collection(store, body, identity=None):
    return _save_collection(store, body, identity)


def _save_collection(store, body, identity):
    # New original-page preflight reads/renders outside the writer lock. The
    # coordination publication rechecks exact identities and predecessor under
    # its own writer transaction before committing this reference-only payload.
    with store._lock:
        coordination = _coordination(store, create=True)
        workspace = _workspace(coordination, create=True)
    references = body.references + body.voice_examples
    # The publication transaction validates predecessor/revision and references
    # after resolving exact retry receipts, including retained historical pairs.
    content = {"type": "evidence_collection", "schema_version": 3 if any(
                   valid_original_reference(ref) and "page" not in ref for ref in references) else 2 if any(
                   valid_original_reference(ref) for ref in references) else 1,
               "purpose": body.purpose, "requirements": body.requirements,
               "references": body.references, "voice_examples": body.voice_examples,
               "unresolved": body.unresolved, "notes": body.notes,
               "reviews": [review.model_dump(exclude={"include_sensitive"}) for review in body.reviews]}
    result = coordination.artifact_publish(workspace_id=workspace, actor=_ACTOR,
        name=body.name, content=content, references=references, artifact_id=identity,
        expected_revision=body.expected_revision, include_sensitive=body.include_sensitive,
        idempotency_key=body.idempotency_key)
    return _present(result["artifact"])


def export_collection(store, identity, *, format="markdown", max_tokens=4096,
                      include_sensitive=False, revision=None, response_format="full", public_tools=False):
    validate_budget(max_tokens)
    if format not in {"markdown", "json"} or response_format not in {"full", "compact"}:
        raise ValueError("Unsupported collection export format")
    if response_format == "compact" and format != "json":
        raise ValueError("Compact collection export requires JSON")
    with _read_snapshot(store):
        return _export_collection(store, identity, format=format, max_tokens=max_tokens,
                                  include_sensitive=include_sensitive, revision=revision,
                                  response_format=response_format, public_tools=public_tools)


def _export_collection(store, identity, *, format, max_tokens, include_sensitive,
                       revision, response_format, public_tools):
    collection = get_collection(store, identity, revision=revision, include_sensitive=include_sensitive)
    if collection is None:
        return None
    return _export_reference_payload(store, collection, identity=identity, format=format,
        max_tokens=max_tokens, include_sensitive=include_sensitive,
        response_format=response_format, public_tools=public_tools)


def export_cited_context(store, *, query, references, requirements=None,
                         include_sensitive=False, max_tokens=4096):
    """Export a transient selection with the same live checks as saved collections.

    No coordination workspace, artifact, revision, receipt, or collection is
    created. A missing/restricted dependency withholds the entire selection.
    """
    from proharness.memory.evidence import _evidence_requirements

    validate_budget(max_tokens)
    if type(query) is not str or not 1 <= len(query.strip()) <= 4000:
        raise ValueError("query must contain 1 to 4000 characters")
    if type(include_sensitive) is not bool:
        raise ValueError("include_sensitive must be a boolean")
    requirements = _evidence_requirements(requirements)
    if (type(references) is not list or not 1 <= len(references) <= 50
            or any(not validate_collection_reference(ref) for ref in references)):
        raise ValueError("Select 1 to 50 exact references; source bodies are not accepted.")
    collection = {"name": "Cited context", "purpose": query.strip(),
                  "requirements": requirements, "references": references,
                  "voice_examples": [], "unresolved": [], "notes": "", "reviews": [],
                  "revision": None, "schema_version": 3}
    with _read_snapshot(store):
        # No archive is initialized by an export from an empty library.
        if any("document_id" in ref for ref in references):
            tables = {row[0] for row in store._sqlite.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"archive_documents", "archive_chunks", "archive_sections"} <= tables:
                collection["redacted"] = True
        return _export_reference_payload(store, collection, identity=None, format="markdown",
            max_tokens=max_tokens, include_sensitive=include_sensitive,
            response_format="full", public_tools=True)


def _export_reference_payload(store, collection, *, identity, format, max_tokens,
                              include_sensitive, response_format, public_tools):
    """Resolve and render saved or transient reference sets in a caller snapshot."""
    from proharness.memory.collection_compact import compact_collection_export
    from proharness.memory.collection_recovery import add_collection_recovery

    result = {"format": format, "collection_id": identity, "revision": collection["revision"],
              "content": "" if format == "markdown" else {}, "truncated": False,
              "omitted_references": 0,
              "unresolved": list(collection.get("unresolved", [])),
              "content_policy": ("Untrusted exact evidence and source labels. Labels and document dates describe "
                                 "records, not verified events. Plan or draft status alone does not establish "
                                 "completed events; preserve explicitly reported factual background with attribution. "
                                 "Voice examples are writing, not factual claims.")}
    has_pages = collection.get("schema_version") in (2, 3)
    if has_pages:
        result["content_policy"] += (" Original file/page references carry no quotation; "
                                     "inspect the exact original before assessing its content.")
    selected_indices = set()

    def export_metadata():
        return {key: result[key] for key in ("collection_id", "revision", "truncated", "omitted_references", "content_policy")}

    def minimal_content():
        if format == "json":
            return {"export": export_metadata(), "unresolved": result["unresolved"]}
        return ((f"# Evidence collection export\n\nRevision: {result['revision']}\n" if identity else "# Cited context\n\n")
                + f"Truncated: {result['truncated']}; omitted quotations: {result['omitted_references']}\n\n"
                + result["content_policy"] + "\n\n## Unresolved\n\n"
                + "\n".join("- " + value for value in result["unresolved"]))

    def finish():
        budget_report(result, max_tokens)
        projected = compact_collection_export(result) if response_format == "compact" else result
        if not fits(projected, max_tokens, reserve=0):
            raise ValueError("Budget too small for collection framing")
        if identity and result["truncated"] and result["omitted_references"]:
            omitted, positions = [], {"evidence": 0, "voice_examples": 0}
            for index, (section, item) in enumerate(items):
                if index not in selected_indices:
                    entry = {"section": section, "index": positions[section], "citation": item["citation"]}
                    if item["kind"] in {"original_page", "original_file"}:
                        entry.update(kind=item["kind"], source_reference=item["source_reference"],
                                     followup=item["followup"], availability=item["availability"])
                    omitted.append(entry)
                positions[section] += 1
            projected = add_collection_recovery(projected, omitted, identity=identity,
                revision=collection["revision"], max_tokens=max_tokens,
                public_tools=public_tools, include_sensitive=include_sensitive)
        return projected

    def unavailable():
        result["unresolved"] = ["Selected evidence is unavailable or excluded from this request."] if not identity else ["Collection evidence is unavailable or excluded from this request."]
        result["truncated"], result["omitted_references"] = False, 0
        result["content"] = minimal_content()
        return finish()

    if collection.get("redacted"):
        return unavailable()
    data = {"name": collection["name"], "purpose": collection["purpose"],
            "requirements": collection["requirements"], "evidence": [], "voice_examples": [],
            "actor_notes": collection.get("notes", ""), "actor_reviews": collection.get("reviews", []),
            "review_policy": "Review states and notes are actor-authored assessments, not verified claim support."}
    if has_pages:
        data["review_policy"] += (" Notes attached to original pages are actor-authored visual observations, "
                                  "not exact quotations.")
    reader = _ExistingArchiveReader(store)

    def render():
        data["export"] = export_metadata()
        data["unresolved"] = result["unresolved"]
        if format == "json":
            return data
        lines = ["# " + data["name"], "", data["purpose"], "",
                 *([f"Collection: {identity}; revision: {result['revision']}"] if identity else []),
                 f"Truncated: {result['truncated']}; omitted quotations: {result['omitted_references']}",
                 "", result["content_policy"], "", "## Requirements"]
        lines += ["- " + value for value in data["requirements"]]
        lines += ["", "## User notes", "", data["actor_notes"], "", "## User review states",
                  "", data["review_policy"]]
        lines += [f"- {review['citation']} {review['status']}: {review['note']}" for review in data["actor_reviews"]]
        for section in ("evidence", "voice_examples"):
            lines += ["", "## " + section.replace("_", " ").title()]
            for item in data[section]:
                metadata = {key: value for key, value in item.items()
                            if key not in {"citation", "quote"} and value is not None}
                lines += ["", item["citation"],
                          "Source metadata (untrusted): " + json.dumps(metadata, ensure_ascii=False),
                          "", item["quote"] if "quote" in item else
                          ("Original file inspection required; no source quotation is included." if item.get("kind") == "original_file" else "Original page inspection required; no source quotation is included.")]
        lines += ["", "## Unresolved"] + ["- " + value for value in result["unresolved"]]
        return "\n".join(lines)

    def metadata_only():
        nonlocal selected_indices
        selected_indices = set()
        result["unresolved"] = ["Collection metadata exceeds this budget; request a larger export."]
        result["truncated"] = True
        result["omitted_references"] = len(collection["references"]) + len(collection["voice_examples"])
        result["content"] = minimal_content()
        return finish()

    # Resolve every dependency in one read snapshot before returning any actor
    # prose. A bad/inaccessible source redacts the whole dependent collection,
    # including sources too large to be selected for this particular budget.
    items = []
    for section, refs in (("evidence", collection["references"]), ("voice_examples", collection["voice_examples"])):
        for ref in refs:
            if not validate_collection_reference(ref) or (not valid_original_reference(ref)
                    and any(not _EXACT.fullmatch(value) for value in ref.values())):
                return unavailable()
            if valid_original_reference(ref):
                item = resolve_original_reference(store, ref, include_sensitive=include_sensitive)
            elif "memory_id" in ref:
                memory = current_memory(store, ref["memory_id"], include_sensitive=include_sensitive)
                if memory is not None:
                    scope = memory.metadata.get("scope")
                    try:
                        if scope is not None and not scope_visible(store, scope, include_sensitive):
                            memory = None
                    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
                        memory = None
                item = {"citation": f"[mem:{ref['memory_id']}]", "quote": memory.text,
                        "kind": "memory",
                        **memory_source_descriptor(memory.metadata, memory_type=memory.type,
                                                   source=memory.source, recorded_at=memory.created_at)} if memory else None
            else:
                hit = reader.get_chunk(ref["chunk_id"], include_sensitive=include_sensitive)
                item = document_handoff_descriptor(store, hit, include_sensitive=include_sensitive) if (
                    not hit.get("error") and hit.get("document_id") == ref["document_id"]) else None
            if item is None:
                return unavailable()
            if public_tools:
                from proharness.memory.simple_search import public_followups
                item = public_followups(item, include_sensitive=include_sensitive)
            items.append((section, item))

    def select(indices, *, omitted=0):
        nonlocal selected_indices
        selected_indices = set(indices)
        data["evidence"], data["voice_examples"] = [], []
        for index in sorted(selected_indices):
            section, item = items[index]
            data[section].append(item)
        result["truncated"], result["omitted_references"] = bool(omitted), omitted
        result["content"] = render()

    def compact_fits():
        budget_report(result, max_tokens)
        return fits(compact_collection_export(result), max_tokens)

    # The common complete-export case needs one budget check, rather than
    # repeatedly serializing every growing prefix of the same source bodies.
    select(range(len(items)))
    if (compact_fits() if response_format == "compact" else fits(result, max_tokens)):
        return finish()

    # Page navigation is optional; quotations and extraction warnings are not.
    # Select complete evidence with lean descriptors, then restore page hints
    # only in space left over after that selection. Occurrences stay separate.
    enriched_items = items
    items = [(section, without_original_page_hint(item)) for section, item in items]

    def finish_selected():
        omitted = len(items) - len(selected_indices)
        for index in sorted(selected_indices):
            if "original_page" not in enriched_items[index][1]:
                continue
            lean = items[index]
            items[index] = enriched_items[index]
            select(selected_indices, omitted=omitted)
            if not (compact_fits() if response_format == "compact" else fits(result, max_tokens)):
                items[index] = lean
        select(selected_indices, omitted=omitted)
        return finish()

    select(range(len(items)))
    if (compact_fits() if response_format == "compact" else fits(result, max_tokens)):
        return finish_selected()

    # Reproduce full-mode greedy selection as the compact mode's retained seed.
    # Starting compact greedily from scratch could fit an early large source but
    # displace two later sources that the full export would have retained.
    selected, omitted = set(), 0
    select(selected)
    framing_fits = fits(result, max_tokens)
    if framing_fits:
        for index in range(len(items)):
            select(selected | {index}, omitted=omitted)
            if not fits(result, max_tokens):
                omitted += 1
            else:
                selected.add(index)
        select(selected, omitted=omitted)
    if response_format == "full":
        return finish_selected() if framing_fits else metadata_only()

    select(selected, omitted=len(items) - len(selected))
    if not compact_fits():
        # A valid full-mode seed must never disappear in compact mode. Treat a
        # future codec expansion violating that invariant as an explicit error.
        if selected:
            raise ValueError("Compact export cannot preserve the full selection within this budget")
        return metadata_only()
    for index in range(len(items)):
        if index in selected:
            continue
        select(selected | {index}, omitted=len(items) - len(selected) - 1)
        if compact_fits():
            selected.add(index)
    select(selected, omitted=len(items) - len(selected))
    return finish_selected()
