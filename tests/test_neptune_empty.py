from __future__ import annotations

import io
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest

from quarry import core, local, neptune_empty
from quarry.neptune_empty import EmptyNeptuneHandler, parse_request


def test_parse_boto3_json_request() -> None:
    query, params = parse_request(
        "application/json",
        json.dumps({"query": "MATCH (n) RETURN n", "parameters": '{"user_id":5527}'}).encode(),
    )
    assert query == "MATCH (n) RETURN n"
    assert params == {"user_id": 5527}


def test_parse_quarry_form_request() -> None:
    query, params = parse_request(
        "application/x-www-form-urlencoded",
        urlencode({"query": "RETURN 1", "parameters": '{"x":1}'}).encode(),
    )
    assert query == "RETURN 1"
    assert params == {"x": 1}


def test_parse_missing_parameters() -> None:
    query, params = parse_request("application/json", b'{"openCypherQuery":"RETURN 1"}')
    assert query == "RETURN 1"
    assert params == {}


@pytest.fixture()
def empty_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), EmptyNeptuneHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_empty_endpoint_health_and_not_found(empty_endpoint: str) -> None:
    with urllib.request.urlopen(f"{empty_endpoint}/health") as response:
        assert json.loads(response.read()) == {"status": "ok", "backend": "empty"}
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{empty_endpoint}/missing")
    assert exc.value.code == 404


