"""Bounded original-page rerenders, cleanup, and unchanged default encoding."""
from __future__ import annotations

import json
import math
import random
import subprocess
import sys
from io import BytesIO
from types import ModuleType, SimpleNamespace

import pytest

from hunter_gatherer.memory import page_image_worker as worker


@pytest.mark.parametrize("requested, expected", [
    (3072, [3072, 1536, 1024, 512]), (2048, [2048, 1536, 1024, 512]),
    (1536, [1536, 1024, 512]), (1200, [1200, 1024, 512]),
    (1024, [1024, 512]), (513, [513, 512]), (512, [512]),
])
def test_candidate_schedule(requested, expected):
    assert worker._candidate_sides(requested, True) == expected
    assert worker._candidate_sides(requested, False) == [requested]


@pytest.mark.parametrize("max_side, fit", [
    (True, True), (512.0, True), ("512", True), (511, True), (3073, True),
    (512, 1), (512, "true"), (512, None),
])
def test_strict_candidate_inputs(max_side, fit):
    with pytest.raises(worker.RenderError) as caught:
        worker._candidate_sides(max_side, fit)
    assert caught.value.result["error_code"] == "invalid_input"


@pytest.mark.parametrize("page", [True, 0, -1, 1.0, "1"])
def test_strict_page_input(tmp_path, page):
    with pytest.raises(worker.RenderError) as caught:
        worker.render("unused.pdf", tmp_path / "out.png", page, 512, True)
    assert caught.value.result["error_code"] == "invalid_input"


def fake_renderer(monkeypatch, output, outcomes, *, dimensions=None, encrypted=False,
                  unreadable=False, actual_dimensions=None, fail_conversion=False,
                  stream_open_failure=False):
    """Track all native/view/stream ownership and reject overlapping attempts."""
    events, objects, kwargs_seen = [], [], []
    monkeypatch.setattr(worker, "MAX_PNG_BYTES", 10)
    original_stream = worker.BoundedPNG

    class Stream(original_stream):
        def __init__(self, path):
            if stream_open_failure:
                raise OSError("deliberate open failure")
            super().__init__(path)
            objects.append(self)
            events.append("stream_open")

        def close(self):
            super().close()
            events.append("stream_close")

    monkeypatch.setattr(worker, "BoundedPNG", Stream)

    class Image:
        def __init__(self, side, outcome):
            self.size = actual_dimensions or (side, side)
            self.closed = False
            self.outcome = outcome
            objects.append(self)
            events.append("image_open")

        def save(self, stream, **kwargs):
            assert kwargs == {"format": "PNG"}  # Existing Pillow defaults only.
            stream.write(b"begin")
            if self.outcome == "too_large":
                stream.write(b"X" * 10)
            elif self.outcome == "encoding_error":
                raise RuntimeError("deliberate encoding failure")
            else:
                stream.write(b"end")

        def close(self):
            self.closed = True
            events.append("image_close")

    class Bitmap:
        def __init__(self, side, outcome):
            self.closed = False
            self.side, self.outcome = side, outcome
            objects.append(self)
            events.append("bitmap_open")

        def to_pil(self):
            if fail_conversion:
                raise RuntimeError("deliberate conversion failure")
            return Image(self.side, self.outcome)

        def close(self):
            self.closed = True
            events.append("bitmap_close")

    class Page:
        closed = False

        def get_size(self):
            return dimensions or (1000, 1000)

        def get_rotation(self):
            return 90

        def render(self, **kwargs):
            # Failed output cannot be reused, even transiently before truncation.
            assert not output.exists()
            assert all(getattr(obj, "closed", obj.stream.closed if isinstance(obj, Stream) else False)
                       for obj in objects)
            kwargs_seen.append(kwargs)
            side = round(kwargs["scale"] * 1000)
            return Bitmap(side, outcomes[len(kwargs_seen) - 1])

        def close(self):
            self.closed = True
            events.append("page_close")

    page = Page()

    class Document:
        def __enter__(self):
            events.append("document_open")
            return self

        def __exit__(self, *args):
            events.append("document_close")

        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return page

    pdfium = ModuleType("pypdfium2")
    raw = ModuleType("pypdfium2.raw")
    raw.FPDF_GetSecurityHandlerRevision = lambda doc: 3 if encrypted else -1

    def open_document(source):
        if unreadable:
            raise RuntimeError("PDF open failed")
        return Document()

    pdfium.PdfDocument = open_document
    pdfium.PYPDFIUM_INFO = "test-renderer"
    pdfium.PDFIUM_INFO = "test-pdfium"
    pdfium.raw = raw
    monkeypatch.setitem(sys.modules, "pypdfium2", pdfium)
    monkeypatch.setitem(sys.modules, "pypdfium2.raw", raw)
    return SimpleNamespace(events=events, objects=objects, renders=kwargs_seen, page=page)


