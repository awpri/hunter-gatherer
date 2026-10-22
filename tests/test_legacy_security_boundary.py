"""Synthetic HTTP/WS admission and static traversal regressions; no live harness."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hunter_gatherer.config import Settings
from hunter_gatherer.server.app import build_app
from hunter_gatherer.server.auth import SingleTokenMiddleware


def settings(**kwargs):
    return Settings(_env_file=None, ui_mount_mcp=False, **kwargs)


def boundary(value, *, client=('127.0.0.1', 50000), base_url='http://127.0.0.1:8765'):
    app = FastAPI()
    app.add_middleware(SingleTokenMiddleware, settings=value)
    @app.get('/')
    def hello():
        return {'ok': True}
    @app.websocket('/ws')
    async def socket(ws: WebSocket):
        await ws.accept()
        await ws.send_json({'ok': True})
        await ws.close()
    return TestClient(app, client=client, base_url=base_url)


@pytest.mark.parametrize('origin', ['http://attacker.invalid', 'null', 'http://127.0.0.1:8766', 'https://127.0.0.1:8765', 'http://127.0.0.1:8765/'])
def test_cross_origin_http_and_ws_denied(origin):
    client = boundary(settings())
    result = client.get('/', headers={'origin': origin})
    assert result.status_code == 403
    assert 'access-control-allow-origin' not in result.headers
    with pytest.raises(WebSocketDisconnect) as error, client.websocket_connect(
        'ws://127.0.0.1:8765/ws', headers={'origin': origin}
    ):
        pass
    assert error.value.code == 1008


def test_same_origin_and_native_clients_work():
    client = boundary(settings())
    assert client.get('/').status_code == 200
    assert client.get('/', headers={'origin':'http://127.0.0.1:8765'}).status_code == 200
    with client.websocket_connect('ws://127.0.0.1:8765/ws', headers={'origin':'http://127.0.0.1:8765'}) as ws:
        assert ws.receive_json() == {'ok': True}


def test_untrusted_host_and_unknown_peer_fail_closed():
    client = boundary(settings())
    assert client.get('/', headers={'host':'attacker.invalid:8765'}).status_code == 403
    assert client.get('/', headers={'host':'127.0.0.1:8766'}).status_code == 403
    assert boundary(settings(), client=('192.0.2.42', 50000)).get('/').status_code == 403


def test_lan_requires_token_for_http_and_ws_including_loopback():
    value = settings(ui_lan_mode=True, ui_token='fictional-token')
    for peer in ['127.0.0.1', '192.0.2.42']:
        client = boundary(value, client=(peer, 50000))
        assert client.get('/').status_code == 401
        assert client.get('/?token=wrong').status_code == 401
        assert client.get('/', headers={'authorization':'Bearer fictional-token'}).status_code == 200
        with pytest.raises(WebSocketDisconnect), client.websocket_connect('ws://127.0.0.1:8765/ws'):
            pass
        with client.websocket_connect('ws://127.0.0.1:8765/ws?token=fictional-token', headers={'origin':'http://127.0.0.1:8765'}) as ws:
            assert ws.receive_json()['ok']
        assert client.get('/?token=fictional-token', headers={'origin':'http://attacker.invalid'}).status_code == 403


def test_lifespan_generated_token_is_read_live():
    value = settings(ui_lan_mode=True, ui_token='')
    client = boundary(value)
    assert client.get('/').status_code == 503
    value.ui_token = 'fictional-generated-token'
    assert client.get('/?token=fictional-generated-token').status_code == 200


def test_explicit_bind_and_custom_port():
    value = settings(ui_host='192.0.2.10', ui_port=9001, ui_lan_mode=True, ui_token='fictional-token')
    client = boundary(value, client=('192.0.2.42', 50000), base_url='http://192.0.2.10:9001')
    assert client.get('/?token=fictional-token', headers={'origin':'http://192.0.2.10:9001'}).status_code == 200


def test_settings_redact_secrets_and_security_is_startup_only():
    app = build_app(settings(openai_api_key='fictional-provider-key', ui_token='fictional-token', mcp_servers='fictional-mcp-secret'))
    client = TestClient(app)
    response = client.get('/api/settings')
    assert response.status_code == 200
    for secret in ['fictional-provider-key', 'fictional-token', 'fictional-mcp-secret']:
        assert secret not in response.text
    assert response.json()['startup_only']['openai_api_key'] == '[configured]'
    assert client.patch('/api/settings', json={'ui_lan_mode':False}).status_code == 409


def test_static_fallback_confines_traversal_absolute_paths_and_symlinks(tmp_path):
    root = tmp_path / 'static'
    root.mkdir()
    (root / 'assets').mkdir()
    (root / 'index.html').write_text('fictional-index')
    (root / 'safe.txt').write_text('fictional-public')
    secret = tmp_path / 'fictional-private.txt'
    secret.write_text('fictional-private-content')
    (root / 'link.txt').symlink_to(secret)
    app = build_app(settings(), static_dir=root)
    client = TestClient(app)
    for path in ['/..%2ffictional-private.txt', '/%2f' + str(secret).lstrip('/'), '/link.txt']:
        response = client.get(path)
        assert response.status_code == 404
        assert 'fictional-private-content' not in response.text
    assert client.get('/safe.txt').text == 'fictional-public'
    assert client.get('/spa-route').text == 'fictional-index'


def test_cli_effective_bind_matches_boundary(monkeypatch):
    import uvicorn

    import hunter_gatherer.server.app as app_module
    from hunter_gatherer import cli
    value = settings()
    seen = {}
    def build(effective):
        seen.update(host=effective.ui_host, port=effective.ui_port)
        return object()
    def run(app, **kwargs):
        seen.update(kwargs)
    monkeypatch.setattr(app_module, 'build_app', build)
    monkeypatch.setattr(uvicorn, 'run', run)
    cli.cmd_serve(SimpleNamespace(host='127.0.0.2', port=9002, verbose=False), value)
    assert seen['host'] == '127.0.0.2' and seen['port'] == 9002
    assert seen['proxy_headers'] is False


def test_lan_navigation_cookie_supports_assets_and_websocket():
    client = boundary(settings(ui_lan_mode=True, ui_token='fictional-token'))
    response = client.get('/?token=fictional-token')
    assert response.status_code == 200
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=strict' in response.headers['set-cookie']
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert client.get('/').status_code == 200
    with client.websocket_connect('ws://127.0.0.1:8765/ws', headers={'origin':'http://127.0.0.1:8765'}) as ws:
        assert ws.receive_json()['ok']


def test_lifespan_token_generation_reaches_existing_middleware(monkeypatch, tmp_path):
    import hunter_gatherer.loop
    import hunter_gatherer.pipelines.scheduler
    value = settings(ui_lan_mode=True, ui_token='', sqlite_path=str(tmp_path / 'unused.db'))
    fake = SimpleNamespace(store=SimpleNamespace(count=lambda:0), close=lambda:None)
    monkeypatch.setattr(hunter_gatherer.loop, 'AgentLoop', lambda settings:fake)
    monkeypatch.setattr(hunter_gatherer.pipelines.scheduler, 'PipelineScheduler',
                        lambda loop:SimpleNamespace(start=lambda:None, stop=lambda:None))
    app = build_app(value)
    with TestClient(app) as client:
        assert value.ui_token
        assert client.get('/healthz').status_code == 401
        assert client.get('/healthz', headers={'authorization':'Bearer ' + value.ui_token}).status_code == 200


def test_static_index_symlink_is_not_served(tmp_path):
    root = tmp_path / 'static'
    root.mkdir()
    (root / 'assets').mkdir()
    secret = tmp_path / 'fictional-private.txt'
    secret.write_text('fictional-private-content')
    (root / 'index.html').symlink_to(secret)
    response = TestClient(build_app(settings(), static_dir=root)).get('/spa-route')
    assert 'fictional-private-content' not in response.text
