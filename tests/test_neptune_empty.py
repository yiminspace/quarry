from __future__ import annotations

import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest

from quarry import core, local, neptune_empty
from quarry.neptune_empty import EmptyNeptuneHandler, load_fixture, parse_request


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


@pytest.fixture()
def mock_endpoint(tmp_path: Path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"responses": [
        {"query_contains": "MATCH (n)", "parameters": {"user_id": "test-user"},
         "results": [{"n": {"name": "test-memory"}}]},
        {"query_contains": "RETURN 0", "results": []},
    ]}), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), EmptyNeptuneHandler)
    server.mock_state = load_fixture(fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server.mock_state
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


def test_mock_endpoint_responses_and_call_cursor(mock_endpoint) -> None:
    endpoint, state = mock_endpoint
    with urllib.request.urlopen(f"{endpoint}/health") as response:
        assert json.loads(response.read()) == {
            "status": "ok", "backend": "mock", "fixture_sha256": state.fixture_sha256,
        }
    request = urllib.request.Request(
        f"{endpoint}/openCypher",
        data=json.dumps({"query": "MATCH (n) RETURN n", "parameters": '{"user_id":"test-user"}'}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        assert json.loads(response.read()) == {"results": [{"n": {"name": "test-memory"}}]}
    with urllib.request.urlopen(f"{endpoint}/__mock__/calls?after=0") as response:
        calls = json.loads(response.read())
    assert calls == {"calls": [{"sequence": 1, "query": "MATCH (n) RETURN n",
                                "parameters": {"user_id": "test-user"}, "matched": True}], "next": 1}
    with urllib.request.urlopen(f"{endpoint}/__mock__/calls?after=1") as response:
        assert json.loads(response.read()) == {"calls": [], "next": 1}


def test_mock_endpoint_unmatched_is_recorded_and_fails(mock_endpoint) -> None:
    endpoint, _ = mock_endpoint
    request = urllib.request.Request(
        f"{endpoint}/openCypher", data=urlencode({"query": "MATCH (n) RETURN n",
            "parameters": '{"user_id":"another-user"}'}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request)
    assert exc.value.code == 501
    assert json.loads(exc.value.read()) == {"message": "no matching mock response"}
    with urllib.request.urlopen(f"{endpoint}/__mock__/calls") as response:
        assert json.loads(response.read())["calls"][0]["matched"] is False
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{endpoint}/__mock__/calls?after=-1")
    assert exc.value.code == 400


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is unavailable")
def test_mock_process_serves_https_and_exposes_calls(tmp_path: Path) -> None:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    local._ensure_neptune_certificate(cert, key)
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"responses":[{"query_contains":"RETURN 1","results":[{"n":1}]}]}',
                       encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen([
        sys.executable, "-m", "quarry.neptune_empty", "--port", str(port),
        "--cert", str(cert), "--key", str(key), "--fixture", str(fixture),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    context = ssl._create_unverified_context()
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with urllib.request.urlopen(f"https://127.0.0.1:{port}/health", context=context,
                                                timeout=0.2) as response:
                    assert json.loads(response.read())["backend"] == "mock"
                break
            except urllib.error.URLError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail("local HTTPS mock did not become ready")
                time.sleep(0.05)
        request = urllib.request.Request(
            f"https://127.0.0.1:{port}/openCypher", data=b'{"query":"RETURN 1"}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, context=context) as response:
            assert json.loads(response.read()) == {"results": [{"n": 1}]}
        assert local.neptune_mock_calls(local.EngineSpec("neptune", "empty", "", port, port, "empty")) == {
            "calls": [{"sequence": 1, "query": "RETURN 1", "parameters": {}, "matched": True}], "next": 1,
        }
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_fixture_validation_and_empty_mode_boundary(tmp_path: Path, empty_endpoint: str) -> None:
    fixture = tmp_path / "fixture.json"
    for invalid in ('{}', '{"responses":[{"query_contains":"x"}]}',
                    '{"responses":[{"query_contains":"","results":[]}]}'):
        fixture.write_text(invalid, encoding="utf-8")
        with pytest.raises(ValueError):
            load_fixture(fixture)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{empty_endpoint}/__mock__/calls")
    assert exc.value.code == 404
    with pytest.raises(ValueError, match="loopback"):
        neptune_empty.serve("0.0.0.0", 8182, "cert", "key", fixture)


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


def test_main_parses_mock_fixture(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(sys, "argv", [
        "neptune-empty", "--port", "8182", "--cert", "cert", "--key", "key",
        "--fixture", "fixture.json",
    ])
    monkeypatch.setattr(neptune_empty, "serve", lambda *args: seen.append(args))
    neptune_empty.main()
    assert seen == [("127.0.0.1", 8182, "cert", "key", Path("fixture.json"))]


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


def test_mock_lifecycle_requires_matching_fixture(monkeypatch, tmp_path: Path) -> None:
    _state_at(monkeypatch, tmp_path)
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"responses":[]}', encoding="utf-8")
    spec = local.EngineSpec("neptune", "empty", "", 18183, 18183, "empty")
    digest = load_fixture(fixture).fixture_sha256
    monkeypatch.setattr(local, "_owned_neptune_pid", lambda _pid: True)
    monkeypatch.setattr(local, "_neptune_health_payload", lambda _port: {
        "backend": "mock", "fixture_sha256": digest,
    })
    local._neptune_paths()[0].parent.mkdir(parents=True)
    local._neptune_paths()[0].write_text("41")
    assert local.start_neptune_empty(spec, fixture=fixture) == "running"
    assert local.neptune_empty_status(spec)["backend"] == "mock"

    fixture.write_text('{"responses":[{"query_contains":"RETURN","results":[]}]}', encoding="utf-8")
    monkeypatch.setattr(local, "port_in_use", lambda _port: True)
    with pytest.raises(core.QuarryError, match="changing modes"):
        local.start_neptune_empty(spec, fixture=fixture)


def test_mock_lifecycle_starts_with_fixture_without_touching_another_endpoint(
    monkeypatch, tmp_path: Path,
) -> None:
    _state_at(monkeypatch, tmp_path)
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"responses":[]}', encoding="utf-8")
    spec = local.EngineSpec("neptune", "empty", "", 18183, 18183, "empty")
    monkeypatch.setattr(local, "_owned_neptune_pid", lambda _pid: False)
    monkeypatch.setattr(local, "port_in_use", lambda _port: False)
    monkeypatch.setattr(local, "_ensure_neptune_certificate", lambda *_args: None)
    health = iter([None, {"backend": "mock", "fixture_sha256": load_fixture(fixture).fixture_sha256}])
    monkeypatch.setattr(local, "_neptune_health_payload", lambda _port: next(health))

    class Proc:
        pid = 99

        def poll(self):
            return None

    commands = []
    monkeypatch.setattr(local.subprocess, "Popen", lambda argv, **_kwargs: (commands.append(argv), Proc())[1])
    assert local.start_neptune_empty(spec, fixture=fixture) == "created"
    assert commands[0][-2:] == ["--fixture", str(fixture)]


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
