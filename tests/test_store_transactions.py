"""Store-level guarantees behind concurrent use: ordering, atomicity and index upkeep.

Two ``MemoryStore`` objects opened on one file stand in for two processes: each
has its own SQLite connection and its own in-memory vector index. Threads only
ever wait on events, never on timing, except to confirm that something is
still blocked. All data is fictional.
"""
import sqlite3
import threading
import time
from array import array

import pytest

from hunter_gatherer.memory.consolidate import Consolidator
from hunter_gatherer.memory.core import set_core_memory
from hunter_gatherer.memory.embeddings import HashingEmbedder
from hunter_gatherer.memory.reflect import Reflector
from hunter_gatherer.memory.schemas import ConsolidationDecision, Memory, MemoryConflictError
from hunter_gatherer.memory.store import MemoryStore, VectorBackend


def mark_sensitive(memory):
    memory.metadata = {**memory.metadata, "sensitive": True}
    return memory


@pytest.fixture
def other(store):
    """A second connection to the same library, as another process would hold."""
    second = MemoryStore(store.settings, embedder=HashingEmbedder(dim=128))
    yield second
    second.close()


# ── ordering between writers ─────────────────────────────────────────────────

def test_a_forget_from_another_process_waits_for_an_open_edit_then_applies(store, other):
    memory = store.add(Memory(text="The fictional lantern is blue."))
    started, finished = threading.Event(), threading.Event()

    def forget():
        started.set()
        other.delete(memory.id)
        finished.set()

    worker = threading.Thread(target=forget)

    def edit(current):
        worker.start()
        assert started.wait(5)
        assert not finished.wait(0.3)  # held back by this open write transaction
        return mark_sensitive(current)

    store.mutate(memory.id, edit)
    worker.join(15)
    assert finished.is_set()
    after = store.get(memory.id)
    # Both decisions took effect, in commit order. Neither undid the other.
    assert after.deleted_at is not None and after.metadata["sensitive"] is True


def test_bookkeeping_from_another_thread_cannot_commit_half_a_write(store):
    memory = store.add(Memory(text="A fictional fact.", importance=5))
    done = threading.Event()

    def bookkeeping():
        store.bump_cached_answer("anything")
        store.delete_cached_answer("anything")
        Consolidator(store=store, settings=store.settings)._audit(
            Memory(text="unrelated"), ConsolidationDecision(op="NOOP"))
        done.set()

    worker = threading.Thread(target=bookkeeping)
    with pytest.raises(RuntimeError), store.write_transaction():
        store._sqlite.execute("UPDATE memories SET importance = 1 WHERE id = ?", (memory.id,))
        worker.start()
        assert not done.wait(0.3)  # waits for the store lock instead of committing for us
        raise RuntimeError("abandon the write")
    worker.join(15)
    assert done.is_set()
    assert store.get(memory.id).importance == 5  # rolled back as a whole


def test_nested_write_transactions_commit_or_roll_back_together(store):
    memory = store.add(Memory(text="A fictional fact.", importance=5))
    with pytest.raises(RuntimeError), store.write_transaction():
        store.mutate(memory.id, mark_sensitive)      # joins the open transaction
        raise RuntimeError("abandon everything")
    assert "sensitive" not in store.get(memory.id).metadata
    with store.write_transaction():
        store.mutate(memory.id, mark_sensitive)
    assert store.get(memory.id).metadata["sensitive"] is True


# ── edits that must reach, but never revive, history ────────────────────────

def test_an_edit_needs_an_active_record_unless_it_asks_for_history(store):
    memory = store.add(Memory(text="A fictional fact."))
    store.delete(memory.id)
    with pytest.raises(MemoryConflictError) as error:
        store.mutate(memory.id, mark_sensitive)
    assert error.value.reason == "deleted"
    store.mutate(memory.id, mark_sensitive, require_active=False)
    after = store.get(memory.id)
    assert after.metadata["sensitive"] is True and after.deleted_at is not None
    with pytest.raises(MemoryConflictError) as error:
        store.mutate("no-such-id", mark_sensitive)
    assert error.value.reason == "missing"


def test_a_copy_that_was_never_read_cannot_drop_protections(store):
    memory = store.add(Memory(text="A fictional clinic visit.", metadata={"sensitive": True}))
    blind = Memory(id=memory.id, text="A fictional clinic visit.", metadata={"category": "note"})
    store.update(blind)
    after = store.get(memory.id)
    assert after.metadata == {"category": "note", "sensitive": True}


