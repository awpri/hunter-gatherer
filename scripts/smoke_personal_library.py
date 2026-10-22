"""Exercise the installed personal-library CLI, warm server and verified restore.

All documents are generated fictional fixtures in a temporary directory. No
ambient configuration or existing library is opened. The JSON report contains
check results, package versions and fixture hashes, never local paths or records.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


def payload(response):
    assert not response.is_error, "MCP returned a tool error"
    assert isinstance(response.structured_content, dict), "Missing structured MCP result"
    return response.structured_content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-lean", action="store_true")
    args = parser.parse_args()
    import hunter_gatherer

    package = Path(hunter_gatherer.__file__).resolve()
    assert package.is_relative_to(Path(sysconfig.get_path("purelib")).resolve()), "Run with an installed wheel outside the source checkout"
    installed = {dist.metadata["Name"].lower().replace("_", "-") for dist in importlib.metadata.distributions()}
    if args.require_lean:
        assert not installed & {"torch", "sentence-transformers", "transformers", "qdrant-client", "docling", "faster-whisper"}, "Heavyweight dependencies found in lean environment"
    hunt = Path(sysconfig.get_path("scripts")) / ("hunt.exe" if os.name == "nt" else "hunt")
    assert hunt.is_file(), "Installed hunt command missing"
    record = {"schema_version": 1, "python": platform.python_version(), "platform": platform.system(),
              "version": importlib.metadata.version("hunter-gatherer"), "installed_package_verified": True,
              "lean_install_verified": args.require_lean, "server_checks": []}
    with tempfile.TemporaryDirectory(prefix="hg-personal-smoke-") as temporary:
        root = Path(temporary).resolve()
        library = root / "personal library"
        sentinel = root / "never-open-ambient.db"
        allowed = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        env.update(HG_SQLITE_PATH=str(sentinel), HG_EMBED_MODEL="must-not-load",
                   PROHARNESS_SQLITE_PATH=str(sentinel), PROHARNESS_EMBED_MODEL="must-not-load",
                   PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")

        def run(arguments, *, success=True):
            response = subprocess.run([str(hunt), *arguments], cwd=root, env=env, capture_output=True,
                                      text=True, encoding="utf-8", timeout=90)
            assert (response.returncode == 0) == success, response.stderr
            return response

        def cli_json(arguments):
            response = run(arguments)
            assert "\x1b" not in response.stdout, "Terminal escapes polluted JSON"
            return json.loads(response.stdout)

        manifest = cli_json(["init", str(library), "--json"])
        assert manifest["search_mode"] == "keyword" and manifest["embedding"] == "hashing:384"
        empty = cli_json(["ask", "What time does the fictional observatory open?", "--library", str(library), "--json"])
        assert not any(page["supporting_passages"] or page["relevant_memories"] for page in empty["pages"])
        run(["init", str(library), "--json"], success=False)
        record["empty_library_and_no_overwrite"] = True
        if "sentence-transformers" not in installed:
            missing_model = root / "missing semantic model"
            failed = run(["init", str(missing_model), "--semantic", "--json"], success=False)
            assert json.loads(failed.stderr)["error"]["type"] == "OptionalDependencyError"
            assert not missing_model.exists(), "Missing optional model left a partial library"
            record["semantic_missing_dependency_no_partial_directory"] = True
        source = root / "fictional-observatory.md"
        exact = "# Fictional observatory schedule\n\nThe fictional observatory opens on Friday at 21:30 UTC.\nWeather closures may cancel observing.\n"
        source.write_bytes(exact.encode("utf-8"))
        imported = cli_json(["import", str(source), "--library", str(library), "--json"])
        assert imported["counts"]["succeeded"] == 1 and imported["complete"]
        question = "When does the fictional observatory open?"
        asked = cli_json(["ask", question, "--library", str(library), "--json"])
        passage = next(item for page in asked["pages"] for item in page["supporting_passages"] if "Friday at 21:30 UTC" in item["quote"])
        citation = passage["citation"]
        opened = cli_json(["open", citation, "--library", str(library), "--json"])
        assert passage["quote"] in [item["quote"] for item in opened["result"]["sources"]]
        human = run(["ask", question, "--library", str(library)]).stdout
        assert "Friday at 21:30 UTC" in human and citation in human
        record["import_cli_exact_ask_open_and_human_output"] = True
        record["fixture_sha256"] = hashlib.sha256(exact.encode("utf-8")).hexdigest()

        async def check_mcp(url):
            async with httpx2.AsyncClient(trust_env=False, timeout=20) as http:
                async with streamable_http_client(url + "/mcp", http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as client:
                        await client.initialize()
                        names = {tool.name for tool in (await client.list_tools()).tools}
                        assert names == {"hg_find", "hg_open", "hg_memory", "hg_collection", "hg_import_documents", "hg_status"}
                        status = payload(await client.call_tool("hg_status", {}))
                        assert Path(status["store"]["database"]).resolve() == library / "library.db"
                        found = payload(await client.call_tool("hg_find", {"query": question}))
                        assert any(item["citation"] == citation for item in found["supporting_passages"])
                        opened = payload(await client.call_tool("hg_open", {"refs": [citation]}))
                        assert opened["statuses"] == ["available"] and passage["quote"] == opened["sources"][0]["quote"]
                        return {"default_mcp_tools": sorted(names), "exact_citation_persisted": True}

        for startup in (1, 2, 3):
            if startup == 3:
                bundle = root / "verified backup"
                report = cli_json(["backup", "create", str(bundle), "--library", str(library)])
                assert report["ok"] and report["documents"] == 1
                assert cli_json(["backup", "verify", str(bundle)])["ok"]
                source.unlink()
                shutil.rmtree(library)
                library = root / "restored library"
                restored = cli_json(["init", str(library), "--from-backup", str(bundle), "--json"])
                assert restored["library_id"] == manifest["library_id"]
                reopened = cli_json(["open", citation, "--library", str(library), "--json"])
                assert passage["quote"] in [item["quote"] for item in reopened["result"]["sources"]]
                originals = list((library / "library.db.archive" / "originals").glob("*/*"))
                assert len(originals) == 1 and originals[0].read_bytes() == exact.encode("utf-8")
                record["backup_verified_restored_with_original_unavailable"] = True
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            logfile = root / f"server-{startup}.log"
            with logfile.open("w", encoding="utf-8") as log:
                process = subprocess.Popen([str(hunt), "serve", str(library), "--port", str(port)], cwd=root,
                                           env=env, stdout=log, stderr=log)
                try:
                    with httpx2.Client(trust_env=False, timeout=2) as http:
                        deadline = time.monotonic() + 60
                        while time.monotonic() < deadline:
                            assert process.poll() is None, "Library server exited before readiness"
                            try:
                                if http.get(url + "/readyz").json()["ready"]:
                                    break
                            except (httpx2.HTTPError, ValueError, KeyError):
                                pass
                            time.sleep(0.25)
                        else:
                            raise TimeoutError("Library startup exceeded 60 seconds")
                        ui = http.get(url + "/")
                        assert ui.status_code == 200 and "/assets/" in ui.text
                        assert http.get(url + "/healthz").json()["status"] == "ok"
                        assert http.get(url + "/api/config").status_code == 404, "Inherited settings surface exposed"
                    warm = cli_json(["ask", question, "--server", url, "--json"])
                    assert any(item["citation"] == citation for page in warm["pages"] for item in page["supporting_passages"])
                    warm_opened = cli_json(["open", citation, "--server", url, "--json"])
                    assert passage["quote"] in [item["quote"] for item in warm_opened["result"]["sources"]]
                    checked = asyncio.run(asyncio.wait_for(check_mcp(url), 60))
                    record["server_checks"].append({"startup": startup, "browser_ready": True,
                                                     "warm_cli_ask_open": True, "restored": startup == 3, **checked})
                except BaseException:
                    log.flush()
                    print(logfile.read_text(encoding="utf-8"), file=sys.stderr)
                    raise
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    assert process.poll() is not None
        assert not sentinel.exists(), "Ambient library was accessed"
    record.update(no_ambient_library_created=True, temporary_servers_stopped=True)
    serialized = json.dumps(record, indent=2) + "\n"
    if args.output:
        args.output.resolve().write_text(serialized, encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
