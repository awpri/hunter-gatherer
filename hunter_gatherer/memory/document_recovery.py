"""Optional PDF recovery in a short-lived worker with Python socket access blocked.

This is an offline dependency guard, not an OS sandbox for untrusted native code.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

_WORKER = threading.BoundedSemaphore(1)
_LIMIT = 16 * 1024 * 1024
_TIMEOUT = 180
_CLEANUP_SECONDS = 5
_CLEANUP_FAILED = False
_INVENTORY_FILES = 4096
_INVENTORY_BYTES = 8 * 1024 * 1024 * 1024
_FINGERPRINT_SCOPE = "configured_cache_snapshot_after_conversion_v1"
_ORIENTATION_RECORDS = 256
_ORIENTATION_BYTES = 64 * 1024
# Reviewed Docling 2.137.0 CLI OSD -> parser -> rotation and coordinate mapping.
# Internal APIs have no public policy switch. Re-review before adding a digest.
_ORIENTATION_API_SHA256 = "c17e116381e978c581f92ef70353995e794d5e64398e07cc7dba3fd9ba2ddc17"
_ORIENTATION_UTILS_SHA256 = "a249c3aa11d43a58422f0989aa3ec4ac570efd5258574f37e95ae4a650f59a02"
_ORIENTATION_SCOPE = "untrusted_engine_preprocessing_decisions_not_verified_page_orientation"
_TABLE_SCOPE = "unverified_docling_table_model_observations"
_TABLE_CELL_LIMIT = 1024
_TABLE_BYTES = 256 * 1024
_TABLE_DIMENSION_LIMIT = 10000
_TABLE_INTEGER_LIMIT = 1000000
_TABLE_ORIENTATIONS = frozenset({"rot_0", "rot_90", "rot_180", "rot_270"})
_TABLE_CELL_FIELDS = frozenset({"ordinal", "kind", "text", "ref", "bbox", "row_span", "col_span",
    "start_row_offset_idx", "end_row_offset_idx", "start_col_offset_idx", "end_col_offset_idx",
    "column_header", "row_header", "row_section", "fillable"})
_TABLE_FIELDS = frozenset({"schema_version", "scope", "status", "text_sha256", "table_ref",
    "num_rows", "num_cols", "orientation", "cell_limit", "total_cell_count", "cells",
    "observations", "omission_reason"})
_TABLE_OMISSION_REASONS = frozenset({"cell_limit_exceeded", "snapshot_bytes_exceeded",
    "invalid_dimensions", "invalid_orientation", "unrepresentable_cell", "invalid_model_schema"})


def _table_observation_bytes(value):
    """Canonical UTF-8 representation shared with structure storage/readers."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _table_cell_valid(cell, ordinal):
    if (not isinstance(cell, dict) or set(cell) != _TABLE_CELL_FIELDS
            or type(cell["ordinal"]) is not int or cell["ordinal"] != ordinal
            or not isinstance(cell["kind"], str) or cell["kind"] not in {"text", "rich"} or not isinstance(cell["text"], str)
            or len(cell["text"]) > _TABLE_BYTES):
        return False
    ref = cell["ref"]
    if ((cell["kind"] == "text" and ref is not None)
            or (cell["kind"] == "rich" and (not isinstance(ref, str) or len(ref) > 256
                or re.fullmatch(r"#/([A-Za-z_]+/\d+|body|furniture)", ref) is None))):
        return False
    for field in ("row_span", "col_span", "start_row_offset_idx", "end_row_offset_idx",
                  "start_col_offset_idx", "end_col_offset_idx"):
        if type(cell[field]) is not int or not -_TABLE_INTEGER_LIMIT <= cell[field] <= _TABLE_INTEGER_LIMIT:
            return False
    if not all(type(cell[field]) is bool for field in ("column_header", "row_header", "row_section", "fillable")):
        return False
    bbox = cell["bbox"]
    if bbox is not None and (not isinstance(bbox, dict)
            or set(bbox) != {"l", "t", "r", "b", "coord_origin"}
            or not isinstance(bbox["coord_origin"], str) or bbox["coord_origin"] not in {"TOPLEFT", "BOTTOMLEFT"}
            or not all(type(bbox[k]) in {int, float} and -1e100 <= bbox[k] <= 1e100
                       and math.isfinite(bbox[k]) for k in ("l", "t", "r", "b"))):
        return False
    return True


def _table_observations(cells, num_rows, num_cols):
    """Describe model-shape observations without repairing or verifying them.

    Compare rectangles directly rather than materializing TableData.grid: it
    duplicates spans, fills holes, clips offsets and shadows overlapping cells.
    """
    observations = []
    if any(c["row_span"] <= 0 or c["col_span"] <= 0
           or c["end_row_offset_idx"] - c["start_row_offset_idx"] != c["row_span"]
           or c["end_col_offset_idx"] - c["start_col_offset_idx"] != c["col_span"] for c in cells):
        observations.append("inconsistent_spans")
    if any(not (0 <= c["start_row_offset_idx"] < c["end_row_offset_idx"] <= num_rows
                and 0 <= c["start_col_offset_idx"] < c["end_col_offset_idx"] <= num_cols) for c in cells):
        observations.append("out_of_range_offsets")
    overlaps, active = False, []
    rectangles = sorted((c for c in cells
                         if c["start_row_offset_idx"] < c["end_row_offset_idx"]
                         and c["start_col_offset_idx"] < c["end_col_offset_idx"]),
                        key=lambda c: (c["start_row_offset_idx"], c["ordinal"]))
    for a in rectangles:
        active = [b for b in active if b["end_row_offset_idx"] > a["start_row_offset_idx"]]
        for b in active:
            if (max(a["start_row_offset_idx"], b["start_row_offset_idx"]) < min(a["end_row_offset_idx"], b["end_row_offset_idx"])
                    and max(a["start_col_offset_idx"], b["start_col_offset_idx"]) < min(a["end_col_offset_idx"], b["end_col_offset_idx"])):
                overlaps = True
                break
        if overlaps:
            break
        active.append(a)
    if overlaps:
        observations.append("overlapping_cells")
    if any(c["kind"] == "rich" for c in cells):
        observations.append("rich_text_unresolved")
    flagged = [c for c in cells if c["column_header"]]
    if not flagged:
        observations.append("no_column_header_flags")
    elif not any(c["start_row_offset_idx"] == 0 for c in flagged):
        observations.append("nonleading_column_header_flags")
    return observations