def test_a_pin_and_a_privacy_change_both_survive(store):
    memory = store.add(Memory(text="A fictional standing fact."))
    real_mutate = store.mutate

    def tighten_first(memory_id, change, **options):
        store.mutate = real_mutate
        real_mutate(memory_id, mark_sensitive)           # another agent, just before the pin
        return real_mutate(memory_id, change, **options)

    store.mutate = tighten_first
    pinned = set_core_memory(store, memory.id)
    assert pinned.metadata["core"] is True and pinned.metadata["sensitive"] is True
    store.delete(memory.id)
    with pytest.raises(ValueError):                       # MemoryConflictError is a ValueError
        set_core_memory(store, memory.id)
    assert store.get(memory.id).deleted_at is not None


# ── automatic passes act only on what they judged ───────────────────────────

def old_unused(text, **fields):
    month = 40 * 24 * 3600
    return Memory(text=text, created_at=time.time() - month, last_accessed=time.time() - month, **fields)


def test_decay_leaves_alone_what_changed_after_it_listed_the_memories(store):
    forgotten = store.add(old_unused("A fictional stale note about copper.", importance=1.1))
    pinned = store.add(old_unused("A fictional stale note about zinc.", importance=1.1))
    untouched = store.add(old_unused("A fictional stale note about tin.", importance=1.1))
    listed = store.list_all(limit=100)
    store.delete(forgotten.id)                           # forgotten by a user meanwhile
    set_core_memory(store, pinned.id)                    # pinned by a user meanwhile
    store.list_all = lambda *args, **kwargs: listed      # the pass still holds its old listing
    decayed, deleted = Reflector(store, store.settings)._decay_pass()
    assert (decayed, deleted) == (0, 1)
    assert store.get(untouched.id).deleted_at is not None        # judged and still unchanged
    assert store.get(pinned.id).is_retrievable()                 # the pin won
    after = store.get(forgotten.id)
    assert after.deleted_at is not None and after.importance == 1.1  # not written back


def test_belief_revision_skips_a_pair_that_changed_after_it_was_judged(store):
    older = store.add(Memory(text="The fictional owner lives in Berlin."))
    newer = store.add(Memory(text="The fictional owner moved to Munich."))
    pair = (store.get(older.id), store.get(newer.id), 0.8)
    store.contradiction_candidates = lambda **kwargs: [pair]
    reflector = Reflector(store, store.settings, judge=lambda old, new: "SUPERSEDE")
    store.delete(older.id)                               # forgotten before the verdict is applied
    assert reflector._contradiction_pass() == 0
    after = store.get(older.id)
    assert after.deleted_at is not None and "superseded_by" not in after.metadata
    assert "valid_from" not in store.get(newer.id).metadata

    fresh = store.add(Memory(text="The fictional owner lives in Hamburg."))
    pair = (store.get(fresh.id), store.get(newer.id), 0.8)
    assert reflector._contradiction_pass() == 1
    assert store.get(fresh.id).metadata["superseded_by"] == newer.id
    assert store.get(newer.id).is_retrievable()


def test_reinforcing_a_procedure_from_two_stale_copies_counts_both(store):
    from hunter_gatherer.memory.procedural import ProceduralMiner

    miner = ProceduralMiner(store, store.embedder)
    procedure = store.add(Memory(text="A fictional procedure.", type="procedural",
                                 metadata={"objective": "tidy", "tools": ["a"], "occurrences": 1}))
    first, second = store.get(procedure.id), store.get(procedure.id)
    miner._bump(first, ["b"])
    miner._bump(second, ["c"])
    after = store.get(procedure.id)
    assert after.metadata["occurrences"] == 3 and after.metadata["tools"] == ["a", "b", "c"]


# ── embedding work and the vector index ─────────────────────────────────────

class ProbingEmbedder(HashingEmbedder):
    """Counts model calls and checks whether the store lock is free during them."""

    def __init__(self):
        super().__init__(dim=128)
        self.store, self.calls, self.lock_was_free = None, 0, []

    def embed_batch(self, texts):
        self.calls += 1
        if self.store is not None:
            free = []

            def probe():
                acquired = self.store._lock.acquire(timeout=2)
                free.append(acquired)
                if acquired:
                    self.store._lock.release()

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join()
            self.lock_was_free.extend(free)
        return super().embed_batch(texts)


@pytest.fixture
def probed(settings):
    embedder = ProbingEmbedder()
    store = MemoryStore(settings, embedder=embedder)
    embedder.store = store
    yield store, embedder
    store.close()


