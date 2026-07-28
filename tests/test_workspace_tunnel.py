"""Unit tests for quarry.workspace + quarry.tunnel.

Everything here is pure/mocked — no real DB, no network, and no real ssh.
Config-touching tests always relocate QUARRY_CONFIG under tmp_path so the
user's real ~/.config/quarry/config.toml is never read or written. Tunnel
tests monkeypatch subprocess.Popen / socket / _wait_port; no ssh is spawned.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from quarry import proxy, tunnel, workspace


# ---------------------------------------------------------------------------
# workspace.py — config discovery
# ---------------------------------------------------------------------------

def _use_config(monkeypatch, tmp_path: Path) -> Path:
    """Point QUARRY_CONFIG at a throwaway file; return its path."""
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("QUARRY_CONFIG", str(cfg))
    return cfg


@pytest.mark.unit
def test_config_path_honours_env(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    assert workspace._config_path() == cfg


@pytest.mark.unit
def test_config_workspaces_absent_file(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)  # file does not exist yet
    assert workspace.config_workspaces() == []
    assert workspace._dirs_from_config() == []


@pytest.mark.unit
def test_config_workspaces_valid_list(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    a = tmp_path / "wsa"
    b = tmp_path / "wsb"
    a.mkdir()
    b.mkdir()
    cfg.write_text(
        f'workspaces = ["{a}", "{b}"]\n', encoding="utf-8"
    )
    # raw (as written)
    assert workspace.config_workspaces() == [str(a), str(b)]
    # resolved Paths
    dirs = workspace._dirs_from_config()
    assert dirs == [a.resolve(), b.resolve()]


@pytest.mark.unit
def test_config_workspaces_malformed_toml_returns_empty(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    cfg.write_text("workspaces = [this is not valid toml\n", encoding="utf-8")
    # no raise; both readers swallow the parse error
    assert workspace.config_workspaces() == []
    assert workspace._dirs_from_config() == []


@pytest.mark.unit
def test_config_workspaces_missing_key(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    cfg.write_text('other = "value"\n', encoding="utf-8")
    assert workspace.config_workspaces() == []
    assert workspace._dirs_from_config() == []


@pytest.mark.unit
def test_config_workspaces_blank_entries_filtered_in_dirs(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    cfg.write_text(f'workspaces = ["{real}", "", "   "]\n', encoding="utf-8")
    # config_workspaces keeps everything raw
    assert workspace.config_workspaces() == [str(real), "", "   "]
    # _dirs_from_config filters blank/whitespace-only entries
    assert workspace._dirs_from_config() == [real.resolve()]


@pytest.mark.unit
def test_config_workspaces_string_instead_of_list_iterates_chars(monkeypatch, tmp_path):
    """CURRENT (documented) behavior: a bare string for `workspaces` is iterated
    char-by-char rather than treated as one path. There is no guard against it."""
    cfg = _use_config(monkeypatch, tmp_path)
    cfg.write_text('workspaces = "abc"\n', encoding="utf-8")
    # config_workspaces yields each character as its own "path"
    assert workspace.config_workspaces() == ["a", "b", "c"]
    # _dirs_from_config likewise resolves each single char against cwd
    dirs = workspace._dirs_from_config()
    assert dirs == [
        Path("a").expanduser().resolve(),
        Path("b").expanduser().resolve(),
        Path("c").expanduser().resolve(),
    ]


# ---------------------------------------------------------------------------
# workspace.py — add / remove / round-trip
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_add_workspace_new_returns_true_and_writes(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    d = tmp_path / "proj"
    d.mkdir()
    added, path = workspace.add_workspace(str(d))
    assert added is True
    assert path == cfg
    assert cfg.exists()
    # round-trips through config_workspaces
    assert workspace.config_workspaces() == [str(d)]


@pytest.mark.unit
def test_add_workspace_duplicate_by_resolved_path_returns_false(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    d = tmp_path / "proj"
    d.mkdir()
    workspace.add_workspace(str(d))
    # add the same dir via a non-normalized path (trailing-dot component) -> resolves equal
    dup = str(d / ".")
    added, path = workspace.add_workspace(dup)
    assert added is False
    assert path == cfg
    # not appended a second time
    assert workspace.config_workspaces() == [str(d)]


@pytest.mark.unit
def test_remove_workspace_present_returns_true(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    workspace.add_workspace(str(a))
    workspace.add_workspace(str(b))
    assert workspace.remove_workspace(str(a)) is True
    assert workspace.config_workspaces() == [str(b)]


@pytest.mark.unit
def test_remove_workspace_absent_returns_false(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    a = tmp_path / "a"
    a.mkdir()
    workspace.add_workspace(str(a))
    absent = tmp_path / "nope"
    assert workspace.remove_workspace(str(absent)) is False
    # unchanged
    assert workspace.config_workspaces() == [str(a)]


@pytest.mark.unit
def test_remove_workspace_by_resolved_path(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    a = tmp_path / "a"
    a.mkdir()
    workspace.add_workspace(str(a))
    # remove via a non-normalized but resolved-equal path
    assert workspace.remove_workspace(str(a / ".")) is True
    assert workspace.config_workspaces() == []


@pytest.mark.unit
def test_write_config_workspaces_roundtrip_and_escaping(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    # include a path with a double-quote and a backslash to exercise escaping
    weird = str(tmp_path / 'we"ird\\path')
    normal = str(tmp_path / "n")
    written = workspace._write_config_workspaces([normal, weird])
    assert written.exists()
    # round-trips exactly (raw) through config_workspaces
    assert workspace.config_workspaces() == [normal, weird]


@pytest.mark.unit
def test_write_config_creates_parent_dir(monkeypatch, tmp_path):
    nested = tmp_path / "deep" / "nested" / "config.toml"
    monkeypatch.setenv("QUARRY_CONFIG", str(nested))
    workspace._write_config_workspaces([str(tmp_path / "x")])
    assert nested.exists()
    assert nested.parent.is_dir()


@pytest.mark.unit
def test_write_config_workspaces_preserves_unknown_keys(monkeypatch, tmp_path):
    """issue #96 prerequisite AC: extra keys in config.toml (anything besides
    `workspaces`) must survive a `workspace add/remove` — _write_config_workspaces
    must read-modify-write merge, not blindly rewrite from a hardcoded template."""
    cfg = _use_config(monkeypatch, tmp_path)
    cfg.write_text(
        'some_other_key = "keep-me"\n'
        'proxy_enabled_workspaces = ["/already/enabled"]\n'
        'workspaces = ["/old"]\n',
        encoding="utf-8",
    )
    d = str(tmp_path / "new")
    workspace.add_workspace(d)
    reloaded = workspace._read_config()
    assert reloaded["some_other_key"] == "keep-me"
    assert reloaded["proxy_enabled_workspaces"] == ["/already/enabled"]
    assert reloaded["workspaces"] == ["/old", d]

    workspace.remove_workspace("/old")
    reloaded = workspace._read_config()
    assert reloaded["some_other_key"] == "keep-me"
    assert reloaded["proxy_enabled_workspaces"] == ["/already/enabled"]
    assert reloaded["workspaces"] == [d]


@pytest.mark.unit
def test_write_config_preserves_section_table(monkeypatch, tmp_path):
    """PR #98 review (r1-1): a `[section]` table (or any other non-scalar/
    non-list construct) in config.toml must survive `workspace add/remove`
    and `qy proxy on/off` byte-for-byte, not be silently dropped."""
    cfg = _use_config(monkeypatch, tmp_path)
    cfg.write_text(
        'workspaces = ["/old"]\n'
        "\n"
        "[proxy]\n"
        'mode = "keep"\n',
        encoding="utf-8",
    )
    d = str(tmp_path / "new")
    workspace.add_workspace(d)
    text = cfg.read_text(encoding="utf-8")
    assert '[proxy]\nmode = "keep"' in text
    reloaded = workspace._read_config()
    assert reloaded["proxy"] == {"mode": "keep"}
    assert reloaded["workspaces"] == ["/old", d]

    workspace.set_proxy_enabled(d, True)
    text = cfg.read_text(encoding="utf-8")
    assert '[proxy]\nmode = "keep"' in text
    reloaded = workspace._read_config()
    assert reloaded["proxy"] == {"mode": "keep"}
    assert workspace.is_proxy_enabled(d)


# ---------------------------------------------------------------------------
# workspace.py — proxy toggle (issue #96)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_proxy_enabled_default_false(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    assert workspace.is_proxy_enabled(str(tmp_path / "ws")) is False
    assert workspace.proxy_enabled_workspaces() == []


@pytest.mark.unit
def test_set_proxy_enabled_roundtrip(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    workspace.set_proxy_enabled(str(ws), True)
    assert workspace.is_proxy_enabled(str(ws)) is True
    # resolved-path matching: relative/absolute variants of the same dir match
    assert workspace.is_proxy_enabled(str(ws) + os.sep) is True

    workspace.set_proxy_enabled(str(ws), False)
    assert workspace.is_proxy_enabled(str(ws)) is False


@pytest.mark.unit
def test_set_proxy_enabled_coexists_with_workspaces_key(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    d = str(tmp_path / "x")
    workspace.add_workspace(d)
    ws = tmp_path / "ws"
    workspace.set_proxy_enabled(str(ws), True)
    assert workspace.config_workspaces() == [d]
    assert workspace.is_proxy_enabled(str(ws)) is True


@pytest.mark.unit
def test_set_proxy_enabled_does_not_duplicate_on_repeat_toggle(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    workspace.set_proxy_enabled(str(ws), True)
    workspace.set_proxy_enabled(str(ws), True)
    assert workspace.proxy_enabled_workspaces().count(str(ws)) == 1


@pytest.mark.unit
def test_set_tunnel_keep_alive_roundtrip(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    workspace.set_tunnel_keep_alive(str(ws), True)
    assert workspace.is_tunnel_keep_alive_enabled(str(ws)) is True
    workspace.set_tunnel_keep_alive(str(ws), False)
    assert workspace.is_tunnel_keep_alive_enabled(str(ws)) is False


@pytest.mark.unit
def test_set_tunnel_reconnect_roundtrip(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    workspace.set_tunnel_reconnect(str(ws), True)
    assert workspace.is_tunnel_reconnect_enabled(str(ws)) is True
    workspace.set_tunnel_reconnect(str(ws), False)
    assert workspace.is_tunnel_reconnect_enabled(str(ws)) is False


# ---------------------------------------------------------------------------
# workspace.py — _split_dirs precedence + build_workspaces + configure
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_split_dirs_explicit_wins(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "fromcfg"
    cfg_dir.mkdir()
    cfg.write_text(f'workspaces = ["{cfg_dir}"]\n', encoding="utf-8")
    e1 = tmp_path / "e1"
    e2 = tmp_path / "e2"
    explicit = os.pathsep.join([str(e1), str(e2)])
    dirs = workspace._split_dirs(explicit)
    assert dirs == [e1.expanduser().resolve(), e2.expanduser().resolve()]


@pytest.mark.unit
def test_split_dirs_explicit_blank_falls_through_to_config(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "fromcfg"
    cfg_dir.mkdir()
    cfg.write_text(f'workspaces = ["{cfg_dir}"]\n', encoding="utf-8")
    # explicit is only separators/whitespace -> no parts -> config used
    dirs = workspace._split_dirs(os.pathsep + "   " + os.pathsep)
    assert dirs == [cfg_dir.resolve()]


@pytest.mark.unit
def test_split_dirs_config_used_when_no_explicit(monkeypatch, tmp_path):
    cfg = _use_config(monkeypatch, tmp_path)
    cfg_dir = tmp_path / "fromcfg"
    cfg_dir.mkdir()
    cfg.write_text(f'workspaces = ["{cfg_dir}"]\n', encoding="utf-8")
    assert workspace._split_dirs(None) == [cfg_dir.resolve()]


@pytest.mark.unit
def test_split_dirs_cwd_fallback(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)  # absent config -> []
    monkeypatch.chdir(tmp_path)
    dirs = workspace._split_dirs(None)
    assert dirs == [Path.cwd()]


@pytest.mark.unit
def test_build_workspaces_primary_paths_and_psql_default(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    monkeypatch.delenv("QUARRY_PSQL", raising=False)
    monkeypatch.delenv("QUARRY_CONNECTIONS_FILE", raising=False)
    monkeypatch.delenv("QUARRY_QUERIES_DIR", raising=False)
    d1 = tmp_path / "one"
    d2 = tmp_path / "two"
    explicit = os.pathsep.join([str(d1), str(d2)])
    wss = workspace.build_workspaces(explicit)
    assert len(wss) == 2
    w0 = wss[0]
    assert isinstance(w0, workspace.Workspace)
    assert w0.home == d1.resolve()
    assert w0.connections_file == d1.resolve() / "connections.toml"
    assert w0.queries_dir == d1.resolve() / "queries"
    assert w0.psql_bin == "psql"
    # second workspace uses its own dir for both
    assert wss[1].connections_file == d2.resolve() / "connections.toml"
    assert wss[1].queries_dir == d2.resolve() / "queries"


@pytest.mark.unit
def test_build_workspaces_env_overrides_only_primary(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    cfile = tmp_path / "custom_conns.toml"
    qdir = tmp_path / "custom_queries"
    monkeypatch.setenv("QUARRY_CONNECTIONS_FILE", str(cfile))
    monkeypatch.setenv("QUARRY_QUERIES_DIR", str(qdir))
    monkeypatch.setenv("QUARRY_PSQL", "/opt/custom/psql")
    d1 = tmp_path / "one"
    d2 = tmp_path / "two"
    explicit = os.pathsep.join([str(d1), str(d2)])
    wss = workspace.build_workspaces(explicit)
    # primary (i==0) picks up the overrides
    assert wss[0].connections_file == cfile.expanduser()
    assert wss[0].queries_dir == qdir.expanduser()
    assert wss[0].psql_bin == "/opt/custom/psql"
    # secondary (i==1) does NOT — falls back to its own dir
    assert wss[1].connections_file == d2.resolve() / "connections.toml"
    assert wss[1].queries_dir == d2.resolve() / "queries"
    # but psql_bin is shared (from env)
    assert wss[1].psql_bin == "/opt/custom/psql"


@pytest.mark.unit
def test_configure_workspace_rebinds_globals(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    d = tmp_path / "cfgd"
    d.mkdir()
    try:
        ws = workspace.configure_workspace(str(d))
        assert ws is workspace.WS
        assert workspace.WS.home == d.resolve()
        assert workspace.WS_LIST[0] is workspace.WS
        assert len(workspace.WS_LIST) == 1
    finally:
        # reset global state so we don't leak to other tests
        workspace.configure_workspace(None)


@pytest.mark.unit
def test_workspace_dataclass_fields():
    w = workspace.Workspace(
        home=Path("/h"),
        connections_file=Path("/h/connections.toml"),
        queries_dir=Path("/h/queries"),
        psql_bin="psql",
    )
    assert w.home == Path("/h")
    assert w.connections_file == Path("/h/connections.toml")
    assert w.queries_dir == Path("/h/queries")
    assert w.psql_bin == "psql"


# ---------------------------------------------------------------------------
# tunnel.py — pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "url,engine,expected",
    [
        ("postgresql://user@dbhost:6000/mydb", "postgres", ("dbhost", 6000)),
        ("postgresql://user@dbhost/mydb", "postgres", ("dbhost", 5432)),
        ("redis://cache:6380/0", "redis", ("cache", 6380)),
        ("redis://cache/0", "redis", ("cache", 6379)),
        ("mysql://u:p@sqlhost:3307/db", "mysql", ("sqlhost", 3307)),
        ("mysql://u:p@sqlhost/db", "mysql", ("sqlhost", 3306)),
        # scheme-less host:port/db -> urlparse via //-prefix
        ("host:5432/db", "postgres", ("host", 5432)),
        ("host/db", "postgres", ("host", 5432)),
    ],
)
def test_db_host_port(url, engine, expected):
    assert tunnel._db_host_port(url, engine) == expected


@pytest.mark.unit
def test_db_host_port_unknown_engine_defaults_5432():
    # known engine default without explicit port
    assert tunnel._db_host_port("neptune://gw/graph", "neptune") == ("gw", 8182)


@pytest.mark.unit
def test_db_host_port_empty_host_falls_back_to_localhost():
    # no hostname parsed -> 127.0.0.1
    host, port = tunnel._db_host_port("postgresql:///db", "postgres")
    assert host == "127.0.0.1"
    assert port == 5432


@pytest.mark.unit
def test_rewrite_url_hostport_user_and_pass():
    out = tunnel._rewrite_url_hostport(
        "postgresql://alice:secret@remote:5432/db", "127.0.0.1", 15000
    )
    assert out == "postgresql://alice:secret@127.0.0.1:15000/db"


@pytest.mark.unit
def test_rewrite_url_hostport_password_only_userinfo():
    # redis://:pw@host  -> username empty, password present
    out = tunnel._rewrite_url_hostport(
        "redis://:mypw@cache:6379/0", "127.0.0.1", 16000
    )
    assert out == "redis://:mypw@127.0.0.1:16000/0"


@pytest.mark.unit
def test_rewrite_url_hostport_user_only_userinfo():
    out = tunnel._rewrite_url_hostport(
        "postgresql://bob@remote:5432/db", "127.0.0.1", 17000
    )
    assert out == "postgresql://bob@127.0.0.1:17000/db"


@pytest.mark.unit
def test_rewrite_url_hostport_no_userinfo():
    out = tunnel._rewrite_url_hostport(
        "postgresql://remote:5432/db", "127.0.0.1", 18000
    )
    assert out == "postgresql://127.0.0.1:18000/db"


@pytest.mark.unit
def test_rewrite_url_hostport_preserves_path_query():
    out = tunnel._rewrite_url_hostport(
        "postgresql://u:p@remote:5432/db?sslmode=require", "127.0.0.1", 19000
    )
    assert out == "postgresql://u:p@127.0.0.1:19000/db?sslmode=require"


@pytest.mark.unit
def test_free_port_returns_int_in_range():
    p = tunnel._free_port()
    assert isinstance(p, int)
    assert 1 <= p <= 65535


# ---------------------------------------------------------------------------
# tunnel.py — _wait_port branches (mock socket / proc)
# ---------------------------------------------------------------------------

class _FakeProc:
    """Stand-in for subprocess.Popen. poll() returns _poll_value."""

    def __init__(self, poll_value=None):
        self._poll_value = poll_value
        self.terminated = False
        self.waited = False

    def poll(self):
        return self._poll_value

    def die(self, code=1):
        self._poll_value = code

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True
        return self._poll_value

    def communicate(self, timeout=None):
        return (b"", b"boom: connection refused")


@pytest.mark.unit
def test_wait_port_ready(monkeypatch):
    import contextlib as _ctx

    @_ctx.contextmanager
    def fake_create_connection(addr, timeout=None):
        yield object()

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    proc = _FakeProc(poll_value=None)  # alive
    assert tunnel._wait_port("127.0.0.1", 12345, proc, timeout=1.0) is True


@pytest.mark.unit
def test_wait_port_proc_exits_early(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("create_connection should not be called when proc is dead")

    monkeypatch.setattr(socket, "create_connection", boom)
    proc = _FakeProc(poll_value=1)  # already dead
    assert tunnel._wait_port("127.0.0.1", 12345, proc, timeout=1.0) is False


@pytest.mark.unit
def test_wait_port_timeout(monkeypatch):
    # connection always refused + proc stays alive -> loop until deadline -> False
    def refuse(*a, **k):
        raise OSError("refused")

    slept = []
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(tunnel.time, "sleep", lambda s: slept.append(s))
    proc = _FakeProc(poll_value=None)
    # tiny timeout so the monotonic deadline passes quickly
    assert tunnel._wait_port("127.0.0.1", 12345, proc, timeout=0.01) is False


# ---------------------------------------------------------------------------
# tunnel.py — open_tunnel / pool / close_all (mock Popen + _wait_port)
# ---------------------------------------------------------------------------

class _Conn:
    def __init__(self, url, ssh_host=None, ssh_user=None, ssh_key=None, ssh_port=None, source=None):
        self.url = url
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.ssh_key = ssh_key
        self.ssh_port = ssh_port
        self.source = source


@pytest.fixture(autouse=True)
def _clear_pool():
    """Never leak pooled fake procs across tests / to real close_all at exit."""
    tunnel._POOL.clear()
    yield
    tunnel._POOL.clear()


@pytest.mark.unit
def test_open_tunnel_no_ssh_host_yields_url_unchanged():
    conn = _Conn("postgresql://u@dbhost:5432/db")
    with tunnel.open_tunnel(conn, "postgres") as url:
        assert url == conn.url
    # nothing pooled
    assert tunnel._POOL == {}


@pytest.mark.unit
def test_open_tunnel_default_disabled_no_proxycommand_in_cmd(monkeypatch):
    """issue #96 AC: workspace proxy toggle off (default) -> ssh cmd has no
    ProxyCommand option at all."""
    fake = _FakeProc(poll_value=None)
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return fake

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54000)
    # should_use_proxy not mocked -> real one runs; with no config/env it returns None
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)

    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion", source="/no/such/ws")
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    cmd = popen_calls[0]
    assert not any("ProxyCommand" in str(c) for c in cmd)


@pytest.mark.unit
def test_open_tunnel_pool_key_differs_by_proxy_dimension(monkeypatch):
    """issue #96 AC: same bastion, proxy on vs off -> two separate ssh
    processes get spawned (no stale/wrong-mode reuse). issue #101 review: the
    *first* (now stale-dimension) tunnel is terminated and dropped from the
    pool as soon as the second one is established — see
    test_open_tunnel_proxy_dimension_change_terminates_stale_tunnel below for
    that cleanup behavior specifically."""
    made = []

    def fake_popen(cmd, **kwargs):
        p = _FakeProc(poll_value=None)
        made.append(p)
        return p

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54100)

    proxy_info = proxy.ProxyInfo(host="127.0.0.1", port=7890, source="system")
    results = iter([None, proxy_info])
    monkeypatch.setattr(tunnel.proxy_mod, "should_use_proxy", lambda *a, **k: next(results))

    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    assert len(made) == 2
    # issue #101: the stale (pre-toggle-flip) dimension was cleaned up, not
    # left running alongside the new one.
    assert len(tunnel._POOL) == 1


@pytest.mark.unit
def test_open_tunnel_proxy_dimension_change_terminates_stale_tunnel(monkeypatch):
    """issue #101 AC: same (ssh target, db target), proxy dimension flips ->
    the old tunnel's ssh process is terminated and it disappears from the pool
    as soon as the new one is up (not left as a zombie until process exit)."""
    made = []

    def fake_popen(cmd, **kwargs):
        p = _FakeProc(poll_value=None)
        made.append(p)
        return p

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54150)

    proxy_info = proxy.ProxyInfo(host="127.0.0.1", port=7890, source="system")
    results = iter([None, proxy_info])
    monkeypatch.setattr(tunnel.proxy_mod, "should_use_proxy", lambda *a, **k: next(results))

    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    first_proc = made[0]
    assert first_proc.terminated is False

    with tunnel.open_tunnel(conn, "postgres"):
        pass
    assert first_proc.terminated is True  # stale (direct) dimension torn down
    assert len(tunnel._POOL) == 1
    remaining = next(iter(tunnel._POOL.values()))
    assert remaining.proc is made[1]  # only the new (proxied) tunnel remains


@pytest.mark.unit
def test_open_tunnel_proxy_dimension_change_terminates_stale_tunnel_on_to_off(monkeypatch):
    """Same as test_open_tunnel_proxy_dimension_change_terminates_stale_tunnel but
    the flip runs the other direction (proxied first, then toggled off) —
    cleanup must not be one-directional."""
    made = []

    def fake_popen(cmd, **kwargs):
        p = _FakeProc(poll_value=None)
        made.append(p)
        return p

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54155)

    proxy_info = proxy.ProxyInfo(host="127.0.0.1", port=7890, source="system")
    results = iter([proxy_info, None])
    monkeypatch.setattr(tunnel.proxy_mod, "should_use_proxy", lambda *a, **k: next(results))

    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    first_proc = made[0]
    assert first_proc.terminated is False

    with tunnel.open_tunnel(conn, "postgres"):
        pass
    assert first_proc.terminated is True  # stale (proxied) dimension torn down
    assert len(tunnel._POOL) == 1
    remaining = next(iter(tunnel._POOL.values()))
    assert remaining.proc is made[1]  # only the new (direct) tunnel remains


@pytest.mark.unit
def test_open_tunnel_reused_tunnel_does_not_trigger_cleanup(monkeypatch):
    """Reusing an already-pooled, alive tunnel (no new dimension) must not
    scan for/terminate anything — cleanup only runs right after a fresh
    tunnel is actually established."""
    made = []

    def fake_popen(cmd, **kwargs):
        p = _FakeProc(poll_value=None)
        made.append(p)
        return p

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54160)

    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    assert len(made) == 1  # pooled + reused
    assert made[0].terminated is False
    assert len(tunnel._POOL) == 1


# ---------------------------------------------------------------------------
# tunnel.py — cross-process registry (issue #101 r1-1)
#
# `_POOL` only ever lives in the memory of the process that opened a given
# tunnel, so a separately-invoked `qy proxy` (its own fresh process, its own
# empty `_POOL`) needs another way to see a tunnel a long-running `qy
# gui`/MCP process is holding open. These tests exercise the on-disk
# registry that makes that possible, independent of the real ssh/subprocess
# plumbing above.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_open_tunnel_registers_new_tunnel_in_cross_process_registry(monkeypatch):
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda cmd, **k: _FakeProc(poll_value=None))
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54170)
    monkeypatch.setattr(tunnel.proxy_mod, "should_use_proxy", lambda *a, **k: None)

    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    with tunnel.open_tunnel(conn, "postgres"):
        pass

    registry = tunnel._load_registry()
    assert len(registry) == 1
    procs = next(iter(registry.values()))
    assert list(procs) == [tunnel._own_pid()]  # nested under this process's own pid (issue #101 r2-1)
    entry = next(iter(procs.values()))
    assert entry == {
        "ssh_target": "root@bastion:22",
        "db_target": "remote-db:5432",
        "local_port": 54170,
        "proxied": False,
        "proxy": None,
    }


@pytest.mark.unit
def test_open_tunnel_dimension_change_removes_stale_registry_entry(monkeypatch):
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda cmd, **k: _FakeProc(poll_value=None))
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54171)
    proxy_info = proxy.ProxyInfo(host="127.0.0.1", port=7890, source="system")
    results = iter([None, proxy_info])
    monkeypatch.setattr(tunnel.proxy_mod, "should_use_proxy", lambda *a, **k: next(results))

    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    with tunnel.open_tunnel(conn, "postgres"):
        pass

    registry = tunnel._load_registry()
    assert len(registry) == 1  # the stale (direct) dimension's entry is gone
    procs = next(iter(registry.values()))
    entry = next(iter(procs.values()))
    assert entry["proxied"] is True


@pytest.mark.unit
def test_close_all_removes_only_this_process_owned_registry_entries(monkeypatch):
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda cmd, **k: _FakeProc(poll_value=None))
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54172)
    monkeypatch.setattr(tunnel.proxy_mod, "should_use_proxy", lambda *a, **k: None)

    # An entry as if written by a different, still-running process.
    foreign = {"other@bastion-z:22|-|db-z:5432|-": {"424242": {
        "ssh_target": "other@bastion-z:22", "db_target": "db-z:5432",
        "local_port": 9999, "proxied": False, "proxy": None,
    }}}
    tunnel._save_registry(foreign)

    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    assert len(tunnel._load_registry()) == 2

    tunnel.close_all()
    registry = tunnel._load_registry()
    assert len(registry) == 1  # only this process's own entry was removed
    procs = next(iter(registry.values()))
    assert procs["424242"]["ssh_target"] == "other@bastion-z:22"


@pytest.mark.unit
def test_close_all_does_not_delete_another_processs_live_entry_for_the_same_target(monkeypatch):
    """issue #101 r2-1: two processes tunneling to the *identical* (ssh
    target, db target, proxy dimension) share one registry key (`rkey`). If
    process A's `close_all()` blindly dropped that whole key, it would erase
    process B's still-live tunnel too — exactly the class of bug the registry
    was introduced to fix in the first place. Entries must be nested per-pid
    so each process only ever touches its own slot."""
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda cmd, **k: _FakeProc(poll_value=None))
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54173)
    monkeypatch.setattr(tunnel.proxy_mod, "should_use_proxy", lambda *a, **k: None)

    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    with tunnel.open_tunnel(conn, "postgres"):
        pass

    rkey = next(iter(tunnel._load_registry()))
    # Simulate a second, independent process registering a live tunnel for
    # the exact same rkey (same ssh/db/proxy dimension), under a different pid.
    registry = tunnel._load_registry()
    registry[rkey]["999999"] = {
        "ssh_target": "root@bastion:22",
        "db_target": "remote-db:5432",
        "local_port": 54174,
        "proxied": False,
        "proxy": None,
    }
    tunnel._save_registry(registry)

    tunnel.close_all()
    registry = tunnel._load_registry()
    assert list(registry) == [rkey]  # the shared rkey survives
    procs = registry[rkey]
    assert list(procs) == ["999999"]  # only this process's own pid-slot is gone
    assert procs["999999"]["local_port"] == 54174


# ---------------------------------------------------------------------------
# tunnel.py — list_tunnels() (issue #101)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_list_tunnels_empty_pool():
    assert tunnel.list_tunnels() == []


@pytest.mark.unit
def test_list_tunnels_reports_direct_and_proxied_entries():
    direct_key = ("bastion-a", 22, "root", "", "db-a", 5432, None)
    proxy_key = ("bastion-b", 22, "deploy", "", "db-b", 3306, ("127.0.0.1", 7890))
    tunnel._POOL[direct_key] = tunnel._Tunnel(_FakeProc(poll_value=None), 15000)
    tunnel._POOL[proxy_key] = tunnel._Tunnel(_FakeProc(poll_value=1), 15001)  # dead

    items = {i["local_port"]: i for i in tunnel.list_tunnels()}
    assert items[15000] == {
        "ssh_target": "root@bastion-a:22",
        "db_target": "db-a:5432",
        "local_port": 15000,
        "proxied": False,
        "proxy": None,
        "alive": True,
    }
    assert items[15001] == {
        "ssh_target": "deploy@bastion-b:22",
        "db_target": "db-b:3306",
        "local_port": 15001,
        "proxied": True,
        "proxy": "127.0.0.1:7890",
        "alive": False,
    }


@pytest.mark.unit
def test_list_tunnels_includes_live_registry_entries_from_other_processes():
    """issue #101 r1-1: a `qy proxy` invocation has an empty `_POOL` of its
    own, so a tunnel that only exists in the on-disk registry (as written by
    a different, long-running process) must still be reported — otherwise
    `qy proxy` always shows an empty list while queries are actively
    flowing through a `qy gui`/MCP process's tunnels."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        tunnel._save_registry({"other@bastion-x:22|-|db-x:5432|-": {"424242": {
            "ssh_target": "other@bastion-x:22", "db_target": "db-x:5432",
            "local_port": port, "proxied": False, "proxy": None,
        }}})
        items = {i["local_port"]: i for i in tunnel.list_tunnels()}
        assert items[port] == {
            "ssh_target": "other@bastion-x:22",
            "db_target": "db-x:5432",
            "local_port": port,
            "proxied": False,
            "proxy": None,
            "alive": True,
        }
    finally:
        srv.close()


