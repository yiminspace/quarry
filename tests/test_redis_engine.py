"""redis_engine execution paths, driven with a mocked `redis-cli` subprocess so
they run everywhere (no live Redis needed). The command-safety guard has its own
file (test_redis_safety.py); this one covers run/scan/inspect + cli resolution."""

from __future__ import annotations

import subprocess

import pytest

from quarry import redis_engine
from quarry.core import QuarryError

pytestmark = pytest.mark.unit

URL = "redis://127.0.0.1:6379/0"


def _proc(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


# ---- resolve_redis_cli ----

def test_resolve_redis_cli_found(monkeypatch):
    monkeypatch.setattr(redis_engine.shutil, "which", lambda c: "/usr/bin/redis-cli" if "redis" in c else None)
    assert redis_engine.resolve_redis_cli().endswith("redis-cli")


def test_resolve_redis_cli_missing_raises(monkeypatch):
    monkeypatch.setattr(redis_engine.shutil, "which", lambda c: None)
    monkeypatch.setattr(redis_engine.os.path, "exists", lambda p: False)
    with pytest.raises(QuarryError) as ei:
        redis_engine.resolve_redis_cli()
    assert "redis-cli not found" in str(ei.value)


# ---- run_redis ----

def test_run_redis_rows(monkeypatch):
    monkeypatch.setattr(redis_engine, "resolve_redis_cli", lambda: "redis-cli")
    monkeypatch.setattr(redis_engine.subprocess, "run", lambda *a, **k: _proc(stdout='["a","b","c"]\n'))
    rows, download_bytes = redis_engine.run_redis(URL, "LRANGE k 0 -1")
    assert rows == [{"value": "a"}, {"value": "b"}, {"value": "c"}]
    assert download_bytes == len('["a","b","c"]\n'.encode("utf-8"))


def test_run_redis_preserves_trailing_newlines(monkeypatch):
    monkeypatch.setattr(redis_engine, "resolve_redis_cli", lambda: "redis-cli")
    monkeypatch.setattr(redis_engine.subprocess, "run", lambda *a, **k: _proc(stdout='"x\\n\\n"\n'))
    rows, download_bytes = redis_engine.run_redis(URL, "GET k")
    assert rows == [{"value": "x\n\n"}]
    assert download_bytes == len('"x\\n\\n"\n'.encode("utf-8"))


def test_run_redis_error_returncode(monkeypatch):
    monkeypatch.setattr(redis_engine, "resolve_redis_cli", lambda: "redis-cli")
    monkeypatch.setattr(redis_engine.subprocess, "run", lambda *a, **k: _proc(stderr="WRONGTYPE", rc=1))
    with pytest.raises(QuarryError) as ei:
        redis_engine.run_redis(URL, "GET k")
    assert "redis error" in str(ei.value) and ei.value.exit_code == 3


def test_run_redis_error_on_stderr_even_rc0(monkeypatch):
    monkeypatch.setattr(redis_engine, "resolve_redis_cli", lambda: "redis-cli")
    monkeypatch.setattr(redis_engine.subprocess, "run", lambda *a, **k: _proc(stdout="", stderr="oops", rc=0))
    with pytest.raises(QuarryError):
        redis_engine.run_redis(URL, "GET k")


def test_run_redis_timeout(monkeypatch):
    monkeypatch.setattr(redis_engine, "resolve_redis_cli", lambda: "redis-cli")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="redis-cli", timeout=1)
    monkeypatch.setattr(redis_engine.subprocess, "run", boom)
    with pytest.raises(QuarryError) as ei:
        redis_engine.run_redis(URL, "GET k", timeout=1)
    assert "timed out" in str(ei.value) and ei.value.exit_code == 2


