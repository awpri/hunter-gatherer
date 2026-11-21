"""A write decided on an earlier read must never undo a newer decision.

Each test forces the order of events that used to go wrong: one agent reads a
memory, another forgets, corrects or re-labels it, then the first one writes.
All data is fictional and lives in a temporary store.
"""
from types import SimpleNamespace

import pytest

from hunter_gatherer.mcp.tools import Harness, call_tool
from hunter_gatherer.memory.consolidate import Consolidator
from hunter_gatherer.memory.embeddings import HashingEmbedder
from hunter_gatherer.memory.schemas import Memory, MemoryConflictError
from hunter_gatherer.memory.store import MemoryStore


def harness_for(store):
    return Harness(store=store, settings=store.settings, loop=SimpleNamespace(), cost=SimpleNamespace())


def memory_tool(store, **arguments):
    return call_tool(harness_for(store), "hg_memory", arguments)


def mark_sensitive(memory):
    memory.metadata = {**memory.metadata, "sensitive": True}
    return memory


def test_stale_copy_cannot_undo_a_forget(store):
    memory = store.add(Memory(text="The fictional lantern is blue."))
    stale = store.get(memory.id)                      # agent A reads
    assert store.delete(memory.id) is True            # agent B forgets
    stale.metadata = {**stale.metadata, "sensitive": True}
    for write in (store.update, store.add):           # agent A writes its old copy
        with pytest.raises(MemoryConflictError) as error:
            write(stale)
        assert error.value.reason == "deleted"
    assert store.get(memory.id).deleted_at is not None
    assert not store.vector_search("fictional lantern blue", top_k=5)


def test_forget_between_duplicate_check_and_write_stays_forgotten(store):
    """The reported race, through the real consolidation path."""
    memory = store.add(Memory(text="The fictional lantern is blue."))
    consolidator = Consolidator(store=store, settings=store.settings)
    candidate = Memory(text="The fictional lantern is blue.", metadata={"sensitive": True})
    decision = consolidator.decide(candidate)         # agent A finds the duplicate
    assert decision.target_id == memory.id
    store.delete(memory.id)                           # agent B forgets it
    consolidator.apply(candidate, decision)           # agent A tightens sensitivity
    forgotten = store.get(memory.id)
    assert forgotten.deleted_at is not None           # the tombstone survived
    assert forgotten.metadata["sensitive"] is True    # and privacy was still tightened
    assert memory.id not in {hit.id for hit in store.vector_search("fictional lantern blue", top_k=5)}


@pytest.mark.parametrize("op", ["DELETE", "UPDATE", "NOOP"])
@pytest.mark.parametrize("sensitive", [False, True])
def test_automatic_reconciliation_preserves_an_edit_committed_during_judgment(store, op, sensitive):
    original = store.add(Memory(text="The fictional lantern is blue."))
    candidate = Memory(text=original.text, metadata={"sensitive": sensitive})
    with MemoryStore(store.settings, embedder=HashingEmbedder(dim=128)) as writer:
        def judge(_candidate_text, judged_text):
            assert judged_text == original.text
            edited = writer.get(original.id)
            edited.text = "The fictional library opens Tuesday."
            writer.update(edited)
            return op

        consolidator = Consolidator(store, store.settings, judge=judge)
        saved = consolidator.store_with_consolidation(candidate)
        assert saved is not None and saved.id == candidate.id
        assert writer.get(saved.id).text == original.text
        edited = writer.get(original.id)
        assert edited.is_retrievable() and edited.text == "The fictional library opens Tuesday."
        assert bool(edited.metadata.get("sensitive")) is sensitive


def test_stale_copy_cannot_drop_a_correction(store):
    memory = store.add(Memory(text="The fictional owner is Mira."))
    stale = store.get(memory.id)
    corrected = store.supersede(memory.id, Memory(text="The fictional owner is Rowan."))
    stale.importance = 7
    with pytest.raises(MemoryConflictError) as error:
        store.update(stale)
    assert (error.value.reason, error.value.current_id) == ("superseded", corrected.id)
    assert store.get(memory.id).metadata["superseded_by"] == corrected.id
    assert not store.get(memory.id).is_retrievable()