def test_embedding_never_holds_the_store_lock(probed):
    store, embedder = probed
    memory = store.add(Memory(text="The fictional lantern is blue."))
    store.supersede(memory.id, Memory(text="The fictional lantern is green."))

    def reword(current):
        current.text = "The fictional lantern is amber."
        return current

    other = store.add(Memory(text="A second fictional fact."))
    store.mutate(other.id, reword)
    assert embedder.lock_was_free and all(embedder.lock_was_free)


def test_changing_labels_does_not_embed_again_and_filters_stay_correct(probed):
    store, embedder = probed
    memory = store.add(Memory(text="The fictional lantern is blue."))
    calls = embedder.calls

    def relabel(current):
        current.type, current.tags = "procedural", ["lamp"]
        return mark_sensitive(current)

    store.mutate(memory.id, relabel)
    reread = store.get(memory.id)
    reread.importance = 8
    store.update(reread)
    assert embedder.calls == calls
    hits = store.vector_search("fictional lantern", top_k=3, filters={"type": "procedural"})
    assert [hit.id for hit in hits] == [memory.id]
    assert hits[0].payload["importance"] == 8


def test_another_process_sees_rewording_and_embeds_only_what_changed(store, other):
    store.add(Memory(text="The fictional heron prefers quartz."))
    target = store.add(Memory(text="The fictional otter prefers basalt."))
    assert other.vector_search("otter basalt", top_k=1)[0].id == target.id
    before = other.write_diagnostics()

    def reword(current):
        current.text = "The fictional otter prefers obsidian."
        return current

    store.mutate(target.id, reword)
    assert other.vector_search("otter obsidian", top_k=1)[0].id == target.id
    after = other.write_diagnostics()
    assert after["index_refreshes"] == before["index_refreshes"] + 1
    assert after["index_refreshed_records"] == before["index_refreshed_records"] + 1
    other.vector_search("otter obsidian", top_k=1)        # nothing committed since: no rescan
    assert other.write_diagnostics()["index_refreshes"] == after["index_refreshes"]


class FlakyIndex(VectorBackend):
    """An external index that can go offline; records stay in SQLite regardless."""

    def __init__(self, inner):
        self.inner, self.online = inner, True

    def upsert(self, memories, dense_vecs):
        if not self.online:
            raise ConnectionError("index offline")
        self.inner.upsert(memories, dense_vecs)

    def search(self, query_dense, query_text, top_k, filters=None):
        return self.inner.search(query_dense, query_text, top_k, filters)

    def delete(self, ids):
        if not self.online:
            raise ConnectionError("index offline")
        self.inner.delete(ids)

    def count(self):
        return self.inner.count()

    def close(self):
        pass


def test_a_failed_index_update_keeps_the_record_and_is_repaired_later(store):
    index = store._backend = FlakyIndex(store._backend)
    index.online = False
    memory = store.add(Memory(text="The fictional lantern is blue."))     # no error: it is stored
    assert store.get(memory.id).text == "The fictional lantern is blue."
    assert not store.vector_search("fictional lantern blue", top_k=3)
    assert store.write_diagnostics()["index_pending"] == 1
    with sqlite3.connect(store.settings.sqlite_path) as reader:
        assert reader.execute("SELECT id FROM memory_index_work").fetchone() == (memory.id,)
    index.online = True
    assert store.reconcile_index() == {"indexed": 1, "removed": 0, "pending": 0}
    assert [hit.id for hit in store.vector_search("fictional lantern blue", top_k=3)] == [memory.id]
    assert store.write_diagnostics()["index_pending"] == 0


def test_diagnostics_count_conflicts_without_recording_content(store):
    memory = store.add(Memory(text="A fictional secret phrase."))
    stale = store.get(memory.id)
    store.delete(memory.id)
    with pytest.raises(MemoryConflictError):
        store.update(stale)
    report = store.write_diagnostics()
    assert report["conflicts"] == {"deleted": 1}
    assert memory.id not in str(report) and "secret" not in str(report)


# ── recovering from interrupted work ─────────────────────────────────────────

def test_a_statement_that_fails_under_the_lock_leaves_no_open_transaction(store, other):
    import sqlite3

    memory = store.add(Memory(text="A fictional fact.", importance=5))
    with pytest.raises(sqlite3.OperationalError), store._lock:
        # An older-style write: no rollback of its own if a statement fails.
        store._sqlite.execute("UPDATE memories SET importance = 1 WHERE id = ?", (memory.id,))
        store._sqlite.execute("UPDATE no_such_table SET anything = 1")
        store._sqlite.commit()
    assert not store._sqlite.in_transaction            # settled as the lock was released
    assert store.get(memory.id).importance == 5        # the failed operation was undone as a whole
    saved = store.add(Memory(text="Saved after the failure."))
    assert other.get(saved.id) is not None             # and later writes really commit
    assert store.write_diagnostics()["store_lock"]["unfinished_transactions_settled"] == 1


