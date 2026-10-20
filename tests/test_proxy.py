"""Unit tests for quarry.proxy (issue #96).

Everything here is pure/mocked: scutil/env are monkeypatched, no real network
call ever happens except a loopback TCP connect against a socket this test
itself opens (for the port-probe tests).
"""

from __future__ import annotations

import socket
import subprocess

import pytest

from quarry import proxy, workspace


SCUTIL_OUTPUT_HTTPS = """\
<dictionary> {
  ExceptionsList : <array> {
    0 : 192.168.0.0/16
    1 : 10.0.0.0/8
    2 : *.local
    3 : localhost
  }
  FTPPassive : 1
  HTTPEnable : 0
  HTTPSEnable : 1
  HTTPSPort : 6152
  HTTPSProxy : 127.0.0.1
}
"""

SCUTIL_OUTPUT_HTTP_ONLY = """\
<dictionary> {
  HTTPEnable : 1
  HTTPPort : 8080
  HTTPProxy : 10.1.1.1
  HTTPSEnable : 0
}
"""

SCUTIL_OUTPUT_DISABLED = """\
<dictionary> {
  HTTPEnable : 0
  HTTPSEnable : 0
}
"""


# ---------------------------------------------------------------------------
# _parse_scutil_proxy
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_parse_scutil_proxy_https_with_exceptions():
    info = proxy._parse_scutil_proxy(SCUTIL_OUTPUT_HTTPS)
    assert info is not None
    assert info.host == "127.0.0.1"
    assert info.port == 6152
    assert info.source == "system"
    assert info.exceptions == ["192.168.0.0/16", "10.0.0.0/8", "*.local", "localhost"]


@pytest.mark.unit
def test_parse_scutil_proxy_http_fallback_when_https_disabled():
    info = proxy._parse_scutil_proxy(SCUTIL_OUTPUT_HTTP_ONLY)
    assert info is not None
    assert info.host == "10.1.1.1"
    assert info.port == 8080
    assert info.exceptions == []


@pytest.mark.unit
def test_parse_scutil_proxy_both_disabled_returns_none():
    assert proxy._parse_scutil_proxy(SCUTIL_OUTPUT_DISABLED) is None


@pytest.mark.unit
def test_parse_scutil_proxy_malformed_port_skipped():
    out = "HTTPSEnable : 1\nHTTPSPort : not-a-number\nHTTPSProxy : 127.0.0.1\n"
    assert proxy._parse_scutil_proxy(out) is None


# ---------------------------------------------------------------------------
# _scutil_proxy / discover_proxy fallback chain
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_scutil_proxy_parses_subprocess_output(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd == ["scutil", "--proxy"]
        return subprocess.CompletedProcess(cmd, 0, stdout=SCUTIL_OUTPUT_HTTPS, stderr="")

    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    info = proxy._scutil_proxy()
    assert info is not None
    assert (info.host, info.port) == ("127.0.0.1", 6152)


@pytest.mark.unit
def test_scutil_proxy_missing_binary_returns_none(monkeypatch):
    def boom(cmd, **kwargs):
        raise FileNotFoundError("no scutil")

    monkeypatch.setattr(proxy.subprocess, "run", boom)
    assert proxy._scutil_proxy() is None


@pytest.mark.unit
def test_scutil_proxy_timeout_returns_none(monkeypatch):
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 3)

    monkeypatch.setattr(proxy.subprocess, "run", boom)
    assert proxy._scutil_proxy() is None


