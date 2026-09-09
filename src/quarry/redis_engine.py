"""Redis engine — talks to Redis via the system `redis-cli` (zero-dependency).

A redis "query" is a redis command string (e.g. `GET foo`, `SCAN 0 COUNT 50`,
`HGETALL bar`). Results map to single-column rows ({"value": ...}) so they flow
through the same QueryResult contract / GUI grid as SQL engines.
"""

from __future__ import annotations

import os
import json
import shlex
import shutil
import subprocess
import time
import math
from typing import Any
from urllib.parse import unquote, urlparse

# Commands that mutate state — blocked unless allow_write=True.
_REDIS_WRITE = {
    "set", "setnx", "setex", "psetex", "mset", "msetnx", "append", "getset", "getdel", "getex",
    "del", "unlink", "expire", "pexpire", "expireat", "pexpireat", "persist", "rename", "renamenx",
    "incr", "decr", "incrby", "decrby", "incrbyfloat",
    "hset", "hsetnx", "hmset", "hincrby", "hincrbyfloat", "hdel",
    "hexpire", "hpexpire", "hexpireat", "hpexpireat", "hpersist", "hgetex", "hgetdel", "hsetex",
    "lpush", "rpush", "lpushx", "rpushx", "lpop", "rpop", "lset", "linsert", "lrem", "ltrim",
    "lmove", "blmove", "rpoplpush", "brpoplpush", "lmpop", "blmpop", "blpop", "brpop",
    "sadd", "srem", "spop", "smove", "sinterstore", "sunionstore", "sdiffstore",
    "zadd", "zincrby", "zrem", "zremrangebyrank", "zremrangebyscore", "zremrangebylex",
    "zpopmin", "zpopmax", "bzpopmin", "bzpopmax", "zmpop", "bzmpop",
    "zrangestore", "zdiffstore", "zinterstore", "zunionstore",
    "flushdb", "flushall", "swapdb", "move", "restore", "copy",
    "setbit", "setrange", "bitop", "pfadd", "pfmerge", "pfdebug", "geoadd", "geosearchstore",
    "xadd", "xdel", "xtrim", "xsetid", "xack", "xclaim", "xautoclaim", "xgroup", "xreadgroup",
    "config", "save", "bgsave", "bgrewriteaof", "shutdown", "slaveof", "replicaof", "failover",
    "subscribe", "publish", "spublish", "psubscribe", "monitor", "debug", "reset",
    "script", "eval", "evalsha", "eval_ro", "evalsha_ro", "fcall", "fcall_ro", "function",
    "acl", "client", "cluster", "slowlog", "latency", "flushslots",
}

# Commands that are reads by default but become writes when a subtoken appears.
_REDIS_COND_WRITE = {
    "sort": {"store"},
    "georadius": {"store", "storedist"},
    "georadiusbymember": {"store", "storedist"},
    "bitfield": {"set", "incrby", "overflow"},
    "memory": {"purge"},
}

# Unknown/module commands are not implicitly trusted as reads. New commands
# require an explicit classification before agents may use them read-only.
_REDIS_READ = set("""
get mget getrange strlen exists type ttl pttl expiretime pexpiretime
hget hmget hgetall hexists hkeys hvals hlen hstrlen hscan httl hpttl hexpiretime hpexpiretime hrandfield
lindex llen lrange lpos scard sismember smismember smembers srandmember sscan sinter sintercard sunion sdiff
zcard zcount zlexcount zrange zrangebyscore zrangebylex zrevrange zrevrangebyscore zrevrangebylex
zrank zrevrank zscore zmscore zscan zrandmember zdiff zinter zintercard zunion
getbit bitcount bitpos bitfield bitfield_ro pfcount geodist geohash geopos georadius georadiusbymember geosearch
xlen xrange xrevrange xread xinfo scan keys randomkey dbsize ping echo time info role command object memory pubsub dump sort sort_ro
""".split())


def resolve_redis_cli() -> str:
    for cand in (os.environ.get("QUARRY_REDIS_CLI", "redis-cli"),
                 "/opt/homebrew/bin/redis-cli", "/usr/local/bin/redis-cli"):
        if shutil.which(cand) or os.path.exists(cand):
            return cand
    from .core import EXIT_CONNECTION_ERROR, QuarryError
    raise QuarryError("redis-cli not found (set QUARRY_REDIS_CLI or install redis)",
                      exit_code=EXIT_CONNECTION_ERROR)


def parse_redis_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    db = parsed.path.lstrip("/") or "0"
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 6379,
        "password": unquote(parsed.password) if parsed.password else None,
        "db": db,
    }


def first_word(command: str) -> str:
    s = command.strip()
    if s.startswith("--"):  # e.g. `--scan`
        return s.split()[0].lstrip("-").lower()
    return (s.split() or [""])[0].lower()


def is_redis_read_only(command: str) -> bool:
    cmd = first_word(command)
    if cmd in _REDIS_WRITE:
        return False
    cond = _REDIS_COND_WRITE.get(cmd)
    if cond:
        try:
            rest = {a.lower() for a in shlex.split(command)[1:]}
        except ValueError:
            rest = {a.lower() for a in command.split()[1:]}
        if rest & cond:
            return False
    return cmd in _REDIS_READ


def _cli_base(url: str) -> list[str]:
    cfg = parse_redis_url(url)
    cmd = [resolve_redis_cli(), "-h", cfg["host"], "-p", str(cfg["port"]), "-n", str(cfg["db"])]
    if cfg["password"]:
        cmd += ["-a", cfg["password"], "--no-auth-warning"]
    return cmd


