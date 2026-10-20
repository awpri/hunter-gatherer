"""Extraction observations and exact text hashes; never reconstruct old runtime facts.

No optional parser/model is imported here. Discoverability is an observation,
not proof that an optional module imports successfully or was used on a page.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import platform
import sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path


def runtime_info():
    return {"implementation": platform.python_implementation(), "version": platform.python_version()}


def fonttools_info():
    failures = []
    try:
        discoverable = importlib.util.find_spec("fontTools") is not None
    except Exception as exc:
        failures.append({"field": "discoverable", "error": type(exc).__name__[:64]})
        # Installed import hooks can fail; capability observation is advisory.
        discoverable = None
    module = sys.modules.get("fontTools")
    package_version = getattr(module, "__version__", None)
    version_source = "loaded_module" if package_version else None
    if not package_version and discoverable:
        try:
            package_version, version_source = version("fonttools"), "distribution_metadata"
        except Exception as exc:
            failures.append({"field": "version", "error": type(exc).__name__[:64]})
            # Metadata discovery may fail independently of successful extraction.
            pass
    result = {"discoverable": discoverable, "version": package_version,
              "version_source": version_source, "cffLib_loaded": "fontTools.cffLib" in sys.modules,
              "page_usage": "unknown"}
    if failures:
        result["observation_errors"] = failures
    return result


def native_pdf_info(pypdf):
    return {"package": "pypdf", "version": getattr(pypdf, "__version__", None),
            "version_source": "imported_module", "fontTools": fonttools_info()}


def source_file_sha256(path, failures, field):
    """Unknown if installed source is unavailable; text extraction still succeeds."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        failures.append({"field": field, "error": type(exc).__name__[:64]})
        return None


def build_provenance(source_sha256, sections, backend, parser_version, *, worker=None, suffix="", supplied=None):
    """Hash an ordered manifest that binds every exact text to its source coordinates."""
    manifest = [{"ordinal": i, "page": s["page"], "section": s["section"],
                 "text_sha256": hashlib.sha256(s["text"].encode("utf-8")).hexdigest(),
                 "characters": len(s["text"])} for i, s in enumerate(sections)]
    for entry, section in zip(manifest, sections, strict=True):
        if "table_structure" in section:
            encoded_structure = json.dumps(section["table_structure"], ensure_ascii=False,
                                           sort_keys=True, separators=(",", ":"))
            entry["table_structure_sha256"] = hashlib.sha256(encoded_structure.encode("utf-8")).hexdigest()
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    implementation = Path(__file__).with_name({"docling": "document_recovery.py",
                                              "agent_supplied": "agent_extraction.py"}.get(backend, "documents.py"))
    failures = []
    result = {"schema_version": 1, "source_sha256": source_sha256,
              "sections": manifest, "ordered_sections_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
              "hash_encoding": "UTF-8; manifest JSON ensure_ascii=false, sort_keys=true, separators=(',',':')",
              "extractor": {"backend": backend, "parser_version": parser_version,
                            "implementation_sha256": source_file_sha256(implementation, failures, "extractor.implementation_sha256")},
              "runtime": runtime_info() if backend == "native" else None,
              "backend_details": None, "config": None}
    if backend == "native":
        pdf = next((s["_extraction"] for s in sections if "_extraction" in s), None)
        if pdf:
            result["backend_details"] = pdf["backend_details"]
            result["config"] = pdf["config"]
            result["page_modes"] = [{"page": s["page"], "mode": s["_extraction"]["mode"]} for s in sections]
        elif suffix != ".pdf":
            result["backend_details"] = {"package": "python_stdlib", "version": platform.python_version()}
            if suffix in {".txt", ".md", ".markdown"}:
                result["config"] = {"encoding": "utf-8-sig", "errors": "strict"}
            if suffix == ".docx":
                component = Path(__file__).with_name("document_extraction.py")
                result["extractor"]["docx_implementation_sha256"] = source_file_sha256(component, failures, "extractor.docx_implementation_sha256")
    elif backend == "agent_supplied":
        result["supplied_extraction"] = supplied
        result["extractor"]["role"] = "manifest_validator; external extraction not observed"
    elif worker is not None:
        result["runtime"] = worker["runtime"]
        result["backend_details"] = worker["backend_details"]
        result["config"] = worker["config"]
    if failures:
        result["observation_errors"] = failures
    return result


def compact_provenance(saved):
    """Omit growing manifests on routine reads without changing stored observations."""
    result = {key: value for key, value in saved.items() if key not in {"sections", "page_modes"}}
    result["section_count"] = len(saved["sections"])
    if "page_modes" in saved:
        result["page_mode_counts"] = dict(Counter(entry["mode"] for entry in saved["page_modes"]))
    config = saved.get("config")
    if isinstance(config, dict) and config.get("schema_version") in (2, 3, 4):
        fingerprints = config.get("fingerprints")
        if isinstance(fingerprints, dict) and isinstance(fingerprints.get("artifacts"), dict):
            result["config"] = {
                **config,
                "fingerprints": {
                    **fingerprints,
                    "artifacts": {key: value for key, value in fingerprints["artifacts"].items() if key != "entries"},
                },
            }
        orientation = config.get("orientation_observations")
        if isinstance(orientation, dict):
            result["config"] = {
                **result.get("config", config),
                "orientation_observations": {key: value for key, value in orientation.items() if key != "records"},
            }
    return result
