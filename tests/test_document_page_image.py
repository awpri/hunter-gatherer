"""Model-free original page rendering, privacy, read-only and bounds tests."""
import hashlib
import os
import importlib
from pathlib import Path
import sqlite3
import struct
import subprocess
import zlib

import pytest

from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.document_originals import load_document_original

backend = importlib.import_module("hunter_gatherer.memory.document_page_image")


def pdf_bytes(*, pages=2, encrypted=False, width=240, height=320):
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=width, height=height)
        stream = DecodedStreamObject()
        stream.set_data(f"{1 if index == 0 else 0} 0 {1 if index else 0} rg 0 0 {width} {height} re f".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def ingest(store, tmp_path, *, data=None, sensitive=False):
    source = tmp_path / "fixture.pdf"
    source.write_bytes(pdf_bytes() if data is None else data)
    result = DocumentArchive(store).ingest(str(source), sensitive=sensitive)
    return source, result["document_id"]


def png_bytes(width=16, height=24):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\x00\x00" * width) * height)) + chunk(b"IEND", b""))


def fake_render(data, page, max_side, **kwargs):
    return {"page_count": 2, "width": 16, "height": 24, "render": {"renderer": "test"}, "_image_png": png_bytes()}


def assert_safe_error(result, code):
    assert result["error_code"] == code, result
    assert set(result) == {"error", "error_code", "http_status"}
    assert "fixture" not in str(result) and "source.pdf" not in str(result)


def test_missing_archive_does_not_create_schema_or_files(store):
    changes = store._sqlite.total_changes
    before = store._sqlite.execute("SELECT name FROM sqlite_master").fetchall()
    assert_safe_error(backend.document_page_image(store, "absent", page=1), "archive_unavailable")
    assert store._sqlite.total_changes == changes
    assert store._sqlite.execute("SELECT name FROM sqlite_master").fetchall() == before
    assert not Path(store.settings.sqlite_path + ".archive").exists()


def test_exact_historical_original_newest_privacy_deletion_before_io(store, tmp_path, monkeypatch):
    source, old = ingest(store, tmp_path)
    old_bytes = source.read_bytes()
    source.write_bytes(pdf_bytes(pages=1))
    new = DocumentArchive(store).ingest(str(source), sensitive=True)["document_id"]
    import hunter_gatherer.memory.document_originals as originals
    called = []
    actual = originals._read_managed
    monkeypatch.setattr(originals, "_read_managed", lambda path: called.append(path) or actual(path))
    monkeypatch.setattr(backend, "_worker_render", lambda *args: pytest.fail("Denied access rendered"))
    assert_safe_error(backend.document_page_image(store, old, page=1), "document_unavailable")
    assert called == []
    assert load_document_original(store, old, include_sensitive=True)["_original_bytes"] == old_bytes
    DocumentArchive(store).forget(new)
    assert_safe_error(load_document_original(store, old, include_sensitive=True), "document_unavailable")


@pytest.mark.parametrize("kind,code", [("corrupt", "original_integrity_failed"), ("escape", "unsafe_original_path"),
                                       ("leaf_symlink", "unsafe_original_path"), ("ancestor_symlink", "unsafe_original_path"),
                                       ("directory", "unsafe_original_path"), ("missing", "original_unavailable")]
                         + ([("fifo", "unsafe_original_path")] if hasattr(os, "mkfifo") else []))
def test_original_integrity_and_managed_paths(store, tmp_path, kind, code):
    _, identity = ingest(store, tmp_path)
    row = store._sqlite.execute("SELECT original_path FROM archive_documents WHERE id=?", (identity,)).fetchone()
    path = Path(row[0])
    if kind == "corrupt":
        path.write_bytes(b"tamper")
    elif kind == "escape":
        store._sqlite.execute("UPDATE archive_documents SET original_path=? WHERE id=?", (str(tmp_path / "escape.pdf"), identity))
        store._sqlite.commit()
    elif kind == "ancestor_symlink":
        parent = path.parent
        moved = parent.with_name("moved")
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
    else:
        path.unlink()
        if kind == "leaf_symlink":
            target = tmp_path / "outside.pdf"
            target.write_bytes(pdf_bytes())
            path.symlink_to(target)
        elif kind == "directory":
            path.mkdir()
        elif kind == "fifo":
            import os
            os.mkfifo(path)
    assert_safe_error(load_document_original(store, identity), code)


def test_original_size_limit_before_read(store, tmp_path):
    _, identity = ingest(store, tmp_path)
    path = Path(store._sqlite.execute("SELECT original_path FROM archive_documents WHERE id=?", (identity,)).fetchone()[0])
    with path.open("wb") as stream:
        stream.truncate(100 * 1024 * 1024 + 1)
    assert_safe_error(load_document_original(store, identity), "original_too_large")


@pytest.mark.parametrize("kwargs", [{"page": True}, {"page": 0}, {"page": -1}, {"page": "1"}, {"page": 1.0},
                                    {"page": 1, "max_side": True}, {"page": 1, "max_side": 511},
                                    {"page": 1, "max_side": 3073}, {"page": 1, "include_sensitive": 1}])
def test_strict_inputs_before_io(store, kwargs, monkeypatch):
    monkeypatch.setattr(backend, "_worker_render", lambda *args: pytest.fail("Invalid input rendered"))
    assert_safe_error(backend.document_page_image(store, "id", **kwargs), "invalid_input")


def test_success_binding_readonly_and_busy_release(store, tmp_path, monkeypatch):
    source, identity = ingest(store, tmp_path)
    changes = store._sqlite.total_changes
    before = sorted(Path(store.settings.sqlite_path + ".archive").rglob("*"))
    monkeypatch.setattr(backend, "_worker_render", fake_render)
    result = backend.document_page_image(store, identity, page=2)
    assert result["page"] == 2 and result["version"] == 1 and result["page_count"] == 2
    assert result["original_content_hash"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result["source_reference"]["page"] == 2 and result["_image_png"] == png_bytes()
    assert result["image_bytes"] == len(result["_image_png"])
    assert store._sqlite.total_changes == changes
    assert before == sorted(Path(store.settings.sqlite_path + ".archive").rglob("*"))
    backend._RENDER_SLOT.acquire()
    try:
        assert_safe_error(backend.document_page_image(store, identity, page=1), "render_busy")
    finally:
        backend._RENDER_SLOT.release()
    assert "_image_png" in backend.document_page_image(store, identity, page=1)


@pytest.mark.parametrize("change", ["privacy", "delete", "hash", "path", "version"])
def test_reauthorize_after_render_against_separate_connection(store, tmp_path, monkeypatch, change):
    _, identity = ingest(store, tmp_path)
    def changing_render(*args):
        with sqlite3.connect(store.settings.sqlite_path) as fresh:
            sql = {"privacy": "sensitive=1", "delete": "deleted_at=42", "hash": "content_hash='changed'",
                   "path": "original_path='changed'", "version": "version=2"}[change]
            fresh.execute(f"UPDATE archive_documents SET {sql} WHERE id=?", (identity,))
        return fake_render(*args)
    monkeypatch.setattr(backend, "_worker_render", changing_render)
    assert_safe_error(backend.document_page_image(store, identity, page=1),
                      "document_unavailable" if change in ("privacy", "delete") else "original_changed")
    assert backend._RENDER_SLOT.acquire(blocking=False)
    backend._RENDER_SLOT.release()


@pytest.mark.parametrize("result", [{"width": 4000}, {"height": 0}, {"page_count": True}, {"_image_png": b"invalid"}, {"_image_png": png_bytes()[:-12]},
                                   {"_image_png": png_bytes(17, 24)}, {"_image_png": b"a" * (2 * 1024 * 1024 + 1)}])
def test_output_validation(store, tmp_path, monkeypatch, result):
    _, identity = ingest(store, tmp_path)
    monkeypatch.setattr(backend, "_worker_render", lambda *args: {**fake_render(*args), **result})
    assert_safe_error(backend.document_page_image(store, identity, page=1), "render_failed")


def test_timeout_and_failure_cleanup(monkeypatch):
    processes = []
    private_paths = []
    class Process:
        pid = 919191
        returncode = 1
        def __init__(self, command, **kwargs):
            private_paths.append(Path(command[3]).parent)
            processes.append(self)
            self.calls = 0
        def wait(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("owned", timeout)
            return 1
    killed = []
    monkeypatch.setattr(Process, "poll", lambda self: None, raising=False)
    monkeypatch.setattr(Process, "kill", lambda self: killed.append(self.pid), raising=False)
    monkeypatch.setattr(backend.subprocess, "Popen", Process)
    monkeypatch.setattr(backend.os, "killpg", lambda pid, sig: killed.append(pid), raising=False)
    assert_safe_error(backend._worker_render(b"%PDF", 1, 512), "render_timeout")
    assert killed == [919191] and processes[0].calls == 2
    assert all(not p.exists() for p in private_paths)
    monkeypatch.setattr(Process, "wait", lambda self, timeout: 1)
    assert_safe_error(backend._worker_render(b"%PDF", 1, 512), "render_failed")
    assert killed == [919191, 919191]


def test_cancellation_releases_slot(store, tmp_path, monkeypatch):
    _, identity = ingest(store, tmp_path)
    def cancelled(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(backend, "_worker_render", cancelled)
    with pytest.raises(KeyboardInterrupt):
        backend.document_page_image(store, identity, page=1)
    assert backend._RENDER_SLOT.acquire(blocking=False)
    backend._RENDER_SLOT.release()


def test_non_pdf(store, tmp_path, monkeypatch):
    source = tmp_path / "source.md"
    source.write_text("plain source")
    identity = DocumentArchive(store).ingest(str(source))["document_id"]
    monkeypatch.setattr(backend, "_worker_render", lambda *args: pytest.fail("Non PDF rendered"))
    assert_safe_error(backend.document_page_image(store, identity, page=1), "unsupported_media_type")


def real_interpreter(monkeypatch):
    # Explicit opt-in runtime path; optional renderer deps are not base deps.
    import os
    interpreter = os.environ.get("PAGE_IMAGE_TEST_PYTHON")
    if not interpreter:
        pytest.skip("Set PAGE_IMAGE_TEST_PYTHON to a trusted interpreter with document-view installed")
    monkeypatch.setenv("HG_PAGE_IMAGE_PYTHON", interpreter)


def test_real_two_page_renderer_and_page_range(store, tmp_path, monkeypatch):
    real_interpreter(monkeypatch)
    _, identity = ingest(store, tmp_path)
    first = backend.document_page_image(store, identity, page=1, max_side=512)
    second = backend.document_page_image(store, identity, page=2, max_side=512)
    assert "error" not in first, first
    assert "error" not in second, second
    assert first["page_count"] == second["page_count"] == 2
    assert first["_image_png"] != second["_image_png"]
    assert first["width"] == 384 and first["height"] == 512
    assert first["render"]["forms"] == "disabled"
    assert_safe_error(backend.document_page_image(store, identity, page=3), "page_out_of_range")


def test_real_pixel_bound_encrypted_and_malformed(monkeypatch):
    real_interpreter(monkeypatch)
    square = backend._worker_render(pdf_bytes(width=1000, height=1000), 1, 3072)
    assert "error" not in square, square
    assert square["width"] * square["height"] <= 4_000_000
    assert_safe_error(backend._worker_render(pdf_bytes(encrypted=True), 1, 512), "pdf_encrypted")
    assert_safe_error(backend._worker_render(b"%PDF-1.7 broken", 1, 512), "pdf_unreadable")


def test_live_check_does_not_reuse_callers_pinned_snapshot(store, tmp_path, monkeypatch):
    _, identity = ingest(store, tmp_path)
    store._sqlite.execute("PRAGMA journal_mode=WAL")
    store._sqlite.execute("BEGIN")
    store._sqlite.execute("SELECT id FROM archive_documents").fetchall()
    def change_private(*args):
        with sqlite3.connect(store.settings.sqlite_path) as fresh:
            fresh.execute("UPDATE archive_documents SET sensitive=1 WHERE id=?", (identity,))
        return fake_render(*args)
    monkeypatch.setattr(backend, "_worker_render", change_private)
    try:
        assert_safe_error(backend.document_page_image(store, identity, page=1), "document_unavailable")
        assert store._sqlite.in_transaction
        assert store._sqlite.execute("SELECT sensitive FROM archive_documents WHERE id=?", (identity,)).fetchone()[0] == 0
    finally:
        store._sqlite.rollback()


def test_worker_cancellation_cleans_owned_group_and_temporary_files(monkeypatch):
    paths, killed = [], []
    class Cancelled:
        pid = 818181
        def __init__(self, command, **kwargs):
            paths.append(Path(command[3]).parent)
            self.calls = 0
        def wait(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt()
            return 1
    monkeypatch.setattr(Cancelled, "poll", lambda self: None, raising=False)
    monkeypatch.setattr(Cancelled, "kill", lambda self: killed.append(self.pid), raising=False)
    monkeypatch.setattr(backend.subprocess, "Popen", Cancelled)
    monkeypatch.setattr(backend.os, "killpg", lambda pid, sig: killed.append(pid), raising=False)
    with pytest.raises(KeyboardInterrupt):
        backend._worker_render(b"%PDF", 1, 512)
    assert killed == [818181] and all(not path.exists() for path in paths)


def test_interpreter_requires_absolute_path(monkeypatch):
    monkeypatch.setenv("HG_PAGE_IMAGE_PYTHON", "python")
    assert_safe_error(backend._worker_render(b"%PDF", 1, 512), "renderer_unavailable")


def test_originals_do_not_change_store_row_factory(store, tmp_path):
    _, identity = ingest(store, tmp_path)
    before = store._sqlite.row_factory
    assert "_original_bytes" in load_document_original(store, identity)
    assert store._sqlite.row_factory is before


def test_real_missing_renderer_dependencies(monkeypatch, tmp_path):
    # Stub only in the owned script directory, while retaining isolated mode.
    import os
    interpreter = os.environ.get("PAGE_IMAGE_TEST_PYTHON")
    if not interpreter:
        pytest.skip("optional renderer integration disabled")
    worker = tmp_path / "worker.py"
    worker.write_text(backend._WORKER.read_text())
    # -I prevents script-local imports; use -S to disable site-packages solely
    # for this subprocess test of the worker's optional dependency failure.
    source, output, result = (tmp_path / name for name in ("input.pdf", "output.png", "result.json"))
    source.write_bytes(pdf_bytes())
    import json
    completed = subprocess.run([interpreter, "-I", "-S", str(worker), str(source), str(output), str(result), "1", "512"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    assert completed.returncode == 0
    assert_safe_error(json.loads(result.read_text()), "renderer_unavailable")


def test_real_png_byte_bound_does_not_retry_or_crop(monkeypatch):
    real_interpreter(monkeypatch)
    from io import BytesIO
    import random
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject, NumberObject, DictionaryObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=2000, height=2000)
    image = DecodedStreamObject()
    image.set_data(random.Random(7).randbytes(2000 * 2000 * 3))
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(2000), NameObject("/Height"): NumberObject(2000),
                  NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Img"): writer._add_object(image)})})
    content = DecodedStreamObject()
    content.set_data(b"q 2000 0 0 2000 0 0 cm /Img Do Q")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    assert_safe_error(backend._worker_render(output.getvalue(), 1, 3072), "image_too_large")


def test_real_rotation_and_visible_cropbox(monkeypatch):
    real_interpreter(monkeypatch)
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import RectangleObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=240, height=320)
    page.cropbox = RectangleObject((10, 20, 210, 300))
    page.rotate(90)
    output = BytesIO()
    writer.write(output)
    result = backend._worker_render(output.getvalue(), 1, 560)
    assert "error" not in result, result
    assert (result["width"], result["height"]) == (560, 400)
    assert result["render"]["intrinsic_rotation_degrees"] == 90
    assert result["render"]["additional_rotation_degrees"] == 0


def legacy_related_documents(store, tmp_path, *, transitive=False):
    """Build a public catalog, then simulate legacy nonpropagated privacy."""
    a_dir, b_dir, c_dir = (tmp_path / name for name in ("copy-a", "copy-b", "copy-c"))
    for directory in (a_dir, b_dir, c_dir):
        directory.mkdir()
    _, a = ingest(store, a_dir)
    source_b, b = ingest(store, b_dir)
    private = b
    if transitive:
        source_b.write_bytes(pdf_bytes(pages=1))
        DocumentArchive(store).ingest(str(source_b))
        _, private = ingest(store, c_dir, data=source_b.read_bytes())
    # Direct metadata edit intentionally recreates old nonpropagated catalogs.
    store._sqlite.execute("UPDATE archive_documents SET sensitive=0")
    store._sqlite.commit()
    return a, private


@pytest.mark.parametrize("transitive", [False, True])
@pytest.mark.parametrize("tombstone", [False, True])
def test_legacy_privacy_closure_denied_before_io_and_opt_in(store, tmp_path, monkeypatch, transitive, tombstone):
    a, private = legacy_related_documents(store, tmp_path, transitive=transitive)
    store._sqlite.execute("UPDATE archive_documents SET sensitive=1,deleted_at=? WHERE id=?", (42 if tombstone else None, private))
    store._sqlite.commit()
    from hunter_gatherer.memory.attribution import _sensitive_documents
    assert _sensitive_documents(store, (a,))
    import hunter_gatherer.memory.document_originals as originals
    actual_read = originals._read_managed
    monkeypatch.setattr(originals, "_read_managed", lambda path: pytest.fail("Private closure reached file I/O"))
    monkeypatch.setattr(backend, "_worker_render", lambda *args: pytest.fail("Private closure reached renderer"))
    assert_safe_error(load_document_original(store, a), "document_unavailable")
    assert_safe_error(backend.document_page_image(store, a, page=1), "document_unavailable")
    monkeypatch.setattr(originals, "_read_managed", actual_read)
    monkeypatch.setattr(backend, "_worker_render", fake_render)
    assert "_original_bytes" in load_document_original(store, a, include_sensitive=True)
    assert "_image_png" in backend.document_page_image(store, a, page=1, include_sensitive=True)


@pytest.mark.parametrize("transitive", [False, True])
def test_legacy_privacy_closure_rechecked_after_render(store, tmp_path, monkeypatch, transitive):
    a, private = legacy_related_documents(store, tmp_path, transitive=transitive)
    store._sqlite.execute("PRAGMA journal_mode=WAL")
    store._sqlite.execute("BEGIN")
    store._sqlite.execute("SELECT id FROM archive_documents").fetchall()
    def mark_related_private(*args):
        with sqlite3.connect(store.settings.sqlite_path) as db:
            db.execute("UPDATE archive_documents SET sensitive=1,deleted_at=42 WHERE id=?", (private,))
        return fake_render(*args)
    monkeypatch.setattr(backend, "_worker_render", mark_related_private)
    try:
        assert_safe_error(backend.document_page_image(store, a, page=1), "document_unavailable")
        assert store._sqlite.in_transaction
    finally:
        store._sqlite.rollback()


@pytest.mark.parametrize("failure", ["create", "write", "cleanup"])
def test_worker_temporary_failures_safe_and_release_slot(store, tmp_path, monkeypatch, failure):
    _, identity = ingest(store, tmp_path)
    actual_temporary = backend.tempfile.TemporaryDirectory
    actual_write = Path.write_bytes
    paths = []
    class Temporary:
        def __init__(self, **kwargs):
            if failure == "create":
                raise OSError("disk full /private/source/secret")
            self.wrapped = actual_temporary(**kwargs)
            self.name = self.wrapped.name
            paths.append(Path(self.name))
        def cleanup(self):
            self.wrapped.cleanup()
            if failure == "cleanup":
                raise OSError("cleanup failed /private/source/secret")
    monkeypatch.setattr(backend.tempfile, "TemporaryDirectory", Temporary)
    def disk_full(path, data):
        if path.name == "source.pdf" and failure == "write":
            actual_write(path, b"partial")
            raise OSError("disk full " + str(path))
        return actual_write(path, data)
    monkeypatch.setattr(Path, "write_bytes", disk_full)
    if failure == "cleanup":
        monkeypatch.setattr(backend.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn failure")))
    else:
        monkeypatch.setattr(backend.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Failed temporary setup spawned worker"))
    result = backend.document_page_image(store, identity, page=1)
    assert_safe_error(result, "render_failed")
    assert "/private/" not in str(result) and "secret" not in str(result)
    assert all(not path.exists() for path in paths)
    assert backend._RENDER_SLOT.acquire(blocking=False)
    backend._RENDER_SLOT.release()


def test_cancellation_preserved_even_if_temporary_cleanup_fails(monkeypatch):
    actual_temporary = backend.tempfile.TemporaryDirectory
    paths, killed = [], []
    class Temporary:
        def __init__(self, **kwargs):
            self.wrapped = actual_temporary(**kwargs)
            self.name = self.wrapped.name
            paths.append(Path(self.name))
        def cleanup(self):
            self.wrapped.cleanup()
            raise OSError("private cleanup path")
    class Process:
        pid = 717171
        def __init__(self, *args, **kwargs):
            self.calls = 0
        def wait(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt()
            return 0
    monkeypatch.setattr(backend.tempfile, "TemporaryDirectory", Temporary)
    monkeypatch.setattr(Process, "poll", lambda self: None, raising=False)
    monkeypatch.setattr(Process, "kill", lambda self: killed.append(self.pid), raising=False)
    monkeypatch.setattr(backend.subprocess, "Popen", Process)
    monkeypatch.setattr(backend.os, "killpg", lambda pid, sig: killed.append(pid), raising=False)
    with pytest.raises(KeyboardInterrupt):
        backend._worker_render(b"%PDF", 1, 512)
    assert killed == [717171] and all(not path.exists() for path in paths)