@pytest.mark.unit
def test_list_tunnels_prunes_dead_registry_entries():
    """A registry entry for a process that has since exited (nothing
    listening on its recorded local port anymore) is garbage-collected as
    soon as anyone reads the registry, instead of accumulating forever."""
    dead_port = tunnel._free_port()  # freed immediately, nothing bound there
    tunnel._save_registry({"ghost@bastion-y:22|-|db-y:5432|-": {"424242": {
        "ssh_target": "ghost@bastion-y:22", "db_target": "db-y:5432",
        "local_port": dead_port, "proxied": False, "proxy": None,
    }}})
    assert tunnel.list_tunnels() == []
    assert tunnel._load_registry() == {}


# ---------------------------------------------------------------------------
# tunnel.py — tunnel_fact_for() (issue #101 r1-2)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_tunnel_fact_for_no_ssh_host_returns_none():
    conn = _Conn("postgresql://u@dbhost:5432/db")
    assert tunnel.tunnel_fact_for(conn, "postgres") is None


@pytest.mark.unit
def test_tunnel_fact_for_no_live_tunnel_returns_none():
    """Nothing has ever been queried for this connection yet (or the old
    tunnel from a proxy-toggle flip was already torn down and the
    replacement not yet created) — there's no fact to report, so the GUI
    badge must stay off rather than guessing."""
    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    assert tunnel.tunnel_fact_for(conn, "postgres") is None


