"""Where a library lives decides how its file is shared. Nobody has to choose.

A store on a local disk uses SQLite's write-ahead log; anywhere that is not
safe it keeps the rollback journal. These tests cover how a location is
recognized, the switch in both directions, the refusal that protects a library
reached from two computers, the upkeep that keeps the main file complete, and
what a client is told when another process keeps the file too long.
"""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

from hunter_gatherer import storage
from hunter_gatherer.memory import store as store_module
from hunter_gatherer.memory.schemas import Memory
from hunter_gatherer.memory.store import MemoryStore
from hunter_gatherer.storage import Location

# The detection itself, kept from before any test replaces it.
classify = storage.classify
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="reads macOS filesystem and cloud-provider signals")


def lives_on(monkeypatch, kind: str, detail: str = "testfs") -> None:
    monkeypatch.setattr(storage, "classify", lambda path: Location(kind, detail))


def size(database: Path, suffix: str) -> int | None:
    path = Path(str(database) + suffix)
    return path.stat().st_size if path.exists() else None


def fake_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


# ── recognizing a location ────────────────────────────────────────────────────
MOUNTS = """\
22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw
40 22 0:30 / /data/team rw,relatime shared:2 master:1 - btrfs /dev/sdb1 rw
41 40 0:31 / /data/team/share rw - cifs //server/share rw
42 22 0:32 / /mnt/with\\040space rw - nfs4 server:/export rw
43 22 0:33 / /mnt/odd rw - fuse.mystery mystery rw
44 22 0:34 / /mnt/hidden rw - nfs server:/x rw
45 22 0:35 / /mnt/hidden rw - ext4 /dev/sdc1 rw
"""


def test_linux_mounts_are_matched_by_the_longest_mount_point():
    def on(path):
        return storage._linux_filesystem(PurePosixPath(path), MOUNTS)

    assert on("/srv/library") == Location("local", "ext4")
    assert on("/data/team/notes") == Location("local", "btrfs")
    assert on("/data/teamname/notes") == Location("local", "ext4")      # not inside /data/team
    assert on("/data/team/share/library") == Location("network", "cifs")
    assert on("/mnt/with space/library") == Location("network", "nfs4")
    assert on("/mnt/odd/library") == Location("unknown", "fuse.mystery")   # unrecognized is never "local"
    assert on("/mnt/hidden/library") == Location("local", "ext4")       # the later mount hides the earlier one
    assert storage._linux_filesystem(PurePosixPath("/anywhere"), "") == Location("unknown", "")


def test_sync_folders_are_recognized_by_name_and_by_marker(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for folder in ("Dropbox/library", "OneDrive - Example/library", "Google Drive/library", "Notes/library",
                   "Projects/shared/deep/library", "Projects/plain/library"):
        (home / folder).mkdir(parents=True)
    (home / "Projects" / "shared" / ".stfolder").mkdir()
    fake_home(monkeypatch, home)

    def service(relative):
        return storage._sync_service(Path(os.path.realpath(home / relative)))

    assert service("Dropbox/library") == "Dropbox"
    assert service("OneDrive - Example/library") == "OneDrive"
    assert service("Google Drive/library") == "Google Drive"
    assert service("Projects/shared/deep/library") == "Syncthing"      # marked two folders up
    assert service("Notes/library") == "" and service("Projects/plain/library") == ""
    (home / "Projects" / "plain" / ".tmp.driveupload").mkdir()
    assert service("Projects/plain/library") == "Google Drive"


def test_classify_puts_the_drive_first_and_never_raises(tmp_path, monkeypatch):
    fake_home(monkeypatch, tmp_path)
    (tmp_path / "Dropbox").mkdir()
    monkeypatch.setattr(storage, "_filesystem", lambda folder: Location("local", "testfs"))
    assert classify(tmp_path / "plain" / "not-created-yet") == Location("local", "testfs")
    assert classify(tmp_path / "Dropbox" / "library") == Location("synced", "Dropbox")
    monkeypatch.setattr(storage, "_filesystem", lambda folder: Location("network", "smbfs"))
    assert classify(tmp_path / "Dropbox" / "library") == Location("network", "smbfs")

    def broken(folder):
        raise OSError("device went away")

    monkeypatch.setattr(storage, "_filesystem", broken)
    assert classify(tmp_path) == Location("unknown", "")


@darwin_only
def test_macos_reads_the_filesystem_and_a_cloud_providers_mark(tmp_path, monkeypatch):
    here = classify(tmp_path)
    assert here.kind == "local" and here.detail in storage._DARWIN_LOCAL
    # A folder a cloud provider manages carries an attribute naming the provider's domain.
    # The real attribute name is reserved to the system, so this looks for a stand-in.
    monkeypatch.setattr(storage, "_PROVIDER_ATTRIBUTE", "org.hunter-gatherer.test-domain")
    managed = tmp_path / "managed"
    (managed / "deep" / "library").mkdir(parents=True)
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                              ctypes.c_uint32, ctypes.c_int]
    libc.setxattr.restype = ctypes.c_int
    value = b"com.example.Dropbox.FileProvider/3F2A-ACCOUNT-IDENTIFIER"
    assert libc.setxattr(os.fsencode(managed), b"org.hunter-gatherer.test-domain", value, len(value), 0, 0) == 0
    assert classify(managed / "deep" / "library") == Location("synced", "Dropbox")   # named, without the identifier
    assert classify(tmp_path / "elsewhere").kind == "local"