def test_run_redis_password_adds_auth_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(redis_engine, "resolve_redis_cli", lambda: "redis-cli")

    def capture(cmd, **k):
        seen["cmd"] = cmd
        return _proc(stdout='"PONG"')
    monkeypatch.setattr(redis_engine.subprocess, "run", capture)
    redis_engine.run_redis("redis://:secret@h:6379/2", "PING")
    assert "-a" in seen["cmd"] and "secret" in seen["cmd"] and "--no-auth-warning" in seen["cmd"]
    assert "-n" in seen["cmd"] and "2" in seen["cmd"]


def test_scan_mode_pages_json_and_keeps_empty_and_newline_keys(monkeypatch):
    monkeypatch.setattr(redis_engine, "resolve_redis_cli", lambda: "redis-cli")
    replies = ['["3",["a","line\\nbreak"]]', '["0",["", "two words"]]']
    commands = []
    monkeypatch.setattr(redis_engine.time, "sleep", lambda _: None)
    def capture(cmd, **kwargs):
        commands.append(cmd)
        return _proc(stdout=replies[len(commands)-1])
    monkeypatch.setattr(redis_engine.subprocess, "run", capture)
    rows, size = redis_engine.run_redis(URL, '--scan --pattern "two *" --count 1 --cursor 2 -i 0.01')
    assert rows == [{"value": k} for k in ['a', 'line\nbreak', '', 'two words']]
    assert size == sum(len(r.encode()) for r in replies)
    assert commands[0][-6:] == ['SCAN', '2', 'MATCH', 'two *', 'COUNT', '1']
    assert commands[1][-6:] == ['SCAN', '3', 'MATCH', 'two *', 'COUNT', '1']


@pytest.mark.parametrize('options', ['--pattern', '--eval script.lua', '--count 0', '--cursor -1', '-i nan', '-i -1', '--count x'])
def test_scan_mode_rejects_invalid_or_unrelated_options(options, monkeypatch):
    monkeypatch.setattr(redis_engine, 'resolve_redis_cli', lambda: pytest.fail('invalid scan reached transport'))
    with pytest.raises(QuarryError):
        redis_engine.run_redis(URL, '--scan ' + options)


def test_scan_mode_has_one_timeout_budget(monkeypatch):
    ticks = iter([0, 0.5, 1.1])
    monkeypatch.setattr(redis_engine.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(redis_engine, 'resolve_redis_cli', lambda: 'redis-cli')
    monkeypatch.setattr(redis_engine.subprocess, 'run', lambda *a, **k: _proc(stdout='["1",[]]'))
    with pytest.raises(QuarryError, match='timed out') as exc:
        redis_engine.run_redis(URL, '--scan', timeout=1)
    assert exc.value.exit_code == 2


# ---- scan_keys ----

def test_scan_keys_returns_and_caps(monkeypatch):
    monkeypatch.setattr(redis_engine, "resolve_redis_cli", lambda: "redis-cli")
    monkeypatch.setattr(redis_engine.subprocess, "run", lambda *a, **k: _proc(stdout="k1\nk2\nk3\n\n"))
    assert redis_engine.scan_keys(URL, count=2) == ["k1", "k2"]


def test_scan_keys_timeout_returns_empty(monkeypatch):
    monkeypatch.setattr(redis_engine, "resolve_redis_cli", lambda: "redis-cli")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="redis-cli", timeout=1)
    monkeypatch.setattr(redis_engine.subprocess, "run", boom)
    assert redis_engine.scan_keys(URL) == []


# ---- keys_with_meta ----

def test_keys_with_meta(monkeypatch):
    monkeypatch.setattr(redis_engine, "scan_keys", lambda url, **k: ["a", "b"])
    def fake_run(url, cmd, **k):
        import shlex
        args = shlex.split(cmd)
        assert args[0] == 'EVAL'
        assert args[2:] == ['2', 'a', 'b']
        assert k['timeout'] == 10
        return [{'value': ['string', '-1']}, {'value': ['hash', '30']}], 0
    monkeypatch.setattr(redis_engine, "run_redis", fake_run)
    out = redis_engine.keys_with_meta(URL)
    assert out == [{"key": "a", "type": "string", "ttl": -1},
                   {"key": "b", "type": "hash", "ttl": 30}]


