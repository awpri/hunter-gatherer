"""The terminal is another exact-evidence client, not an answer generator."""
from __future__ import annotations

import io
import json
import socket
import sqlite3
from pathlib import Path

import pytest
from rich.console import Console

from proharness import cli
from proharness.demo import create_demo, demo_settings
from proharness.memory.documents import DocumentArchive
from proharness.memory.schemas import Memory
from proharness.memory.store import MemoryStore
from proharness.terminal import passage_excerpt, render_ask, render_open, safe_text


@pytest.fixture
def library(tmp_path):
    directory = tmp_path / "fictional library"
    manifest = create_demo(directory)
    return directory, manifest


def ask(directory, query, *options):
    return cli.main(["ask", query, "--demo-library", str(directory), *options])


def test_demo_ask_json_is_exact_offline_and_ignores_user_config(library, monkeypatch, capsys):
    directory, manifest = library
    monkeypatch.setattr(cli, "get_settings", lambda: pytest.fail("personal configuration loaded"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("outbound connection"))
    monkeypatch.setattr("proharness.loop.AgentLoop", lambda *a, **k: pytest.fail("agent initialized"))
    assert ask(directory, "is the lantern pilot real or no", "--json") == 0
    output = capsys.readouterr()
    assert output.err == ""
    response = json.loads(output.out)
    assert response["schema_version"] == 1 and response["kind"] == "ask"
    assert response["has_more"] is False
    assert len(response["pages"]) == 1
    packet = response["pages"][0]
    assert packet["continuation"]["next_cursor"] is None
    assert packet["bounded_format"]["contract"]
    assert packet["projection"] and packet["retrieval_assessment"]
    assert len(packet["supporting_passages"]) == 2
    for passage in packet["supporting_passages"]:
        expected = next(item for item in manifest["documents"] if item["document_id"] == passage["document_id"])
        assert passage["quote"] == Path(expected["input"]).read_text(encoding="utf-8")
        assert "Fictional source." in passage["quote"]
        assert passage["citation"].startswith("[doc:")
    assert "\x1b" not in output.out


def test_terminal_focuses_authenticity_and_schedule_without_rewriting(library, capsys):
    directory, _ = library
    assert ask(directory, "is the lantern pilot real or no") == 0
    output = capsys.readouterr().out
    assert "Fictional source." in output and "all named people are invented." in output
    assert "Correction to version 1" not in output
    assert "Source quotes, not a generated answer." in output
    assert "[doc:" in output and "\x1b" not in output
    assert ask(directory, "What is the Lantern pilot's weekly review schedule?") == 0
    output = capsys.readouterr().out
    assert "Tuesday at 10:00 UTC." in output


def test_excerpt_is_contiguous_and_retains_negation_context():
    quote = ("# Example lab report\n\nThis is not a fictional source. "
             "The workshop uses real records, anonymized for participants.\n\n"
             "Other projects use a sample dataset with five entries. "
             "The temperature sensor streams real-time measurements every second.")
    excerpt = passage_excerpt(quote, "Is the lab report fictional?")
    assert excerpt.text in quote
    assert "not a fictional source" in excerpt.text
    assert "uses real records" in excerpt.text
    focused = excerpt.text[excerpt.focus_start:excerpt.focus_end]
    assert focused == "This is not a fictional source."
    realtime = passage_excerpt(quote, "How often does the temperature sensor stream real-time measurements?")
    assert "temperature sensor" in realtime.text[realtime.focus_start:realtime.focus_end]
    assert passage_excerpt(quote, "astronaut moonwalk").text == quote


def test_open_across_invocations_is_exact_and_needs_no_embedding(library, monkeypatch, capsys):
    directory, _ = library
    assert ask(directory, "weekly review schedule", "--json") == 0
    passage = json.loads(capsys.readouterr().out)["pages"][0]["supporting_passages"][0]
    monkeypatch.setattr("proharness.memory.store.MemoryStore", lambda *a, **k: pytest.fail("writable store initialized"))
    monkeypatch.setattr("proharness.memory.embeddings.get_embedder", lambda *a: pytest.fail("embedder initialized"))
    for view in ("text", "context", "outline"):
        assert cli.main(["open", passage["citation"], "--demo-library", str(directory), "--view", view, "--json"]) == 0
        response = json.loads(capsys.readouterr().out)
        assert response["kind"] == "open"
        body = response["result"]
        if view == "text":
            assert body["sources"][0]["quote"] == passage["quote"]
        elif view == "context":
            assert any(source["quote"] == passage["quote"] for source in body["chunks"])
        else:
            assert body["sections"]


def test_open_enforces_current_privacy_for_saved_memories(library, capsys):
    directory, _ = library
    with MemoryStore(demo_settings(directory)) as store:
        memory = store.add(Memory(text="Private launch password is only a fictional example.",
                                  metadata={"sensitive": True}))
    command = ["open", f"[mem:{memory.id}]", "--demo-library", str(directory), "--json"]
    assert cli.main(command) == 1
    denied = capsys.readouterr()
    assert json.loads(denied.out)["result"]["statuses"] == ["unavailable"]
    assert "launch password" not in denied.out
    assert cli.main(command + ["--include-sensitive"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["sources"][0]["text"] == memory.text


def test_json_escapes_controls_losslessly_and_human_output_is_literal(library, tmp_path, capsys):
    directory, _ = library
    malicious = "Terminal control sample [bold red]literal[/bold red] \x1b]52;c;CLIPBOARD\x07\r\u202e secret.\n"
    source = tmp_path / "terminal.txt"
    source.write_text(malicious, encoding="utf-8")
    with MemoryStore(demo_settings(directory)) as store:
        DocumentArchive(store).ingest(str(source), title="Terminal sample [bold red] \x1b]2;TITLE\x07")
    assert ask(directory, "terminal control sample", "--json") == 0
    raw = capsys.readouterr().out
    assert "\x1b" not in raw and "\u202e" not in raw
    result = json.loads(raw)
    passage = next(item for item in result["pages"][0]["supporting_passages"] if "CLIPBOARD" in item["quote"])
    assert "\x1b]52;c;CLIPBOARD\x07" in passage["quote"]
    stream = io.StringIO()
    render_ask(result, Console(file=stream, force_terminal=True, width=100, markup=False, highlight=False))
    human = stream.getvalue()
    assert "\x1b]52" not in human and "\x1b]2;TITLE" not in human and "\u202e" not in human
    assert "[bold red]literal[/bold red]" in human
    assert "\\u001b]52" in human and "Control characters are shown as escapes" in human
    assert safe_text("a\rb\b\u009b\u202e") == "a\\u000db\\u0008\\u009b\\u202e"


def test_missing_and_unrelated_databases_fail_without_creating_or_migrating(tmp_path, monkeypatch, capsys):
    from proharness.config import Settings

    missing = tmp_path / "absent" / "not-a-library.db"
    monkeypatch.setattr(cli, "get_settings", lambda: Settings.model_construct(sqlite_path=str(missing)))
    for extra in ([], ["--database", str(missing)], ["--demo-library", str(missing.parent)]):
        assert cli.main(["ask", "question", "--json", *extra]) == 1
        output = capsys.readouterr()
        assert output.out == "" and json.loads(output.err)["error"]
        assert not missing.parent.exists()
    unrelated = tmp_path / "unrelated.db"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    before = unrelated.read_bytes()
    assert cli.main(["ask", "question", "--database", str(unrelated), "--json"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "not an initialized evidence library" in output.err
    assert unrelated.read_bytes() == before

    lookalike = tmp_path / "unrelated-memory.db"
    with sqlite3.connect(lookalike) as connection:
        connection.execute("CREATE TABLE memories(id TEXT, text TEXT)")
    before = lookalike.read_bytes()
    assert cli.main(["ask", "question", "--database", str(lookalike), "--json"]) == 1
    assert "not an initialized evidence library" in capsys.readouterr().err
    assert lookalike.read_bytes() == before


def test_ask_rejects_mismatched_model_before_writable_store(library, monkeypatch, capsys):
    directory, manifest = library
    database = Path(manifest["database"])
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE store_meta SET value='st:different-model' WHERE key='embedder_fingerprint'")
    before = database.read_bytes()
    monkeypatch.setattr("proharness.memory.store.MemoryStore", lambda *a, **k: pytest.fail("writable store initialized"))
    assert ask(directory, "question", "--json") == 1
    output = capsys.readouterr()
    assert output.out == "" and "does not match this library" in json.loads(output.err)["error"]["message"]
    assert database.read_bytes() == before


def test_ask_consumes_continuations_before_exit_and_reports_remaining(library, monkeypatch, capsys):
    directory, _ = library
    calls = []
    def bounded_page(_harness, **kwargs):
        calls.append(kwargs)
        return {"query": kwargs["query"], "supporting_passages": [], "relevant_memories": [],
                "continuation": {"next_cursor": f"token-{len(calls)}", "scope": "test"}}
    monkeypatch.setattr("proharness.memory.simple_search.find", bounded_page)
    assert ask(directory, "question", "--pages", "2", "--json") == 0
    response = json.loads(capsys.readouterr().out)
    assert response["has_more"] is True and len(response["pages"]) == 2
    assert calls[0]["cursor"] is None and calls[1]["cursor"] == "token-1"
    assert all(page["continuation"]["next_cursor"] is None for page in response["pages"])
    assert "token-" not in json.dumps(response)


def test_empty_results_are_success_and_bad_options_are_usage_errors(library, capsys):
    directory, _ = library
    assert ask(directory, "zzzzzzastronautmoonwalk", "--json") == 0
    response = json.loads(capsys.readouterr().out)
    assert response["pages"][0]["supporting_passages"] == []
    assert response["pages"][0]["relevant_memories"] == []
    for arguments in (["ask", ""], ["ask", "question", "--pages", "21"],
                      ["ask", "question", "--max-tokens", "0"],
                      ["ask", "question", "--demo-library", "x", "--database", "y"],
                      ["open", "ref", "--offset", "-1"]):
        with pytest.raises(SystemExit) as error:
            cli.build_parser().parse_args(arguments)
        assert error.value.code == 2


def test_open_invalid_reference_reports_failure_without_leaking(library, capsys):
    directory, _ = library
    assert cli.main(["open", "not-a-citation", "--demo-library", str(directory), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["result"]["statuses"] == ["invalid"]


def test_human_source_navigation_retains_title_version_page_and_limitations():
    stream = io.StringIO()
    output = Console(file=stream, width=100, markup=False, highlight=False)
    render_open({"result": {
        "source_info": {"title": "Earlier lab report", "version": 1},
        "version": 1, "is_latest": False,
        "sections": [{"section": "Safety", "page": 4}],
        "next_offset": 20, "extraction_warnings_omitted": 1,
    }}, output)
    text = stream.getvalue()
    assert "Earlier lab report" in text and "Version 1 · Historical" in text
    assert "Page 4 · Safety" in text and "--offset 20" in text
    assert "incomplete warning details" in text
    stream.truncate(0)
    stream.seek(0)
    render_open({"result": {"source_info": {"title": "Lab report"},
                            "chunks": [{"quote": "Exact quote.", "citation": "[doc:example]"}],
                            "omitted_counts": {"output_budget": 1}}}, output)
    assert "Lab report" in stream.getvalue() and "Some context was omitted" in stream.getvalue()
