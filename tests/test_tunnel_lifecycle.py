"""Process identity failures must never authorize reuse of a shared forward."""
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from quarry import tunnel


@pytest.mark.unit
@pytest.mark.parametrize("pid", [None, "invalid", 0, -1])
def test_invalid_owner_pid_never_runs_ps(monkeypatch, pid):
    run = Mock(side_effect=AssertionError("must not probe invalid PID"))
    monkeypatch.setattr(tunnel.subprocess, "run", run)
    assert tunnel._process_identity(pid) is None
    run.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired("ps", 2)])
def test_unavailable_identity_probe_rejects_live_port(monkeypatch, error):
    monkeypatch.setattr(tunnel.subprocess, "run", Mock(side_effect=error))
    monkeypatch.setattr(tunnel, "_port_open", lambda *args: True)
    assert not tunnel._entry_alive(12345, {"local_port": 1234, "owner_identity": "old",
                                         "ssh_identity": "old ssh", "ssh_pid": 12346})


@pytest.mark.unit
@pytest.mark.parametrize("code,text", [(1, "stale process output"), (0, " \n")])
def test_failed_or_empty_ps_output_is_not_an_identity(monkeypatch, code, text):
    monkeypatch.setattr(tunnel.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], code, text, ""))
    assert tunnel._process_identity(12345) is None


@pytest.mark.integration
def test_real_child_identity_disappears_after_reaping():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        identity = tunnel._process_identity(child.pid)
        assert identity and "time.sleep(30)" in identity
        assert tunnel._process_identity(child.pid) == identity
        assert tunnel._process_identity(os.getpid()) != identity
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert tunnel._process_identity(child.pid) is None


@pytest.mark.unit
def test_shutdown_reaps_child_that_ignores_termination():
    process = Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired("ssh", 3), 0]
    tunnel._stop_process(tunnel._Tunnel(process, 1234))
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert process.wait.call_count == 2


@pytest.mark.unit
def test_pool_shutdown_during_setup_reaps_unpublished_forward(monkeypatch):
    from quarry.core import Connection
    child = Mock()
    child.wait.return_value = 0
    forward = tunnel._Tunnel(child, 1234)
    def finish_after_shutdown(*args, **kwargs):
        tunnel.close_all()
        return forward
    monkeypatch.setattr(tunnel.proxy_mod, "should_use_proxy", lambda *a, **kw: None)
    monkeypatch.setattr(tunnel, "_registry_attached_tunnel", lambda key: None)
    monkeypatch.setattr(tunnel, "_make_tunnel", finish_after_shutdown)
    conn = Connection(key="db", url="postgresql://user@db/app", ssh_host="bastion")
    with pytest.raises(RuntimeError, match="pool closed"):
        with tunnel.open_tunnel(conn, "postgres"):
            pytest.fail("a cancelled pool must not yield a forward")
    child.terminate.assert_called_once_with()
    child.wait.assert_called_once()
    assert not tunnel._POOL
    assert not tunnel._OWNED_REGISTRY_KEYS