def test_a_callers_own_open_transaction_is_continued_then_committed(store, other):
    memory = store.add(Memory(text="A fictional fact.", importance=5))
    # Opened by the caller before any store call, as some integrations do.
    store._sqlite.execute("UPDATE memories SET importance = 7 WHERE id = ?", (memory.id,))
    assert store.get(memory.id).importance == 7        # reads inside it see it, and leave it open
    assert store._sqlite.in_transaction
    saved = store.add(Memory(text="Written inside the caller's transaction."))
    assert not store._sqlite.in_transaction
    assert other.get(saved.id) is not None and other.get(memory.id).importance == 7


def test_a_write_made_during_a_read_snapshot_is_kept(store, other):
    from hunter_gatherer.memory.library_health import _read_snapshot

    with _read_snapshot(store):
        store.count()
        saved = store.add(Memory(text="Written while a read snapshot was open."))
    assert not store._sqlite.in_transaction
    assert other.get(saved.id) is not None


def test_lock_waits_are_counted_for_diagnostics(store):
    before = store.write_diagnostics()["store_lock"]
    holding, release = threading.Event(), threading.Event()

    def hold():
        with store._lock:
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(5)
    waiter = threading.Thread(target=store.assert_embedder_current)   # needs the store lock
    waiter.start()
    time.sleep(0.05)
    release.set()
    holder.join(5)
    waiter.join(5)
    after = store.write_diagnostics()["store_lock"]
    assert after["waited"] == before["waited"] + 1
    assert after["wait_ms_max"] > 0 and after["acquisitions"] > before["acquisitions"]


class CachingEmbedder(ProbingEmbedder):
    """Like the neural embedder: remembers texts, so only new ones reach the model."""

    caches_by_text = True

    def __init__(self):
        super().__init__()
        self.seen, self.model_runs = {}, []

    def embed_batch(self, texts):
        fresh = [text for text in texts if text not in self.seen]
        if fresh:
            before = len(self.lock_was_free)
            for text, vector in zip(fresh, super().embed_batch(fresh), strict=True):
                self.seen[text] = vector
            self.model_runs.append((tuple(fresh), all(self.lock_was_free[before:])))
        return [list(self.seen[text]) for text in texts]

    def embed(self, text):
        return self.embed_batch([text])[0]

    def fingerprint(self):
        return "caching-test:128"          # not a hashing fingerprint: semantic paths apply


def test_a_profile_is_built_under_one_hold_of_the_store_lock(store, settings):
    from types import SimpleNamespace

    from hunter_gatherer.memory.profile import build_profile

    for index in range(40):
        store.add(Memory(text=f"Fictional standing fact {index} about quartz."))
    holds = store.write_diagnostics()["store_lock"]["acquisitions"]
    report = build_profile(SimpleNamespace(store=store, settings=settings), "Which fact mentions quartz?")
    assert report["count"] > 0 and report["memory_total"] == 40
    # Dozens of small reads, one hold. Taken one at a time, every other client's
    # operation could cut in between them, and a busy server starved the profile.
    assert store.write_diagnostics()["store_lock"]["acquisitions"] - holds == 1


def test_a_profile_embeds_its_question_before_taking_the_store_lock(settings):
    from types import SimpleNamespace

    from hunter_gatherer.memory.profile import build_profile

    embedder = CachingEmbedder()
    store = MemoryStore(settings, embedder=embedder)
    try:
        store.add(Memory(text="The fictional heron prefers quartz."))
        embedder.store = store
        report = build_profile(SimpleNamespace(store=store, settings=settings), "Which stone does the heron prefer?")
        assert report["count"] == 1
        asked = [run for run in embedder.model_runs if "Which stone does the heron prefer?" in run[0]]
        assert asked == [(("Which stone does the heron prefer?",), True)]    # once, with the lock free
    finally:
        store.close()


