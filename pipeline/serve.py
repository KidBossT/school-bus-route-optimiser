"""
Open the pages the way their maps expect:

    python pipeline/serve.py            -> http://localhost:8777/routes.html
    python pipeline/serve.py dashboard  -> http://localhost:8777/dashboard.html

The Google Maps browser key is locked to this address, so opening the HTML
file directly (file://...) shows no map. Serves outputs/ on this computer only
(127.0.0.1); nothing is reachable from the network. Ctrl+C to stop.
"""

from __future__ import annotations

import functools
import http.server
import sys
import threading
import webbrowser
from pathlib import Path

PORT = 8777


def main() -> int:
    out = Path(__file__).resolve().parent.parent / "outputs"
    page = "dashboard.html" if "dashboard" in sys.argv[1:] else "routes.html"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    except OSError:
        print(f"Port {PORT} is busy - the pages are probably already being served.")
        webbrowser.open(f"http://localhost:{PORT}/{page}")
        return 0
    url = f"http://localhost:{PORT}/{page}"
    print(f"Serving {out} at {url}  (Ctrl+C to stop)")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
