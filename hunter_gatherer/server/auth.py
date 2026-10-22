"""HTTP and WebSocket admission for the legacy workbench.

Same-origin loopback access is local. LAN mode requires a shared token from
all clients. Keep Settings live so lifespan-generated tokens take effect.
"""
from __future__ import annotations

import ipaddress
import secrets
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response


def is_loopback(client_host: str | None) -> bool:
    if client_host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return False


def _authority(value, default_port=80):
    if not value or any(char in value for char in '/\\?#@, \t\r\n'):
        return None
    try:
        parsed = urlsplit('http://' + value)
        port = parsed.port if parsed.port is not None else default_port
        return (parsed.hostname.lower(), port) if 1 <= port <= 65535 else None
    except (ValueError, AttributeError):
        return None


class SingleTokenMiddleware:
    """Fail closed before dispatching HTTP requests and WebSocket connections."""

    def __init__(self, app, token="", *, lan_mode=False, settings=None):
        self.app, self.token, self.lan_mode, self.settings = app, token, lan_mode, settings

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http', 'websocket'}:
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)
        client = connection.client.host if connection.client else None
        settings = self.settings
        lan_mode = settings.ui_lan_mode if settings is not None else self.lan_mode
        token = settings.ui_token if settings is not None else self.token
        hosts = connection.headers.getlist('host')
        scheme = 'https' if scope.get('scheme') in {'https', 'wss'} else 'http'
        default_port = 443 if scheme == 'https' else 80
        authority = _authority(hosts[0], default_port) if len(hosts) == 1 else None
        permitted = False
        if authority is not None:
            host, port = authority
            allowed = {'127.0.0.1', 'localhost', '::1'}
            if settings is not None:
                bind = settings.ui_host.lower().strip('[]')
                if bind not in {'0.0.0.0', '::', ''}:
                    allowed.add(bind)
                # A wildcard LAN bind admits the socket's actual destination,
                # never arbitrary DNS names supplied by an attacker.
                server = scope.get('server')
                if server and server[0] not in {'0.0.0.0', '::'}:
                    allowed.add(server[0].lower().strip('[]'))
            permitted = host in allowed and (settings is None or port == settings.ui_port)
            # Only the in-process test transport uses this synthetic peer.
            permitted |= client == 'testclient' and host == 'testserver' and port == 80
        origins = connection.headers.getlist('origin')
        origin_valid = not origins
        if len(origins) == 1 and authority is not None:
            try:
                parsed = urlsplit(origins[0])
                origin_valid = (parsed.scheme == scheme and not parsed.path and not parsed.query
                                and not parsed.fragment and _authority(parsed.netloc, default_port) == authority)
            except ValueError:
                origin_valid = False
        status, detail = 403, 'Host or Origin not permitted.'
        if permitted and origin_valid:
            if not lan_mode and is_loopback(client):
                return await self.app(scope, receive, send)
            if lan_mode:
                presented = (_bearer(connection.headers.get('authorization'))
                             or connection.query_params.get('token')
                             or connection.cookies.get('hg_token'))
                if token and presented and secrets.compare_digest(presented.encode(), token.encode()):
                    # A token-bearing navigation establishes an HttpOnly cookie
                    # for same-origin assets and browser WebSocket handshakes.
                    async def authenticated_send(message):
                        if scope['type'] == 'http' and message['type'] == 'http.response.start':
                            headers = list(message.get('headers', []))
                            headers.extend([(b'cache-control', b'no-store'), (b'referrer-policy', b'no-referrer')])
                            if connection.query_params.get('token'):
                                cookie = Response()
                                cookie.set_cookie('hg_token', token, httponly=True,
                                                  samesite='strict', secure=scope.get('scheme') == 'https')
                                headers.extend(cookie.raw_headers[-1:])
                            message = {**message, 'headers': headers}
                        await send(message)
                    return await self.app(scope, receive, authenticated_send)
                status, detail = (401, 'Invalid or missing token.') if token else (503, 'LAN token unavailable.')
            else:
                detail = 'Non-loopback access requires LAN mode.'
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008, 'reason': detail})
        else:
            await JSONResponse({'detail': detail}, status_code=status)(scope, receive, send)


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == 'bearer':
        return parts[1].strip()
    return None