@pytest.mark.unit
def test_tunnel_fact_for_matches_live_pooled_tunnel():
    key = ("bastion", 22, "root", "", "remote-db", 5432, ("127.0.0.1", 7890))
    tunnel._POOL[key] = tunnel._Tunnel(_FakeProc(poll_value=None), 15002)
    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    fact = tunnel.tunnel_fact_for(conn, "postgres")
    assert fact["proxied"] is True
    assert fact["alive"] is True
    assert fact["local_port"] == 15002


@pytest.mark.unit
def test_open_tunnel_proxy_enabled_injects_proxycommand(monkeypatch):
    fake = _FakeProc(poll_value=None)
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return fake

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54200)

    proxy_info = proxy.ProxyInfo(host="127.0.0.1", port=7890, source="system")
    monkeypatch.setattr(tunnel.proxy_mod, "should_use_proxy", lambda *a, **k: proxy_info)

    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    cmd = popen_calls[0]
    assert any("ProxyCommand=" in str(c) and "quarry.proxycommand" in str(c) for c in cmd)
    assert any("127.0.0.1 7890" in str(c) for c in cmd)


@pytest.mark.unit
def test_open_tunnel_proxy_enabled_but_port_unreachable_falls_back_direct(monkeypatch, tmp_path):
    """issue #96 AC: workspace proxy enabled but nothing listens on the proxy
    port -> should_use_proxy's real port-probe returns None -> falls back to a
    direct connection, no error, no ProxyCommand."""
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("QUARRY_CONFIG", str(cfg))
    ws_home = str(tmp_path / "ws")
    workspace.set_proxy_enabled(ws_home, True)

    # discover a proxy pointing at a port nothing listens on
    unreachable_port = tunnel._free_port()
    monkeypatch.setattr(
        proxy, "discover_proxy",
        lambda: proxy.ProxyInfo(host="127.0.0.1", port=unreachable_port, source="system"),
    )

    fake = _FakeProc(poll_value=None)
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return fake

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54300)

    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion", source=ws_home)
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    cmd = popen_calls[0]
    assert not any("ProxyCommand" in str(c) for c in cmd)