def assert_closed(state):
    assert state.events[-2:] == ["page_close", "document_close"]
    assert all(obj.stream.closed if isinstance(obj, worker.BoundedPNG) else obj.closed
               for obj in state.objects)


def test_full_page_retry_closes_resources_and_removes_partial(tmp_path, monkeypatch):
    output = tmp_path / "out.png"
    state = fake_renderer(monkeypatch, output, ["too_large", "too_large", "success"])
    result = worker.render("input.pdf", output, 1, 2048, True)
    assert output.read_bytes() == b"beginend"
    assert_closed(state)
    render = result["render"]
    assert render["requested_max_side"] == 2048 and render["max_side"] == 1024
    assert render["fit_to_byte_limit"] is True and render["downsampling_retry"] is True
    assert render["attempt_count"] == 3
    assert [item["max_side"] for item in render["attempts"]] == [2048, 1536, 1024]
    assert [item["outcome"] for item in render["attempts"]] == ["image_too_large", "image_too_large", "success"]
    assert result["width"] == result["height"] == render["attempts"][-1]["width"] == 1024
    assert "legibility is not guaranteed" in render["resolution_notice"]
    for kwargs in state.renders:
        assert kwargs["rotation"] == 0 and kwargs["crop"] == (0, 0, 0, 0)
        assert kwargs["may_draw_forms"] is False and kwargs["draw_annots"] is True
        assert kwargs["fill_color"] == (255, 255, 255, 255)


@pytest.mark.parametrize("side, count", [(2048, 4), (512, 1)])
def test_all_attempts_fail_without_output(tmp_path, monkeypatch, side, count):
    output = tmp_path / "out.png"
    state = fake_renderer(monkeypatch, output, ["too_large"] * count)
    with pytest.raises(worker.RenderError) as caught:
        worker.render("input.pdf", output, 1, side, True)
    assert not output.exists()
    assert_closed(state)
    result = caught.value.result
    assert result["error_code"] == "image_too_large" and result["http_status"] == 413
    render = result["render"]
    assert render["max_side"] == 512 and render["requested_max_side"] == side
    assert render["fit_to_byte_limit"] is True and render["attempt_count"] == count
    assert render["downsampling_retry"] is (count > 1)
    assert bool(render["resolution_notice"]) is (count > 1)
    assert all(attempt["outcome"] == "image_too_large" for attempt in render["attempts"])
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize("fit", [False, True])
def test_first_attempt_success_and_default_metadata(tmp_path, monkeypatch, fit):
    output = tmp_path / "out.png"
    state = fake_renderer(monkeypatch, output, ["success"])
    result = worker.render("input.pdf", output, 1, 1536, fit)
    assert_closed(state)
    assert len(state.renders) == 1
    render = result["render"]
    assert render["downsampling_retry"] is False and render["max_side"] == 1536
    if fit:
        assert render["attempt_count"] == 1 and render["resolution_notice"] is None
        assert render["attempts"] == [{"max_side": 1536, "width": 1536, "height": 1536, "outcome": "success"}]
    else:
        assert set(render) == {"renderer", "renderer_version", "pdfium_version", "pillow_version",
                               "max_side", "max_pixels", "scale", "intrinsic_rotation_degrees",
                               "additional_rotation_degrees", "crop", "background", "forms",
                               "annotations", "ocr", "downsampling_retry"}


def test_fitting_disabled_never_retries_size_failure(tmp_path, monkeypatch):
    output = tmp_path / "out.png"
    state = fake_renderer(monkeypatch, output, ["too_large", "success"])
    with pytest.raises(worker.RenderError) as caught:
        worker.render("input.pdf", output, 1, 2048, False)
    assert caught.value.result["error_code"] == "image_too_large"
    assert "render" not in caught.value.result
    assert len(state.renders) == 1 and not output.exists()
    assert_closed(state)


def test_unrelated_failure_after_size_retry_stops_schedule(tmp_path, monkeypatch):
    output = tmp_path / "out.png"
    state = fake_renderer(monkeypatch, output, ["too_large", "encoding_error", "success"])
    with pytest.raises(RuntimeError):
        worker.render("input.pdf", output, 1, 2048, True)
    assert len(state.renders) == 2 and not output.exists()
    assert_closed(state)


