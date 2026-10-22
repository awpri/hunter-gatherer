"""Bounded caller-supplied transcription, tied to exact original bytes.

The manifest is data, never instructions. Checksums establish identity, not
transcription accuracy or the truth of the producer's identity and page labels.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".json": "application/json",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}
WARNING = "Agent-supplied transcription and locations are unverified; compare with the original before relying on them."

CATALOG_WARNING = "Source cataloged without extracted text; inspect the original. No passage coverage or answerability is established."

PARTIAL_WARNING = "Agent reported partial extraction; missing passages do not establish absence in the original."


def validate_options(path, backend, ocr_engine, ocr_orientation, pdf_backend):
    if path is None:
        return
    if not isinstance(path, str) or not path.strip() or len(path) > 4096:
        raise ValueError("extraction_manifest_path must be a nonempty local path, at most 4096 characters")
    if backend != "native" or any(v is not None for v in (ocr_engine, ocr_orientation, pdf_backend)):
        raise ValueError("extraction_manifest_path replaces extraction; omit OCR/backend options")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Extraction manifest contains duplicate JSON keys")
        result[key] = value
    return result


def _text(value, label, maximum, *, blank=False):
    if not isinstance(value, str) or len(value) > maximum or (not blank and not value.strip()):
        raise ValueError(f"Invalid extraction manifest {label}")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"Invalid UTF-8 in extraction manifest {label}") from exc


def load_manifest(path, source_sha256, suffix):
    """Read once; validate before parsing/indexing or mutating the archive."""
    # The adapter consumes supplied text and never parses or executes the
    # original. Keep archive/download/backup names within their shared grammar.
    # Unknown formats remain opaque bytes rather than guessed executable MIME.
    if type(suffix) is not str or not re.fullmatch(r"(?:\.[a-z0-9_-]{1,24})?", suffix):
        raise ValueError("Agent extraction requires no extension or a safe extension of 1–24 ASCII letters, digits, underscores or hyphens")
    fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("Extraction manifest must be a regular file no larger than 16 MiB")
        data = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("Extraction manifest exceeds 16 MiB")
    try:
        manifest = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Extraction manifest must be valid UTF-8 JSON") from exc
    required = {"schema_version", "source_sha256", "producer", "coverage", "sections"}
    if (not isinstance(manifest, dict) or not required <= manifest.keys()
            or manifest.keys() - required - {"warnings", "catalog_only"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1):
        raise ValueError("Extraction manifest requires schema_version 1, source_sha256, producer, coverage and sections")
    digest = manifest["source_sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != source_sha256:
        raise ValueError("Extraction manifest source_sha256 does not match the original bytes")
    producer = manifest["producer"]
    if not isinstance(producer, dict) or set(producer) != {"name", "version"}:
        raise ValueError("Extraction producer requires name and version")
    for key, value in producer.items():
        _text(value, f"producer.{key}", 200)
    if manifest["coverage"] not in ("complete", "partial"):
        raise ValueError("Extraction coverage must be complete or partial (caller-reported)")
    catalog_only = manifest.get("catalog_only", False)
    if type(catalog_only) is not bool:
        raise ValueError("catalog_only must be a boolean")
    if catalog_only and manifest["coverage"] != "partial":
        raise ValueError("catalog_only requires coverage=partial")
    sections = manifest["sections"]
    if catalog_only:
        if sections != []:
            raise ValueError("catalog_only requires an empty sections list")
    elif not isinstance(sections, list) or not 1 <= len(sections) <= 4096:
        raise ValueError("Extraction manifest requires 1–4096 ordered sections")
    for section in sections:
        if not isinstance(section, dict) or set(section) != {"page", "section", "text"}:
            raise ValueError("Each extraction section requires page, section and text")
        page = section["page"]
        if page is not None and (type(page) is not int or not 1 <= page <= 100000):
            raise ValueError("Extraction page must be null or an integer from 1 to 100000")
        _text(section["section"], "section label", 500)
        _text(section["text"], "section text", MAX_MANIFEST_BYTES, blank=True)
    if not catalog_only and not any(s["text"].strip() for s in sections):
        raise ValueError("Extraction manifest has no searchable text")
    warnings = manifest.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 32:
        raise ValueError("Extraction warnings must be a list of at most 32 strings")
    for warning in warnings:
        _text(warning, "warning", 500)
    provenance = {
        "manifest_sha256": hashlib.sha256(data).hexdigest(),
        "producer": producer, "producer_attribution": "caller_reported",
        "coverage": manifest["coverage"], "coverage_attribution": "caller_reported",
        "text_verification": "unverified", "locations_verification": "unverified",
    }
    if catalog_only:
        provenance["catalog_only"] = True
    notices = [CATALOG_WARNING if catalog_only else WARNING, *[f"Agent-reported warning: {w}" for w in warnings]]
    if manifest["coverage"] == "partial":
        notices.insert(1, PARTIAL_WARNING)
    return sections, notices, MEDIA_TYPES.get(suffix, "application/octet-stream"), provenance