def _validate_table_structure(value, text=None):
    """Validate a bounded model snapshot, optionally bound to exact Markdown.

    Raw cell text/ref/coordinates are engine observations, not source quotes.
    Invalid predictions that remain representable retain their original values
    plus diagnostics. Invalid transport/schema is refused, never repaired.
    """
    if (not isinstance(value, dict) or set(value) != _TABLE_FIELDS
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["scope"] != _TABLE_SCOPE or not isinstance(value["status"], str)
            or value["status"] not in {"complete", "omitted"}
            or type(value["cell_limit"]) is not int or value["cell_limit"] != _TABLE_CELL_LIMIT
            or type(value["total_cell_count"]) is not int or not 0 <= value["total_cell_count"] <= 1000000000
            or not isinstance(value["text_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", value["text_sha256"]) is None
            or not isinstance(value["table_ref"], str) or len(value["table_ref"]) > 256
            or re.fullmatch(r"#/tables/\d+", value["table_ref"]) is None
            or not isinstance(value["cells"], list) or len(value["cells"]) > _TABLE_CELL_LIMIT
            or not isinstance(value["observations"], list)):
        raise ValueError("Invalid table structure schema")
    if text is not None and (not isinstance(text, str)
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != value["text_sha256"]):
        raise ValueError("Table structure Markdown hash mismatch")
    omitted = value["status"] == "omitted"
    for key in ("num_rows", "num_cols"):
        dimension = value[key]
        if not (omitted and dimension is None) and (type(dimension) is not int or not 0 <= dimension <= _TABLE_DIMENSION_LIMIT):
            raise ValueError("Invalid table dimensions")
    orientation = value["orientation"]
    if not (omitted and orientation is None) and (not isinstance(orientation, str) or orientation not in _TABLE_ORIENTATIONS):
        raise ValueError("Invalid table orientation")
    if omitted:
        if (value["cells"] or value["observations"] != ["metadata_omitted"]
                or not isinstance(value["omission_reason"], str) or value["omission_reason"] not in _TABLE_OMISSION_REASONS):
            raise ValueError("Invalid table omission")
    else:
        if (value["omission_reason"] is not None or len(value["cells"]) != value["total_cell_count"]
                or not all(_table_cell_valid(cell, i) for i, cell in enumerate(value["cells"], 1))):
            raise ValueError("Invalid table cells")
        if value["observations"] != _table_observations(value["cells"], value["num_rows"], value["num_cols"]):
            raise ValueError("Invalid table observations")
    try:
        if len(_table_observation_bytes(value)) > _TABLE_BYTES:
            raise ValueError("Table structure exceeds size limit")
    except (TypeError, UnicodeError, OverflowError) as exc:
        raise ValueError("Invalid table structure encoding") from exc
    return value


def _table_structure_snapshot(table, text):
    """Retain raw model cells in original order; never resolve rich references."""
    data = getattr(table, "data", None)
    if data is None:
        return None  # Compatibility with legacy adapter stubs lacking table data.
    raw_cells = getattr(data, "table_cells", None)
    value = {"schema_version": 1, "scope": _TABLE_SCOPE, "status": "complete",
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "table_ref": table.self_ref, "num_rows": getattr(data, "num_rows", None),
        "num_cols": getattr(data, "num_cols", None),
        "orientation": getattr(getattr(data, "orientation", None), "value", getattr(data, "orientation", None)),
        "cell_limit": _TABLE_CELL_LIMIT,
        "total_cell_count": len(raw_cells) if isinstance(raw_cells, (list, tuple)) else 0,
        "cells": [], "observations": [], "omission_reason": None}

    def omit(reason):
        value.update(status="omitted", cells=[], observations=["metadata_omitted"], omission_reason=reason)
        for field in ("num_rows", "num_cols"):
            if type(value[field]) is not int or not 0 <= value[field] <= _TABLE_DIMENSION_LIMIT:
                value[field] = None
        if not isinstance(value["orientation"], str) or value["orientation"] not in _TABLE_ORIENTATIONS:
            value["orientation"] = None
        return _validate_table_structure(value, text)

    if not isinstance(raw_cells, (list, tuple)):
        return omit("invalid_model_schema")
    if any(type(value[k]) is not int or not 0 <= value[k] <= _TABLE_DIMENSION_LIMIT for k in ("num_rows", "num_cols")):
        return omit("invalid_dimensions")
    if not isinstance(value["orientation"], str) or value["orientation"] not in _TABLE_ORIENTATIONS:
        return omit("invalid_orientation")
    if len(raw_cells) > _TABLE_CELL_LIMIT:
        return omit("cell_limit_exceeded")
    cell_bytes = len(_table_observation_bytes(value))
    for ordinal, raw in enumerate(raw_cells, 1):
        try:
            ref = getattr(raw, "ref", None)
            bbox = getattr(raw, "bbox", None)
            cell = {k: getattr(raw, k) for k in sorted(_TABLE_CELL_FIELDS - {"ordinal", "kind", "ref", "bbox"})}
            cell.update(ordinal=ordinal, kind="rich" if ref is not None else "text",
                ref=getattr(ref, "cref", None) if ref is not None else None,
                bbox=bbox.model_dump(mode="json") if bbox is not None else None)
            if isinstance(cell["text"], str) and len(cell["text"]) > _TABLE_BYTES:
                return omit("snapshot_bytes_exceeded")
            if not _table_cell_valid(cell, ordinal):
                return omit("unrepresentable_cell")
            cell_bytes += len(_table_observation_bytes(cell)) + (1 if ordinal > 1 else 0)
            if cell_bytes > _TABLE_BYTES:
                return omit("snapshot_bytes_exceeded")
            value["cells"].append(cell)
        except (AttributeError, TypeError, ValueError, UnicodeError, OverflowError):
            return omit("unrepresentable_cell")
    value["observations"] = _table_observations(value["cells"], value["num_rows"], value["num_cols"])
    if len(_table_observation_bytes(value)) > _TABLE_BYTES:
        return omit("snapshot_bytes_exceeded")
    return _validate_table_structure(value, text)



@dataclass(frozen=True)
class RecoveryExtraction:
    sections: list[dict]
    warnings: list[str]
    media_type: str
    parser_version: str
    provenance: dict | None = None


def _orientation_policy(engine, policy):
    if policy is not None and (engine != "tesseract" or policy not in ("auto", "preserve")):
        raise ValueError("ocr_orientation requires Tesseract and must be 'auto' or 'preserve'.")
    return (policy or "auto") if engine == "tesseract" else None


def _validate_orientation_observations(value, policy):
    fields = {"scope", "record_limit", "total_count", "omitted_count", "records"}
    if (not isinstance(value, dict) or set(value) != fields or value["scope"] != _ORIENTATION_SCOPE
            or type(value["record_limit"]) is not int or value["record_limit"] != _ORIENTATION_RECORDS
            or not isinstance(value["records"], list) or len(value["records"]) > _ORIENTATION_RECORDS
            or type(value["total_count"]) is not int or not 0 <= value["total_count"] <= 1000000
            or type(value["omitted_count"]) is not int or value["omitted_count"] < 0
            or value["total_count"] != len(value["records"]) + value["omitted_count"]):
        raise ValueError("Invalid orientation observations")
    for index, entry in enumerate(value["records"], 1):
        if (not isinstance(entry, dict) or set(entry) != {"sequence", "page", "region", "status",
                "reported_orientation_degrees", "orientation_confidence", "applied_orientation_degrees"}
                or type(entry["sequence"]) is not int or entry["sequence"] != index
                or entry["page"] is not None or entry["region"] is not None
                or entry["status"] not in ({"bypassed"} if policy == "preserve" else {"detected", "failed"})):
            raise ValueError("Invalid orientation decision")
        reported, applied, confidence = (entry[k] for k in
            ("reported_orientation_degrees", "applied_orientation_degrees", "orientation_confidence"))
        if (type(applied) is not int or applied not in (0, 90, 180, 270)
                or (reported is not None and (type(reported) is not int or reported not in (0, 90, 180, 270)))
                or (confidence is not None and not _finite_number(confidence, 0, 1000000))):
            raise ValueError("Invalid orientation numeric observation")
        if entry["status"] == "detected":
            if reported is None or applied != (-reported) % 360:
                raise ValueError("Inconsistent auto orientation decision")
        elif reported is not None or confidence is not None or applied != 0:
            raise ValueError("Inconsistent bypass/failure orientation decision")
    if len(json.dumps(value, allow_nan=False, separators=(",", ":")).encode()) > _ORIENTATION_BYTES:
        raise ValueError("Orientation observations exceed size limit")


class _OrientationObservations:
    """Bounded worker-only engine outputs, with unknown page/region coordinates.

    Reported degrees is the raw clockwise OSD value. Applied degrees is the
    counterclockwise value returned to Docling's preprocessing branch;
    it does not independently verify image rotation, OCR success or orientation.
    """
    def __init__(self):
        self.records = []
        self.total_count = 0
        self.lock = threading.Lock()

    def add(self, status):
        with self.lock:
            self.total_count += 1
            if self.total_count > 1000000:
                raise ValueError("Orientation decision count exceeds limit")
            record = {"sequence": self.total_count, "page": None, "region": None,
                      "status": status, "reported_orientation_degrees": None,
                      "orientation_confidence": None, "applied_orientation_degrees": 0}
            if len(self.records) < _ORIENTATION_RECORDS:
                self.records.append(record)
            return record

    def snapshot(self):
        with self.lock:
            return {"scope": _ORIENTATION_SCOPE, "record_limit": _ORIENTATION_RECORDS,
                    "total_count": self.total_count, "omitted_count": self.total_count - len(self.records),
                    "records": [dict(record) for record in self.records]}


def _load_orientation_api():
    from docling.models.stages.ocr import tesseract_ocr_cli_model as module
    from docling.models.stages.ocr import tesseract_utils
    # Pin reviewed control flow as well as callable API. An internal method
    # rename/semantic change must fail before any conversion or OCR is launched.
    if (hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != _ORIENTATION_API_SHA256
            or hashlib.sha256(Path(tesseract_utils.__file__).read_bytes()).hexdigest() != _ORIENTATION_UTILS_SHA256
            or not callable(getattr(module, "_parse_orientation", None))
            or not callable(getattr(module.TesseractOcrCliModel, "_perform_osd", None))):
        raise ValueError("Unsupported Docling Tesseract orientation API; review adapter compatibility before recovery.")
    return module


@contextmanager
def _tesseract_orientation_context(policy, observations):
    """Short-lived worker hooks only; restore even if conversion fails.

    The reviewed CLI implementation runs OSD regardless of fixed English and
    has no public preserve switch. None bypasses detection; the parser hook
    returns zero to the existing rotation/coordinate path, without fake OSD.
    """
    module = _load_orientation_api()
    model = module.TesseractOcrCliModel
    original_osd, original_parser = model._perform_osd, module._parse_orientation
    local = threading.local()

    def perform_osd(self, filename):
        record = observations.add("bypassed" if policy == "preserve" else "failed")
        local.record = record
        if policy == "preserve":
            return None
        try:
            result = original_osd(self, filename)
        except BaseException:
            local.record = None
            raise
        local.result = result
        return result

    def parse_orientation(result):
        record = getattr(local, "record", None)
        if record is None:
            raise ValueError("Unsupported Docling orientation call order")
        local.record = None
        if policy == "preserve":
            if result is not None:
                raise ValueError("Unexpected OSD output during preserve")
            return 0
        if result is not getattr(local, "result", None):
            raise ValueError("Unsupported Docling orientation result association")
        selected = original_parser(result)  # Exact existing auto decision, no threshold.
        if type(selected) is not int or selected not in (0, 90, 180, 270):
            raise ValueError("Invalid Docling orientation decision")
        # The reviewed parser negates native clockwise OSD degrees modulo 360.
        # Retain the native report separately from the value handed to Docling.
        orientation_values = result.loc[result["key"] == "Orientation in degrees"].value.tolist()
        raw_orientation = orientation_values[0] if orientation_values else None
        if not isinstance(raw_orientation, str) or len(raw_orientation) > 64:
            raise ValueError("Invalid native orientation observation")
        reported = int(raw_orientation.strip())
        if reported not in (0, 90, 180, 270) or selected != (-reported) % 360:
            raise ValueError("Unsupported Docling orientation convention")
        confidence = None
        try:
            values = result.loc[result["key"] == "Orientation confidence"].value.tolist()
            raw = values[0] if len(values) == 1 else None
            if isinstance(raw, str) and len(raw) <= 64:
                candidate = float(raw.strip())
                if _finite_number(candidate, 0, 1000000):
                    confidence = candidate
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            pass  # An unavailable confidence observation is not invented.
        record.update(status="detected", reported_orientation_degrees=reported,
                      orientation_confidence=confidence, applied_orientation_degrees=selected)
        return selected

    model._perform_osd, module._parse_orientation = perform_osd, parse_orientation
    try:
        yield
    finally:
        model._perform_osd, module._parse_orientation = original_osd, original_parser


def extract_offline(path, *, ocr_engine="easyocr", ocr_orientation=None, pdf_backend=None):
    """Compatibility API returning source sections, warnings and media type."""
    result = extract_offline_result(path, ocr_engine=ocr_engine, ocr_orientation=ocr_orientation, pdf_backend=pdf_backend)
    return result.sections, result.warnings, result.media_type


def require_searchable_text(sections):
    """Reject unusable recovery without claiming that the original contains text."""
    if not any(section["text"].strip() for section in sections):
        raise ValueError("Recovery produced no searchable text; no document version was created. "
                         "The PDF may be blank or content may not have been recovered. "
                         "Inspect the original or explicitly try another OCR engine or PDF renderer.")


def extract_offline_result(path, *, ocr_engine="easyocr", ocr_orientation=None, pdf_backend=None) -> RecoveryExtraction:
    """Recover source text and worker parser provenance in an isolated runtime."""
    if ocr_engine not in ("easyocr", "tesseract"):
        raise ValueError("ocr_engine must be 'easyocr' or 'tesseract'.")
    if pdf_backend not in (None, "pdfium"):
        raise ValueError("pdf_backend must be omitted or 'pdfium'.")
    policy = _orientation_policy(ocr_engine, ocr_orientation)
    # The Python socket guard cannot constrain native helpers. Only configured,
    # trusted local binaries are supported; owned groups require POSIX sessions.
    if os.name != "posix" or not hasattr(os, "killpg"):
        raise ValueError("Local document recovery requires POSIX process-group support for trusted native helpers.")
    if _CLEANUP_FAILED:
        raise ValueError("Document recovery is disabled because a prior worker group could not be verified gone. Restart the service after checking it.")
    command, tessdata = _tesseract_paths() if ocr_engine == "tesseract" else ("", "")
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        raise ValueError("Docling recovery currently accepts PDF files.")
    artifacts = os.environ.get("HG_DOCLING_ARTIFACTS", "")
    artifact_path = Path(artifacts).expanduser()
    if not artifacts or not artifact_path.is_dir():
        raise ValueError("Configure a Docling runtime with HG_DOCLING_PYTHON and prefetch Docling models; set HG_DOCLING_ARTIFACTS to that local directory.")
    artifacts = str(artifact_path.resolve())
    python = os.environ.get("HG_DOCLING_PYTHON", sys.executable)
    executable = Path(python)
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("HG_DOCLING_PYTHON must name an absolute executable Python interpreter.")
    if not _WORKER.acquire(blocking=False):
        raise ValueError("A document recovery is already running. Retry when it finishes.")
    try:
        if _CLEANUP_FAILED:
            raise ValueError("Document recovery is disabled because a prior worker group could not be verified gone. Restart the service after checking it.")
        with tempfile.TemporaryDirectory(prefix="hunter_gatherer-recovery-") as directory:
            output = Path(directory) / "extraction.json"
            env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}
            try:
                result = _run_owned_worker([python, "-I", str(Path(__file__).resolve()), str(path.resolve()), str(output), artifacts, ocr_engine, command, tessdata] + ([policy or "", pdf_backend] if pdf_backend is not None else ([policy] if policy is not None else [])),
                                           env=env, timeout=_TIMEOUT)
            except OSError as exc:
                raise ValueError("Cannot launch the configured local document recovery interpreter.") from exc
            except subprocess.TimeoutExpired:
                raise ValueError("Local document recovery exceeded three minutes; worker group removed. Try a smaller PDF.") from None
            if result.returncode or not output.is_file() or output.stat().st_size > _LIMIT * 4:
                raise ValueError("Local document recovery failed. Check the optional dependency and complete offline model cache.")
            try:
                data = json.loads(output.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                                  parse_constant=_invalid_constant)
                sections, warnings, parser_version = _validate_payload(data)
                config = data.get("provenance", {}).get("config", {})
                if config and config.get("ocr_engine") != ("EasyOcrOptions" if ocr_engine == "easyocr" else "TesseractCliOcrOptions"):
                    raise ValueError("Worker returned a different OCR engine")
                if pdf_backend is not None and (config.get("schema_version") != 4
                        or config.get("pdf_backend", {}).get("name") != pdf_backend):
                    raise ValueError("Worker returned a different PDF renderer")
                if pdf_backend is None and "pdf_backend" in config:
                    raise ValueError("Worker returned an unrequested PDF renderer")
                if ocr_engine == "tesseract" and (config.get("schema_version") not in (3, 4)
                        or config.get("ocr_orientation") != policy
                        or config["ocr_details"]["executable"] != command
                        or config["ocr_details"]["data_directory"] != tessdata):
                    raise ValueError("Worker returned different Tesseract configuration")
            except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
                raise ValueError("Local document recovery returned invalid extraction data.") from exc
            if sum(len(s["text"]) for s in sections) > _LIMIT:
                raise ValueError("Recovered document exceeds the extracted-text limit.")
            require_searchable_text(sections)
            return RecoveryExtraction(sections, warnings, "application/pdf", parser_version, data.get("provenance"))
    finally:
        _WORKER.release()