def test_page_out_of_range_never_allocates_bitmap(tmp_path, monkeypatch):
    output = tmp_path / "out.png"
    state = fake_renderer(monkeypatch, output, ["success"])
    with pytest.raises(worker.RenderError) as caught:
        worker.render("input.pdf", output, 2, 2048, True)
    assert caught.value.result["error_code"] == "page_out_of_range"
    assert not state.renders and not output.exists()


def test_bounded_png_does_not_write_chunk_that_exceeds_limit(tmp_path, monkeypatch):
    output = tmp_path / "out.png"
    monkeypatch.setattr(worker, "MAX_PNG_BYTES", 10)
    stream = worker.BoundedPNG(output)
    try:
        stream.write(b"X" * 10)
        with pytest.raises(worker.RenderError) as caught:
            stream.write(b"Y")
        assert stream.count == 10 and caught.value.result["error_code"] == "image_too_large"
    finally:
        stream.close()
    assert output.read_bytes() == b"X" * 10 and stream.stream.closed


def test_reduced_attempt_corrects_only_one_ulp_side_roundoff(tmp_path):
    # Exact PDFium page dimensions from the CIA p3 regression; 1536 / height
    # rounds back to 1536.0000000000002 and used to reject the reduction.
    class Page:
        def get_size(self):
            return 568.3200073242188, 749.760009765625

        def render(self, **kwargs):
            self.scale = kwargs["scale"]
            size = tuple(math.ceil(dimension * self.scale) for dimension in self.get_size())
            from PIL import Image

            class Bitmap:
                def to_pil(self):
                    return Image.new("RGB", size, "white")

                def close(self):
                    pass

            return Bitmap()

    page = Page()
    output = tmp_path / "out.png"
    with pytest.raises(worker.RenderError) as caught:
        worker._render_attempt(page, output, 1536)
    assert caught.value.result["error_code"] == "page_dimensions_invalid"
    assert not output.exists()
    width, height, scale = worker._render_attempt(page, output, 1536, correct_roundoff=True)
    assert (width, height) == (1165, 1536)
    assert scale == math.nextafter(1536 / page.get_size()[1], 0.0)
    output.unlink()
    width, height, scale = worker._render_attempt(page, output, 2048)
    assert (width, height) == (1553, 2048)
    assert scale == 2048 / page.get_size()[1]


@pytest.mark.parametrize("config, code, render_count", [
    ({"dimensions": (float("nan"), 1000)}, "page_dimensions_invalid", 0),
    ({"actual_dimensions": (2049, 2049)}, "image_dimensions_invalid", 1),
    ({"encrypted": True}, "pdf_encrypted", 0),
    ({"unreadable": True}, "pdf_unreadable", 0),
])
def test_unrelated_render_errors_do_not_retry(tmp_path, monkeypatch, config, code, render_count):
    output = tmp_path / "out.png"
    state = fake_renderer(monkeypatch, output, ["success"] * 4, **config)
    with pytest.raises(worker.RenderError) as caught:
        worker.render("input.pdf", output, 1, 2048, True)
    assert caught.value.result["error_code"] == code
    assert "render" not in caught.value.result
    assert len(state.renders) == render_count and not output.exists()
    if render_count:
        assert_closed(state)


@pytest.mark.parametrize("outcome, config", [
    ("encoding_error", {}), ("success", {"fail_conversion": True}),
    ("success", {"stream_open_failure": True}),
])
def test_generic_failures_release_every_allocated_resource(tmp_path, monkeypatch, outcome, config):
    output = tmp_path / "out.png"
    state = fake_renderer(monkeypatch, output, [outcome, "success"], **config)
    with pytest.raises((RuntimeError, OSError)):
        worker.render("input.pdf", output, 1, 2048, True)
    assert len(state.renders) == 1 and not output.exists()
    assert_closed(state)


@pytest.mark.parametrize("flag, expected", [(None, False), ("false", False), ("true", True)])
def test_cli_old_arguments_and_explicit_boolean(tmp_path, monkeypatch, flag, expected):
    output, metadata = tmp_path / "out.png", tmp_path / "result.json"
    argv = ["worker", "input.pdf", str(output), str(metadata), "1", "512"]
    if flag is not None:
        argv.append(flag)
    calls = []
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(worker, "_limits", lambda: calls.append("limits"))

    def render(*args, **kwargs):
        calls.append((args, kwargs))
        return {"width": 512, "height": 512}

    monkeypatch.setattr(worker, "render", render)
    worker.main()
    assert calls == ["limits", (("input.pdf", output, 1, 512), {"fit_to_byte_limit": expected})]
    assert json.loads(metadata.read_text()) == {"width": 512, "height": 512}