@pytest.mark.unit
def test_scutil_proxy_nonzero_exit_returns_none(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(proxy.subprocess, "run", fake_run)
    assert proxy._scutil_proxy() is None


@pytest.mark.unit
def test_env_proxy_reads_all_proxy(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.setenv("ALL_PROXY", "http://proxy.internal:3128")
    info = proxy._env_proxy()
    assert info is not None
    assert (info.host, info.port, info.source) == ("proxy.internal", 3128, "env")


@pytest.mark.unit
def test_env_proxy_reads_https_proxy_without_scheme(monkeypatch):
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "10.0.0.5:8888")
    info = proxy._env_proxy()
    assert info is not None
    assert (info.host, info.port) == ("10.0.0.5", 8888)


@pytest.mark.unit
def test_env_proxy_none_when_unset(monkeypatch):
    for var in ("ALL_PROXY", "HTTPS_PROXY", "all_proxy", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    assert proxy._env_proxy() is None


@pytest.mark.unit
def test_discover_proxy_darwin_prefers_scutil(monkeypatch):
    monkeypatch.setattr(proxy.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(proxy, "_scutil_proxy", lambda: proxy.ProxyInfo("1.2.3.4", 999, "system"))
    monkeypatch.setattr(proxy, "_env_proxy", lambda: proxy.ProxyInfo("9.9.9.9", 111, "env"))
    info = proxy.discover_proxy()
    assert (info.host, info.port, info.source) == ("1.2.3.4", 999, "system")


@pytest.mark.unit
def test_discover_proxy_darwin_falls_back_to_env_when_scutil_absent(monkeypatch):
    monkeypatch.setattr(proxy.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(proxy, "_scutil_proxy", lambda: None)
    monkeypatch.setattr(proxy, "_env_proxy", lambda: proxy.ProxyInfo("9.9.9.9", 111, "env"))
    info = proxy.discover_proxy()
    assert (info.host, info.port, info.source) == ("9.9.9.9", 111, "env")


@pytest.mark.unit
def test_discover_proxy_non_darwin_skips_scutil(monkeypatch):
    monkeypatch.setattr(proxy.platform, "system", lambda: "Linux")

    def boom():
        raise AssertionError("_scutil_proxy should not run on non-macOS")

    monkeypatch.setattr(proxy, "_scutil_proxy", boom)
    monkeypatch.setattr(proxy, "_env_proxy", lambda: None)
    assert proxy.discover_proxy() is None


@pytest.mark.unit
def test_discover_proxy_none_when_neither_present(monkeypatch):
    monkeypatch.setattr(proxy.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(proxy, "_scutil_proxy", lambda: None)
    monkeypatch.setattr(proxy, "_env_proxy", lambda: None)
    assert proxy.discover_proxy() is None


# ---------------------------------------------------------------------------
# exceptions-list matching
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_host_in_exceptions_loopback_always_exempt():
    assert proxy.host_in_exceptions("127.0.0.1", []) is True
    assert proxy.host_in_exceptions("localhost", []) is True
    assert proxy.host_in_exceptions("::1", []) is True


@pytest.mark.unit
def test_host_in_exceptions_cidr_match():
    assert proxy.host_in_exceptions("10.1.2.3", ["10.0.0.0/8"]) is True
    assert proxy.host_in_exceptions("192.168.1.1", ["10.0.0.0/8"]) is False


@pytest.mark.unit
def test_host_in_exceptions_wildcard_domain():
    assert proxy.host_in_exceptions("box.local", ["*.local"]) is True
    assert proxy.host_in_exceptions("local", ["*.local"]) is True
    assert proxy.host_in_exceptions("example.com", ["*.local"]) is False


@pytest.mark.unit
def test_host_in_exceptions_exact_hostname_match():
    assert proxy.host_in_exceptions("bastion.internal", ["bastion.internal"]) is True
    assert proxy.host_in_exceptions("other.internal", ["bastion.internal"]) is False


@pytest.mark.unit
def test_host_in_exceptions_no_match():
    assert proxy.host_in_exceptions("8.8.8.8", ["10.0.0.0/8", "*.local"]) is False


@pytest.mark.unit
def test_host_in_exceptions_empty_host_false():
    assert proxy.host_in_exceptions("", ["10.0.0.0/8"]) is False


# ---------------------------------------------------------------------------
# _port_listening
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_port_listening_true_for_real_open_socket():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert proxy._port_listening("127.0.0.1", port) is True
    finally:
        srv.close()


@pytest.mark.unit
def test_port_listening_false_for_nothing_bound():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()  # freed — nothing listens there now
    assert proxy._port_listening("127.0.0.1", port) is False


# ---------------------------------------------------------------------------
# should_use_proxy
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_should_use_proxy_override_false_forces_direct(monkeypatch):
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    monkeypatch.setattr(proxy, "discover_proxy", lambda: proxy.ProxyInfo("1.2.3.4", 80, "system"))
    assert proxy.should_use_proxy("target", workspace_home="/ws", override=False) is None


@pytest.mark.unit
def test_should_use_proxy_none_defers_to_disabled_toggle(monkeypatch):
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: False)
    assert proxy.should_use_proxy("target", workspace_home="/ws", override=None) is None


@pytest.mark.unit
def test_should_use_proxy_none_defers_to_enabled_toggle_and_uses_discovery(monkeypatch):
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    info = proxy.ProxyInfo("1.2.3.4", 80, "system")
    monkeypatch.setattr(proxy, "discover_proxy", lambda: info)
    monkeypatch.setattr(proxy, "_port_listening", lambda host, port, timeout=0.3: True)
    result = proxy.should_use_proxy("target", workspace_home="/ws", override=None)
    assert result is info


@pytest.mark.unit
def test_should_use_proxy_true_ignores_toggle(monkeypatch):
    calls = []
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: calls.append(home) or False)
    info = proxy.ProxyInfo("1.2.3.4", 80, "system")
    monkeypatch.setattr(proxy, "discover_proxy", lambda: info)
    monkeypatch.setattr(proxy, "_port_listening", lambda host, port, timeout=0.3: True)
    result = proxy.should_use_proxy("target", workspace_home="/ws", override=True)
    assert result is info
    assert calls == []  # toggle never even consulted when override=True


@pytest.mark.unit
def test_should_use_proxy_no_discovery_returns_none(monkeypatch):
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    monkeypatch.setattr(proxy, "discover_proxy", lambda: None)
    assert proxy.should_use_proxy("target", workspace_home="/ws", override=None) is None


@pytest.mark.unit
def test_should_use_proxy_target_in_exceptions_returns_none(monkeypatch):
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    info = proxy.ProxyInfo("1.2.3.4", 80, "system", exceptions=["10.0.0.0/8"])
    monkeypatch.setattr(proxy, "discover_proxy", lambda: info)
    assert proxy.should_use_proxy("10.1.2.3", workspace_home="/ws", override=None) is None


@pytest.mark.unit
def test_should_use_proxy_unreachable_port_returns_none(monkeypatch):
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    info = proxy.ProxyInfo("1.2.3.4", 80, "system")
    monkeypatch.setattr(proxy, "discover_proxy", lambda: info)
    monkeypatch.setattr(proxy, "_port_listening", lambda host, port, timeout=0.3: False)
    assert proxy.should_use_proxy("target", workspace_home="/ws", override=None) is None


# ---------------------------------------------------------------------------
# evaluate_proxy (issue #101) — same decision tree as should_use_proxy, but
# exposes *why* a target didn't get proxied.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_evaluate_proxy_override_false_reason():
    d = proxy.evaluate_proxy("target", workspace_home="/ws", override=False)
    assert d.proxy is None
    assert d.reason == "override_off"


@pytest.mark.unit
def test_evaluate_proxy_disabled_toggle_reason(monkeypatch):
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: False)
    d = proxy.evaluate_proxy("target", workspace_home="/ws", override=None)
    assert d.proxy is None
    assert d.reason == "disabled"


@pytest.mark.unit
def test_evaluate_proxy_not_discovered_reason(monkeypatch):
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    monkeypatch.setattr(proxy, "discover_proxy", lambda: None)
    d = proxy.evaluate_proxy("target", workspace_home="/ws", override=None)
    assert d.proxy is None
    assert d.reason == "not_discovered"
    assert d.discovered is None


@pytest.mark.unit
def test_evaluate_proxy_exception_list_reason(monkeypatch):
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    info = proxy.ProxyInfo("1.2.3.4", 80, "system", exceptions=["10.0.0.0/8"])
    monkeypatch.setattr(proxy, "discover_proxy", lambda: info)
    d = proxy.evaluate_proxy("10.1.2.3", workspace_home="/ws", override=None)
    assert d.proxy is None
    assert d.reason == "exception_list"
    assert d.discovered is info


@pytest.mark.unit
def test_evaluate_proxy_port_unreachable_reason(monkeypatch):
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    info = proxy.ProxyInfo("1.2.3.4", 80, "system")
    monkeypatch.setattr(proxy, "discover_proxy", lambda: info)
    monkeypatch.setattr(proxy, "_port_listening", lambda host, port, timeout=0.3: False)
    d = proxy.evaluate_proxy("target", workspace_home="/ws", override=None)
    assert d.proxy is None
    assert d.reason == "port_unreachable"
    assert d.discovered is info


@pytest.mark.unit
def test_evaluate_proxy_ok_reason(monkeypatch):
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    info = proxy.ProxyInfo("1.2.3.4", 80, "system")
    monkeypatch.setattr(proxy, "discover_proxy", lambda: info)
    monkeypatch.setattr(proxy, "_port_listening", lambda host, port, timeout=0.3: True)
    d = proxy.evaluate_proxy("target", workspace_home="/ws", override=None)
    assert d.proxy is info
    assert d.reason == "ok"
    assert d.discovered is info


@pytest.mark.unit
def test_evaluate_proxy_discovered_param_skips_discovery(monkeypatch):
    def boom():
        raise AssertionError("discover_proxy should not run when `discovered` is given")

    monkeypatch.setattr(proxy, "discover_proxy", boom)
    monkeypatch.setattr(workspace, "is_proxy_enabled", lambda home: True)
    monkeypatch.setattr(proxy, "_port_listening", lambda host, port, timeout=0.3: True)
    info = proxy.ProxyInfo("9.9.9.9", 1, "system")
    d = proxy.evaluate_proxy("target", workspace_home="/ws", override=None, discovered=info)
    assert d.proxy is info

    # discovered=None means "already looked, found nothing" — also skips discovery
    d2 = proxy.evaluate_proxy("target", workspace_home="/ws", override=None, discovered=None)
    assert d2.proxy is None
    assert d2.reason == "not_discovered"
