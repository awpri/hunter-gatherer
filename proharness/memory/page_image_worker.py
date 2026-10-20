"""Bounded render subprocess entry point; source bytes never enter IPC stdout.

Runs by absolute script path under an isolated interpreter. No network, OCR,
model, forms initialization, JavaScript, or document actions are invoked.
"""
from __future__ import annotations

import json
import math
import sys
from contextlib import closing
from pathlib import Path

MAX_PIXELS = 4_000_000
MAX_PNG_BYTES = 2 * 1024 * 1024


class RenderError(Exception):
    def __init__(self, code, message, status=422):
        self.result = {"error": message, "error_code": code, "http_status": status}


class BoundedPNG:
    def __init__(self, path):
        self.stream = path.open("wb")
        self.count = 0

    def write(self, data):
        if self.count + len(data) > MAX_PNG_BYTES:
            raise RenderError("image_too_large", "PNG output exceeds the 2 MiB limit; no image returned.", 413)
        self.count += len(data)
        return self.stream.write(data)

    def flush(self):
        self.stream.flush()

    def close(self):
        self.stream.close()


def _limits():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (15, 16))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PNG_BYTES + 65536, MAX_PNG_BYTES + 65536))
        # Address-space bounds work reliably on Linux; macOS malloc may fail
        # prematurely with RLIMIT_AS, so CPU/file/wall bounds apply there.
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass


def _candidate_sides(max_side, fit_to_byte_limit):
    if type(max_side) is not int or not 512 <= max_side <= 3072:
        raise RenderError("invalid_input", "max_side must be an integer between 512 and 3072.")
    if type(fit_to_byte_limit) is not bool:
        raise RenderError("invalid_input", "fit_to_byte_limit must be a boolean.")
    return [max_side] + ([side for side in (1536, 1024, 512) if side < max_side]
                         if fit_to_byte_limit else [])


def _render_attempt(page, output, max_side, *, correct_roundoff=False, region_bbox=None):
    width, height = page.get_size()
    if not all(math.isfinite(v) and 0 < v <= 1_000_000 for v in (width, height)):
        raise RenderError("page_dimensions_invalid", "PDF page dimensions are unsupported.")
    # Constrain allocation before each full-page render, including ceil rounding.
    scale = min(max_side / max(width, height), math.sqrt(MAX_PIXELS / (width * height)))
    predicted = (math.ceil(width * scale), math.ceil(height * scale))
    if predicted[0] * predicted[1] > MAX_PIXELS:
        scale *= math.sqrt(MAX_PIXELS / (predicted[0] * predicted[1])) * (1 - 1e-9)
        predicted = (math.ceil(width * scale), math.ceil(height * scale))
    # The division/multiply roundtrip can exceed the requested integer side by
    # one ULP. Correct only reduced attempts, preserving the original render's
    # exact scale and pixel behavior. This is pre-allocation scale accounting,
    # never a crop or a retry after an unrelated dimension failure.
    if (correct_roundoff and max(predicted) == max_side + 1
            and max(width * scale, height * scale) <= math.nextafter(float(max_side), math.inf)):
        scale = math.nextafter(scale, 0.0)
        predicted = (math.ceil(width * scale), math.ceil(height * scale))
    if min(predicted) < 1 or max(predicted) > max_side or predicted[0] * predicted[1] > MAX_PIXELS:
        raise RenderError("page_dimensions_invalid", "PDF page dimensions exceed render bounds.", 413)
    # PDFium applies intrinsic rotation and the visible CropBox on every attempt.
    # Each bitmap and its PIL view close before allocating the next bitmap.
    try:
        with (
            closing(page.render(scale=scale, rotation=0, crop=(0, 0, 0, 0), may_draw_forms=False,
                                draw_annots=True, fill_color=(255, 255, 255, 255))) as bitmap,
            closing(bitmap.to_pil()) as image,
        ):
            actual_width, actual_height = image.size
            if min(image.size) < 1 or max(image.size) > max_side or actual_width * actual_height > MAX_PIXELS:
                raise RenderError("image_dimensions_invalid", "Renderer output exceeds dimension bounds.", 413)
            try:
                with closing(BoundedPNG(output)) as stream:
                    if region_bbox is None:
                        image.save(stream, format="PNG")
                    else:
                        left, top, right, bottom = region_bbox
                        bounds = (math.floor(left * actual_width), math.floor(top * actual_height),
                                  math.ceil(right * actual_width), math.ceil(bottom * actual_height))
                        with closing(image.crop(bounds)) as cropped:
                            cropped.save(stream, format="PNG")
                            actual_width, actual_height = cropped.size
            except RenderError as exc:
                if exc.result["error_code"] == "image_too_large":
                    exc.dimensions = (actual_width, actual_height)
                raise
        return actual_width, actual_height, scale
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def _fit_metadata(max_side, attempts):
    selected = attempts[-1]["max_side"] if attempts else max_side
    reduced = len(attempts) > 1
    success = attempts and attempts[-1]["outcome"] == "success"
    notice = None
    if reduced:
        notice = (f"Page rendered at a reduced maximum side of {selected}px "
                  f"(requested {max_side}px) to fit the 2 MiB PNG limit; "
                  "legibility is not guaranteed." if success else
                  f"Full-page rendering was reduced to a maximum side of {selected}px "
                  f"(requested {max_side}px), but PNG output still exceeded the 2 MiB limit; "
                  "no image returned.")
    return {"requested_max_side": max_side, "max_side": selected, "fit_to_byte_limit": True,
            "downsampling_retry": reduced, "attempt_count": len(attempts), "attempts": attempts,
            "resolution_notice": notice}


