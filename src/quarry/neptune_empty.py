"""A local Neptune openCypher endpoint with empty and fixture-backed modes."""

from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


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


@dataclass(frozen=True)
class MockResponse:
    query_contains: str
    parameters: dict[str, object]
    results: list[object]

    def matches(self, query: str, parameters: dict[str, object]) -> bool:
        return self.query_contains in query and all(
            key in parameters and parameters[key] == value
            for key, value in self.parameters.items()
        )


@dataclass
class MockNeptuneState:
    responses: list[MockResponse]
    fixture_sha256: str
    calls: list[dict[str, object]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def execute(self, query: str, parameters: dict[str, object]) -> list[object] | None:
        response = next((item for item in self.responses if item.matches(query, parameters)), None)
        with self.lock:
            self.calls.append({
                "sequence": len(self.calls) + 1,
                "query": query,
                "parameters": parameters,
                "matched": response is not None,
            })
        return response.results if response is not None else None

    def calls_after(self, after: int) -> dict[str, object]:
        with self.lock:
            return {"calls": [dict(call) for call in self.calls if call["sequence"] > after],
                    "next": len(self.calls)}


def load_fixture(path: Path) -> MockNeptuneState:
    content = path.read_bytes()
    payload = json.loads(content)
    if not isinstance(payload, dict) or not isinstance(payload.get("responses"), list):
        raise ValueError("fixture must contain a responses array")
    responses = []
    for item in payload["responses"]:
        if not isinstance(item, dict):
            raise ValueError("each response must be an object")
        query_contains = item.get("query_contains")
        parameters = item.get("parameters", {})
        results = item.get("results")
        if (not isinstance(query_contains, str) or not query_contains
                or not isinstance(parameters, dict) or not all(isinstance(key, str) for key in parameters)
                or not isinstance(results, list)):
            raise ValueError("response requires query_contains, optional parameters, and results array")
        responses.append(MockResponse(query_contains, parameters, results))
    return MockNeptuneState(responses, hashlib.sha256(content).hexdigest())


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
        path = urlsplit(self.path)
        mock = getattr(self.server, "mock_state", None)
        if path.path == "/health" and not path.query:
            payload: dict[str, object] = {"status": "ok", "backend": "mock" if mock else "empty"}
            if isinstance(mock, MockNeptuneState):
                payload["fixture_sha256"] = mock.fixture_sha256
            self._json(200, payload)
        elif path.path == "/__mock__/calls" and isinstance(mock, MockNeptuneState):
            raw_after = parse_qs(path.query).get("after", ["0"])
            if len(raw_after) != 1 or not raw_after[0].isdigit():
                self._json(400, {"message": "after must be a non-negative integer"})
                return
            self._json(200, mock.calls_after(int(raw_after[0])))
        else:
            self._json(404, {"message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.lower() != "/opencypher":
            self._json(404, {"message": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            query, parameters = parse_request(self.headers.get("Content-Type", ""), self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            self._json(400, {"message": "invalid openCypher request"})
            return
        mock = getattr(self.server, "mock_state", None)
        if isinstance(mock, MockNeptuneState):
            results = mock.execute(query, parameters)
            if results is None:
                self._json(501, {"message": "no matching mock response"})
                return
            self._json(200, {"results": results})
        else:
            self._json(200, {"results": []})

    def log_message(self, _format: str, *_args: object) -> None:
        return


def serve(host: str, port: int, cert: str, key: str, fixture: Path | None = None) -> None:
    if fixture is not None and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("mock mode must bind to loopback")
    mock = load_fixture(fixture) if fixture is not None else None
    server = ThreadingHTTPServer((host, port), EmptyNeptuneHandler)
    if mock is not None:
        server.mock_state = mock
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
    parser.add_argument("--fixture", type=Path,
                        help="JSON responses for an opt-in, in-memory local openCypher mock")
    args = parser.parse_args()
    if args.fixture is None:
        serve(args.host, args.port, args.cert, args.key)
    else:
        serve(args.host, args.port, args.cert, args.key, args.fixture)


if __name__ == "__main__":
    main()
