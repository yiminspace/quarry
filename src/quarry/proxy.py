"""System/env HTTP(S) proxy discovery for SSH tunnels (issue #96).

Background: over a cross-border SSH tunnel a bare `ssh -L` forward can throttle
to ~15KB/s, which is invisible for small results but hits `run_psql_capture`'s
hard timeout the moment a row carries a large field. Routing the same SSH
session's TCP stream through the machine's HTTP(S) proxy (`ProxyCommand`, see
`proxycommand.py`) fixes the throughput; this module is the "should we, and
where" half of that: locating a usable proxy and deciding whether a given
target should go through it.

Zero-dependency, stdlib only. No Happy-Eyeballs-style connect race — SSH
handshake latency direct vs proxied is close enough that racing them tends to
pick the slow (direct) path anyway; instead, whether to use the proxy is an
explicit, persisted per-workspace choice (see `workspace.is_proxy_enabled`),
checked here against a real (cheap) port probe so a stale/dead proxy just
falls back to a direct connection instead of erroring.
"""

from __future__ import annotations

import ipaddress
import os
import platform
import re
import socket
import subprocess
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import workspace as workspace_mod


@dataclass
class ProxyInfo:
    host: str
    port: int
    source: str  # "system" (scutil) | "env" (ALL_PROXY/HTTPS_PROXY)
    exceptions: list[str] = field(default_factory=list)  # raw ExceptionsList entries (system only)


# Sentinel default for `evaluate_proxy(discovered=...)` (issue #101): distinct
# from `None`, which means "a caller already ran discover_proxy() and it found
# nothing" (skip re-discovering). Lets a caller iterating many connections in
# one workspace (the GUI's env-pill badges) probe discover_proxy() and the
# port-liveness check once and reuse the answer, instead of once per
# connection for what is always the same machine-wide proxy.
UNSET_DISCOVERY = object()


@dataclass
class ProxyDecision:
    """The full should-we-proxy verdict for one target — `should_use_proxy`'s
    plain ProxyInfo|None collapses *why* into a single None, which is fine for
    routing but not for explaining a silent fallback to a user (issue #101's
    CLI stderr hint) or showing "proxy on but nothing's listening" in the GUI."""

    proxy: ProxyInfo | None
    # "override_off" | "disabled" | "not_discovered" | "port_unreachable" | "exception_list" | "ok"
    reason: str
    discovered: ProxyInfo | None = None  # whatever discover_proxy() found, even when unused


_SCUTIL_INDEX_RE = re.compile(r"^\d+\s*:\s*(.+)$")


def _parse_scutil_proxy(output: str) -> ProxyInfo | None:
    """Parse `scutil --proxy` key:value output, e.g.:

        HTTPSEnable : 1
        HTTPSPort : 6152
        HTTPSProxy : 127.0.0.1
        ExceptionsList : <array> {
          0 : 192.168.0.0/16
          1 : 10.0.0.0/8
          2 : *.local
          3 : localhost
        }
    """
    kv: dict[str, str] = {}
    exceptions: list[str] = []
    in_exceptions = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if in_exceptions:
            if line.startswith("}"):
                in_exceptions = False
                continue
            m = _SCUTIL_INDEX_RE.match(line)
            if m:
                exceptions.append(m.group(1).strip())
            continue
        if line.startswith("ExceptionsList"):
            in_exceptions = True
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            kv[k.strip()] = v.strip()

    # HTTPS proxy setting wins (our traffic — SSH over CONNECT — is tunneled the
    # same way regardless of scheme, but HTTPSProxy is the more deliberate one
    # of the two when both are configured).
    for prefix in ("HTTPS", "HTTP"):
        if kv.get(f"{prefix}Enable") == "1":
            host, port = kv.get(f"{prefix}Proxy"), kv.get(f"{prefix}Port")
            if host and port:
                try:
                    return ProxyInfo(host=host, port=int(port), source="system", exceptions=exceptions)
                except ValueError:
                    continue
    return None


def _scutil_proxy() -> ProxyInfo | None:
    try:
        proc = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _parse_scutil_proxy(proc.stdout)


