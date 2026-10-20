"""Unit tests for quarry.proxycommand (issue #96).

connect_tunnel()/relay_stdio() are exercised against a real local TCP "mock
proxy" socket this test spins up itself (loopback only, no external network).
The bidirectional-relay + CLI behavior is exercised end-to-end by spawning the
module as a real subprocess (`python -m quarry.proxycommand ...`), since
relay_stdio() reads/writes actual OS file descriptors (sys.stdin/stdout) that
can't be swapped in-process.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading

import pytest

from quarry import proxycommand

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _mock_proxy_server(response: bytes, echo: bool = False):
    """Bind a loopback listening socket; return (host, port, thread, result).

    The background thread accepts exactly one connection, reads until it sees
    the end of the CONNECT request headers, records the raw request bytes into
    result['request'], writes `response`, and — if echo — relays anything the
    client sends back verbatim until the client closes its write side.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    result: dict = {}

    def _serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            result["request"] = buf
            conn.sendall(response)
            if echo:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    conn.sendall(data)
        finally:
            conn.close()
            srv.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return host, port, t, result


# ---------------------------------------------------------------------------
# connect_tunnel() — CONNECT handshake
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_connect_tunnel_success_returns_usable_socket():
    host, port, t, result = _mock_proxy_server(
        b"HTTP/1.1 200 Connection Established\r\n\r\n", echo=True
    )
    sock = proxycommand.connect_tunnel(host, port, "internal-bastion", 22)
    try:
        assert sock.getpeername() == (host, port)
        sock.sendall(b"ping")
        assert sock.recv(4) == b"ping"  # echoed back -> socket is genuinely usable
    finally:
        sock.close()
    t.join(timeout=2)
    assert b"CONNECT internal-bastion:22 HTTP/1.1" in result["request"]
    assert b"Host: internal-bastion:22" in result["request"]


@pytest.mark.unit
def test_connect_tunnel_non_200_raises_connectionerror():
    host, port, t, _ = _mock_proxy_server(
        b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"
    )
    with pytest.raises(ConnectionError) as ei:
        proxycommand.connect_tunnel(host, port, "target", 5432)
    assert "407" in str(ei.value)
    t.join(timeout=2)


@pytest.mark.unit
def test_connect_tunnel_5xx_raises_connectionerror():
    host, port, t, _ = _mock_proxy_server(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
    with pytest.raises(ConnectionError) as ei:
        proxycommand.connect_tunnel(host, port, "target", 5432)
    assert "502" in str(ei.value)
    t.join(timeout=2)


@pytest.mark.unit
def test_connect_tunnel_empty_response_raises_connectionerror():
    host, port, t, _ = _mock_proxy_server(b"")  # proxy closes without replying
    with pytest.raises(ConnectionError) as ei:
        proxycommand.connect_tunnel(host, port, "target", 5432)
    assert "empty response" in str(ei.value)
    t.join(timeout=2)


@pytest.mark.unit
def test_connect_tunnel_proxy_unreachable_raises_oserror():
    # nothing listens on this port
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    with pytest.raises(OSError):
        proxycommand.connect_tunnel("127.0.0.1", port, "target", 5432, connect_timeout=1.0)


# ---------------------------------------------------------------------------
# main() — CLI argument handling (no real socket I/O)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_main_wrong_arg_count_returns_2(capsys):
    assert proxycommand.main(["only-one-arg"]) == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.unit
def test_main_invalid_port_returns_2(capsys):
    assert proxycommand.main(["proxyhost", "not-a-port", "target", "22"]) == 2
    assert "invalid port" in capsys.readouterr().err


@pytest.mark.unit
def test_main_connect_failure_returns_1_with_readable_error(monkeypatch, capsys):
    def boom(*a, **k):
        raise ConnectionError("proxy CONNECT target:22 failed: HTTP/1.1 407 Proxy Authentication Required")

    monkeypatch.setattr(proxycommand, "connect_tunnel", boom)
    rc = proxycommand.main(["proxyhost", "8080", "target", "22"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "quarry.proxycommand:" in err
    assert "407" in err


# ---------------------------------------------------------------------------
# End-to-end subprocess: real CONNECT handshake + bidirectional stdio relay
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_subprocess_relays_stdio_bidirectionally_through_mock_proxy():
    host, port, server_thread, _ = _mock_proxy_server(
        b"HTTP/1.1 200 Connection Established\r\n\r\n", echo=True
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC_DIR
    proc = subprocess.Popen(
        [sys.executable, "-m", "quarry.proxycommand", host, str(port), "remote-host", "22"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        out, err = proc.communicate(input=b"hello through the tunnel", timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err.decode("utf-8", "replace")
    assert out == b"hello through the tunnel"  # echoed back by the mock proxy -> real relay
    server_thread.join(timeout=2)


@pytest.mark.unit
def test_subprocess_exits_nonzero_on_proxy_rejection():
    host, port, server_thread, _ = _mock_proxy_server(
        b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC_DIR
    proc = subprocess.Popen(
        [sys.executable, "-m", "quarry.proxycommand", host, str(port), "remote-host", "22"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 1
    assert out == b""
    assert b"407" in err
    server_thread.join(timeout=2)
