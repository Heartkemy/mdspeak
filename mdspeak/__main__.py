"""Launch mdspeak as a desktop window.

uvicorn on a background thread, then a native WebView window pointed at it.
Without pywebview installed it falls back to the default browser — the app is
identical either way, only the frame differs.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.request

import uvicorn

from mdspeak.server import app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.15)
    return False


def main() -> None:
    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()

    if not _wait(url):
        raise SystemExit("server did not start")
    print(f"mdspeak on {url}")

    try:
        import webview  # pywebview
    except ImportError:
        import webbrowser

        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return
    else:
        webview.create_window("mdspeak", url, width=880, height=900, min_size=(560, 620))
        webview.start()


if __name__ == "__main__":
    main()
