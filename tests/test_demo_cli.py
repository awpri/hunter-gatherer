"""Demo entry points ignore user config and remain offline across restart."""
import asyncio
import json
import socket

import pytest

from proharness import cli
from proharness.config import Settings
from proharness.memory.embeddings import HashingEmbedder, get_embedder


def test_explicit_hashing_never_probes_model_packages(monkeypatch):
    monkeypatch.setattr('proharness.memory.embeddings._st_available', lambda: pytest.fail('model dependency probe'))
    embedder = get_embedder(Settings(_env_file=None, embed_model='hashing'))
    assert isinstance(embedder, HashingEmbedder) and embedder.fingerprint() == 'hashing:384'
    assert embedder.embed('Lantern review') == embedder.embed('Lantern review')


@pytest.fixture
def offline_loop():
    # Windows asyncio creates its local wake-up socket pair when the loop is
    # initialized. Create that OS mechanism before denying all application IO.
    with asyncio.Runner() as runner:
        runner.get_loop()
        yield runner


def test_cli_demo_create_and_serve_ignore_ambient_store_and_network(tmp_path, monkeypatch, capsys, offline_loop):
    target = tmp_path / 'offline demo'
    real = tmp_path / 'real.db'
    real.write_bytes(b'untouched existing user data')
    monkeypatch.setenv('PROHARNESS_SQLITE_PATH', str(real))
    monkeypatch.setenv('PROHARNESS_EMBED_MODEL', 'not-a-model')
    monkeypatch.setattr(cli, 'get_settings', lambda: pytest.fail('ambient settings'))
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('outbound network'))
    assert cli.main(['demo', 'create', str(target)]) == 0
    assert json.loads(capsys.readouterr().out)['fictional']
    assert cli.main(['demo', 'create', str(target)]) == 1
    running = []
    def inspect_app(app, **options):
        owner = app.state.library_harness
        assert owner.settings.sqlite_path == str(target / 'demo.db')
        assert owner.settings.local_only and owner.settings.embed_model == 'hashing'
        assert isinstance(owner.loop.embedder, HashingEmbedder)
        assert options == {'host': '127.0.0.1', 'port': 8767, 'proxy_headers': False}
        from proharness.mcp.profiles import LIBRARY_TOOL_NAMES
        from proharness.mcp.server import build_server
        server = build_server(harness=owner, tool_profile='library')[0]
        assert {t.name for t in offline_loop.run(server.list_tools())} == set(LIBRARY_TOOL_NAMES)
        response = offline_loop.run(server.call_tool('proharness_find', {'query': 'Lantern weekly review schedule'}))
        assert not response.is_error
        assert 'Tuesday' in str(response.structured_content)
        running.append(owner)
    monkeypatch.setattr('uvicorn.run', inspect_app)
    assert cli.main(['demo', 'serve', str(target)]) == 0
    assert cli.main(['demo', 'serve', str(target)]) == 0
    assert len(running) == 2
    assert real.read_bytes() == b'untouched existing user data'
    import sqlite3

    for owner in running:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            owner.store._sqlite.execute('SELECT 1')


def test_demo_cli_port_validation_and_missing_target():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(['demo', 'serve', 'target', '--port', '0'])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(['demo', 'create'])
