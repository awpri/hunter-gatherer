"""Exercise an installed wheel using only an isolated fictional demo (no model weights).

Requires the document-view extra. Run with an installed environment's Python from outside the checkout. The optional
--output retains JSON evidence; temporary demo files and server logs are removed.
"""

import argparse
import base64
import hashlib
from io import BytesIO
import asyncio
import importlib.metadata
import json
import os
import platform
import re
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client


def check_packaged_assets(http, base_url):
    """Follow local HTML assets and CSS font references, with a finite cap."""
    html = http.get(base_url + "/")
    html.raise_for_status()
    pending = re.findall(r'(?:src|href)=["\']([^"\']+)["\']', html.text)
    assert any("/assets/" in item and item.endswith(".js") for item in pending)
    visited = set()
    fonts = 0
    while pending:
        reference = pending.pop()
        if reference.startswith("data:"):
            continue
        url = urljoin(base_url + "/", reference)
        assert urlsplit(url).netloc == urlsplit(base_url).netloc, "External packaged asset"
        if url in visited:
            continue
        visited.add(url)
        assert len(visited) <= 64, "Packaged asset traversal exceeded bound"
        response = http.get(url)
        response.raise_for_status()
        path = urlsplit(url).path
        if path.endswith(".css"):
            pending.extend(
                urljoin(url, value.strip("\"' "))
                for value in re.findall(r"url\(([^)]+)\)", response.text)
            )
        elif path.endswith(".woff2"):
            assert response.content.startswith(b"wOF2"), "Font route did not return WOFF2"
            fonts += 1
    assert fonts >= 2, "Both bundled font families must resolve through CSS"
    for notice in ("/fonts/OFL.txt", "/licenses/Lucide-LICENSE.txt"):
        response = http.get(base_url + notice)
        response.raise_for_status()
        assert "license" in response.text.casefold() and "<html" not in response.text.casefold()
    return {"local_assets": len(visited), "woff2_fonts": fonts, "license_routes": 2}



def tool_payload(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(part.text for part in result.content if part.type == "text"))