@pytest.mark.parametrize("args", [["1", "512", "TRUE"], ["1", "512", "1"],
                                  ["1", "512", "true", "extra"], ["x", "512"], ["1"]])
def test_cli_rejects_invalid_inputs_and_removes_existing_output(tmp_path, monkeypatch, args):
    output, metadata = tmp_path / "out.png", tmp_path / "result.json"
    output.write_bytes(b"partial")
    monkeypatch.setattr(sys, "argv", ["worker", "input.pdf", str(output), str(metadata), *args])
    monkeypatch.setattr(worker, "_limits", lambda: None)
    monkeypatch.setattr(worker, "render", lambda *args, **kwargs: pytest.fail("invalid input rendered"))
    worker.main()
    result = json.loads(metadata.read_text())
    assert result["error_code"] == "invalid_input" and not output.exists()
    assert str(tmp_path) not in json.dumps(result)


def test_cli_generic_failure_removes_partial_and_does_not_expose_paths(tmp_path, monkeypatch):
    output, metadata = tmp_path / "out.png", tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["worker", "input.pdf", str(output), str(metadata), "1", "512", "true"])
    monkeypatch.setattr(worker, "_limits", lambda: None)

    def fail(*args, **kwargs):
        output.write_bytes(b"partial")
        raise OSError(str(output))

    monkeypatch.setattr(worker, "render", fail)
    worker.main()
    result = json.loads(metadata.read_text())
    assert result["error_code"] == "render_failed" and not output.exists()
    assert str(tmp_path) not in json.dumps(result)


def noisy_pdf():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
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
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_real_oversized_scan_fits_with_same_encoding_as_direct_render(tmp_path):
    pytest.importorskip("pypdfium2")
    pytest.importorskip("PIL")
    source, output, metadata = (tmp_path / name for name in ("input.pdf", "out.png", "result.json"))
    source.write_bytes(noisy_pdf())
    run = subprocess.run([sys.executable, "-I", str(worker.__file__), str(source), str(output),
                          str(metadata), "1", "3072", "true"], capture_output=True, timeout=20)
    assert run.returncode == 0 and run.stdout == b""
    result = json.loads(metadata.read_text())
    assert "error" not in result, result
    render = result["render"]
    assert render["requested_max_side"] == 3072 and render["max_side"] < 3072
    assert 2 <= render["attempt_count"] <= 4 and render["downsampling_retry"] is True
    assert len(output.read_bytes()) <= worker.MAX_PNG_BYTES
    assert result["width"] * result["height"] <= worker.MAX_PIXELS
    assert [attempt["max_side"] for attempt in render["attempts"]] == [3072, 1536, 1024, 512][:render["attempt_count"]]
    assert render["attempts"][-1]["outcome"] == "success"
    direct = tmp_path / "direct.png"
    direct_result = worker.render(str(source), direct, 1, render["max_side"])
    assert direct.read_bytes() == output.read_bytes()
    assert direct_result["render"]["scale"] == render["scale"]
    assert direct_result["render"]["downsampling_retry"] is False


def test_real_cli_default_and_explicit_false_produce_same_png_and_metadata(tmp_path):
    pytest.importorskip("pypdfium2")
    pytest.importorskip("PIL")
    from pypdf import PdfWriter
    from pypdf.generic import RectangleObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=240, height=320)
    page.cropbox = RectangleObject((10, 20, 210, 300))
    page.rotate(90)
    source = tmp_path / "input.pdf"
    with source.open("wb") as stream:
        writer.write(stream)
    results = []
    for suffix, args in (("old", []), ("explicit", ["false"])):
        output, metadata = tmp_path / f"{suffix}.png", tmp_path / f"{suffix}.json"
        run = subprocess.run([sys.executable, "-I", str(worker.__file__), str(source),
                              str(output), str(metadata), "1", "560", *args],
                             capture_output=True, timeout=20)
        assert run.returncode == 0 and run.stdout == b""
        results.append((output.read_bytes(), json.loads(metadata.read_text())))
    assert results[0] == results[1]
    result = results[0][1]
    assert (result["width"], result["height"]) == (560, 400)
    assert result["render"]["intrinsic_rotation_degrees"] == 90
    assert "fit_to_byte_limit" not in result["render"]
