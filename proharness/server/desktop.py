"""Desktop shell — boots the FastAPI server in a thread and opens a pywebview
window on localhost.

This is the "desktop app": a native OS window (WebKit/Edge/Chromium depending
on platform) pointed at the loopback UI server. The server is the same one
that's LAN-reachable when ``ui_lan_mode`` is on — so the desktop shell and a
phone browser can talk to the same instance.

Native file pickers are exposed to the frontend via the JS bridge (FileApi):
``window.pywebview.api.open_file_picker()`` returns a chosen path. Falls back
to the REST ``/api/files`` endpoints when pywebview isn't available (server-
only / browser mode).

Run:
    python -m proharness.server.desktop
    # or after `pip install -e '.[desktop]'`:
    proharness-ui
"""
from __future__ import annotations

import logging
import threading
import time

from proharness.config import Settings

log = logging.getLogger(__name__)


class FileApi:
    """JS bridge exposed to the frontend as ``window.pywebview.api.*``.

    Provides native file open/save dialogs that browsers can't — this is the
    reason the desktop shell exists (local file access).
    """

    def open_file_picker(self) -> str | None:
        """Open a native file-open dialog; return the chosen path or None."""
        import webview

        result = webview.windows[0].create_file_dialog(webview.OPEN_DIALOG)
        if result:
            return result[0] if isinstance(result, list) else result
        return None

    def save_file_picker(self, suggested_name: str = "untitled.txt") -> str | None:
        """Open a native save dialog; return the chosen path or None."""
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG, save_filename=suggested_name,
        )
        return result if isinstance(result, str) else None


def main() -> None:
    """Console-script entry: boot the server + open a pywebview window."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    host = settings.ui_host or "127.0.0.1"
    port = settings.ui_port

    # Boot the FastAPI server in a daemon thread.
    import uvicorn

    from proharness.server.app import build_app

    app = build_app(settings)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", proxy_headers=False)
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    log.info("ProHarness UI server on http://%s:%d", host, port)

    # Wait briefly for the server to be reachable.
    import httpx

    url = f"http://{host}:{port}/healthz"
    for _ in range(50):
        try:
            if httpx.get(url, timeout=0.5).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)

    # Open the desktop window.
    try:
        import webview

        webview.create_window(
            "ProHarness",
            f"http://{host}:{port}/",
            js_api=FileApi(),
            min_size=(900, 600),
        )
        webview.start()
        # When the window closes, shut the server down.
        server.should_exit = True
    except ImportError:
        log.warning(
            "pywebview not installed; open http://%s:%d/ in a browser "
            "(pip install -e '.[desktop]' for the native window)",
            host, port,
        )
        # Block so the server keeps running for browser access.
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            server.should_exit = True


if __name__ == "__main__":
    main()