@pytest.mark.unit
def test_open_tunnel_with_ssh_rewrites_and_pools(monkeypatch):
    fake = _FakeProc(poll_value=None)  # alive
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return fake

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 54321)

    conn = _Conn(
        "postgresql://alice:pw@remote-db:5432/app",
        ssh_host="bastion.example.com",
        ssh_user="deploy",
    )
    with tunnel.open_tunnel(conn, "postgres") as url:
        assert url == "postgresql://alice:pw@127.0.0.1:54321/app"

    # exactly one tunnel spawned + pooled
    assert len(popen_calls) == 1
    assert len(tunnel._POOL) == 1
    # command targets the right bastion + forward
    cmd = popen_calls[0]
    assert cmd[0] == "ssh"
    assert "-L" in cmd
    li = cmd.index("-L")
    assert cmd[li + 1] == "127.0.0.1:54321:remote-db:5432"
    assert "deploy@bastion.example.com" in cmd


@pytest.mark.unit
def test_open_tunnel_pool_reuse_second_call(monkeypatch):
    fake = _FakeProc(poll_value=None)
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return fake

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 44444)

    conn = _Conn(
        "postgresql://u@remote-db:5432/app",
        ssh_host="bastion",
        ssh_user="root",
    )
    with tunnel.open_tunnel(conn, "postgres") as url1:
        pass
    with tunnel.open_tunnel(conn, "postgres") as url2:
        pass
    assert url1 == url2
    # only spawned once — pool reuse on the second call
    assert len(popen_calls) == 1
    assert len(tunnel._POOL) == 1