async def check_original_region(client, root, citation=None):
    """Exercise original pixels through public tools with a fictional PDF only."""
    from PIL import Image
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    if citation is None:
        writer = PdfWriter()
        page = writer.add_blank_page(width=240, height=320)
        drawing = DecodedStreamObject()
        drawing.set_data(b"1 0 0 rg 0 0 120 320 re f 0 0 1 rg 120 0 120 320 re f")
        page[NameObject("/Contents")] = writer._add_object(drawing)
        data = BytesIO()
        writer.write(data)
        original = data.getvalue()
        path = root / "fictional-color-blocks.pdf"
        path.write_bytes(original)
        planned = tool_payload(await client.call_tool("proharness_import_documents", {
            "action": "create", "paths": [str(path)], "idempotency_key": "fictional-pdf",
        }))
        imported = tool_payload(await client.call_tool("proharness_import_documents", {
            "action": "run", "job_id": planned["job_id"],
        }))
        assert imported["complete"] and imported["counts"]["succeeded"] == 1, imported
        document_id = imported["items"][0]["document_id"]
        attached = await client.call_tool("proharness_import_documents", {
            "action": "enrich", "idempotency_key": "fictional-region",
            "enrichment": {"document_id": document_id, "expected_version": 1,
                "expected_content_hash": hashlib.sha256(original).hexdigest(),
                "producer": {"id": "fictional-release-smoke"}, "records": [
                    {"key": "left-block", "kind": "visual_region", "text": "crimson cobalt inspection blocks",
                     "anchors": [], "region": {"page": 1, "bbox": [0, 0, 0.5, 0.5]}}]},
        })
        assert not attached.is_error, attached.content
        found = tool_payload(await client.call_tool("proharness_find", {
            "query": "crimson cobalt inspection blocks", "max_tokens": 4096,
        }))
        item = found["visual_regions"][0]
        assert item["verification"] == "unverified" and "quote" not in item
        citation = item["citation"]
    images = {}
    hashes = {}
    for view in ("page", "region"):
        result = await client.call_tool("proharness_open", {"refs": [citation], "view": view})
        assert not result.is_error, result.content
        metadata = result.structured_content
        assert metadata["citation"] == citation and metadata["verification"] == "unverified"
        assert "_image_png" not in metadata
        image = next(part for part in result.content if part.type == "image")
        assert image.mime_type == "image/png"
        pixels = base64.b64decode(image.data)
        hashes[view] = hashlib.sha256(pixels).hexdigest()
        images[view] = Image.open(BytesIO(pixels)).convert("RGB")
    whole, crop = images["page"], images["region"]
    expected = whole.crop((0, 0, (whole.width + 1) // 2, (whole.height + 1) // 2))
    assert crop.size == expected.size and crop.tobytes() == expected.tobytes()
    return citation, {"native_page_image": True, "native_region_image": True,
                      "crop_matches_original_pixels": True, "page_size": whole.size,
                      "crop_size": crop.size, "png_sha256": hashes,
                      "annotation_verification": "unverified"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-lean", action="store_true", help="assert heavyweight optional packages are absent")
    parser.add_argument(
        "--allow-source",
        action="store_true",
        help="Development check only; allow editable/source import",
    )
    args = parser.parse_args()
    import proharness

    imported = Path(proharness.__file__).resolve()
    package_root = Path(sysconfig.get_path("purelib")).resolve()
    assert args.allow_source or imported.is_relative_to(package_root), (
        f"Source checkout imported: {imported}"
    )
    record = {
        "python": sys.version,
        "platform": platform.platform(),
        "import_path": str(imported),
        "package_count": len(list(importlib.metadata.distributions())),
        "installed_package_path_verified": imported.is_relative_to(package_root),
        "checks": [],
    }
    if args.require_lean:
        installed = {dist.metadata["Name"].lower().replace("_", "-") for dist in importlib.metadata.distributions()}
        unwanted = installed & {"torch", "sentence-transformers", "transformers", "qdrant-client", "openai", "anthropic", "docling", "faster-whisper"}
        assert not unwanted, f"Unexpected heavyweight dependencies: {sorted(unwanted)}"
        record["lean_install_verified"] = True
    with tempfile.TemporaryDirectory(prefix="proharness-smoke-") as temporary:
        root = Path(temporary).resolve()
        target = root / "fictional-demo"
        sentinel = root / "never-create-real.db"
        # Keep Windows process/DLL and temporary-directory essentials, stripping providers,
        # PYTHONPATH, .env settings and all ambient ProHarness configuration.
        allowed = {
            "PATH",
            "HOME",
            "USERPROFILE",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        env.update(
            PROHARNESS_SQLITE_PATH=str(sentinel),
            PROHARNESS_EMBED_MODEL="must-not-load",
            PYTHONNOUSERSITE="1",
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
        )
        command = [sys.executable, "-I", "-B", "-m", "proharness.cli"]
        created = subprocess.run(
            command + ["demo", "create", str(target)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        if created.returncode:
            print(created.stdout, file=sys.stderr)
            print(created.stderr, file=sys.stderr)
            created.check_returncode()
        assert json.loads(created.stdout)["fictional"] is True
        assert json.loads((target / "manifest.json").read_text())["fictional"] is True
        # Exercise the actual packaged entry point, with no web server needed.
        hunt = Path(sysconfig.get_path("scripts")) / ("hunt.exe" if os.name == "nt" else "hunt")
        assert hunt.is_file(), "The hunt console entry point is missing"
        def cli_json(arguments):
            response = subprocess.run([str(hunt), *arguments], cwd=root, env=env,
                                      capture_output=True, text=True, encoding="utf-8", timeout=60)
            assert response.returncode == 0, response.stderr
            assert "\x1b" not in response.stdout, "ANSI polluted JSON output"
            return json.loads(response.stdout)
        assert cli_json(["setup", "--json"])["kind"] == "setup"
        asked = cli_json(["ask", "What is the Lantern pilot's weekly review schedule?",
                          "--demo-library", str(target), "--json"])
        assert asked["schema_version"] == 1 and asked["kind"] == "ask"
        passage = next(item for page in asked["pages"] for item in page["supporting_passages"] if "Tuesday" in item["quote"])
        opened = cli_json(["open", passage["citation"], "--demo-library", str(target), "--json"])
        assert passage["quote"] in [item["quote"] for item in opened["result"]["sources"]]
        record["terminal_ask_open_setup"] = True
        collection_id = None
        region_citation = None

        async def exercise(url, number):
            nonlocal collection_id, region_citation
            async with httpx2.AsyncClient(trust_env=False, timeout=20) as http:  # noqa: SIM117
                async with streamable_http_client(url + "/mcp", http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as client:
                        await client.initialize()
                        names = {tool.name for tool in (await client.list_tools()).tools}
                        assert names == {
                            "proharness_status", "proharness_find", "proharness_open",
                            "proharness_collection", "proharness_memory", "proharness_import_documents",
                        }, names
                        if number == 1:
                            remembered = await client.call_tool("proharness_memory", {
                                "action": "remember", "text": "The fictional Lantern demo uses a local evidence library.",
                                "source": "fictional release smoke",
                            })
                            assert not remembered.is_error, remembered.content
                            imported_path = root / "fictional-smoke-import.md"
                            imported_path.write_text("# Fictional smoke source\nThe demo inspection marker is amber-lantern.\n")
                            planned = tool_payload(await client.call_tool("proharness_import_documents", {
                                "action": "create", "paths": [str(imported_path)],
                                "idempotency_key": "fictional-smoke-import",
                            }))
                            imported = tool_payload(await client.call_tool("proharness_import_documents", {
                                "action": "run", "job_id": planned["job_id"],
                            }))
                            assert imported["complete"] and imported["counts"]["succeeded"] == 1, imported
                        status = (
                            await client.call_tool("proharness_status", {})
                        ).structured_content
                        assert Path(status["store"]["database"]).resolve() == target / "demo.db"
                        found = (
                            await client.call_tool(
                                "proharness_find", {"query": "Lantern pilot weekly review schedule"}
                            )
                        ).structured_content
                        passage = next(
                            p for p in found["supporting_passages"] if "Tuesday" in p["quote"]
                        )
                        opened = (
                            await client.call_tool(
                                "proharness_open", {"refs": [passage["citation"]]}
                            )
                        ).structured_content
                        assert opened["statuses"] == ["available"]
                        assert "Tuesday" in opened["sources"][0]["quote"]
                        if number == 1:
                            saved = (
                                await client.call_tool(
                                    "proharness_collection",
                                    {
                                        "action": "save",
                                        "collection": {
                                            "name": "Fictional smoke evidence",
                                            "references": [
                                                {
                                                    "document_id": passage["document_id"],
                                                    "chunk_id": passage["chunk_id"],
                                                }
                                            ],
                                        },
                                        "idempotency_key": "portable-smoke",
                                    },
                                )
                            ).structured_content
                            collection_id = saved["id"]
                        else:
                            exported = (
                                await client.call_tool(
                                    "proharness_collection",
                                    {"action": "export", "collection_id": collection_id},
                                )
                            ).structured_content
                            assert "Tuesday" in json.dumps(exported)
                        region_citation, visual_report = await check_original_region(client, root, region_citation)
                        return {
                            "startup": number,
                            "original_region": visual_report,
                            "tools": len(names),
                            "all_six_tools_invoked": number == 1,
                            "find_open": True,
                            "collection_saved" if number == 1 else "collection_persisted": True,
                        }

        for number in (1, 2, 3):
            if number == 3:
                # Exercise the installed recovery code after both earlier servers
                # have stopped; make the original store unavailable before Open.
                from proharness.backup import create_backup, restore_backup, verify_backup

                original = target
                bundle = root / "verified-backup"
                assert create_backup(original / "demo.db", bundle)["originals"] >= 3
                assert verify_backup(bundle)["ok"]
                target = root / "restored-demo"
                target.mkdir()
                assert restore_backup(bundle, target / "demo.db")["restored"]
                manifest = json.loads((original / "manifest.json").read_text())
                manifest["database"] = str(target / "demo.db")
                (target / "manifest.json").write_text(json.dumps(manifest))
                original.rename(root / "original-store-unavailable")
                record["backup_verified_and_restored"] = True
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            with (root / f"server-{number}.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command + ["demo", "serve", str(target), "--port", str(port)],
                    cwd=root,
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                try:
                    with httpx2.Client(trust_env=False, timeout=2) as http:
                        deadline = time.monotonic() + 60
                        while time.monotonic() < deadline:
                            if process.poll() is not None:
                                raise RuntimeError("Demo server exited")
                            try:
                                if http.get(url + "/readyz").json()["ready"]:
                                    break
                            except (httpx2.HTTPError, ValueError, KeyError):
                                pass
                            time.sleep(0.25)
                        else:
                            raise TimeoutError("Demo readiness exceeded 60 seconds")
                        asset_report = check_packaged_assets(http, url)
                    if number == 1:
                        warm = cli_json(["ask", "Is the Lantern pilot real?", "--server", url, "--json"])
                        warm_passage = next(item for page in warm["pages"] for item in page["supporting_passages"]
                                            if "Fictional source." in item["quote"])
                        warm_opened = cli_json(["open", warm_passage["citation"], "--server", url, "--json"])
                        assert warm_passage["quote"] in [item["quote"] for item in warm_opened["result"]["sources"]]
                        record["terminal_warm_server"] = True
                    record["checks"].append(
                        {**asyncio.run(asyncio.wait_for(exercise(url, number), 60)),
                         "assets": asset_report, "restored_store": number == 3}
                    )
                except BaseException:
                    log.flush()
                    print((root / f"server-{number}.log").read_text(), file=sys.stderr)
                    raise
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    assert process.poll() is not None
        assert not sentinel.exists(), "Ambient store was accessed"
    record.update(no_ambient_store_created=True, temporary_servers_stopped=True)
    serialized = json.dumps(record, indent=2) + "\n"
    if args.output:
        args.output.resolve().write_text(serialized, encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
