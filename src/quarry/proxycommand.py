"""`ssh -o ProxyCommand=...` helper: tunnel ssh's stdio through an HTTP(S) proxy
via CONNECT (issue #96).

Zero-dependency, stdlib only — matches tunnel.py's zero-dependency stance and
avoids `nc`, whose `-X connect` flag is BSD-only (GNU/busybox netcat use a
different, incompatible flag set) and which may not even be on PATH for a
GUI/launchd-spawned process.

Invoked by tunnel.py as (ssh substitutes %h/%p with the ssh target itself):

    ssh -o 'ProxyCommand=<sys.executable> -m quarry.proxycommand <proxy_host> <proxy_port> %h %p' ...

Connects to the proxy, issues `CONNECT <target_host>:<target_port>`, and on a
2xx response relays stdin/stdout byte-for-byte against the resulting socket —
exactly what ssh expects a ProxyCommand's stdio to be. A non-2xx response (or
any connect failure) prints a one-line error to stderr and exits non-zero, so
ssh's own error handling surfaces it instead of hanging on a dead pipe.
"""

from __future__ import annotations

import os
import socket
import sys
import threading

CONNECT_TIMEOUT_SEC = 10.0


def _read_response_headers(sock: socket.socket) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def connect_tunnel(
    proxy_host: str, proxy_port: int, target_host: str, target_port: int,
    connect_timeout: float = CONNECT_TIMEOUT_SEC,
) -> socket.socket:
    """Open a TCP connection to the proxy and CONNECT it through to
    target_host:target_port. Returns the raw socket, ready to relay. Raises
    OSError (connect failed) or ConnectionError (non-2xx CONNECT response)."""
    sock = socket.create_connection((proxy_host, proxy_port), timeout=connect_timeout)
    request = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n"
        "Proxy-Connection: keep-alive\r\n"
        "\r\n"
    ).encode("ascii")
    sock.sendall(request)
    headers = _read_response_headers(sock)
    status_line = headers.split(b"\r\n", 1)[0].decode("ascii", "replace").strip()
    parts = status_line.split(None, 2)
    if len(parts) < 2 or not parts[1].startswith("2"):
        sock.close()
        raise ConnectionError(f"proxy CONNECT {target_host}:{target_port} failed: "
                              f"{status_line or '(empty response)'}")
    sock.settimeout(None)
    return sock


def _pump_fd_to_sock(fd: int, sock: socket.socket) -> None:
    try:
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            sock.sendall(data)
    except OSError:
        pass
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _pump_sock_to_fd(sock: socket.socket, fd: int) -> None:
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            os.write(fd, data)
    except OSError:
        pass


def relay_stdio(sock: socket.socket) -> None:
    """Block relaying stdin -> sock and sock -> stdout until either side closes."""
    t = threading.Thread(target=_pump_fd_to_sock, args=(sys.stdin.fileno(), sock), daemon=True)
    t.start()
    _pump_sock_to_fd(sock, sys.stdout.fileno())
    t.join(timeout=2)
    try:
        sock.close()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 4:
        print("usage: python -m quarry.proxycommand <proxy_host> <proxy_port> <target_host> <target_port>",
              file=sys.stderr)
        return 2
    proxy_host, proxy_port_raw, target_host, target_port_raw = argv
    try:
        proxy_port, target_port = int(proxy_port_raw), int(target_port_raw)
    except ValueError:
        print(f"quarry.proxycommand: invalid port in {argv!r}", file=sys.stderr)
        return 2
    try:
        sock = connect_tunnel(proxy_host, proxy_port, target_host, target_port)
    except (OSError, ConnectionError) as exc:
        print(f"quarry.proxycommand: {exc}", file=sys.stderr)
        return 1
    relay_stdio(sock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