def run_redis(url: str, command: str, *, timeout: int = 30) -> tuple[list[dict[str, Any]], int]:
    """Run a redis command, return (rows, download_bytes). SCAN/KEYS/LRANGE/etc
    → one row per element. download_bytes (issue #104) is redis-cli's stdout
    text, UTF-8 encoded — an approximation of the real wire-protocol payload,
    since redis-cli doesn't expose that."""
    argv = shlex.split(command)
    if argv and argv[0] == "--scan":
        return _run_scan(url, argv[1:], timeout=timeout)
    # RESP-aware output distinguishes nil, an empty string and embedded
    # newlines. -e makes server errors fail instead of becoming data rows.
    cmd = [arg for arg in _cli_base(url) if arg != "--raw"] + ["--json", "-e"] + argv
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        from .core import EXIT_CONNECTION_ERROR, QuarryError, _with_timeout_hint
        raise QuarryError(_with_timeout_hint(f"redis-cli timed out after {timeout}s"),
                          exit_code=EXIT_CONNECTION_ERROR)
    if proc.returncode != 0 or proc.stderr.strip():
        from .core import EXIT_SQL_ERROR, QuarryError
        raise QuarryError(f"redis error: {proc.stderr.strip() or proc.stdout.strip()}", exit_code=EXIT_SQL_ERROR)
    try:
        value = json.loads(proc.stdout, parse_int=str, parse_float=str)
    except json.JSONDecodeError as exc:
        from .core import EXIT_SQL_ERROR, QuarryError
        raise QuarryError("redis-cli returned invalid JSON (requires redis-cli 6+)", exit_code=EXIT_SQL_ERROR) from exc
    values = value if isinstance(value, list) else [value]
    return [{"value": item} for item in values], len(proc.stdout.encode("utf-8"))


def _run_scan(url: str, options: list[str], *, timeout: int) -> tuple[list[dict[str, Any]], int]:
    """Implement CLI scan mode using JSON SCAN replies, keeping even newline keys.

    Only scan options are accepted; never forward unrelated redis-cli modes or
    connection overrides hidden behind a read-only --scan request.
    """
    from .core import EXIT_CONNECTION_ERROR, QuarryError, _with_timeout_hint
    values = {"--pattern": "*", "--count": "10", "--cursor": "0", "-i": "0"}
    if len(options) % 2 or any(flag not in values for flag in options[::2]):
        raise QuarryError("--scan accepts --pattern, --count, --cursor and -i, each with a value")
    values.update(zip(options[::2], options[1::2]))
    try:
        count, cursor = int(values["--count"]), int(values["--cursor"])
        interval = float(values["-i"])
        if count < 1 or cursor < 0 or interval < 0 or not math.isfinite(interval):
            raise ValueError
    except ValueError as exc:
        raise QuarryError("--scan requires positive count and non-negative cursor/interval") from exc
    deadline = time.monotonic() + timeout
    rows, download_bytes = [], 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QuarryError(_with_timeout_hint(f"redis-cli timed out after {timeout}s"),
                              exit_code=EXIT_CONNECTION_ERROR)
        page, size = run_redis(url, shlex.join(["SCAN", str(cursor), "MATCH", values["--pattern"],
                                               "COUNT", str(count)]), timeout=remaining)
        cursor = int(page[0]["value"])
        rows.extend({"value": key} for key in page[1]["value"])
        download_bytes += size
        if cursor == 0:
            return rows, download_bytes
        if interval:
            time.sleep(min(interval, max(0, deadline - time.monotonic())))


def scan_keys(url: str, *, pattern: str = "*", count: int = 500) -> list[str]:
    cmd = _cli_base(url) + ["--scan", "--pattern", pattern, "--count", "100"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return []
    keys = [ln for ln in proc.stdout.splitlines() if ln]
    return keys[:count]


def keys_with_meta(url: str, *, pattern: str = "*", cap: int = 400) -> list[dict[str, Any]]:
    """Return [{key, type, ttl}] for up to `cap` keys (TYPE+TTL per key).

    Cheap for small keyspaces; capped so a huge DB can't stall the UI.
    """
    keys = scan_keys(url, pattern=pattern, count=cap)
    out: list[dict[str, Any]] = []
    for k in keys:
        try:
            t, _ = run_redis(url, f"TYPE {shlex.quote(k)}", timeout=10)
            ttl, _ = run_redis(url, f"TTL {shlex.quote(k)}", timeout=10)
            out.append({"key": k, "type": t[0]["value"] if t else "?",
                        "ttl": int(ttl[0]["value"]) if ttl else -1})
        except Exception:
            out.append({"key": k, "type": "?", "ttl": -1})
    return out


def inspect_key(url: str, key: str) -> list[dict[str, Any]]:
    """TYPE-aware read of a key, for the GUI 'click a key' flow."""
    t_rows, _ = run_redis(url, f"TYPE {shlex.quote(key)}")
    ktype = t_rows[0]["value"] if t_rows else "none"
    reader = {
        "string": f"GET {shlex.quote(key)}",
        "hash": f"HGETALL {shlex.quote(key)}",
        "list": f"LRANGE {shlex.quote(key)} 0 -1",
        "set": f"SMEMBERS {shlex.quote(key)}",
        "zset": f"ZRANGE {shlex.quote(key)} 0 -1 WITHSCORES",
    }.get(ktype)
    if not reader:
        return [{"key": key, "type": ktype, "value": "(unsupported type)"}]
    rows, _ = run_redis(url, reader)
    return [{"key": key, "type": ktype, "value": r["value"]} for r in rows]