def test_correction_keeps_privacy_tightened_after_it_was_prepared(store):
    memory = store.add(Memory(text="A fictional clinic visit."))
    prepared_from = store.get(memory.id)              # correction is prepared from this copy
    store.mutate(memory.id, mark_sensitive)           # another agent marks it private
    corrected = store.supersede(memory.id, Memory(
        text="A fictional clinic visit, corrected.", metadata=dict(prepared_from.metadata)))
    assert store.get(corrected.id).metadata["sensitive"] is True


def test_second_correction_gets_a_conflict_and_a_retry_is_harmless(store):
    memory = store.add(Memory(text="The fictional trial starts Monday."))
    first = memory_tool(store, action="replace", memory_id=memory.id, text="It starts Tuesday.")
    second = memory_tool(store, action="replace", memory_id=memory.id, text="It starts Friday.")
    assert first["replaced"] is True
    assert second["replaced"] is False and second["error_code"] == "memory_conflict"
    assert second["conflict"] == {"reason": "superseded", "memory_id": memory.id,
                                  "current_id": first["memory"]["id"]}
    retry = memory_tool(store, action="replace", memory_id=memory.id, text="It starts Tuesday.")
    assert retry["replayed"] is True and retry["memory"]["id"] == first["memory"]["id"]
    assert [m.text for m in store.list_all() if m.is_retrievable()] == ["It starts Tuesday."]


def test_forgetting_twice_changes_nothing_the_second_time(store):
    memory = store.add(Memory(text="A fictional note to remove."))
    first = memory_tool(store, action="forget", memory_id=memory.id)
    second = memory_tool(store, action="forget", memory_id=memory.id)
    assert first["deleted"] is True and "already_forgotten" not in first
    assert second["deleted"] is True and second["already_forgotten"] is True
    assert second["preview"] is None


def test_saving_the_same_fact_twice_keeps_one_record(store):
    arguments = {"action": "remember", "text": "The fictional telescope has a blue case.", "permanent": True}
    first = memory_tool(store, **arguments)
    again = memory_tool(store, **arguments)
    assert first["stored"] is True
    assert again["stored"] is False and again["duplicate_of"] == first["id"]
    assert store.count() == 1


def test_another_process_finds_and_loses_memories_without_restarting(store):
    other = MemoryStore(store.settings, embedder=HashingEmbedder(dim=128))  # a second connection
    try:
        memory = store.add(Memory(text="The quartz heron migrates in the fictional spring."))
        assert memory.id in {hit.id for hit in other.vector_search("quartz heron migrates", top_k=3)}
        store.delete(memory.id)
        assert not other.vector_search("quartz heron migrates", top_k=3)
    finally:
        other.close()


def test_another_client_sees_metadata_only_changes_in_filtered_vector_search(store):
    query = "The fictional blue lantern."
    stronger = store.add(Memory(text=query))
    weaker = store.add(Memory(text="A fictional workshop checklist."))
    with MemoryStore(store.settings, embedder=HashingEmbedder(dim=128)) as reader:
        def matches(kind):
            return reader.vector_search(query, top_k=1, filters={"type": kind})

        assert matches("semantic")[0].id == stronger.id
        assert matches("procedural") == []
        weaker.type = "procedural"
        store.update(weaker)
        result = matches("procedural")
        assert [hit.id for hit in result] == [weaker.id]
        assert result[0].payload["type"] == "procedural"

        weaker.type = "episodic"
        store.update(weaker)
        assert matches("procedural") == []
        assert [hit.id for hit in matches(["procedural", "episodic"])] == [weaker.id]


@pytest.mark.parametrize("during_preparation", [False, True])
def test_find_sees_a_semantic_only_match_saved_by_another_client(settings, monkeypatch, during_preparation):
    from hunter_gatherer.memory import evidence

    class VehicleEmbedder:
        dim = 2

        def fingerprint(self):
            return "fictional-vehicles:2"

        def embed(self, text):
            vehicle = "vehicle" in text or "automobile" in text
            return [1., 0.] if vehicle else [0., 1.]

        def embed_batch(self, texts):
            return [self.embed(text) for text in texts]

        def embed_sparse(self, text):
            return None

    with (MemoryStore(settings, embedder=VehicleEmbedder()) as reader,
          MemoryStore(settings, embedder=VehicleEmbedder()) as writer):
        parked = Memory(text="The passenger vehicle belongs in the garage.")
        if during_preparation:
            prepare = evidence.prepare_evidence_search

            def prepare_then_save(*args, **kwargs):
                archive = prepare(*args, **kwargs)
                writer.add(parked)
                return archive

            monkeypatch.setattr(evidence, "prepare_evidence_search", prepare_then_save)
        else:
            writer.add(parked)

        result = call_tool(harness_for(reader), "hg_find", {"query": "automobile"})
        assert [item["citation"] for item in result["relevant_memories"]] == [f"[mem:{parked.id}]"]