def _validate_tesseract_paths(command, data):
    binary, directory = Path(command), Path(data)
    if (not command or not binary.is_absolute() or not binary.is_file()
            or not os.access(binary, os.X_OK)):
        raise ValueError("HG_TESSERACT_COMMAND must name an absolute executable trusted local Tesseract binary.")
    if not data or not directory.is_absolute() or not directory.is_dir():
        raise ValueError("HG_TESSERACT_DATA must name an absolute local tessdata directory.")
    for name in ("eng.traineddata", "osd.traineddata"):
        file = directory / name
        if not file.is_file() or file.stat().st_size == 0 or not os.access(file, os.R_OK):
            raise ValueError("HG_TESSERACT_DATA requires readable local eng.traineddata and osd.traineddata; no downloads or fallback.")
    return str(binary.resolve()), str(directory.resolve())


def _tesseract_paths():
    return _validate_tesseract_paths(os.environ.get("HG_TESSERACT_COMMAND", ""),
                                     os.environ.get("HG_TESSERACT_DATA", ""))


def _group_exists(group):
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    # Permission errors mean that disappearance cannot be verified.
    return True


def _stop_group(process):
    """TERM, KILL if necessary, reap, and prove this owned group disappeared.

    This assumes trusted helpers remain in their inherited process group;
    it cannot contain deliberately detached descendants or native networking.
    """
    deadline = time.monotonic() + _CLEANUP_SECONDS
    group = process.pid  # start_new_session=True makes PID the owned PGID.
    for sig, grace in ((signal.SIGTERM, min(1.0, _CLEANUP_SECONDS / 2)),
                       (signal.SIGKILL, _CLEANUP_SECONDS)):
        try:  # noqa: SIM105 - Retain the benchmarked process-group cleanup logic.
            os.killpg(group, sig)
        except ProcessLookupError:
            pass
        phase_end = min(deadline, time.monotonic() + grace)
        while True:
            process.poll()  # Reap our immediate child, including a zombie leader.
            if process.returncode is not None and not _group_exists(group):
                return
            remaining = phase_end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.02, remaining))
    process.poll()
    if process.returncode is None or _group_exists(group):
        raise ValueError("Cannot verify the local recovery worker group is gone; further recovery is disabled until service restart.")