def test_empty_endpoint_accepts_both_paths_and_rejects_bad_requests(empty_endpoint: str) -> None:
    for path, body, content_type in (
        ("/openCypher", urlencode({"query": "RETURN 1"}).encode(),
         "application/x-www-form-urlencoded"),
        ("/opencypher", b'{"query":"MATCH (n) RETURN n"}', "application/json"),
    ):
        request = urllib.request.Request(
            f"{empty_endpoint}{path}", data=body, headers={"Content-Type": content_type})
        with urllib.request.urlopen(request) as response:
            assert json.loads(response.read()) == {"results": []}
    for path, body in (("/other", b"{}"), ("/opencypher", b"not-json")):
        request = urllib.request.Request(
            f"{empty_endpoint}{path}", data=body, headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        assert exc.value.code in {400, 404}


def test_serve_wraps_socket_and_runs(monkeypatch) -> None:
    calls = []

    class Server:
        socket = object()

        def __init__(self, address, handler):
            calls.append((address, handler))

        def serve_forever(self):
            calls.append("serve")

    class Context:
        def load_cert_chain(self, *, certfile, keyfile):
            calls.append((certfile, keyfile))

        def wrap_socket(self, sock, *, server_side):
            calls.append((sock, server_side))
            return "wrapped"

    monkeypatch.setattr(neptune_empty, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(neptune_empty.ssl, "SSLContext", lambda _protocol: Context())
    neptune_empty.serve("127.0.0.1", 8182, "cert", "key")
    assert calls[-1] == "serve"


def test_main_parses_arguments(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(sys, "argv", [
        "neptune-empty", "--port", "8182", "--cert", "cert", "--key", "key",
    ])
    monkeypatch.setattr(neptune_empty, "serve", lambda *args: seen.append(args))
    neptune_empty.main()
    assert seen == [("127.0.0.1", 8182, "cert", "key")]


def _state_at(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local, "neptune_state_dir", lambda: tmp_path / "state")


def test_pid_and_ownership_helpers(monkeypatch, tmp_path: Path) -> None:
    _state_at(monkeypatch, tmp_path)
    assert local._read_neptune_pid() is None
    pid_path = local._neptune_paths()[0]
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("bad")
    assert local._read_neptune_pid() is None
    pid_path.write_text("42")
    assert local._read_neptune_pid() == 42
    assert local._owned_neptune_pid(None) is False
    monkeypatch.setattr(local.os, "kill", lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    assert local._owned_neptune_pid(42) is False
    monkeypatch.setattr(local.os, "kill", lambda *_args: None)
    monkeypatch.setattr(
        local.subprocess, "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0, "stdout": "python -m quarry.neptune_empty"})(),
    )
    assert local._owned_neptune_pid(42) is True


def test_certificate_creation_paths(monkeypatch, tmp_path: Path) -> None:
    cert, key = tmp_path / "cert", tmp_path / "key"
    cert.write_text("cert")
    key.write_text("key")
    local._ensure_neptune_certificate(cert, key)
    cert.unlink()
    key.unlink()
    monkeypatch.setattr(local.shutil, "which", lambda _name: None)
    with pytest.raises(core.QuarryError):
        local._ensure_neptune_certificate(cert, key)
    monkeypatch.setattr(local.shutil, "which", lambda _name: "/usr/bin/openssl")
    monkeypatch.setattr(
        local.subprocess, "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 1, "stderr": "failed"})(),
    )
    with pytest.raises(core.QuarryError, match="failed to create"):
        local._ensure_neptune_certificate(cert, key)


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_neptune_health(monkeypatch) -> None:
    monkeypatch.setattr(
        local.urllib.request, "urlopen",
        lambda *_args, **_kwargs: _Response(b'{"backend":"empty"}'),
    )
    assert local._neptune_health(8182) is True
    monkeypatch.setattr(
        local.urllib.request, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    assert local._neptune_health(8182) is False


def test_start_neptune_empty_lifecycle(monkeypatch, tmp_path: Path) -> None:
    _state_at(monkeypatch, tmp_path)
    spec = local.EngineSpec("neptune", "empty", "", 18183, 18183, "empty")
    monkeypatch.setattr(local, "_owned_neptune_pid", lambda _pid: True)
    monkeypatch.setattr(local, "_neptune_health", lambda _port: True)
    local._neptune_paths()[0].parent.mkdir(parents=True)
    local._neptune_paths()[0].write_text("41")
    assert local.start_neptune_empty(spec) == "running"

    monkeypatch.setattr(local, "_owned_neptune_pid", lambda _pid: False)
    monkeypatch.setattr(local, "port_in_use", lambda _port: True)
    with pytest.raises(core.QuarryError, match="already in use"):
        local.start_neptune_empty(spec)

    monkeypatch.setattr(local, "port_in_use", lambda _port: False)
    monkeypatch.setattr(local, "_ensure_neptune_certificate", lambda *_args: None)
    health = iter([False, True])
    monkeypatch.setattr(local, "_neptune_health", lambda _port: next(health))

    class Proc:
        pid = 99

        def poll(self):
            return None

    monkeypatch.setattr(local.subprocess, "Popen", lambda *_args, **_kwargs: Proc())
    monkeypatch.setattr(local.time, "sleep", lambda _seconds: None)
    assert local.start_neptune_empty(spec) == "created"
    assert local._read_neptune_pid() == 99


def test_start_neptune_empty_reports_child_failure(monkeypatch, tmp_path: Path) -> None:
    _state_at(monkeypatch, tmp_path)
    spec = local.EngineSpec("neptune", "empty", "", 18183, 18183, "empty")
    monkeypatch.setattr(local, "_owned_neptune_pid", lambda _pid: False)
    monkeypatch.setattr(local, "port_in_use", lambda _port: False)
    monkeypatch.setattr(local, "_ensure_neptune_certificate", lambda *_args: None)
    monkeypatch.setattr(local, "_neptune_health", lambda _port: False)

    class Proc:
        pid = 99

        def poll(self):
            return 1

    monkeypatch.setattr(local.subprocess, "Popen", lambda *_args, **_kwargs: Proc())
    with pytest.raises(core.QuarryError, match="did not become ready"):
        local.start_neptune_empty(spec)


def test_down_and_status_neptune_empty(monkeypatch, tmp_path: Path) -> None:
    _state_at(monkeypatch, tmp_path)
    pid, cert, key, log = local._neptune_paths()
    pid.parent.mkdir(parents=True)
    for path, value in ((pid, "77"), (cert, "c"), (key, "k"), (log, "l")):
        path.write_text(value)
    owned = iter([True, False, False])
    monkeypatch.setattr(local, "_owned_neptune_pid", lambda _pid: next(owned))
    killed = []
    monkeypatch.setattr(local.os, "kill", lambda process, signal: killed.append((process, signal)))
    monkeypatch.setattr(local.time, "sleep", lambda _seconds: None)
    result = local.down_neptune_empty(purge=True)
    assert result["stopped"] is True
    assert killed == [(77, 15)]
    assert not pid.parent.exists()

    monkeypatch.setattr(local, "_read_neptune_pid", lambda: 88)
    monkeypatch.setattr(local, "_owned_neptune_pid", lambda _pid: True)
    monkeypatch.setattr(local, "_neptune_health", lambda _port: True)
    status = local.neptune_empty_status()
    assert status["running"] is True and status["backend"] == "empty" and status["pid"] == 88


def test_generic_local_lifecycle_dispatches_neptune(monkeypatch) -> None:
    monkeypatch.setattr(local, "start_neptune_empty", lambda _spec: "created")
    monkeypatch.setattr(local, "down_neptune_empty", lambda _spec, purge: {"purged": purge})
    monkeypatch.setattr(local, "neptune_empty_status", lambda _spec: {"running": True})
    assert local.start_container(local.NEPTUNE_SPEC) == "created"
    assert local.down_engine(local.NEPTUNE_SPEC, purge=True) == {"purged": True}
    assert local.engine_status(local.NEPTUNE_SPEC) == {"running": True}
