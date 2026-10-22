"""Inspect optional packages without importing them, downloading models or opening a library."""
from __future__ import annotations

import importlib.metadata
import json
import os
import shlex
import shutil
import sys

from rich.console import Console
from rich.table import Table

CAPABILITIES = (
    ("semantic", "Semantic search", ("sentence-transformers",), "Local model weights download on first configured use."),
    ("document-view", "PDF page images", ("pypdfium2", "Pillow"), "Local PDF rendering; no model needed."),
    ("document-recovery", "PDF layout / OCR", ("docling",), "Model preparation is separate from package installation."),
    ("qdrant", "Qdrant vector index", ("qdrant-client",), "Requires a separately configured Qdrant service."),
    ("voice", "Speech input", ("faster-whisper", "silero-vad", "sounddevice", "numpy", "torchaudio"), "Requires audio hardware and model weights."),
    ("tts", "Local speech output", ("piper-tts",), "Requires a configured voice model."),
    ("tts-openai", "OpenAI speech output", ("openai",), "Requires provider credentials; sends spoken text to the provider."),
    ("local", "SQLite vector extension", ("sqlite-vec",), "Optional index; the base library includes an in-memory index."),
    ("pgcontext", "PostgreSQL index experiment", ("psycopg",), "Requires a separately configured database."),
    ("desktop", "Desktop window", ("pywebview",), "Requires platform webview support."),
)


def setup_report() -> dict:
    capabilities = []
    uv = shutil.which("uv")
    installer = [uv, "pip", "install", "--python", sys.executable] if uv else [sys.executable, "-m", "pip", "install"]
    for extra, label, packages, note in CAPABILITIES:
        installed = {}
        missing = []
        for name in packages:
            try:
                installed[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                missing.append(name)
        capabilities.append({
            "extra": extra, "label": label,
            "status": "installed" if not missing else "partial" if installed else "not_installed",
            "installed_packages": installed, "missing_packages": missing,
            "install_argv": [*installer, f".[{extra}]"],
            "note": note,
        })
    return {
        "schema_version": 1,
        "kind": "setup",
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "virtual_environment": sys.prefix != sys.base_prefix,
        "capabilities": capabilities,
        "install_working_directory": "hunter/gatherer source checkout",
        "inspection": "Package metadata only; model files, service connectivity and native libraries are not tested.",
    }


def cmd_setup(args, _settings=None) -> int:
    report = setup_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=True))
        return 0
    console = Console()
    console.print("[bold]hunter/gatherer · optional capabilities[/bold]")
    console.print("The core library and fictional demo need none of these extras.\n")
    table = Table(box=None, padding=(0, 2, 0, 0))
    table.add_column("Capability")
    table.add_column("Packages")
    table.add_column("Extra")
    for item in report["capabilities"]:
        status = {"installed": "installed", "partial": "incomplete", "not_installed": "optional"}[item["status"]]
        table.add_row(item["label"], status, item["extra"])
    console.print(table)
    console.print("\n[bold]Install only what you need[/bold]")
    if not report["virtual_environment"]:
        console.print("Create a dedicated virtual environment first (see docs/getting-started.md).")
    else:
        # The checkout is explicit: the current distribution name is not a published h/g release.
        console.print("From your h/g source checkout, for example:")
        argv = next(item["install_argv"] for item in report["capabilities"] if item["extra"] == "semantic")
        command = ("& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
                   if os.name == "nt" else shlex.join(argv))
        console.print(command, markup=False, highlight=False, soft_wrap=True)
    console.print("\nPackage presence does not check models or services. Nothing was downloaded.", style="dim")
    return 0