def test_forgetting_a_memory_someone_just_corrected_explains_instead_of_guessing(store):
    memory = store.add(Memory(text="The fictional trial starts Monday."))
    corrected = memory_tool(store, action="replace", memory_id=memory.id, text="It starts Tuesday.")
    late = memory_tool(store, action="forget", memory_id=memory.id)   # decided before the correction
    assert late["deleted"] is False and late["error_code"] == "memory_conflict"
    assert late["conflict"] == {"reason": "superseded", "memory_id": memory.id,
                                "current_id": corrected["memory"]["id"]}
    assert store.get(memory.id).deleted_at is None                    # history untouched
    assert store.get(corrected["memory"]["id"]).is_retrievable()      # the correction stands


def test_correcting_a_memory_someone_just_forgot_reports_that_it_is_gone(store):
    memory = store.add(Memory(text="A fictional plan."))
    memory_tool(store, action="forget", memory_id=memory.id)
    late = memory_tool(store, action="replace", memory_id=memory.id, text="A revised fictional plan.")
    assert late["replaced"] is False and late["error_code"] == "memory_conflict"
    assert late["conflict"] == {"reason": "deleted", "memory_id": memory.id}
    assert store.count() == 0                                         # the forget was not undone


@pytest.mark.parametrize("settled", ["forgotten", "corrected"])
def test_a_private_memory_that_changed_stays_indistinguishable_from_a_missing_one(store, settled):
    memory = store.add(Memory(text="A fictional private note.", metadata={"sensitive": True}))
    if settled == "forgotten":
        store.delete(memory.id)
    else:
        store.supersede(memory.id, Memory(text="A corrected fictional private note."))
    for action, extra in (("forget", {}), ("replace", {"text": "Anything."})):
        assert memory_tool(store, action=action, memory_id=memory.id, **extra)["error"] == \
            "Memory not found or unavailable"
        assert memory_tool(store, action=action, memory_id="absent", **extra)["error"] == \
            "Memory not found or unavailable"
    opted_in = memory_tool(store, action="forget", memory_id=memory.id, include_sensitive=True)
    assert opted_in.get("already_forgotten") or opted_in["error_code"] == "memory_conflict"


@pytest.mark.parametrize("include_sensitive", [False, True])
def test_correction_rechecks_privacy_after_a_concurrent_pin(store, monkeypatch, include_sensitive):
    from hunter_gatherer.mcp import tools
    from hunter_gatherer.memory.core import set_core_memory

    memory = store.add(Memory(text="The fictional trial starts Monday."))
    validate, raced = tools._validated_metadata, []

    def other_agent_acts_first(harness, metadata, **options):
        if not raced:                                    # between the read and the write
            raced.append(True)
            set_core_memory(store, memory.id)
            store.mutate(memory.id, mark_sensitive)
        return validate(harness, metadata, **options)

    monkeypatch.setattr(tools, "_validated_metadata", other_agent_acts_first)
    result = memory_tool(store, action="replace", memory_id=memory.id, text="It starts Tuesday.",
                         include_sensitive=include_sensitive)
    if not include_sensitive:
        assert result["error"] == "Memory not found or unavailable"
        assert store.get(memory.id).is_retrievable()
        return
    assert result["replaced"] is True
    current = store.get(result["memory"]["id"])
    assert current.metadata["core"] is True and current.metadata["retention"] == "permanent"
    assert current.metadata["sensitive"] is True