@pytest.mark.unit
def test_open_tunnel_dead_pooled_proc_respawns(monkeypatch):
    procs = [_FakeProc(poll_value=1), _FakeProc(poll_value=None)]  # 1st dead, 2nd alive
    made = []

    def fake_popen(cmd, **kwargs):
        p = procs[len(made)]
        made.append(p)
        return p

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 33333)

    conn = _Conn("postgresql://u@remote:5432/app", ssh_host="host")
    # First call: pool a proc that is already 'dead' (poll->1)
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    # Second call: pooled proc not alive -> discarded + a fresh one spawned
    with tunnel.open_tunnel(conn, "postgres"):
        pass
    assert len(made) == 2


@pytest.mark.unit
def test_open_tunnel_failed_tunnel_raises(monkeypatch):
    from quarry.core import QuarryError

    dead = _FakeProc(poll_value=1)  # ssh dies immediately

    def fake_popen(cmd, **kwargs):
        return dead

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 22222)
    # do not patch _wait_port -> real one runs; proc.poll()->1 so it returns False fast

    conn = _Conn("postgresql://u@remote:5432/app", ssh_host="host")
    with pytest.raises(QuarryError) as ei:
        with tunnel.open_tunnel(conn, "postgres"):
            pass
    assert "ssh tunnel to host failed" in str(ei.value)
    assert "connection refused" in str(ei.value)
    assert dead.terminated is True
    # failed tunnel is not pooled
    assert tunnel._POOL == {}