@pytest.mark.parametrize("contextual", [False, True])
def test_a_search_embeds_its_question_before_taking_the_store_lock(settings, contextual):
    from types import SimpleNamespace

    from hunter_gatherer.memory.simple_search import find

    embedder = CachingEmbedder()
    store = MemoryStore(settings, embedder=embedder)
    try:
        store.add(Memory(text="The fictional heron prefers quartz."))
        embedder.store = store
        find(SimpleNamespace(store=store, settings=settings), "Which stone does the heron prefer?",
             requirements=["heron habitat"], contextual_requirements=contextual)
        # With the question as context, a detail is searched, and embedded, as one text.
        detail = "Which stone does the heron prefer?\nheron habitat" if contextual else "heron habitat"
        asked = [run for run in embedder.model_runs if "Which stone does the heron prefer?" in run[0]]
        assert asked == [(("Which stone does the heron prefer?", detail), True)]
        assert not [run for run in embedder.model_runs if detail in run[0] and not run[1]]
    finally:
        store.close()


class StandInModel:
    """Takes bge-small's place inside the real neural embedder: float32 vectors,
    and a count of the texts it encodes while the store lock is held."""

    def __init__(self):
        self.vectors, self.store, self.texts_under_lock = HashingEmbedder(dim=64), None, 0

    def encode(self, texts, **_options):
        # These tests use one thread, so a held store lock is held by the search.
        if self.store is not None and self.store._lock.depth:
            self.texts_under_lock += len(texts)
        return [array("f", vector) for vector in self.vectors.embed_batch(list(texts))]