def render(source, output, page_number, max_side, fit_to_byte_limit=False, region_bbox=None):
    if region_bbox is not None and (type(region_bbox) is not list or len(region_bbox) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in region_bbox)
        or not 0 <= region_bbox[0] < region_bbox[2] <= 1 or not 0 <= region_bbox[1] < region_bbox[3] <= 1):
        raise RenderError("invalid_input", "Invalid normalized region bounds.")
    if type(page_number) is not int or page_number < 1:
        raise RenderError("invalid_input", "page must be a positive integer.")
    candidates = _candidate_sides(max_side, fit_to_byte_limit)
    try:
        import PIL
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw
    except ImportError:
        raise RenderError("renderer_unavailable", "PDF page renderer unavailable; install the document-view extra.", 503) from None
    try:
        document = pdfium.PdfDocument(source)
    except Exception:
        raise RenderError("pdf_unreadable", "PDF is malformed, encrypted, or unsupported.") from None
    with document:
        # Reject even passwordless encrypted PDFs, rather than bypassing the
        # document's encryption policy using PDFium's default empty password.
        if raw.FPDF_GetSecurityHandlerRevision(document) >= 0:
            raise RenderError("pdf_encrypted", "Encrypted PDFs are not supported.")
        page_count = len(document)
        if page_number > page_count:
            raise RenderError("page_out_of_range", "Requested page is outside the PDF page range.")
        with closing(document[page_number - 1]) as page:
            rotation = page.get_rotation()
            attempts = []
            for side in candidates:
                try:
                    actual_width, actual_height, scale = _render_attempt(
                        page, output, side, correct_roundoff=side != max_side, region_bbox=region_bbox)
                except RenderError as exc:
                    if exc.result["error_code"] == "image_too_large":
                        width, height = exc.dimensions
                        attempts.append({"max_side": side, "width": width, "height": height,
                                         "outcome": "image_too_large"})
                        if side != candidates[-1]:
                            continue
                    if fit_to_byte_limit and exc.result["error_code"] == "image_too_large":
                        exc.result["render"] = _fit_metadata(max_side, attempts)
                    raise
                attempts.append({"max_side": side, "width": actual_width, "height": actual_height,
                                 "outcome": "success"})
                break
            return {"page_count": page_count, "width": actual_width, "height": actual_height,
                    "render": {"renderer": "pypdfium2", "renderer_version": str(pdfium.PYPDFIUM_INFO),
                               "pdfium_version": str(pdfium.PDFIUM_INFO), "pillow_version": PIL.__version__,
                               "max_side": side, "max_pixels": MAX_PIXELS, "scale": scale,
                               "intrinsic_rotation_degrees": rotation, "additional_rotation_degrees": 0,
                               "crop": "PDF visible CropBox; no additional crop" if region_bbox is None else "normalized displayed-page pixel crop",
                               **({"region_bbox": region_bbox, "coordinate_system": "normalized top-left displayed visible CropBox after intrinsic rotation"} if region_bbox is not None else {}), "background": "white",
                               "forms": "disabled", "annotations": "visible", "ocr": False,
                               "downsampling_retry": False,
                               **(_fit_metadata(max_side, attempts) if fit_to_byte_limit else {})}}


def main():
    _limits()
    if len(sys.argv) < 4:
        return 1
    source, output, result_path = sys.argv[1:4]
    try:
        if len(sys.argv) not in (6, 7, 8) or (len(sys.argv) >= 7 and sys.argv[6] not in ("true", "false")):
            raise RenderError("invalid_input", "Worker arguments must include page, max_side, and optional true/false fitting.")
        try:
            page, max_side = int(sys.argv[4]), int(sys.argv[5])
        except ValueError:
            raise RenderError("invalid_input", "page and max_side must be integers.") from None
        options = {}
        if len(sys.argv) == 8:
            try:
                options["region_bbox"] = json.loads(sys.argv[7])
            except ValueError:
                raise RenderError("invalid_input", "Region bounds must be JSON.") from None
        result = render(source, Path(output), page, max_side,
                        fit_to_byte_limit=len(sys.argv) >= 7 and sys.argv[6] == "true", **options)
    except RenderError as exc:
        result = exc.result
    except Exception:
        result = {"error": "PDF page rendering failed; no image returned.", "error_code": "render_failed", "http_status": 422}
    if "error" in result:
        Path(output).unlink(missing_ok=True)
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