@darwin_only
def test_macos_recognizes_icloud_and_managed_cloud_folders(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    managed = home / "Library" / "CloudStorage"
    for folder in (cloud / "Notes", home / "Documents" / "library", home / "Desktop",
                   managed / "OneDrive-Personal" / "library", managed / "GoogleDrive-example-account" / "My Drive",
                   home / "Library" / "Application Support" / "hunter-gatherer"):
        folder.mkdir(parents=True)
    (cloud / "Documents").symlink_to(home / "Documents")       # iCloud's Desktop & Documents option, on for Documents
    fake_home(monkeypatch, home)
    assert classify(cloud / "Notes") == Location("synced", "iCloud Drive")
    assert classify(home / "Documents" / "library") == Location("synced", "iCloud Drive")
    assert classify(home / "Desktop").kind == "local"
    assert classify(managed / "OneDrive-Personal" / "library") == Location("synced", "OneDrive")
    assert classify(managed / "GoogleDrive-example-account" / "My Drive") == Location("synced", "Google Drive")
    assert classify(home / "Library" / "Application Support" / "hunter-gatherer").kind == "local"


# ── choosing and explaining ───────────────────────────────────────────────────
def test_only_a_local_disk_gets_the_write_ahead_log():
    assert storage.preferred_journal(Location("local")) == "wal"
    for kind in ("network", "synced", "unknown"):
        assert storage.preferred_journal(Location(kind)) == "delete"
        assert storage.preferred_journal(Location(kind), "wal") == "delete"
    with pytest.raises(ValueError):
        storage.preferred_journal(Location("local"), "persist")


def test_reports_explain_the_choice_and_warn_about_risky_folders():
    local = storage.report(Location("local", "apfs"), "wal")
    assert (local["chosen"], local["switch_pending"], local["warning"]) == ("automatically", False, None)
    assert "local disk (apfs)" in local["explanation"]
    synced = storage.report(Location("synced", "iCloud Drive"), "wal")
    assert synced["switch_pending"] and "iCloud Drive" in synced["explanation"]
    assert "hunt backup create" in synced["warning"]
    network = storage.report(Location("network", "smbfs"), "delete")
    assert not network["switch_pending"] and "hunt serve" in network["warning"]
    unknown = storage.report(Location("unknown", "fuse.mystery"), "delete")
    assert unknown["warning"] is None and "safe everywhere" in unknown["explanation"]
    pinned = storage.report(Location("local"), "delete", "delete")
    assert (pinned["chosen"], pinned["switch_pending"]) == ("by configuration", False)


# ── a store on a local disk ───────────────────────────────────────────────────
def test_a_local_store_uses_the_log_and_keeps_the_main_file_complete(settings, embedder, tmp_path, monkeypatch):
    lives_on(monkeypatch, storage.LOCAL)
    database = Path(settings.sqlite_path)
    with MemoryStore(settings, embedder=embedder) as store:
        assert store.storage_report()["journal_mode"] == "wal"
        memory = store.add(Memory(text="The fictional heron is grey."))
        assert size(database, "-wal") == 0                       # folded in as the write finished
        copy = tmp_path / "copied-alone.db"
        shutil.copyfile(database, copy)                          # the main file by itself, as a person might copy it
        with contextlib.closing(sqlite3.connect(copy)) as alone:
            assert alone.execute("SELECT text FROM memories WHERE id=?", (memory.id,)).fetchone()[0] == memory.text
        assert store.write_diagnostics()["log_folds"] >= 1
    assert storage.file_journal_mode(database) == "wal"
    assert size(database, "-wal") is None and size(database, "-shm") is None    # nothing left beside it


def test_folding_waits_for_a_reader_and_then_catches_up(settings, embedder, monkeypatch):
    lives_on(monkeypatch, storage.LOCAL)
    database = Path(settings.sqlite_path)
    with MemoryStore(settings, embedder=embedder) as store, contextlib.closing(sqlite3.connect(database)) as reader:
        store.add(Memory(text="First fictional note."))
        reader.execute("BEGIN")
        assert reader.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
        started = time.perf_counter()
        store.add(Memory(text="Second fictional note."))         # with a rollback journal this would wait for the reader
        assert time.perf_counter() - started < 5
        assert size(database, "-wal") > 0 and store.write_diagnostics()["log_folds_deferred"] >= 1
        assert reader.execute("SELECT count(*) FROM memories").fetchone()[0] == 1   # its view is undisturbed
        reader.execute("COMMIT")
        store.settle()                                           # what the end of every tool call does
        assert size(database, "-wal") == 0
        assert reader.execute("SELECT count(*) FROM memories").fetchone()[0] == 2


def test_folding_leaves_open_transactions_and_unfinished_reads_alone(settings, embedder, monkeypatch):
    lives_on(monkeypatch, storage.LOCAL)
    database = Path(settings.sqlite_path)
    with MemoryStore(settings, embedder=embedder) as store:
        first = store.add(Memory(text="First fictional note."))
        with store.write_transaction():
            store.add(Memory(text="Second fictional note."))
            assert store.get(first.id) is not None and store._sqlite.in_transaction
        assert size(database, "-wal") == 0

        store._sqlite.execute("BEGIN")                           # a transaction the caller owns
        store._sqlite.execute("INSERT INTO store_meta VALUES ('fictional-key', 'kept open')")
        assert store.get(first.id) is not None
        assert store._sqlite.in_transaction                      # neither committed nor rolled back for it
        store._sqlite.rollback()
        assert store.get_meta("fictional-key") is None

        cursor = store._sqlite.execute("SELECT id FROM memories")
        cursor.fetchone()                                        # a result that is only half read
        store.add(Memory(text="Third fictional note."))          # cannot fold past it, and does not fail
        assert store.count() == 3
        cursor.close()
        store.settle()
        assert size(database, "-wal") == 0


# ── moving between safe and unsafe places ─────────────────────────────────────
def test_a_library_moved_somewhere_unsafe_switches_back_and_forth(settings, embedder, monkeypatch):
    lives_on(monkeypatch, storage.LOCAL)
    database = Path(settings.sqlite_path)
    with MemoryStore(settings, embedder=embedder) as store:
        memory = store.add(Memory(text="The fictional heron is grey."))
    assert storage.file_journal_mode(database) == "wal"

    lives_on(monkeypatch, storage.SYNCED, "Example Sync")
    with MemoryStore(settings, embedder=embedder) as store:
        report = store.storage_report()
        assert (report["journal_mode"], report["switch_pending"]) == ("delete", False)
        assert "Example Sync" in report["explanation"] and report["warning"]
        assert store.get(memory.id).text == "The fictional heron is grey."
        store.add(Memory(text="Written with the rollback journal."))
        assert size(database, "-wal") is None
    assert storage.file_journal_mode(database) == "delete"

    lives_on(monkeypatch, storage.LOCAL)
    with MemoryStore(settings, embedder=embedder) as store:
        assert store.storage_report()["journal_mode"] == "wal" and store.count() == 2

    pinned = settings.model_copy(update={"sqlite_journal": "delete"})     # for tests and measurements only
    with MemoryStore(pinned, embedder=embedder) as store:
        report = store.storage_report()
        assert (report["journal_mode"], report["chosen"]) == ("delete", "by configuration")
    with pytest.raises(ValueError, match="sqlite_journal"):
        MemoryStore(settings.model_copy(update={"sqlite_journal": "persist"}), embedder=embedder)


def test_a_switch_that_has_to_wait_happens_once_the_library_is_free(settings, embedder, monkeypatch):
    lives_on(monkeypatch, storage.LOCAL)
    database = Path(settings.sqlite_path)
    MemoryStore(settings, embedder=embedder).close()             # now a write-ahead database
    monkeypatch.setattr(store_module, "_JOURNAL_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(store_module, "_FOLD_CHECK_SECONDS", 0.0)
    lives_on(monkeypatch, storage.SYNCED, "Example Sync")
    other = sqlite3.connect(database)
    try:
        other.execute("SELECT count(*) FROM memories").fetchone()          # connected, idle
        with MemoryStore(settings, embedder=embedder) as store:
            report = store.storage_report()
            assert (report["journal_mode"], report["switch_pending"]) == ("wal", True)
            store.add(Memory(text="Still works while it waits."))
            other.close()
            assert store.count() == 1                            # any later call notices the file is free
            report = store.storage_report()
            assert (report["journal_mode"], report["switch_pending"]) == ("delete", False)
    finally:
        other.close()


def test_opening_does_not_wait_for_a_busy_library(settings, embedder, monkeypatch):
    lives_on(monkeypatch, storage.SYNCED)
    database = Path(settings.sqlite_path)
    MemoryStore(settings, embedder=embedder).close()             # a rollback-journal database
    monkeypatch.setattr(store_module, "LOCK_WAIT_SECONDS", 5.0)
    lives_on(monkeypatch, storage.LOCAL)
    with contextlib.closing(sqlite3.connect(database, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO store_meta VALUES ('fictional-key', 'being written')")
        started = time.perf_counter()
        with MemoryStore(settings, embedder=embedder) as store:
            assert time.perf_counter() - started < 3             # it did not wait out the writer
            report = store.storage_report()
            assert (report["journal_mode"], report["switch_pending"]) == ("delete", True)
            writer.execute("ROLLBACK")
    with MemoryStore(settings, embedder=embedder) as store:
        assert store.storage_report()["journal_mode"] == "wal"


@pytest.mark.parametrize("kind", [storage.NETWORK, storage.UNKNOWN])
@pytest.mark.parametrize("preference", ["auto", "delete", "wal"])
def test_a_write_ahead_library_in_use_where_computers_may_share_it_is_refused(
        settings, embedder, monkeypatch, kind, preference):
    lives_on(monkeypatch, storage.LOCAL)
    database = Path(settings.sqlite_path)
    with MemoryStore(settings, embedder=embedder) as store:
        memory = store.add(Memory(text="The fictional heron is grey."))

    lives_on(monkeypatch, kind, "examplefs")
    settings.sqlite_journal = preference
    with contextlib.closing(sqlite3.connect(database)) as elsewhere:        # as another computer might have it open
        assert elsewhere.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
        with pytest.raises(storage.UnsafeLocationError, match="write-ahead mode") as refusal:
            MemoryStore(settings, embedder=embedder)
        assert str(database.parent) not in str(refusal.value)
        text = elsewhere.execute("SELECT text FROM memories WHERE id=?", (memory.id,)).fetchone()[0]
        assert text == "The fictional heron is grey."            # the other user is undisturbed
    assert storage.file_journal_mode(database) == "wal"         # refused, not half-switched

    with MemoryStore(settings, embedder=embedder) as store:      # with nothing else connected it is switched
        assert store.storage_report()["journal_mode"] == "delete"
        assert store.get(memory.id).text == "The fictional heron is grey."


# ── another process keeps the file too long ───────────────────────────────────
@pytest.mark.parametrize("kind,hold,reads_continue", [
    (storage.SYNCED, "BEGIN EXCLUSIVE", False),      # rollback journal: everyone waits for the holder
    (storage.LOCAL, "BEGIN IMMEDIATE", True),        # write-ahead log: only other writers wait
])
def test_a_busy_library_is_reported_as_something_to_repeat(tmp_path, monkeypatch, kind, hold, reads_continue):
    from fastapi.testclient import TestClient

    from hunter_gatherer.library import build_library_harness, create_library, library_settings
    from hunter_gatherer.mcp.server import _http_options, build_server
    from hunter_gatherer.mcp.tools import call_tool
    from hunter_gatherer.server.library_app import build_library_app

    lives_on(monkeypatch, kind)
    monkeypatch.setattr(store_module, "LOCK_WAIT_SECONDS", 0.2)
    target = tmp_path / "library"
    create_library(target)
    harness = build_library_harness(library_settings(target))
    app = build_library_app(harness, build_server(harness=harness, tool_profile="library")[0],
                            http_options=_http_options("127.0.0.1", 8767))
    remember = {"action": "remember", "text": "The fictional heron is grey."}
    try:
        with contextlib.closing(sqlite3.connect(target / "library.db", isolation_level=None)) as holder:
            holder.execute(hold)
            holder.execute("INSERT INTO store_meta VALUES ('fictional-key', 'held')")
            result = call_tool(harness, "hg_memory", remember)
            assert (result["error_code"], result["retryable"]) == ("library_busy", True)
            assert "Repeat it" in result["error"] and "locked" not in result["error"]
            found = call_tool(harness, "hg_find", {"query": "fictional heron"})
            assert found.get("error_code") == (None if reads_continue else "library_busy")
            with TestClient(app, base_url="http://127.0.0.1:8767", raise_server_exceptions=False) as client:
                response = client.post("/api/library/memories", headers={"host": "127.0.0.1:8767"},
                                       json={"text": "A fictional note from the browser."})
                assert response.status_code == 503 and response.json()["error_code"] == "library_busy"
                assert response.headers["retry-after"] == "1"
            holder.execute("ROLLBACK")
        repeated = call_tool(harness, "hg_memory", remember)     # nothing was half-written: this is the first copy
        assert repeated["stored"] is True and harness.store.count() == 1
    finally:
        harness.loop.close()
