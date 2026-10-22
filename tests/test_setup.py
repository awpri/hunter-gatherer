"""Setup is a read-only metadata check, safe without a library or optional runtime."""
import importlib.metadata
import json
import socket
import sys

from hunter_gatherer import cli
from hunter_gatherer.setup import setup_report


def test_setup_json_does_not_load_configuration_or_connect(monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: (_ for _ in ()).throw(AssertionError("configuration loaded")))
    monkeypatch.setattr(socket.socket, "connect", lambda *args: (_ for _ in ()).throw(AssertionError("network used")))
    before = set(sys.modules)
    assert cli.main(["setup", "--json"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert captured.err == ""
    assert report["kind"] == "setup" and report["schema_version"] == 1
    assert not ({"torch", "sentence_transformers", "faster_whisper", "docling"} & (set(sys.modules) - before))
    assert {entry["extra"] for entry in report["capabilities"]} >= {"semantic", "voice", "qdrant", "document-view"}


def test_partial_package_install_is_not_reported_as_installed(monkeypatch):
    def version(name):
        if name == "pypdfium2":
            return "5.14.0"
        raise importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(importlib.metadata, "version", version)
    report = setup_report()
    render = next(item for item in report["capabilities"] if item["extra"] == "document-view")
    assert render["status"] == "partial"
    assert render["missing_packages"] == ["Pillow"]
    assert render["install_argv"][-1] == ".[document-view]"
    assert report["install_working_directory"] == "hunter/gatherer source checkout"