def test_repeated_searches_of_a_large_library_run_no_model_under_the_store_lock(settings):
    """Find scores up to 40 candidates per question by their descriptions,
    inside the store lock. With 2,000 memories the embedder's cache used to
    forget those vectors between searches, so every search re-ran the model."""
    from types import SimpleNamespace

    from hunter_gatherer.memory.embeddings import SentenceTransformerEmbedder
    from hunter_gatherer.memory.simple_search import find

    model, asked_under_lock = StandInModel(), []

    class NeuralEmbedder(SentenceTransformerEmbedder):
        def embed_batch(self, texts):
            if model.store is not None and model.store._lock.depth:
                asked_under_lock.append(len(texts))
            return super().embed_batch(texts)

    embedder = NeuralEmbedder("fictional-model")       # not hashing: candidates are scored
    embedder._model, embedder.dim = model, 64
    store = MemoryStore(settings, embedder=embedder)
    places = ("heron", "otter", "kestrel", "lantern", "quartz", "basalt", "willow", "copper",
              "glacier", "harbor", "juniper", "marble", "cedar", "falcon", "orchid", "pebble")
    things = ("tide charts", "seed bank", "trail markers", "field kit", "weather logs", "map tiles",
              "rope inventory", "camp roster")
    try:
        rows = [Memory(text=f"The fictional {places[n % 16]} team keeps the {places[n // 16 % 16]} "
                            f"{things[n // 256 % 8]}; entry {n}.",
                       tags=["fieldwork"] if n % 3 else [],
                       metadata={"category": "project_context"} if n % 5 else {}).to_sql_row()
                for n in range(2000)]
        with store.write_transaction():
            store._sqlite.executemany("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        store.reconcile_index()                            # indexed as if another process wrote them
        model.store = store
        harness = SimpleNamespace(store=store, settings=settings)
        questions = [f"Which {thing} does the {place} team keep?" for place in places[:8] for thing in things[:4]]

        def search_all():
            return [[item["citation"] for item in find(harness, question)["relevant_memories"]]
                    for question in questions]

        first = search_all()                               # each description is embedded once
        model.texts_under_lock, asked_under_lock[:] = 0, []
        assert search_all() == first                       # the same memories, in the same order
        assert sum(asked_under_lock) > 500                 # candidates were scored inside the lock...
        assert model.texts_under_lock == 0                 # ...without running the model there
    finally:
        store.close()


def test_the_embedding_cache_returns_exactly_what_the_model_produced():
    from hunter_gatherer.memory.embeddings import SentenceTransformerEmbedder

    produced = {"float32": array("f", [0.1, -0.25, 1 / 3]).tolist(), "float64": [0.1, -0.25, 1 / 3]}
    runs = []

    class Model:
        def encode(self, texts, **_options):
            runs.append(list(texts))
            return [array("d", produced[text]) for text in texts]

    embedder = SentenceTransformerEmbedder("fictional-model")
    embedder._model, embedder.dim = Model(), 3
    first = embedder.embed_batch(["float32", "float64"])
    first[0][0] = 99.0                                     # a caller's edit stays the caller's
    again = embedder.embed_batch(["float64", "float32", "float32"])
    assert runs == [["float32", "float64"]]
    assert again == [produced["float64"], produced["float32"], produced["float32"]]
    assert again[1] is not again[2]
    # Held in 4 bytes per value when that changes nothing, in 8 otherwise.
    assert sorted(vector.itemsize for vector in embedder._dense_cache.values()) == [4, 8]


def test_a_search_retries_index_entries_that_failed_earlier(store):
    index = store._backend = FlakyIndex(store._backend)
    index.online = False
    memory = store.add(Memory(text="The fictional lantern is blue."))
    assert not store.vector_search("fictional lantern blue", top_k=3)     # retried, still offline
    index.online = True
    assert not store.vector_search("fictional lantern blue", top_k=3)     # not retried again so soon
    store._index_retry_at = 0.0                                            # ...until the wait is over
    assert [hit.id for hit in store.vector_search("fictional lantern blue", top_k=3)] == [memory.id]
    assert store.write_diagnostics()["index_pending"] == 0


def test_opening_a_library_repairs_a_persistent_index_left_behind_by_a_crash(settings):
    pytest.importorskip("sqlite_vec")
    settings.vector_backend = "sqlite_vec"
    store = MemoryStore(settings, embedder=HashingEmbedder(dim=128))
    unindexed = store.add(Memory(text="The fictional heron prefers quartz."))
    forgotten = store.add(Memory(text="The fictional otter prefers basalt."))
    kept = store.add(Memory(text="The fictional kestrel prefers marble."))
    # What a process that stopped between the record and its index entry leaves:
    # a record without a vector, and a vector for a record that was forgotten.
    store._sqlite.execute("DELETE FROM memory_vec WHERE id = ?", (unindexed.id,))
    store._sqlite.execute("UPDATE memories SET deleted_at = 1.0 WHERE id = ?", (forgotten.id,))
    store._sqlite.commit()
    store.close()

    reopened = MemoryStore(settings, embedder=HashingEmbedder(dim=128))
    try:
        indexed = {row[0] for row in reopened._sqlite.execute("SELECT id FROM memory_vec")}
        assert indexed == {unindexed.id, kept.id}
        assert reopened.vector_search("heron quartz", top_k=1)[0].id == unindexed.id
        assert reopened.get(forgotten.id).deleted_at is not None
    finally:
        reopened.close()


def test_searching_while_other_threads_read_does_not_freeze_with_sqlite_vec(settings):
    """A SQL function calling back into Python deadlocked here: the callback waits for
    the interpreter lock while a reader holding it waits for the shared connection."""
    pytest.importorskip("sqlite_vec")
    settings.vector_backend = "sqlite_vec"
    store = MemoryStore(settings, embedder=HashingEmbedder(dim=128))
    try:
        ids = [store.add(Memory(text=f"Fictional copper sensor fact {index} about quartz.")).id
               for index in range(120)]
        searching = threading.Event()
        searching.set()

        def search():
            try:
                for _ in range(120):
                    assert store.vector_search("copper sensor quartz", top_k=5)
            finally:
                searching.clear()

        def read():
            position = 0
            while searching.is_set():
                store.get(ids[position % len(ids)])
                position += 1

        threads = [threading.Thread(target=search, daemon=True)]
        threads += [threading.Thread(target=read, daemon=True) for _ in range(3)]
        for thread in threads:
            thread.start()
        threads[0].join(60)
        frozen = threads[0].is_alive()
        searching.clear()
        assert not frozen, "search and concurrent reads deadlocked"
        for thread in threads[1:]:
            thread.join(10)
    finally:
        if not threads[0].is_alive():
            store.close()


@pytest.mark.parametrize("through", ["store", "connection"])
def test_reads_from_many_threads_return_the_row_that_was_asked_for(store, through):
    """One connection serves every thread. On current Python versions, threads running
    the same SQL shared a cached statement and received each other's rows."""
    memories = [store.add(Memory(text=f"Fictional fact number {index}.")) for index in range(60)]
    wrong, failures = [], []

    def read(offset):
        try:
            for step in range(1500):
                wanted = memories[(offset + step * 7) % len(memories)]
                if through == "store":
                    found = store.get(wanted.id)
                    seen = (found.id, found.text) if found is not None else None
                else:   # a code path that forgets the store lock must still be safe
                    row = store._sqlite.execute("SELECT id, text FROM memories WHERE id = ?",
                                                (wanted.id,)).fetchone()
                    seen = tuple(row) if row is not None else None
                if seen != (wanted.id, wanted.text):
                    wrong.append((wanted.id, seen))
        except Exception as error:  # noqa: BLE001 — any failure here is the defect
            failures.append(error)

    threads = [threading.Thread(target=read, args=(number * 13,)) for number in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not failures and not wrong


def test_concurrent_sqlite_text_updates_commit_the_vector_for_the_winning_revision(settings, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    pytest.importorskip("sqlite_vec")
    settings.vector_backend = "sqlite_vec"
    model = HashingEmbedder(dim=128)
    with MemoryStore(settings, embedder=model) as first, MemoryStore(settings, embedder=HashingEmbedder(dim=128)) as second:
        original = first.add(Memory(text="The fictional label is blue."))
        pending = first.get(original.id)
        pending.text = "The fictional label is violet."
        entered, resume = threading.Event(), threading.Event()
        embed = model.embed_batch

        def wait_for_newer_writer(texts):
            if texts == [pending.text]:
                entered.set()
                assert resume.wait(10)
            return embed(texts)

        monkeypatch.setattr(model, "embed_batch", wait_for_newer_writer)
        with ThreadPoolExecutor(max_workers=1) as workers:
            stale = workers.submit(first.update, pending)
            try:
                assert entered.wait(10)
                winner = second.get(original.id)
                winner.text = "The fictional label is orange."
                second.update(winner)
            finally:
                resume.set()
            with pytest.raises(MemoryConflictError):
                stale.result(timeout=10)
    with MemoryStore(settings, embedder=model) as reopened:
        assert reopened.get(original.id).text == winner.text
        actual = reopened._sqlite.execute("SELECT embedding FROM memory_vec WHERE id = ?", (winner.id,)).fetchone()[0]
        assert actual == array("f", model.embed_batch([winner.text])[0]).tobytes()
        assert reopened.reconcile_index() == {"indexed": 0, "removed": 0, "pending": 0}


@pytest.mark.parametrize("operation", ["update", "mutate"])
def test_embedding_failure_does_not_commit_a_changed_sqlite_row(settings, monkeypatch, operation):
    pytest.importorskip("sqlite_vec")
    settings.vector_backend = "sqlite_vec"
    model = HashingEmbedder(dim=128)
    with MemoryStore(settings, embedder=model) as store:
        original = store.add(Memory(text="The fictional label is blue."))

        def unavailable(texts):
            raise RuntimeError("fictional embedding failure")

        def reword(current):
            current.text = "The fictional label is violet."
            return current

        monkeypatch.setattr(model, "embed_batch", unavailable)
        with pytest.raises(RuntimeError, match="embedding failure"):
            if operation == "update":
                store.update(reword(store.get(original.id)))
            else:
                store.mutate(original.id, reword)
        assert store.get(original.id).text == original.text


def test_deferred_external_delivery_preserves_newest_text_and_survives_a_restart(settings, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from hunter_gatherer.memory import store as store_module
    from hunter_gatherer.memory.store import InMemoryBackend

    model = HashingEmbedder(dim=128)
    index = FlakyIndex(InMemoryBackend(model))
    remote = settings.model_copy(update={"vector_backend": "qdrant"})
    monkeypatch.setattr(store_module, "_qdrant_modules", lambda: ())
    backend = MemoryStore._init_backend
    monkeypatch.setattr(MemoryStore, "_init_backend",
                        lambda self: index if self.settings.vector_backend == "qdrant" else backend(self))
    with MemoryStore(remote, embedder=model) as first, MemoryStore(remote, embedder=model) as second:
        original = first.add(Memory(text="The fictional label is blue."))
        old = first.get(original.id)
        old.text = "The fictional label is violet."
        deliver = first._deliver_external
        committed, resume = threading.Event(), threading.Event()

        def delay_after_commit(*args):
            committed.set()
            assert resume.wait(10)
            deliver(*args)

        monkeypatch.setattr(first, "_deliver_external", delay_after_commit)
        with ThreadPoolExecutor(max_workers=1) as workers:
            late_delivery = workers.submit(first.update, old)
            try:
                assert committed.wait(10)
                latest = second.get(original.id)
                latest.text = "The fictional label is orange."
                second.update(latest)
            finally:
                resume.set()
            late_delivery.result(timeout=10)
        assert index.inner.digests()[original.id] == index.inner.text_digest(latest.text)

        with pytest.raises(RuntimeError), second.write_transaction():
            second.add(Memory(id="fictional-rollback", text="Fictional abandoned update."))
            raise RuntimeError("rollback")
        assert "fictional-rollback" not in index.inner.digests()

        index.online = False
        latest.text = "The fictional label is white."
        second.update(latest)
        assert second.write_diagnostics()["index_pending"] == 1
        assert not second.vector_search(latest.text)  # no stale hit during the outage
    with MemoryStore(settings, embedder=model) as local:
        latest = local.get(original.id)
        latest.text = "The fictional label is green."
        local.update(latest)
    index.online = True
    with MemoryStore(remote, embedder=model) as reopened:
        assert reopened.write_diagnostics()["index_pending"] == 0
        assert index.inner.digests()[original.id] == index.inner.text_digest(latest.text)


def test_find_repairs_a_sqlite_vector_after_another_backend_commits(settings):
    from types import SimpleNamespace
    from hunter_gatherer.memory.simple_search import find

    pytest.importorskip("sqlite_vec")
    persistent = settings.model_copy(update={"vector_backend": "sqlite_vec"})
    model = HashingEmbedder(dim=128)
    with MemoryStore(persistent, embedder=model) as reader, MemoryStore(settings, embedder=model) as writer:
        original = reader.add(Memory(text="The fictional label is blue."))
        edited = writer.get(original.id)
        edited.text = "The fictional label is pink."
        writer.update(edited)
        assert reader.write_diagnostics()["index_pending"] == 1
        relabeled = reader.get(original.id)
        relabeled.tags = ["fieldwork"]
        reader.update(relabeled)
        assert reader.write_diagnostics()["index_pending"] == 1
        find(SimpleNamespace(store=reader, settings=persistent), "fictional pink label")
        assert reader.write_diagnostics()["index_pending"] == 0
        data = reader._sqlite.execute("SELECT embedding FROM memory_vec WHERE id = ?", (original.id,)).fetchone()[0]
        assert data == array("f", model.embed_batch([edited.text])[0]).tobytes()
        following = writer.add(Memory(text="The fictional kestrel flies."))
        assert reader.vector_search(following.text, top_k=1)[0].id == following.id
        assert reader.write_diagnostics()["index_pending"] == 0


def test_initial_index_registration_recovers_a_malformed_legacy_ledger(settings):
    pytest.importorskip("sqlite_vec")
    model = HashingEmbedder(dim=128)
    with MemoryStore(settings, embedder=model) as memory_store:
        original = memory_store.add(Memory(text="Fictional copper compass."))
        memory_store.set_meta("memory_index_pending", "legacy ledger is truncated [")
    persistent = settings.model_copy(update={"vector_backend": "sqlite_vec"})
    with MemoryStore(persistent, embedder=model) as recovered:
        assert recovered.vector_search(original.text, top_k=1)[0].id == original.id
        assert recovered.write_diagnostics()["index_pending"] == 0


@pytest.mark.parametrize("when", ["before", "during"])
def test_local_model_migration_checks_registered_qdrant_even_during_staging(settings, monkeypatch, when):
    from hunter_gatherer.memory import store as store_module
    from hunter_gatherer.memory.store import InMemoryBackend

    original_model = HashingEmbedder(dim=128)
    target_model = HashingEmbedder(dim=64)
    with MemoryStore(settings, embedder=original_model) as original:
        memory = original.add(Memory(text="The fictional label is blue."))
    remote = settings.model_copy(update={"vector_backend": "qdrant"})
    monkeypatch.setattr(store_module, "_qdrant_modules", lambda: ())
    monkeypatch.setattr(MemoryStore, "_init_backend", lambda self: InMemoryBackend(self.embedder))

    def register_remote():
        with MemoryStore(remote, embedder=original_model):
            pass

    if when == "before":
        register_remote()
    else:
        embed = target_model.embed_batch

        def register_while_staging(texts):
            register_remote()
            return embed(texts)

        monkeypatch.setattr(target_model, "embed_batch", register_while_staging)
    with MemoryStore(settings, embedder=target_model, allow_embedder_mismatch=True) as migrating:
        with pytest.raises(RuntimeError, match="Qdrant collection and alias migration"):
            migrating.rebuild_vectors()
        assert migrating.get_meta("embedder_fingerprint") == original_model.fingerprint()
        assert migrating.get(memory.id).text == memory.text
    with MemoryStore(settings, embedder=original_model) as same_model:
        assert same_model.rebuild_vectors() == 1


def test_a_local_only_library_can_still_migrate_embedding_models(settings):
    old = HashingEmbedder(dim=128)
    new = HashingEmbedder(dim=64)
    with MemoryStore(settings, embedder=old) as first:
        memory = first.add(Memory(text="A fictional blue label."))
    with MemoryStore(settings, embedder=new, allow_embedder_mismatch=True) as migrating:
        assert migrating.rebuild_vectors() == 1
        assert migrating.get_meta("embedder_fingerprint") == new.fingerprint()
        assert migrating.vector_search(memory.text, top_k=1)[0].id == memory.id