@pytest.mark.unit
def test_make_tunnel_missing_ssh_key_raises(monkeypatch, tmp_path):
    from quarry.core import EXIT_CONNECTION_ERROR, QuarryError

    # Popen must never be reached — the missing key errors first.
    def boom_popen(*a, **k):
        raise AssertionError("Popen must not run when the ssh key is missing")

    monkeypatch.setattr(tunnel.subprocess, "Popen", boom_popen)
    missing_key = tmp_path / "no_such_key"
    conn = _Conn(
        "postgresql://u@remote:5432/app",
        ssh_host="host",
        ssh_key=str(missing_key),
    )
    with pytest.raises(QuarryError) as ei:
        tunnel._make_tunnel(conn, "remote", 5432)
    assert "ssh key not found" in str(ei.value)
    assert ei.value.exit_code == EXIT_CONNECTION_ERROR


@pytest.mark.unit
def test_make_tunnel_uses_key_and_port_in_cmd(monkeypatch, tmp_path):
    key = tmp_path / "id_bastion"
    key.write_text("KEY", encoding="utf-8")
    fake = _FakeProc(poll_value=None)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return fake

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: True)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 45678)

    conn = _Conn(
        "postgresql://u@remote:5432/app",
        ssh_host="bastion",
        ssh_user="ops",
        ssh_key=str(key),
        ssh_port=2222,
    )
    t = tunnel._make_tunnel(conn, "remote", 5432)
    assert t.local_port == 45678
    assert t.alive() is True
    cmd = captured["cmd"]
    assert "-i" in cmd and cmd[cmd.index("-i") + 1] == str(key)
    assert "-p" in cmd and cmd[cmd.index("-p") + 1] == "2222"
    assert "ops@bastion" in cmd


