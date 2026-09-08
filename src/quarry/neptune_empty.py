"""A local, deliberately non-persistent Neptune openCypher endpoint."""

from __future__ import annotations

import argparse
import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


def parse_request(content_type: str, body: bytes) -> tuple[str, dict[str, object]]:
    if "application/json" in content_type:
        payload = json.loads(body or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON request must be an object")
        query = payload.get("query") or payload.get("openCypherQuery") or ""
        raw_params = payload.get("parameters") or {}
        if isinstance(raw_params, str):
            raw_params = json.loads(raw_params)
        return str(query), raw_params if isinstance(raw_params, dict) else {}
    payload = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    raw_params = payload.get("parameters", ["{}"])[0]
    try:
        params = json.loads(raw_params)
    except json.JSONDecodeError:
        params = {}
    return payload.get("query", [""])[0], params if isinstance(params, dict) else {}


class EmptyNeptuneHandler(BaseHTTPRequestHandler):
    server_version = "QuarryEmptyNeptune/1.0"

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"status": "ok", "backend": "empty"})
        else:
            self._json(404, {"message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.lower() != "/opencypher":
            self._json(404, {"message": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            parse_request(self.headers.get("Content-Type", ""), self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            self._json(400, {"message": "invalid openCypher request"})
            return
        self._json(200, {"results": []})

    def log_message(self, _format: str, *_args: object) -> None:
        return


def serve(host: str, port: int, cert: str, key: str) -> None:
    server = ThreadingHTTPServer((host, port), EmptyNeptuneHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert, keyfile=key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    serve(args.host, args.port, args.cert, args.key)


if __name__ == "__main__":
    main()
