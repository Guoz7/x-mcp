"""End-to-end harness: serves a minimal MCP Apps host page and proxies /mcp.

The proxy only exists so the browser page and the MCP server share one origin.
It forwards requests unchanged to the real x-starter HTTP server.
"""

from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
FORWARD = ("Content-Type", "Accept", "Mcp-Session-Id", "MCP-Protocol-Version", "Origin")
RETURN = ("Content-Type", "Mcp-Session-Id", "Cache-Control")


def make_handler(upstream: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            print("e2e-host:", fmt % args, file=sys.stderr)

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] not in ("/", "/host.html"):
                self.send_error(404)
                return
            body = (HERE / "host.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _proxy(self, method: str) -> None:
            if self.path != "/mcp":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            data = self.rfile.read(length) if length else None
            headers = {k: self.headers[k] for k in FORWARD if self.headers.get(k)}
            request = Request(upstream, data=data, headers=headers, method=method)
            try:
                response = urlopen(request, timeout=30)
                status, rheaders, body = response.status, response.headers, response.read()
            except HTTPError as exc:
                status, rheaders, body = exc.code, exc.headers, exc.read()
            self.send_response(status)
            for key in RETURN:
                if rheaders.get(key):
                    self.send_header(key, rheaders[key])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self._proxy("POST")

        def do_DELETE(self) -> None:
            self._proxy("DELETE")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--upstream", default="http://127.0.0.1:8765/mcp")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.upstream))
    print(f"e2e host on http://127.0.0.1:{args.port}/ -> {args.upstream}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
