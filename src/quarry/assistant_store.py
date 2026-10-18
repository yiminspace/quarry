"""Local assistant conversations, independent of the lifetime of GUI tabs.

SQL revisions are compare-and-set tokens. Appending a reply never changes the
SQL revision, so a late reply can be kept without overwriting a newer draft.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import workspace
from .core import QuarryError


class Conflict(QuarryError):
    status_code = 409


class NotFound(QuarryError):
    status_code = 404


def _path() -> Path:
    home = Path(os.environ.get("QUARRY_AI_HOME") or workspace._config_path().parent / "assistant")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return home / "history.sqlite3"


@contextmanager
def _db(*, write=False):
    path = _path()
    # Create with restrictive permissions before SQLite opens it. The journal
    # inherits the database permissions; no model credentials belong here.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
        if write:
            db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _load(db, id: str) -> dict:
    if not isinstance(id, str):
        raise QuarryError("Conversation id is required")
    row = db.execute("SELECT document FROM conversations WHERE id = ?", (id,)).fetchone()
    if row is None:
        raise NotFound("Conversation not found")
    return json.loads(row[0])


def _public(doc: dict) -> dict:
    return {key: value for key, value in doc.items() if not key.startswith("_")}


def _save(db, doc: dict) -> dict:
    doc["updatedAt"] = int(time.time() * 1000)
    db.execute("INSERT OR REPLACE INTO conversations VALUES (?, ?)",
               (doc["id"], json.dumps(doc, ensure_ascii=False)))
    return _public(doc)


def _sql(value) -> str:
    if not isinstance(value, str) or len(value) > 200_000:
        raise QuarryError("SQL must be text, up to 200,000 characters")
    return value


def _cas(doc: dict, revision: int) -> None:
    if type(revision) is not int or revision != doc["revision"]:
        raise Conflict("This conversation changed. Reopen it from History before applying SQL.")


def create(*, db: str, env: str | None, workspace: str | None, sql: str,
           fingerprint: str, title: str | None = None) -> dict:
    doc = {"id": str(uuid.uuid4()), "title": (title or "SQL conversation")[:120],
           "db": db, "env": env, "workspace": workspace, "sql": _sql(sql),
           "revision": 0, "messages": [], "checkpoints": [],
           "_fingerprint": fingerprint}
    with _db(write=True) as connection:
        return _save(connection, doc)


def get(id: str) -> dict:
    with _db() as db:
        return _public(_load(db, id))


def connection_fingerprint(id: str) -> str:
    with _db() as db:
        return _load(db, id)["_fingerprint"]


def list_conversations() -> list[dict]:
    with _db() as db:
        docs = [json.loads(row[0]) for row in db.execute("SELECT document FROM conversations")]
    return sorted(({k: v for k, v in _public(doc).items() if k not in ("messages", "checkpoints")}
                   for doc in docs), key=lambda d: d["updatedAt"], reverse=True)


def update(id: str, revision: int, sql: str) -> dict:
    sql = _sql(sql)
    with _db(write=True) as db:
        doc = _load(db, id)
        _cas(doc, revision)
        if doc["sql"] != sql:
            doc["sql"] = sql
            doc["revision"] += 1
        return _save(db, doc)


def append_message(conversation_id: str, role: str, content: str, **extra) -> dict:
    if role not in ("user", "assistant", "system") or not isinstance(content, str):
        raise QuarryError("Invalid conversation message")
    if len(content) > 200_000:
        raise QuarryError("Assistant message is too large")
    message = {"id": extra.get("id") or str(uuid.uuid4()), "role": role, "content": content}
    for key in ("sql", "status", "checkpointId", "requestId", "baseRevision"):
        if key in extra:
            message[key] = _sql(extra[key]) if key == "sql" else extra[key]
    with _db(write=True) as db:
        doc = _load(db, conversation_id)
        if any(m["id"] == message["id"] for m in doc["messages"]):
            raise Conflict("This reply has already been saved")
        if role == "user" and not any(m["role"] == "user" for m in doc["messages"]):
            title = extra.get("title") or content
            doc["title"] = " ".join(title.split())[:100] or "SQL conversation"
        doc["messages"].append(message)
        return _save(db, doc)


def _checkpoint(doc: dict, sql: str, message_id: str) -> dict | None:
    if doc["sql"] == sql:
        return None
    checkpoint = {"id": str(uuid.uuid4()), "messageId": message_id,
                  "beforeSql": doc["sql"], "afterSql": sql,
                  "createdAt": int(time.time() * 1000)}
    doc["checkpoints"].append(checkpoint)
    doc["sql"] = sql
    doc["revision"] += 1
    return checkpoint


def apply(id: str, revision: int, sql: str, message_id: str, *, explicit: bool = False) -> dict:
    sql = _sql(sql)
    with _db(write=True) as db:
        doc = _load(db, id)
        _cas(doc, revision)
        message = next((m for m in doc["messages"] if m["id"] == message_id), None)
        if not message or message["role"] != "assistant" or message.get("sql") != sql:
            raise QuarryError("SQL does not match a completed assistant proposal")
        if message.get("status", "complete") != "complete":
            raise QuarryError("An incomplete reply cannot change SQL")
        if not explicit and message.get("baseRevision", revision) != revision:
            raise Conflict("SQL changed while the assistant was replying. Review the suggestion before applying it.")
        if message.get("checkpointId") and not explicit:
            raise Conflict("This SQL change has already been applied")
        checkpoint = _checkpoint(doc, sql, message_id)
        if checkpoint:
            message["checkpointId"] = checkpoint["id"]
        return _save(db, doc)


def restore(id: str, revision: int, checkpoint_id: str) -> dict:
    with _db(write=True) as db:
        doc = _load(db, id)
        checkpoint = next((cp for cp in doc["checkpoints"] if cp["id"] == checkpoint_id), None)
        if not checkpoint:
            raise NotFound("Checkpoint not found")
        source = next((m for m in doc["messages"] if m["id"] == checkpoint["messageId"]), None)
        if not source or source["role"] != "assistant":
            # Earlier versions created checkpoints for restore messages. Keep
            # that history readable, but do not allow a restore-of-a-restore.
            raise QuarryError("Only assistant SQL changes can be restored")
        target = checkpoint["beforeSql"]
        if doc["sql"] == target:
            # Retrying an already completed request is a no-op, even when the
            # caller still has its pre-restore revision. Never save here: the
            # history order, SQL revision and message list must stay unchanged.
            if type(revision) is not int or not 0 <= revision <= doc["revision"]:
                _cas(doc, revision)
            return _public(doc)
        _cas(doc, revision)
        restoration = {"id": str(uuid.uuid4()), "beforeSql": doc["sql"],
                       "afterSql": target, "createdAt": int(time.time() * 1000)}
        # Preserve displaced drafts alongside the original change rather than
        # manufacturing another actionable checkpoint for every restore.
        checkpoint.setdefault("restorations", []).append(restoration)
        doc["sql"] = target
        doc["revision"] += 1
        doc["messages"].append({"id": restoration["id"], "role": "system",
                                "content": "Previous SQL restored.",
                                "restoredCheckpointId": checkpoint["id"]})
        return _save(db, doc)