@pytest.mark.unit
def test_tunnel_alive_reflects_poll():
    alive = tunnel._Tunnel(_FakeProc(poll_value=None), 5000)
    dead = tunnel._Tunnel(_FakeProc(poll_value=0), 5001)
    assert alive.alive() is True
    assert dead.alive() is False


@pytest.mark.unit
def test_open_tunnel_attaches_to_live_registry_entry_without_spawning_ssh(monkeypatch):
    """issue #112: when another process already owns a live tunnel for the same
    key, this process should attach to that local forward rather than opening a
    second ssh process."""
    popen_calls = []
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda cmd, **k: popen_calls.append(cmd))
    monkeypatch.setattr(tunnel.proxy_mod, "should_use_proxy", lambda *a, **k: None)
    monkeypatch.setattr(tunnel, "_port_open", lambda host, port: port == 55123)
    conn = _Conn("postgresql://u@remote-db:5432/app", ssh_host="bastion")
    db_host, db_port = tunnel._db_host_port(conn.url, "postgres")
    key = (conn.ssh_host, 22, "root", "", db_host, db_port, None)
    rkey = tunnel._registry_key(key)
    tunnel._save_registry({rkey: {"999999": {
        "ssh_target": "root@bastion:22",
        "db_target": "remote-db:5432",
        "local_port": 55123,
        "proxied": False,
        "proxy": None,
    }}})

    with tunnel.open_tunnel(conn, "postgres") as url:
        assert "127.0.0.1:55123" in url
    assert popen_calls == []
    pooled = next(iter(tunnel._POOL.values()))
    assert pooled.proc is None
    assert pooled.attached is True


