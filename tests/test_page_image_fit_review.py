"""Independent adversarial checks of fitted provenance and failure wording."""
import importlib
from copy import deepcopy

from test_document_page_image import ingest
from test_page_image_fit_backend import fitted_result

backend = importlib.import_module("hunter_gatherer.memory.document_page_image")


def failed_history():
    render = fitted_result()["render"]
    render.update(max_side=512, attempt_count=4,
                  attempts=[{"max_side": side, "width": side, "height": 1,
                             "outcome": "image_too_large"}
                            for side in (2048, 1536, 1024, 512)])
    return render


def test_huge_integer_scale_fails_closed_and_releases_slot(store, tmp_path, monkeypatch):
    _, identity = ingest(store, tmp_path)
    rendered = fitted_result()
    rendered["render"]["scale"] = 10 ** 400
    monkeypatch.setattr(backend, "_worker_render", lambda *args, **kw: deepcopy(rendered))
    response = backend.document_page_image(store, identity, page=1, fit_to_byte_limit=True)
    assert response["error_code"] == "render_failed"
    assert "_image_png" not in response
    assert backend._RENDER_SLOT.acquire(blocking=False)
    backend._RENDER_SLOT.release()


def test_exhausted_attempt_notice_does_not_claim_an_image_was_delivered():
    metadata = backend._fitting_metadata(failed_history(), 2048, failed=True)
    assert metadata is not None
    assert "no image returned" in metadata["resolution_notice"].lower()
    assert "rendered at a lower resolution to fit" not in metadata["resolution_notice"]
    assert all(attempt["outcome"] == "image_too_large" for attempt in metadata["attempts"])


def test_unknown_worker_fields_are_removed_from_public_provenance(store, tmp_path, monkeypatch):
    _, identity = ingest(store, tmp_path)
    rendered = fitted_result()
    rendered["render"]["private_path"] = "/private/secret/source.pdf"
    rendered["render"]["attempts"][0]["secret"] = "private original data"
    monkeypatch.setattr(backend, "_worker_render", lambda *args, **kw: deepcopy(rendered))
    response = backend.document_page_image(store, identity, page=1, fit_to_byte_limit=True)
    assert "error" not in response
    assert "private_path" not in str(response["render"])
    assert "secret" not in str(response["render"])


def test_fitted_failure_deadline_after_final_reauthorization(store, tmp_path, monkeypatch):
    _, identity = ingest(store, tmp_path)
    now = [100.0]
    monkeypatch.setattr(backend.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(backend, "_worker_render", lambda *args, **kw: {
        "error": "size", "error_code": "image_too_large", "http_status": 413,
        "render": failed_history()})
    calls = []
    def auth(*args, **kwargs):
        calls.append(args[1])
        now[0] = 104.0
        return None
    monkeypatch.setattr(backend, "reauthorize_document_original", auth)
    response = backend.document_page_image(store, identity, page=1, fit_to_byte_limit=True,
                                           _deadline=103.0)
    assert calls == [identity]
    assert response["error_code"] == "render_timeout"
    assert "render" not in response and "_image_png" not in response
