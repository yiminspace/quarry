"""Redis read-only guard — keyword blocklist + conditional (STORE/SET) writes.

Pure unit tests (no redis needed): they exercise `is_redis_read_only` directly.
"""

from __future__ import annotations

import pytest

from quarry.redis_engine import first_word, is_redis_read_only, parse_redis_url

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("cmd", [
    "GET k", "MGET a b", "SCAN 0", "TYPE k", "TTL k", "HGETALL h", "HGET h f",
    "LRANGE l 0 -1", "SMEMBERS s", "ZRANGE z 0 -1", "EXISTS k", "STRLEN k",
    "--scan", "SORT mylist ALPHA", "BITFIELD k GET u8 0", "GEORADIUS g 0 0 1 km",
    "GEOPOS g member", "OBJECT ENCODING k", "DBSIZE",
])
def test_reads_allowed(cmd):
    assert is_redis_read_only(cmd) is True


@pytest.mark.parametrize("cmd", [
    "SET k v", "SETEX k 10 v", "DEL k", "UNLINK k", "FLUSHALL", "FLUSHDB",
    "EXPIRE k 10", "PERSIST k", "RENAME a b", "INCR k", "HSET h f v", "HDEL h f",
    "LPUSH l v", "RPOP l", "SADD s m", "ZADD z 1 m", "RESTORE k 0 dump",
    "CONFIG SET x y", "SHUTDOWN", "SCRIPT LOAD x", "EVAL x 0", "DEBUG SLEEP 1",
    "GETEX k EX 10", "GETDEL k", "COPY a b",
])
def test_writes_blocked(cmd):
    assert is_redis_read_only(cmd) is False


@pytest.mark.parametrize("cmd", [
    "SORT mylist STORE dest",
    "SORT mylist BY weight_* STORE dest",
    "GEORADIUS g 0 0 1 km STORE dest",
    "GEORADIUSBYMEMBER g m 1 km STOREDIST dest",
    "BITFIELD k SET u8 0 255",
    "BITFIELD k INCRBY u8 0 10",
])
def test_conditional_writes_blocked(cmd):
    """Commands that read by default but mutate when a STORE/SET subtoken appears."""
    assert is_redis_read_only(cmd) is False


@pytest.mark.parametrize("cmd", [
    "SINTERSTORE dest a b", "SUNIONSTORE dest a b", "ZRANGESTORE dest src 0 -1",
    "ZUNIONSTORE dest 2 a b", "BITOP AND dest a b", "GEOSEARCHSTORE dest src",
    "LMPOP 2 a b LEFT", "BLPOP a 0", "ZMPOP 1 z MIN",
])
def test_store_and_pop_variants_blocked(cmd):
    assert is_redis_read_only(cmd) is False


def test_first_word_handles_flags():
    assert first_word("--scan --pattern x") == "scan"
    assert first_word("  GET  k ") == "get"
    assert first_word("") == ""


def test_parse_redis_url():
    cfg = parse_redis_url("redis://:pw@10.0.0.1:6380/3")
    assert cfg == {"host": "10.0.0.1", "port": 6380, "password": "pw", "db": "3"}


def test_parse_redis_url_defaults():
    cfg = parse_redis_url("redis://host/")
    assert cfg["host"] == "host" and cfg["port"] == 6379 and cfg["db"] == "0"