def _run_owned_worker(command, *, env, timeout):
    """Bound the entire process group on normal exit, error, timeout or cancellation."""
    global _CLEANUP_FAILED
    if _CLEANUP_FAILED:
        raise ValueError("Document recovery is disabled because a prior worker group could not be verified gone.")
    if os.name != "posix" or not hasattr(os, "killpg"):
        raise ValueError("Local document recovery requires POSIX process-group support.")
    process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        return subprocess.CompletedProcess(command, process.wait(timeout=timeout))
    finally:
        try:
            _stop_group(process)
        except BaseException:
            _CLEANUP_FAILED = True
            raise


_LEGACY_CONFIG = {"do_ocr": True, "do_table_structure": True, "enable_remote_services": False,
                  "ocr_engine": "EasyOcrOptions", "ocr_languages": ["en"], "download_enabled": False,
                  "max_num_pages": 1000, "max_file_size": 100 * 1024 * 1024}


def _bounded_string(value, maximum=4096):
    return isinstance(value, str) and 0 < len(value) <= maximum and "\x00" not in value


def _absolute_string(value):
    return _bounded_string(value) and Path(value).is_absolute()


def _finite_number(value, lower, upper):
    return type(value) in (int, float) and math.isfinite(value) and lower <= value <= upper


def _validate_fingerprint(item, *, absolute=False):
    if (not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}
            or not _bounded_string(item["path"])
            or type(item["size"]) is not int or not 0 <= item["size"] <= _INVENTORY_BYTES
            or not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
        raise ValueError("Invalid file fingerprint")
    path = Path(item["path"])
    if (absolute and not path.is_absolute()) or (not absolute and (path.is_absolute() or ".." in path.parts)):
        raise ValueError("Invalid fingerprint path")