@pytest.mark.unit
def test_close_all_terminates_pooled_and_clears(monkeypatch):
    p1 = _FakeProc(poll_value=None)
    p2 = _FakeProc(poll_value=None)
    tunnel._POOL[("k1",)] = tunnel._Tunnel(p1, 5000)
    tunnel._POOL[("k2",)] = tunnel._Tunnel(p2, 5001)
    tunnel.close_all()
    assert p1.terminated is True
    assert p2.terminated is True
    assert tunnel._POOL == {}


@pytest.mark.unit
def test_close_all_swallows_terminate_errors():
    class _BadProc(_FakeProc):
        def terminate(self):
            raise RuntimeError("already gone")

    tunnel._POOL[("k",)] = tunnel._Tunnel(_BadProc(), 5000)
    # must not raise
    tunnel.close_all()
    assert tunnel._POOL == {}


@pytest.mark.unit
def test_make_tunnel_wait_fail_surfaces_stderr(monkeypatch):
    """_wait_port False -> terminate + communicate stderr surfaced in the error."""
    from quarry.core import QuarryError

    fake = _FakeProc(poll_value=None)  # 'alive' at spawn but port never opens

    def fake_popen(cmd, **kwargs):
        return fake

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: False)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 11111)

    conn = _Conn("postgresql://u@remote:5432/app", ssh_host="host")
    with pytest.raises(QuarryError) as ei:
        tunnel._make_tunnel(conn, "remote", 5432)
    # stderr from communicate() bubbles up
    assert "connection refused" in str(ei.value)
    assert fake.terminated is True


@pytest.mark.unit
def test_make_tunnel_wait_fail_cleanup_swallows_exception(monkeypatch):
    """When _wait_port fails and terminate()/communicate() themselves raise, the
    cleanup except-block swallows it and we fall back to the generic detail."""
    from quarry.core import QuarryError

    class _BadCleanupProc(_FakeProc):
        def terminate(self):
            raise RuntimeError("cannot terminate")

        def communicate(self, timeout=None):
            raise RuntimeError("cannot communicate")

    bad = _BadCleanupProc(poll_value=None)
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda cmd, **k: bad)
    monkeypatch.setattr(tunnel, "_wait_port", lambda *a, **k: False)
    monkeypatch.setattr(tunnel, "_free_port", lambda: 10101)

    conn = _Conn("postgresql://u@remote:5432/app", ssh_host="host")
    with pytest.raises(QuarryError) as ei:
        tunnel._make_tunnel(conn, "remote", 5432)
    # no stderr captured -> generic fallback detail
    assert "port not ready / timeout" in str(ei.value)