def test_keys_with_meta_swallows_errors(monkeypatch):
    monkeypatch.setattr(redis_engine, "scan_keys", lambda url, **k: ["x"])

    def boom(*a, **k):
        raise QuarryError("boom")
    monkeypatch.setattr(redis_engine, "run_redis", boom)
    assert redis_engine.keys_with_meta(URL) == [{"key": "x", "type": "?", "ttl": -1}]


def test_metadata_batch_keeps_untrusted_keys_out_of_script(monkeypatch):
    import shlex
    keys = ['a b', "x'); redis.call('DEL','victim'); --", 'quote"key']
    monkeypatch.setattr(redis_engine, 'scan_keys', lambda *a, **k: keys)
    calls = []
    def run(url, command, **kwargs):
        args = shlex.split(command)
        calls.append(args)
        assert args[3:] == keys
        assert 'DEL' not in args[1]
        return [{'value': ['string', '-1']} for _ in keys], 0
    monkeypatch.setattr(redis_engine, 'run_redis', run)
    assert len(redis_engine.keys_with_meta(URL)) == 3
    assert len(calls) == 1


# ---- inspect_key ----

@pytest.mark.parametrize("ktype,reader_cmd", [
    ("string", "GET"), ("hash", "HGETALL"), ("list", "LRANGE"),
    ("set", "SMEMBERS"), ("zset", "ZRANGE"),
])
def test_inspect_key_dispatches_by_type(monkeypatch, ktype, reader_cmd):
    seen = []

    def fake_run(url, cmd, **k):
        seen.append(cmd)
        if cmd.startswith("TYPE"):
            return [{"value": ktype}], 0
        return [{"value": "v1"}, {"value": "v2"}], 0
    monkeypatch.setattr(redis_engine, "run_redis", fake_run)
    rows = redis_engine.inspect_key(URL, "mykey")
    assert seen[0].startswith("TYPE") and seen[1].startswith(reader_cmd)
    assert rows == [{"key": "mykey", "type": ktype, "value": "v1"},
                    {"key": "mykey", "type": ktype, "value": "v2"}]


def test_inspect_key_unsupported_type(monkeypatch):
    monkeypatch.setattr(redis_engine, "run_redis", lambda url, cmd, **k: ([{"value": "stream"}], 0))
    rows = redis_engine.inspect_key(URL, "s")
    assert rows == [{"key": "s", "type": "stream", "value": "(unsupported type)"}]


def test_inspect_key_missing(monkeypatch):
    # TYPE returns empty -> ktype defaults to "none" -> unsupported
    monkeypatch.setattr(redis_engine, "run_redis", lambda url, cmd, **k: ([], 0))
    rows = redis_engine.inspect_key(URL, "gone")
    assert rows[0]["type"] == "none"


def test_info_raw_text_is_preserved(monkeypatch):
    raw = '# Server\r\nredis_version:8.6.1\r\n\r\n'
    monkeypatch.setattr(redis_engine, 'resolve_redis_cli', lambda: 'redis-cli')
    monkeypatch.setattr(redis_engine.subprocess, 'run', lambda *a, **k: _proc(stdout=raw))
    rows, size = redis_engine.run_redis(URL, 'INFO server')
    assert rows == [{'value': raw}]
    assert size == len(raw.encode())


def test_malformed_other_command_still_fails(monkeypatch):
    monkeypatch.setattr(redis_engine, 'resolve_redis_cli', lambda: 'redis-cli')
    monkeypatch.setattr(redis_engine.subprocess, 'run', lambda *a, **k: _proc(stdout='not JSON'))
    with pytest.raises(QuarryError, match='invalid JSON for this command'):
        redis_engine.run_redis(URL, 'GET k')