def _env_proxy() -> ProxyInfo | None:
    for var in ("ALL_PROXY", "HTTPS_PROXY", "all_proxy", "https_proxy"):
        raw = os.environ.get(var)
        if not raw or not raw.strip():
            continue
        raw = raw.strip()
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        if parsed.hostname and parsed.port:
            return ProxyInfo(host=parsed.hostname, port=parsed.port, source="env")
    return None


def discover_proxy() -> ProxyInfo | None:
    """Zero-config discovery: macOS system proxy (scutil) first, then
    ALL_PROXY/HTTPS_PROXY env vars, else None (no proxy)."""
    if platform.system() == "Darwin":
        found = _scutil_proxy()
        if found is not None:
            return found
    return _env_proxy()


def _host_matches_exception(host: str, entry: str) -> bool:
    entry = entry.strip()
    if not entry:
        return False
    if "/" in entry:  # CIDR, e.g. 10.0.0.0/8
        try:
            return ipaddress.ip_address(host) in ipaddress.ip_network(entry, strict=False)
        except ValueError:
            return False
    if entry.startswith("*."):  # domain suffix wildcard, e.g. *.local
        return host == entry[2:] or host.endswith(entry[1:])
    if entry == host:
        return True
    try:  # both sides could be IPs written differently (e.g. leading zeros)
        return ipaddress.ip_address(host) == ipaddress.ip_address(entry)
    except ValueError:
        return False


def host_in_exceptions(host: str, exceptions: list[str]) -> bool:
    """True if `host` is covered by the proxy's exceptions list (loopback is
    always exempt, regardless of what the list says, since a bastion on
    localhost is never worth detouring through a proxy)."""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
    except ValueError:
        pass
    return any(_host_matches_exception(host, entry) for entry in exceptions)


def _port_listening(host: str, port: int, timeout: float = 0.3) -> bool:
    """Cheap probe only — no handshake retry-on-failure (see module docstring
    in tunnel.py: a real ssh failure must surface as itself, not get masked by
    a silent proxy fallback that doubles the failure path's latency)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def evaluate_proxy(
    target_host: str | None, *, workspace_home: "str | os.PathLike", override: bool | None = None,
    discovered: "ProxyInfo | None | object" = UNSET_DISCOVERY,
) -> ProxyDecision:
    """Like `should_use_proxy`, but returns the full `ProxyDecision` (verdict +
    *why*) instead of collapsing every "don't proxy" case into a bare None —
    issue #101 needs to tell "workspace proxy is on but nothing was
    discovered" apart from "discovered but unreachable" apart from "target is
    in the exceptions list" (the last one is an expected routing choice, not a
    problem).

    `discovered`: pass an already-called `discover_proxy()` result (or `None`
    if that call already came up empty) to skip a second lookup — see
    `UNSET_DISCOVERY`.
    """
    if override is False:
        return ProxyDecision(None, "override_off")
    if override is None and not workspace_mod.is_proxy_enabled(workspace_home):
        return ProxyDecision(None, "disabled")
    info = discover_proxy() if discovered is UNSET_DISCOVERY else discovered
    if info is None:
        return ProxyDecision(None, "not_discovered")
    if target_host and host_in_exceptions(target_host, info.exceptions):
        return ProxyDecision(None, "exception_list", discovered=info)
    if not _port_listening(info.host, info.port):
        return ProxyDecision(None, "port_unreachable", discovered=info)
    return ProxyDecision(info, "ok", discovered=info)


def should_use_proxy(
    target_host: str | None, *, workspace_home: "str | os.PathLike", override: bool | None = None,
) -> ProxyInfo | None:
    """The ProxyInfo to route `target_host` through, or None to connect directly.

    `override`: False forces direct (CLI `--no-proxy`); None defers to the
    workspace's persisted toggle (`workspace.is_proxy_enabled`); True forces an
    attempt regardless of the toggle. Either way, a discovered-but-unreachable
    proxy (nothing listening on the port) or a target covered by the system
    exceptions list still resolves to None.

    Thin wrapper around `evaluate_proxy` for callers (tunnel.py, Neptune's
    direct-HTTPS path) that only need the routing verdict, not the reason.
    """
    return evaluate_proxy(target_host, workspace_home=workspace_home, override=override).proxy