def test_many_agents_saving_the_same_fact_at_once_store_it_once(store):
    import threading

    stores = [store] + [MemoryStore(store.settings, embedder=HashingEmbedder(dim=128)) for _ in range(3)]
    start, results = threading.Barrier(len(stores)), []

    def save(own):
        start.wait(10)
        results.append(memory_tool(own, action="remember", text="The fictional heron prefers quartz."))

    try:
        threads = [threading.Thread(target=save, args=(own,)) for own in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert len(results) == len(stores)
        stored = [result for result in results if result["stored"]]
        assert len(stored) == 1 and store.count() == 1
        assert {result.get("duplicate_of") for result in results if not result["stored"]} == {stored[0]["id"]}
    finally:
        for own in stores[1:]:
            own.close()


def test_rest_edit_of_a_memory_that_changed_is_refused(store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hunter_gatherer.server.routers.memory import router

    app = FastAPI()
    app.state.loop, app.state.settings = SimpleNamespace(store=store), store.settings
    app.include_router(router, prefix="/memory")
    client = TestClient(app)
    memory = store.add(Memory(text="The fictional owner is Mira."))
    read = store.get

    def then_corrected(memory_id):
        record = read(memory_id)
        store.get = read
        store.supersede(memory.id, Memory(text="The fictional owner is Rowan."))
        return record

    store.get = then_corrected
    response = client.patch(f"/memory/{memory.id}", json={"importance": 9})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "superseded"
    assert store.get(memory.id).importance != 9


def test_reading_and_searching_hash_nothing_until_a_write_compares(store, monkeypatch):
    """A search reads every record, so the change check must cost a read nothing."""
    from hunter_gatherer.memory import schemas
    from hunter_gatherer.memory.simple_search import find

    kettle = store.add(Memory(text="The fictional kettle is copper."))
    store.add(Memory(text="The fictional ladder is oak."))
    hashed = []
    digest = schemas.stored_token
    monkeypatch.setattr(schemas, "stored_token", lambda row: hashed.append(row[0]) or digest(row))

    copy = store.get(kettle.id)
    assert len(store.list_all()) == len(list(store.iter_all())) == 2
    assert store.vector_search("fictional kettle copper", top_k=5)
    page = find(SimpleNamespace(store=store, settings=store.settings), "fictional kettle copper")
    assert f"[mem:{kettle.id}]" in {item["citation"] for item in page["relevant_memories"]}
    assert hashed == []

    copy.importance = 7
    store.update(copy)                                # only now is the row it read hashed
    assert set(hashed) == {kettle.id}
    assert store.get(kettle.id).importance == 7


def test_a_copy_is_judged_by_the_row_it_read_not_by_its_later_edits(store):
    """The check is worked out late, but always from the row as it was read."""
    memory = store.add(Memory(text="The fictional gate code is amber."))
    first, second = store.get(memory.id), store.get(memory.id)   # two agents read

    first.text = "The fictional gate code is amber, checked."     # A edits its copy, then writes:
    first.metadata = {**first.metadata, "reviewed": True}         # its edits are not what it read
    assert store.update(first).text == "The fictional gate code is amber, checked."
    first.importance = 8
    store.update(first)                               # and it may write again after its own write

    second.importance = 3                             # B read before A wrote and is asked only now
    with pytest.raises(MemoryConflictError) as error:
        store.update(second)
    assert error.value.reason == "changed"
    current = store.get(memory.id)
    assert (current.text, current.importance) == ("The fictional gate code is amber, checked.", 8)
    assert current.metadata["reviewed"] is True


def test_a_privacy_change_is_noticed_by_a_copy_read_before_it(store):
    memory = store.add(Memory(text="A fictional clinic visit."))
    stale = store.get(memory.id)
    store.mutate(memory.id, mark_sensitive)           # only metadata changed
    stale.tags = ["health"]
    with pytest.raises(MemoryConflictError) as error:
        store.update(stale)
    assert error.value.reason == "changed"
    assert store.get(memory.id).metadata["sensitive"] is True


def test_only_copies_read_from_the_store_claim_to_have_seen_a_row(store):
    import copy
    from dataclasses import replace

    memory = store.add(Memory(text="The fictional lantern is blue."))
    read = store.get(memory.id)
    by_hand = replace(read, text="The fictional lantern is green.")
    assert by_hand._stored_token is None and Memory(id=memory.id)._stored_token is None
    duplicate = copy.copy(read)                       # taken before the digest was ever asked for
    assert read._stored_token == duplicate._stored_token == memory._stored_token
    assert copy.deepcopy(read)._stored_token == read._stored_token
    assert len(read._stored_token) == 64
    read.text = "The fictional lantern is red."
    assert read._stored_token == duplicate._stored_token   # edits never change the claim


def test_correction_replay_compares_the_metadata_and_writer(store):
    from hunter_gatherer.access import AccessPolicy, access_scope

    record = store.add(Memory(text="The fictional gate opens Monday."))
    correction = {"action": "replace", "memory_id": record.id, "text": "The gate opens Friday."}
    with access_scope(AccessPolicy(principal="pat")):
        first = memory_tool(store, **correction, metadata={"category": "opening"})
        retry = memory_tool(store, **correction, metadata={"category": "opening"})
        assert retry["replayed"] and retry["memory"]["id"] == first["memory"]["id"]
        different = memory_tool(store, **correction, metadata={"sensitive": True})
        assert different["error_code"] == "memory_conflict"
    with access_scope(AccessPolicy(principal="sam")):
        different_writer = memory_tool(store, **correction, metadata={"category": "opening"})
        assert different_writer["error_code"] == "memory_conflict"
    assert store.get(first["memory"]["id"]).metadata["recorded_by"] == "pat"


def test_a_correction_retry_checks_the_new_versions_visibility(store):
    record = store.add(Memory(text="A fictional museum opens on Monday."))
    correction = {"action": "replace", "memory_id": record.id, "text": "It opens on Friday.",
                  "metadata": {"sensitive": True, "note": "fictional curator note"}}
    first = memory_tool(store, **correction)
    assert first["replaced"] and first["memory"] is None
    private_id = store.get(record.id).metadata["superseded_by"]
    default_retry = memory_tool(store, **correction)
    assert default_retry["error_code"] == "memory_conflict"
    assert "current_id" not in default_retry["conflict"] and "memory" not in default_retry
    opted_in = memory_tool(store, **correction, include_sensitive=True)
    assert opted_in["replayed"] and opted_in["memory"]["id"] == private_id


@pytest.mark.parametrize("change", ["superseded", "private", "edited"])
def test_forget_checks_the_current_target_before_committing(store, monkeypatch, change):
    old = store.add(Memory(text="The fictional ferry departs Friday."))
    delete = store.delete

    def another_client_writes_first(memory_id, **options):
        if change == "superseded":
            store.supersede(memory_id, Memory(text="The ferry departs Saturday."))
        elif change == "private":
            store.mutate(memory_id, mark_sensitive)
        else:
            edit = store.get(memory_id)
            edit.text = "The ferry departs on alternate Fridays."
            store.update(edit)
        return delete(memory_id, **options)

    monkeypatch.setattr(store, "delete", another_client_writes_first)
    result = memory_tool(store, action="forget", memory_id=old.id)
    assert store.get(old.id).deleted_at is None
    if change == "private":
        assert result["error"] == "Memory not found or unavailable"
    else:
        assert result["deleted"] is False and result["error_code"] == "memory_conflict"
        assert result["conflict"]["reason"] == ("superseded" if change == "superseded" else "changed")


@pytest.mark.parametrize("action", ["replace", "forget"])
def test_changing_a_linked_sources_privacy_is_checked_inside_the_write(store, tmp_path, monkeypatch, action):
    from hunter_gatherer.memory.documents import DocumentArchive

    path = tmp_path / "museum.md"
    path.write_text("# Fictional museum\n\nThe museum opens Friday.\n")
    archive = DocumentArchive(store)
    archive.ingest(str(path))
    hit = archive.search("museum opens")["hits"][0]
    memory = store.add(Memory(text="Fictional museum opening.", metadata={
        "evidence_refs": [{"document_id": hit["document_id"], "chunk_id": hit["chunk_id"]}]}))
    method = "supersede" if action == "replace" else "delete"
    write = getattr(store, method)

    def protect_source_first(*args, **kwargs):
        archive.ingest(str(path), sensitive=True)
        return write(*args, **kwargs)

    monkeypatch.setattr(store, method, protect_source_first)
    arguments = {"text": "The museum opens Friday."} if action == "replace" else {}
    result = memory_tool(store, action=action, memory_id=memory.id, **arguments)
    assert result["error"] == "Memory not found or unavailable"
    assert store.get(memory.id).is_retrievable()