def _inventory_digest(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _validate_config(config):
    # Stored legacy extractions retain their exact historical configuration.
    if config == _LEGACY_CONFIG:
        return
    fields = set(_LEGACY_CONFIG) | {"schema_version", "ocr_mode", "ocr_scale", "force_full_page_ocr",
                                   "bitmap_area_threshold", "recognition_threshold", "psm", "ocr_details", "fingerprints"}
    if isinstance(config, dict) and config.get("schema_version") == 4:
        fields.add("pdf_backend")
        renderer = config.get("pdf_backend")
        if (not isinstance(renderer, dict)
                or set(renderer) != {"name", "class", "package", "version", "version_source"}
                or renderer["name"] != "pdfium"
                or renderer["class"] != "docling.backend.pypdfium2_backend.PyPdfiumDocumentBackend"
                or renderer["package"] != "pypdfium2"
                or renderer["version_source"] != "worker_distribution_metadata"
                or not _bounded_string(renderer["version"], 256)):
            raise ValueError("Invalid PDF renderer provenance")
    if isinstance(config, dict) and (config.get("schema_version") == 3
            or (config.get("schema_version") == 4 and config.get("ocr_engine") == "TesseractCliOcrOptions")):
        fields |= {"ocr_orientation", "orientation_observations"}
        if config.get("ocr_engine") != "TesseractCliOcrOptions" or config.get("ocr_orientation") not in ("auto", "preserve"):
            raise ValueError("Invalid orientation policy")
        _validate_orientation_observations(config.get("orientation_observations"), config["ocr_orientation"])
    if not isinstance(config, dict) or set(config) != fields or type(config["schema_version"]) is not int or config["schema_version"] not in (2, 3, 4):
        raise ValueError("Invalid recovery configuration")
    for key in ("do_ocr", "do_table_structure", "enable_remote_services", "download_enabled"):
        if type(config[key]) is not bool or config[key] != _LEGACY_CONFIG[key]:
            raise ValueError("Invalid recovery configuration")
    for key in ("max_num_pages", "max_file_size"):
        if type(config[key]) is not int or config[key] != _LEGACY_CONFIG[key]:
            raise ValueError("Invalid conversion limits")
    easy = config["ocr_engine"] == "EasyOcrOptions"
    if (config["ocr_engine"] not in ("EasyOcrOptions", "TesseractCliOcrOptions")
            or config["ocr_languages"] != (["en"] if easy else ["eng"])):
        raise ValueError("Invalid OCR selection")
    # None records an unavailable observation rather than an invented default.
    if config["ocr_mode"] not in (None, "default", "pdf_aware_layout_regions"):
        raise ValueError("Invalid OCR mode")
    if config["force_full_page_ocr"] is not None and config["force_full_page_ocr"] is not False:
        raise ValueError("Invalid full-page OCR observation")
    for key, lower, upper in (("ocr_scale", 0.01, 100), ("bitmap_area_threshold", 0, 1), ("recognition_threshold", 0, 1)):
        if config[key] is not None and not _finite_number(config[key], lower, upper):
            raise ValueError("Invalid OCR numeric observation")
    if config["psm"] is not None or (not easy and (config["ocr_scale"] != 3.0 or config["recognition_threshold"] is not None)):
        raise ValueError("Invalid frozen Tesseract configuration")
    details = config["ocr_details"]
    if not isinstance(details, dict) or set(details) != {"package", "version", "version_source", "executable", "executable_version", "data_directory"}:
        raise ValueError("Invalid OCR details")
    if details["package"] != ("easyocr" if easy else None) or details["version_source"] != ("worker_distribution_metadata" if easy else "not_applicable"):
        raise ValueError("Invalid OCR package")
    for key in ("version", "executable_version"):
        if details[key] is not None and not _bounded_string(details[key], 256):
            raise ValueError("Invalid OCR version")
    if easy:
        if any(details[k] is not None for k in ("executable", "executable_version", "data_directory")):
            raise ValueError("Invalid EasyOCR helper details")
    elif (details["version"] is not None or not _absolute_string(details["executable"])
          or not _absolute_string(details["data_directory"])):
        raise ValueError("Invalid Tesseract helper details")
    fingerprints = config["fingerprints"]
    if (not isinstance(fingerprints, dict) or set(fingerprints) != {"scope", "artifacts", "ocr_assets", "executable"}
            or fingerprints["scope"] != _FINGERPRINT_SCOPE):
        raise ValueError("Invalid fingerprint scope")
    inventory = fingerprints["artifacts"]
    if (not isinstance(inventory, dict) or set(inventory) != {"root", "sha256", "file_count", "total_bytes", "entries"}
            or not _absolute_string(inventory["root"]) or not isinstance(inventory["entries"], list)
            or len(inventory["entries"]) > _INVENTORY_FILES):
        raise ValueError("Invalid artifact inventory")
    entries = inventory["entries"]
    for entry in entries:
        _validate_fingerprint(entry)
    if (type(inventory["file_count"]) is not int or inventory["file_count"] != len(entries)
            or type(inventory["total_bytes"]) is not int
            or inventory["total_bytes"] != sum(e["size"] for e in entries)
            or inventory["total_bytes"] > _INVENTORY_BYTES
            or [e["path"] for e in entries] != sorted(set(e["path"] for e in entries))
            or inventory["sha256"] != _inventory_digest(entries)):
        raise ValueError("Inconsistent artifact inventory")
    assets = fingerprints["ocr_assets"]
    if not isinstance(assets, list) or len(assets) != (0 if easy else 2):
        raise ValueError("Invalid OCR assets")
    if easy:
        if fingerprints["executable"] is not None:
            raise ValueError("Unexpected EasyOCR binary fingerprint")
    else:
        _validate_fingerprint(fingerprints["executable"], absolute=True)
        for asset in assets:
            _validate_fingerprint(asset, absolute=True)
        if (fingerprints["executable"]["path"] != details["executable"]
                or [a["path"] for a in assets] != [str(Path(details["data_directory"]) / name) for name in ("eng.traineddata", "osd.traineddata")]):
            raise ValueError("Inconsistent Tesseract fingerprints")


def _file_fingerprint(file, path):
    size = file.stat().st_size
    if size > _INVENTORY_BYTES:
        raise ValueError("Local recovery fingerprint exceeds inventory size limit")
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        total = 0
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > _INVENTORY_BYTES:
                raise ValueError("Local recovery fingerprint exceeds inventory size limit")
            digest.update(chunk)
    if total != size or file.stat().st_size != size:
        raise ValueError("Local recovery artifact changed during fingerprinting")
    return {"path": path, "size": size, "sha256": digest.hexdigest()}


def _artifact_inventory(root):
    """Snapshot configured cache files, not a claim of which models were loaded.

    Includes regular files and file symlinks (hashes target bytes), including
    hidden files. Does not traverse directory symlinks or hash directories.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("Configured recovery artifact directory is unavailable")
    entries, total = [], 0
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs.sort()
        for name in sorted(files):
            file = Path(directory) / name
            if not file.is_file():
                raise ValueError("Recovery cache contains an unsupported nonregular file")
            if len(entries) >= _INVENTORY_FILES or total + file.stat().st_size > _INVENTORY_BYTES:
                raise ValueError("Recovery model inventory exceeds bounded limits")
            entry = _file_fingerprint(file, file.relative_to(root).as_posix())
            entries.append(entry)
            total += entry["size"]
    entries.sort(key=lambda entry: entry["path"])
    return {"root": str(root), "sha256": _inventory_digest(entries), "file_count": len(entries),
            "total_bytes": total, "entries": entries}


def _distribution_version(package):
    try:
        return package_version(package)
    except PackageNotFoundError:
        return None


def _executable_version(command):
    # Bounded output; trusted executable inherits the worker's owned group.
    with tempfile.TemporaryFile() as stream:
        result = subprocess.run([command, "--version"], stdout=stream, stderr=stream,
                                stdin=subprocess.DEVNULL, timeout=10, check=False)
        stream.seek(0)
        first = stream.read(4096).decode("utf-8", errors="replace").splitlines()
    if result.returncode or not first:
        return None
    line = first[0].strip()
    return line if _bounded_string(line, 256) else None


def _worker_config(ocr, engine, artifacts, command, tessdata, *, ocr_orientation=None, orientation_observations=None, pdf_backend=None):
    easy = engine == "easyocr"
    mode = getattr(ocr, "mode", None)
    mode = getattr(mode, "value", mode)
    config = {**_LEGACY_CONFIG, "schema_version": 2,
              "ocr_engine": "EasyOcrOptions" if easy else "TesseractCliOcrOptions",
              "ocr_languages": list(ocr.lang), "ocr_mode": mode,
              "ocr_scale": getattr(ocr, "scale", None),
              "force_full_page_ocr": getattr(ocr, "force_full_page_ocr", None),
              "bitmap_area_threshold": getattr(ocr, "bitmap_area_threshold", None),
              "recognition_threshold": getattr(ocr, "confidence_threshold", None) if easy else None,
              "psm": None if easy else ocr.psm,
              "ocr_details": {"package": "easyocr" if easy else None,
                              "version": _distribution_version("easyocr") if easy else None,
                              "version_source": "worker_distribution_metadata" if easy else "not_applicable",
                              "executable": None if easy else command,
                              "executable_version": None if easy else _executable_version(command),
                              "data_directory": None if easy else tessdata},
              "fingerprints": {"scope": _FINGERPRINT_SCOPE, "artifacts": _artifact_inventory(artifacts),
                               "ocr_assets": [] if easy else [_file_fingerprint(Path(tessdata) / name, str(Path(tessdata) / name))
                                                               for name in ("eng.traineddata", "osd.traineddata")],
                               "executable": None if easy else _file_fingerprint(Path(command), command)}}
    if orientation_observations is not None:
        if easy:
            raise ValueError("Orientation observations require Tesseract")
        config.update(schema_version=3, ocr_orientation=ocr_orientation,
                      orientation_observations=orientation_observations)
    if pdf_backend is not None:
        config.update(schema_version=4, pdf_backend=pdf_backend)
    _validate_config(config)
    return config


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON constant")


def _validate_payload(data):
    if not isinstance(data, dict) or set(data) not in ({"backend", "parser_version", "sections", "warnings"}, {"backend", "parser_version", "sections", "warnings", "provenance"}):
        raise ValueError("Invalid payload fields")
    parser_version = data["parser_version"]
    if data["backend"] != "docling" or not isinstance(parser_version, str) or not re.fullmatch(r"docling-[0-9][A-Za-z0-9.!+_-]{0,127}", parser_version):
        raise ValueError("Invalid parser provenance")
    if "provenance" in data:
        provenance = data["provenance"]
        if (not isinstance(provenance, dict) or set(provenance) != {"runtime", "backend_details", "config"}
                or not isinstance(provenance["runtime"], dict)
                or set(provenance["runtime"]) != {"implementation", "version"}
                or not all(isinstance(v, str) and 0 < len(v) <= 128 for v in provenance["runtime"].values())
                or provenance["backend_details"] != {"package": "docling", "version": parser_version[8:], "version_source": "worker_distribution_metadata"}):
            raise ValueError("Invalid runtime provenance")
        try:
            _validate_config(provenance["config"])
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("Invalid runtime provenance") from exc
    sections, warnings = data["sections"], data["warnings"]
    if not isinstance(sections, list) or not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        raise ValueError("Invalid extraction lists")
    for section in sections:
        if (not isinstance(section, dict) or set(section) not in (
                {"page", "section", "text", "geometry", "blocks"},
                {"page", "section", "text", "geometry", "blocks", "table_structure"})):
            raise ValueError("Invalid section fields")
        page, text = section["page"], section["text"]
        if page is not None and (type(page) is not int or page < 1 or page > 1000):
            raise ValueError("Invalid page")
        if not isinstance(text, str) or not isinstance(section["section"], str):
            raise ValueError("Invalid source text")
        geometry, blocks = section["geometry"], section["blocks"]
        if not isinstance(geometry, list) or not isinstance(blocks, list):
            raise ValueError("Invalid structure lists")
        for prov in geometry:
            if not isinstance(prov, dict) or set(prov) != {"page_no", "bbox", "charspan"}:
                raise ValueError("Invalid geometry")
            if type(prov["page_no"]) is not int or not 1 <= prov["page_no"] <= 1000:
                raise ValueError("Invalid provenance page")
            bbox, span = prov["bbox"], prov["charspan"]
            if not isinstance(bbox, dict) or set(bbox) != {"l", "t", "r", "b", "coord_origin"}:
                raise ValueError("Invalid bounding box")
            if bbox["coord_origin"] not in {"TOPLEFT", "BOTTOMLEFT"} or not all(type(bbox[k]) in {int, float} and math.isfinite(bbox[k]) for k in ("l", "t", "r", "b")):
                raise ValueError("Invalid bounding coordinates")
            # Docling spans refer to the original item, not exported table Markdown.
            if not isinstance(span, list) or len(span) != 2 or not all(type(n) is int for n in span) or not 0 <= span[0] <= span[1]:
                raise ValueError("Invalid provenance span")
        if geometry and page != geometry[0]["page_no"]:
            raise ValueError("Inconsistent page provenance")
        if not geometry and page is not None:
            raise ValueError("Missing page provenance")
        for block in blocks:
            if not isinstance(block, dict) or set(block) != {"kind", "start", "end"} or not isinstance(block["kind"], str):
                raise ValueError("Invalid block")
            if type(block["start"]) is not int or type(block["end"]) is not int or not 0 <= block["start"] <= block["end"] <= len(text):
                raise ValueError("Invalid block span")
        if "table_structure" in section:
            if (len(blocks) != 1 or blocks[0]["kind"] not in {"table", "document_index"}
                    or blocks[0]["start"] != 0 or blocks[0]["end"] != len(text)):
                raise ValueError("Table structure requires a standalone table section")
            _validate_table_structure(section["table_structure"], text)
    return sections, warnings, parser_version


def _disable_network():
    # Missing artifacts fail rather than fetching data. This Python-level guard
    # does not intercept native libraries or external helper processes.
    import socket

    def blocked(*args, **kwargs):
        raise OSError("Document recovery network access is disabled")

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.create_connection = blocked


def _worker(source, output, artifacts, ocr_engine="easyocr", command="", tessdata="", ocr_orientation=None, pdf_backend=None):
    if pdf_backend not in (None, "pdfium"):
        raise ValueError("Invalid worker PDF renderer")
    policy = _orientation_policy(ocr_engine, ocr_orientation or None)
    _disable_network()
    from docling.datamodel.base_models import ConversionStatus, InputFormat
    from docling.datamodel.pipeline_options import EasyOcrOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    if ocr_engine == "easyocr":
        if command or tessdata:
            raise ValueError("EasyOCR must not receive native helper configuration")
        ocr = EasyOcrOptions(lang=["en"], download_enabled=False)
    elif ocr_engine == "tesseract":
        from docling.datamodel.pipeline_options import TesseractCliOcrOptions
        # Recheck frozen paths, never discover binaries or models from PATH.
        command, tessdata = _validate_tesseract_paths(command, tessdata)
        os.environ["TESSDATA_PREFIX"] = tessdata
        ocr = TesseractCliOcrOptions(lang=["eng"], tesseract_cmd=command,
                                   path=tessdata, scale=3.0, psm=None, mode="default")
    else:
        raise ValueError("Invalid worker OCR engine")
    options = PdfPipelineOptions(artifacts_path=Path(artifacts), enable_remote_services=False,
                                 ocr_options=ocr)
    options.do_ocr = True
    options.do_table_structure = True
    orientation = _OrientationObservations() if ocr_engine == "tesseract" else None
    hook = _tesseract_orientation_context(policy, orientation) if orientation is not None else nullcontext()
    backend_options, renderer = {}, None
    if pdf_backend == "pdfium":
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        backend_options["backend"] = PyPdfiumDocumentBackend
        renderer = {"name": "pdfium", "class": PyPdfiumDocumentBackend.__module__ + "." + PyPdfiumDocumentBackend.__name__,
                    "package": "pypdfium2", "version": package_version("pypdfium2"),
                    "version_source": "worker_distribution_metadata"}
    with hook:
        converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options, **backend_options)})
        result = converter.convert(Path(source), max_num_pages=1000, max_file_size=100 * 1024 * 1024)
    if result.status not in {ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS}:
        raise ValueError("Docling did not successfully convert this PDF")
    sections = _sections_from_document(result.document)
    warnings = ["Local layout/OCR-derived text: compare quotations, tables, and reading order with the original. Extraction is not verification."]
    if result.status == ConversionStatus.PARTIAL_SUCCESS:
        warnings.append("Docling reported partial conversion; some PDF content may be missing.")
    docling_version = package_version("docling")
    provenance = {"runtime": {"implementation": platform.python_implementation(), "version": platform.python_version()},
                  "backend_details": {"package": "docling", "version": docling_version,
                                      "version_source": "worker_distribution_metadata"},
                  "config": _worker_config(ocr, ocr_engine, artifacts, command, tessdata,
                                           ocr_orientation=policy,
                                           orientation_observations=orientation.snapshot() if orientation is not None else None,
                                           pdf_backend=renderer)}
    payload = {"backend": "docling", "parser_version": "docling-" + docling_version,
               "sections": sections, "warnings": warnings, "provenance": provenance}
    Path(output).write_text(json.dumps(payload), encoding="utf-8")


def _table_markdown_with_refs(document, table, excluded_refs):
    """Match table export while identifying refs whose text it actually emits.

    Serializer ``visited`` is too broad (nested tables mark unrendered notes),
    and table spans alone omit rich cells. Track successful nested results and
    follow only cell refs for the nested-table flattening case.
    """
    from docling_core.transforms.serializer.common import create_ser_result
    from docling_core.transforms.serializer.markdown import MarkdownDocSerializer
    from docling_core.types.doc import RichTableCell, TableItem, TextItem

    rendered_refs = set()

    def flattened_refs(item, excluded):
        if isinstance(item, TableItem):
            if item.self_ref in excluded:
                return False
            has_text = False
            for row in item.data.grid:
                for cell in row:
                    if isinstance(cell, RichTableCell):
                        has_text = flattened_refs(cell.ref.resolve(document), excluded) or has_text
                    else:
                        has_text = bool((cell.text or "").strip()) or has_text
            if has_text:
                rendered_refs.add(item.self_ref)
            return has_text
        has_text = isinstance(item, TextItem) and item.self_ref not in excluded and bool(item.text.strip())
        if has_text:
            rendered_refs.add(item.self_ref)
        for child in item.children:
            has_text = flattened_refs(child.resolve(document), excluded) or has_text
        return has_text

    class TrackingSerializer(MarkdownDocSerializer):
        def get_excluded_refs(self, **kwargs):
            return super().get_excluded_refs(**kwargs) | excluded_refs

        def serialize_captions(self, item, **kwargs):
            # Captions remain independently citable on their original page.
            # Validate references even though their text is emitted by traversal.
            for ref in item.captions:
                ref.resolve(document)
            return create_ser_result()

        def serialize(self, *, item=None, **kwargs):
            result = super().serialize(item=item, **kwargs)
            if result.text.strip():
                rendered_refs.update(span.item.self_ref for span in result.spans)
                if kwargs.get("_nested_in_table") and isinstance(item, TableItem):
                    flattened_refs(item, self.get_excluded_refs(**kwargs))
            return result

    result = TrackingSerializer(doc=document).serialize(item=table)
    return result.text, rendered_refs


def _table_excluded_refs(document):
    from docling_core.types.doc import ContentLayer

    items = list(document.iterate_items(
        with_groups=True, traverse_pictures=True, included_content_layers=set(ContentLayer)))
    refs = {item.self_ref for item, _ in items
            if getattr(item, "content_layer", ContentLayer.BODY) != ContentLayer.BODY}
    # Captions keep their original standalone text and provenance, even when
    # nested-table flattening would otherwise pull them into rich-cell text.
    refs.update(ref.resolve(document).self_ref for item, _ in items
                for ref in getattr(item, "captions", []))
    return refs


_ALLOWED = frozenset({'text', 'list_item', 'section_header', 'title'})

def _label(item):
    value = getattr(item, 'label', '')
    return str(getattr(value, 'value', value)).split('.')[-1].lower()

def _nearest_picture(document, item):
    parent = getattr(item, 'parent', None)
    seen = set()
    while parent is not None:
        if parent.cref in seen:
            raise ValueError('Cycle in figure parent references')
        seen.add(parent.cref)
        ancestor = parent.resolve(document)
        if _label(ancestor) == 'picture':
            return ancestor.self_ref
        parent = getattr(ancestor, 'parent', None)
    return None

class _FigureSectionCollector:
    def __init__(self, document, section_limit=1800):
        if type(section_limit) is not int or section_limit < 1:
            raise ValueError('section_limit must be a positive integer')
        self.document = document
        self.section_limit = section_limit
        self.sections = []
        self.pending = None
        self.key = None
        self.run_counts = {}

    def boundary(self):
        if self.pending is not None:
            self.sections.append(self.pending)
        self.pending = self.key = None

    def add(self, item, section):
        # Empty traversal events still separate reading groups (e.g. pictures).
        if section is None:
            self.boundary()
            return
        geometry = section.get('geometry', [])
        page = section['page']
        picture = _nearest_picture(self.document, item) if _label(item) in _ALLOWED else None
        eligible = (picture is not None and page is not None and geometry
                    and all(p['page_no'] == page for p in geometry))
        if not eligible:
            self.boundary()
            self.sections.append(section)
            return
        key = (picture, page)
        text = section['text']
        if self.key != key or (self.pending is not None and
                              len(self.pending['text']) + 2 + len(text) > self.section_limit):
            self.boundary()
        if self.pending is None:
            self.key = key
            run = self.run_counts.get(picture, 0) + 1
            self.run_counts[picture] = run
            self.pending = {'page': page,
                            'section': f'Page {page} · figure_text {picture} · run {run}',
                            'text': text, 'geometry': list(geometry),
                            'blocks': [dict(block) for block in section['blocks']]}
        else:
            offset = len(self.pending['text']) + 2
            self.pending['text'] += '\n\n' + text
            self.pending['geometry'].extend(geometry)
            self.pending['blocks'].extend({**block, 'start': offset + block['start'],
                                          'end': offset + block['end']} for block in section['blocks'])

    def finish(self):
        self.boundary()
        return self.sections


def _sections_from_document(document):
    collector, total = _FigureSectionCollector(document), 0
    items = list(document.iterate_items(traverse_pictures=True))
    source_items = {item.self_ref: (position, item) for position, (item, _) in enumerate(items)
                    if hasattr(item, "self_ref")}
    table_exports, table_geometry, rendered_refs, table_total = {}, {}, set(), 0
    excluded_refs = None
    # Preorder puts valid owned rich-cell subtrees after their table. Resolve
    # tables first so embedded rich-cell text is omitted from outer traversal.
    for item, _level in items:
        if getattr(item, "self_ref", None) in rendered_refs:
            continue
        raw_label = getattr(item, "label", "text")
        label = str(getattr(raw_label, "value", raw_label)).split(".")[-1].lower()
        if label in {"table", "document_index"}:
            if excluded_refs is None:
                excluded_refs = _table_excluded_refs(document)
            text, refs = _table_markdown_with_refs(document, item, excluded_refs | rendered_refs)
            table_total += len(text)
            if table_total > _LIMIT:
                raise ValueError("Extraction limit exceeded")
            table_exports[id(item)] = text
            # Child spans are item-local; preserve their original provenance
            # after the table's own geometry without reinterpreting charspans.
            children = sorted(source_items[ref] for ref in refs - {item.self_ref}
                              if ref in source_items)
            table_geometry[id(item)] = [p for _, child in children
                                        for p in getattr(child, "prov", [])]
            rendered_refs.update(refs - {item.self_ref})
    for item, _level in items:
        if getattr(item, "self_ref", None) in rendered_refs:
            collector.add(item, None)
            continue
        raw_label = getattr(item, "label", "text")
        label = str(getattr(raw_label, "value", raw_label)).split(".")[-1].lower()
        if label in {"table", "document_index"}:
            text = table_exports[id(item)]
        else:
            text = getattr(item, "text", "")
        if not text.strip():
            collector.add(item, None)
            continue
        provenance = list(getattr(item, "prov", [])) + table_geometry.get(id(item), [])
        page = provenance[0].page_no if provenance else None
        total += len(text)
        if total > _LIMIT:
            raise ValueError("Extraction limit exceeded")
        geometry = [p.model_dump(mode="json") for p in provenance]
        section = {"page": page, "section": f"Page {page} · {label}" if page else label,
                         "text": text, "geometry": geometry,
                         "blocks": [{"kind": label, "start": 0, "end": len(text)}]}
        if label in {"table", "document_index"}:
            snapshot = _table_structure_snapshot(item, text)
            if snapshot is not None:
                section["table_structure"] = snapshot
        collector.add(item, section)
    sections = collector.finish()
    if sum(len(section["text"]) for section in sections) > _LIMIT:
        raise ValueError("Extraction limit exceeded")
    return sections


if __name__ == "__main__":
    _worker(*sys.argv[1:])
