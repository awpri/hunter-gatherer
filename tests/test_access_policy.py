"""A host's access policy is enforced on every call, whatever the tool arguments say.

The policy stands in for decisions a hosting application makes after it has
authenticated a user. Nothing a model can put in a tool call may widen it.
All data is fictional.
"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from hunter_gatherer.access import (
    ADMINISTER,
    IMPORT,
    LOCAL,
    READ,
    WRITE,
    AccessPolicy,
    access_scope,
    required_capability,
    restricted_view,
)
from hunter_gatherer.mcp import tools
from hunter_gatherer.mcp.profiles import LIBRARY_TOOL_NAMES
from hunter_gatherer.mcp.tools import Harness, call_tool
from hunter_gatherer.memory.collection_input import CollectionInput
from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.import_jobs import ImportJobs
from hunter_gatherer.memory.library_collections import save_collection
from hunter_gatherer.memory.schemas import Memory

READ_ONLY = AccessPolicy(write=False, import_files=False, administer=False)
NO_SENSITIVE = AccessPolicy(read_sensitive=False, administer=False)


@pytest.fixture
def owner(store):
    return Harness(store=store, settings=store.settings, loop=SimpleNamespace(), cost=SimpleNamespace())


def denied(result):
    return result.get("error_code") == "access_denied"


# ── every action of every tool is classified ────────────────────────────────

def test_every_action_is_classified_and_unknown_tools_need_full_trust():
    for name in tools.all_tool_names():
        actions = tools.get_schema(name).get("properties", {}).get("action", {}).get("enum")
        if actions:
            for action in actions:
                assert required_capability(name, {"action": action}) in {READ, WRITE, IMPORT}, (name, action)
            # An action the family does not know is treated as its most demanding one.
            assert required_capability(name, {"action": "something-new"}) != READ
    assert required_capability("hg_a_tool_added_later", {}) == ADMINISTER
    # The six-tool library surface never needs administration.
    for name in LIBRARY_TOOL_NAMES:
        for action in tools.get_schema(name).get("properties", {}).get("action", {}).get("enum") or [None]:
            assert required_capability(name, {"action": action} if action else {}) != ADMINISTER


def test_reading_and_writing_actions_of_one_family_are_judged_separately():
    assert required_capability("hg_memory", {"action": "profile"}) == READ
    assert required_capability("hg_memory", {"action": "forget"}) == WRITE
    assert required_capability("hg_collection", {"action": "export"}) == READ
    assert required_capability("hg_collection", {"action": "save"}) == WRITE
    assert required_capability("hg_import_documents", {}) == IMPORT            # default action: preview
    assert required_capability("hg_import_documents", {"action": "enrichments"}) == READ
    assert required_capability("hg_import_documents", {"action": "revoke_enrichment"}) == WRITE
    assert required_capability("hg_backup", {}) == ADMINISTER


# ── the default is unchanged ────────────────────────────────────────────────

def test_without_a_policy_nothing_is_restricted(owner, store):
    assert LOCAL.unrestricted
    stored = call_tool(owner, "hg_memory", {"action": "remember", "text": "A fictional fact.", "sensitive": True})
    assert stored["stored"] and "database" in stored["store"]
    found = call_tool(owner, "hg_memory", {"action": "history", "memory_id": stored["id"], "include_sensitive": True})
    assert not denied(found)
    assert "recorded_by" not in store.get(stored["id"]).metadata


# ── a read-only caller ──────────────────────────────────────────────────────

def test_a_read_only_view_can_search_but_change_nothing(owner, store, tmp_path):
    memory = store.add(Memory(text="The fictional heron prefers quartz."))
    saved = save_collection(store, CollectionInput(name="Herons", idempotency_key="k",
                                                   references=[{"memory_id": memory.id}]))
    view = restricted_view(owner, READ_ONLY)
    page = call_tool(view, "hg_find", {"query": "heron quartz"})
    assert f"[mem:{memory.id}]" in json.dumps(page) and "database" not in page["store"]
    assert call_tool(view, "hg_open", {"refs": [f"[mem:{memory.id}]"]})["sources"]
    assert not denied(call_tool(view, "hg_collection", {"action": "get", "collection_id": saved["id"]}))
    before = store._sqlite.total_changes
    note = tmp_path / "note.md"
    note.write_text("# Fictional note\n\nNothing to import.\n")
    for name, arguments in (
        ("hg_memory", {"action": "remember", "text": "An unwanted fact."}),
        ("hg_memory", {"action": "replace", "memory_id": memory.id, "text": "Rewritten."}),
        ("hg_memory", {"action": "forget", "memory_id": memory.id}),
        ("hg_collection", {"action": "save", "idempotency_key": "x", "collection": {"name": "New"}}),
        ("hg_import_documents", {"action": "preview", "paths": [str(note)]}),
        ("hg_import_documents", {"action": "revoke_enrichment", "enrichment": {"document_id": "d", "batch_id": "b"}}),
        ("hg_backup", {"destination": str(tmp_path / "copy")}),
        ("hg_set_core_memory", {"memory_id": memory.id}),
    ):
        assert denied(call_tool(view, name, arguments)), name
    assert store._sqlite.total_changes == before and not (tmp_path / "copy").exists()
    assert store.get(memory.id).is_retrievable() and store.count() == 1


# ── a caller who may not read sensitive records ─────────────────────────────

@pytest.mark.parametrize("value", ["false", 1, None])
def test_python_dispatch_requires_a_boolean_sensitive_flag(owner, value):
    for harness in (owner, restricted_view(owner, NO_SENSITIVE)):
        with pytest.raises(ValueError, match="include_sensitive"):
            call_tool(harness, "hg_list_memories", {"include_sensitive": value})


def test_the_sensitive_flag_is_a_request_the_policy_can_refuse(owner, store):
    private = store.add(Memory(text="A fictional clinic visit on the amber wing.", metadata={"sensitive": True}))
    public = store.add(Memory(text="A fictional public plan."))
    view = restricted_view(owner, NO_SENSITIVE)
    for name, arguments in (
        ("hg_find", {"query": "fictional clinic visit"}),
        ("hg_open", {"refs": [f"[mem:{private.id}]"]}),
        ("hg_memory", {"action": "profile"}),
        ("hg_memory", {"action": "history", "memory_id": private.id}),
    ):
        assert "amber" not in json.dumps(call_tool(view, name, arguments)), name
        assert denied(call_tool(view, name, {**arguments, "include_sensitive": True})), name
    assert "amber" in json.dumps(call_tool(owner, "hg_find", {"query": "fictional clinic visit",
                                                               "include_sensitive": True}))
    # Writing is allowed here, but not to a record this caller cannot see.
    assert call_tool(view, "hg_memory", {"action": "forget", "memory_id": private.id})["error"] == \
        "Memory not found or unavailable"
    # The older single-purpose tools act on an ID without checking visibility,
    # so they are closed to this caller altogether.
    for name, arguments in (("hg_forget", {"memory_id": private.id}),
                            ("hg_replace_memory", {"memory_id": private.id, "text": "Overwritten."}),
                            ("hg_set_core_memory", {"memory_id": private.id})):
        assert denied(call_tool(view, name, arguments)), name
    assert store.get(private.id).is_retrievable() and "core" not in store.get(private.id).metadata
    assert call_tool(view, "hg_memory", {"action": "forget", "memory_id": public.id})["deleted"] is True


def test_a_caller_without_sensitive_access_cannot_cite_or_echo_sensitive_sources(owner, store, tmp_path):
    path = tmp_path / "private.md"
    path.write_text("# Fictional record\n\nThe vault code is a fictional seven-two-nine.\n")
    DocumentArchive(store).ingest(str(path), sensitive=True)
    chunk = store._sqlite.execute("SELECT id FROM archive_chunks").fetchone()[0]
    memory = store.add(Memory(text="A fictional note."))
    view = restricted_view(owner, NO_SENSITIVE)
    for arguments in (
        {"action": "remember", "text": "A new fictional note.", "metadata": {"evidence_refs": [{"chunk_id": chunk}]}},
        {"action": "replace", "memory_id": memory.id, "text": "Corrected.",
         "metadata": {"evidence_refs": [{"chunk_id": chunk}]}},
    ):
        with pytest.raises(ValueError, match="unavailable"):
            call_tool(view, "hg_memory", arguments)
    correction = {"action": "replace", "memory_id": memory.id, "text": "Corrected.",
                  "metadata": {"evidence_refs": [{"chunk_id": chunk}]}}
    with pytest.raises(ValueError, match="unavailable"):
        call_tool(owner, "hg_memory", correction)
    # The owner can explicitly read and cite it; the correction inherits its privacy.
    cited = call_tool(owner, "hg_memory", {**correction, "include_sensitive": True})
    assert "seven-two-nine" in json.dumps(cited)
    assert store.get(cited["memory"]["id"]).metadata["sensitive"] is True


# ── nothing in the arguments can widen the policy ───────────────────────────

def test_arguments_cannot_grant_access_or_choose_another_library(owner, store, tmp_path):
    view = restricted_view(owner, READ_ONLY)
    elsewhere = str(tmp_path / "other.db")
    for arguments in (
        {"action": "remember", "text": "x", "access_policy": {"write": True}},
        {"action": "remember", "text": "x", "principal": "owner"},
        {"action": "profile", "database": elsewhere},
        {"action": "profile", "sqlite_path": elsewhere},
    ):
        result = call_tool(view, "hg_memory", arguments)
        assert result.get("error"), arguments            # refused or rejected as unknown
    assert store.count() == 0 and not os.path.exists(elsewhere)
    with pytest.raises(TypeError):
        call_tool(view, "hg_find", {"query": "x", "access_policy": {"write": True}})
    # A request scope narrows what the harness allows; it can never add to it.
    with access_scope(AccessPolicy()):
        assert denied(call_tool(view, "hg_memory", {"action": "remember", "text": "x"}))
    with access_scope(READ_ONLY):
        assert denied(call_tool(owner, "hg_memory", {"action": "remember", "text": "x"}))
        with access_scope(AccessPolicy()):
            assert denied(call_tool(owner, "hg_memory", {"action": "remember", "text": "x"}))
    assert call_tool(owner, "hg_memory", {"action": "remember", "text": "The owner is unaffected."})["stored"]


# ── imports read only from folders the host allows ──────────────────────────

def test_import_roots_require_a_collection_of_nonempty_directory_paths(tmp_path):
    for invalid in (str(tmp_path), tmp_path, b"", bytearray(), memoryview(b""), {}, iter([tmp_path])):
        with pytest.raises(TypeError, match="import_roots"):
            AccessPolicy(import_roots=invalid)
    with pytest.raises(ValueError, match="nonempty"):
        AccessPolicy(import_roots=[""])
    assert AccessPolicy(import_roots=[tmp_path]).import_roots == (str(tmp_path.resolve()),)


def test_imports_are_confined_to_allowed_folders(owner, store, tmp_path):
    allowed, outside = tmp_path / "uploads", tmp_path / "elsewhere"
    allowed.mkdir()
    outside.mkdir()
    (allowed / "ok.md").write_text("# Fictional upload\n\nThe heron prefers quartz.\n")
    (outside / "secret.md").write_text("# Not yours\n\nA fictional server file.\n")
    (allowed / "link.md").symlink_to(outside / "secret.md")
    view = restricted_view(owner, AccessPolicy(administer=False, import_roots=(str(allowed),)))
    inside = call_tool(view, "hg_import_documents", {"action": "preview", "paths": [str(allowed / "ok.md")]})
    assert inside["file_count"] == 1
    # Neither a parent-directory path nor a link out of the folder is a way out of it.
    for path in (outside / "secret.md", allowed / ".." / "elsewhere" / "secret.md", outside,
                 allowed / "link.md"):
        with pytest.raises(PermissionError):
            call_tool(view, "hg_import_documents", {"action": "preview", "paths": [str(path)], "recursive": True})
    with access_scope(view.access_policy):
        with pytest.raises(PermissionError):
            ImportJobs.preview([str(outside / "secret.md")])
        with pytest.raises(PermissionError):
            DocumentArchive(store).ingest(str(outside / "secret.md"))
    created = call_tool(view, "hg_import_documents", {"action": "create", "paths": [str(allowed / "ok.md")],
                                                       "idempotency_key": "one"})
    done = call_tool(view, "hg_import_documents", {"action": "run", "job_id": created["job_id"]})
    assert done["counts"]["succeeded"] == 1


# ── authorship comes from the host, not the caller ──────────────────────────

def test_the_host_names_the_author_and_a_caller_cannot(owner, store):
    view = restricted_view(owner, AccessPolicy(administer=False, principal="member-17"))
    stored = call_tool(view, "hg_memory", {"action": "remember", "text": "The fictional trial starts Monday.",
                                           "source": "claimed-to-be-the-owner"})
    assert store.get(stored["id"]).metadata["recorded_by"] == "member-17"
    with pytest.raises(ValueError, match="recorded_by"):
        call_tool(view, "hg_memory", {"action": "remember", "text": "Another fact.",
                                      "metadata": {"recorded_by": "owner"}})
    other = restricted_view(owner, AccessPolicy(administer=False, principal="member-42"))
    corrected = call_tool(other, "hg_memory", {"action": "replace", "memory_id": stored["id"],
                                               "text": "It starts Tuesday."})
    assert store.get(corrected["memory"]["id"]).metadata["recorded_by"] == "member-42"
    assert store.get(stored["id"]).metadata["recorded_by"] == "member-17"      # history keeps its author


# ── the same limits through the MCP and web surfaces ────────────────────────

def test_an_mcp_server_built_on_a_view_enforces_it_in_worker_threads(owner, store):
    from hunter_gatherer.mcp.server import build_server

    store.add(Memory(text="The fictional heron prefers quartz."))
    server = build_server(harness=restricted_view(owner, READ_ONLY), tool_profile="library")[0]

    def invoke(name, **arguments):
        response = asyncio.run(server.call_tool(name, arguments))
        return response.is_error, json.loads(next(part.text for part in response.content if part.type == "text"))

    failed, page = invoke("hg_find", query="heron quartz")
    assert not failed and page["relevant_memories"]
    failed, refused = invoke("hg_memory", action="remember", text="An unwanted fact.")
    assert failed and refused["error_code"] == "access_denied"
    failed, status = invoke("hg_status")
    assert not failed and "database" not in status["store"]
    assert store.count() == 1

    # A host calling in-process narrows one request the same way.
    full = build_server(harness=owner, tool_profile="library")[0]

    async def scoped():
        with access_scope(READ_ONLY):
            return await full.call_tool("hg_memory", {"action": "remember", "text": "An unwanted fact."})

    assert asyncio.run(scoped()).is_error and store.count() == 1


def test_the_local_web_api_is_not_served_to_a_restricted_harness(owner, store):
    from fastapi.testclient import TestClient

    from hunter_gatherer.mcp.server import _http_options, build_server
    from hunter_gatherer.server.library_app import build_library_app

    store.add(Memory(text="The fictional heron prefers quartz."))
    DocumentArchive(store)
    options = _http_options("127.0.0.1", 8767)
    headers = {"host": "127.0.0.1:8767"}
    for harness, expected in ((owner, 200), (restricted_view(owner, READ_ONLY), 403)):
        owner.loop = SimpleNamespace(embedder=store.embedder)
        app = build_library_app(harness, build_server(harness=harness, tool_profile="library")[0],
                                http_options=options)
        with TestClient(app, base_url="http://127.0.0.1:8767") as client:
            assert client.get("/api/library/overview", headers=headers).status_code == expected
            assert client.get("/healthz", headers=headers).status_code == 200


def test_host_middleware_can_decide_the_policy_for_each_http_request(owner, store):
    """The hosting pattern: authenticate, then scope the request before h/g sees it."""
    from fastapi.testclient import TestClient

    from hunter_gatherer.mcp.server import _http_options, build_server
    from hunter_gatherer.server.library_app import build_library_app

    class HostAuthentication:
        """Stands in for a host's own login and membership checks."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            role = dict(scope["headers"]).get(b"x-test-role", b"").decode()   # decided by the host
            with access_scope(AccessPolicy(principal=role) if role == "owner" else READ_ONLY):
                await self.app(scope, receive, send)

    owner.loop = SimpleNamespace(embedder=store.embedder)
    app = HostAuthentication(build_library_app(
        owner, build_server(harness=owner, tool_profile="library")[0],
        http_options=_http_options("127.0.0.1", 8767)))
    headers = {"host": "127.0.0.1:8767", "accept": "application/json, text/event-stream",
               "content-type": "application/json"}

    def remember(client, role, text):
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "hg_memory", "arguments": {"action": "remember", "text": text}}}
        return client.post("/mcp/library", headers={**headers, "x-test-role": role}, json=call).text

    with TestClient(app, base_url="http://127.0.0.1:8767") as client:
        assert "access_denied" in remember(client, "guest", "A fictional fact from a guest.")
        assert store.count() == 0
        assert "access_denied" not in remember(client, "owner", "A fictional fact from the owner.")
    assert [memory.metadata.get("recorded_by") for memory in store.list_all()] == ["owner"]
