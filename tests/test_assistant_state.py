"""Persistence and metadata boundaries independent of model output."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from quarry import assistant_schema as schema, assistant_store as store, workspace
from quarry.core import QuarryError
from conftest import requires_db, requires_mysql


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setenv("QUARRY_AI_HOME", str(tmp_path / "assistant"))
    return store.create(db="shop", env="dev", workspace=str(tmp_path), sql="SELECT 1", fingerprint="one")


def test_conversation_survives_reopening_and_has_no_private_fields(history):
    store.append_message(history["id"], "user", "Show customer order totals")
    loaded = store.get(history["id"])
    assert loaded["messages"][0]["content"] == "Show customer order totals"
    assert loaded["title"] == "Show customer order totals"
    assert store.connection_fingerprint(history["id"]) == "one"
    assert "_fingerprint" not in loaded
    assert store.list_conversations()[0]["id"] == history["id"]
    assert "messages" not in store.list_conversations()[0]
    assert store._path().stat().st_mode & 0o777 == 0o600


def test_sql_cas_rejects_stale_writes_and_aba(history):
    c = store.update(history["id"], 0, "SELECT 2")
    c = store.update(c["id"], c["revision"], "SELECT 1")
    assert c["revision"] == 2
    with pytest.raises(store.Conflict):
        store.update(c["id"], 0, "SELECT 3")


def test_auto_explanation_history_title_identifies_sql(history):
    c = store.append_message(history["id"], "user", "Explain this SQL.",
                             title="SELECT id, name\nFROM customers")
    assert c["title"] == "SELECT id, name FROM customers"
    c = store.append_message(history["id"], "user", "And the ordering?")
    assert c["title"] == "SELECT id, name FROM customers"
    assert "title" not in c["messages"][0]


def test_concurrent_cas_has_only_one_winner(history):
    def attempt(sql):
        try:
            store.update(history["id"], 0, sql)
            return True
        except store.Conflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, ["SELECT 2", "SELECT 3"])) == 1


def test_interrupted_or_forged_proposals_cannot_apply(history):
    c = store.append_message(history["id"], "assistant", "partial", sql="SELECT 2", status="interrupted")
    with pytest.raises(QuarryError):
        store.apply(c["id"], 0, "SELECT 2", c["messages"][-1]["id"])
    with pytest.raises(QuarryError):
        store.apply(c["id"], 0, "SELECT 9", c["messages"][-1]["id"])
    assert store.get(c["id"])["sql"] == "SELECT 1"


def test_checkpoint_restores_sql_and_preserves_newer_draft_and_chat(history):
    c = store.append_message(history["id"], "assistant", "Updated.", sql="SELECT 2", baseRevision=0)
    c = store.apply(c["id"], 0, "SELECT 2", c["messages"][-1]["id"])
    cp = c["checkpoints"][0]["id"]
    c = store.update(c["id"], c["revision"], "SELECT 3 -- handwritten")
    c = store.restore(c["id"], c["revision"], cp)
    assert c["sql"] == "SELECT 1"
    assert len(c["checkpoints"]) == 1
    assert c["checkpoints"][0]["restorations"][0]["beforeSql"] == "SELECT 3 -- handwritten"
    assert c["messages"][0]["content"] == "Updated."
    assert c["messages"][-1]["content"] == "Previous SQL restored."
    assert "checkpointId" not in c["messages"][-1]
    assert c["messages"][-1]["restoredCheckpointId"] == cp


def test_restore_retries_are_idempotent_and_do_not_create_an_undo_chain(history):
    c = store.append_message(history["id"], "assistant", "Updated.", sql="SELECT 2", baseRevision=0)
    c = store.apply(c["id"], 0, "SELECT 2", c["messages"][-1]["id"])
    cp = c["checkpoints"][0]["id"]
    before_revision = c["revision"]
    restored = store.restore(c["id"], before_revision, cp)
    for revision in (before_revision, restored["revision"], before_revision):
        assert store.restore(c["id"], revision, cp) == restored
    assert store.get(c["id"]) == restored
    assert len(restored["checkpoints"]) == 1
    assert len(restored["checkpoints"][0]["restorations"]) == 1
    assert len(restored["messages"]) == 2
    assert restored["revision"] == before_revision + 1

    # A subsequent handwritten draft can still be restored, but a delayed
    # earlier request cannot replace it. Both displaced drafts remain saved.
    edited = store.update(c["id"], restored["revision"], "SELECT 3 -- new draft")
    with pytest.raises(store.Conflict):
        store.restore(c["id"], before_revision, cp)
    again = store.restore(c["id"], edited["revision"], cp)
    assert len(again["checkpoints"]) == 1
    assert [r["beforeSql"] for r in again["checkpoints"][0]["restorations"]] == [
        "SELECT 2", "SELECT 3 -- new draft",
    ]


def test_concurrent_duplicate_restores_create_one_history_event(history):
    c = store.append_message(history["id"], "assistant", "Updated.", sql="SELECT 2", baseRevision=0)
    c = store.apply(c["id"], 0, "SELECT 2", c["messages"][-1]["id"])
    cp = c["checkpoints"][0]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.restore(c["id"], c["revision"], cp), range(2)))
    assert results[0] == results[1]
    assert len(results[0]["messages"]) == 2
    assert len(results[0]["checkpoints"]) == 1


def test_legacy_restore_checkpoints_stay_readable_but_cannot_be_restored(history):
    c = store.append_message(history["id"], "assistant", "Updated.", sql="SELECT 2", baseRevision=0)
    c = store.apply(c["id"], 0, "SELECT 2", c["messages"][-1]["id"])
    with store._db(write=True) as db:
        doc = store._load(db, c["id"])
        legacy = store._checkpoint(doc, "SELECT 1", "old-restore-message")
        doc["messages"].append({"id": "old-restore-message", "role": "system",
                                "content": "Previous SQL restored.", "checkpointId": legacy["id"]})
        c = store._save(db, doc)
    with pytest.raises(QuarryError, match="Only assistant SQL changes"):
        store.restore(c["id"], c["revision"], legacy["id"])
    assert store.get(c["id"]) == c
    assert len(c["checkpoints"]) == 2


def test_late_reply_is_retained_but_requires_explicit_apply(history):
    c = store.update(history["id"], 0, "SELECT 3 -- handwritten")
    c = store.append_message(c["id"], "assistant", "Suggestion", sql="SELECT 2", baseRevision=0)
    with pytest.raises(store.Conflict):
        store.apply(c["id"], c["revision"], "SELECT 2", c["messages"][-1]["id"])
    c = store.apply(c["id"], c["revision"], "SELECT 2", c["messages"][-1]["id"], explicit=True)
    assert c["checkpoints"][-1]["beforeSql"] == "SELECT 3 -- handwritten"


def test_explicit_apply_recovers_when_browser_kept_newer_draft(history):
    c = store.append_message(history["id"], "assistant", "Suggestion", sql="SELECT 2", baseRevision=0)
    message_id = c["messages"][-1]["id"]
    c = store.apply(c["id"], 0, "SELECT 2", message_id)
    # Typing between the HTTP apply and browser completion keeps the editor
    # draft and syncs it back. Its visible Apply action must still work.
    c = store.update(c["id"], c["revision"], "SELECT 3 -- typed during apply")
    c = store.apply(c["id"], c["revision"], "SELECT 2", message_id, explicit=True)
    assert c["sql"] == "SELECT 2"
    assert c["checkpoints"][-1]["beforeSql"] == "SELECT 3 -- typed during apply"


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "shop.db"
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE customer (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customer(id), amount NUMERIC);
            CREATE TABLE "daily.events" (id INTEGER PRIMARY KEY, note TEXT);
            CREATE TABLE compound (a INTEGER, b INTEGER, PRIMARY KEY (b,a));
        ''')
    (tmp_path / "connections.toml").write_text('[shop]\nurl="sqlite:///shop.db"\nenv="dev"\n')
    old_ws, old_list = workspace.WS, workspace.WS_LIST
    workspace.configure_workspace(str(tmp_path))
    try:
        yield schema.resolve_context(str(tmp_path), "shop", "dev")
    finally:
        workspace.WS, workspace.WS_LIST = old_ws, old_list


def test_catalog_returns_keys_and_foreign_key_without_data(catalog):
    out = schema.describe_tables(catalog, ["orders", "customer", "compound"])["tables"]
    assert out[0]["primaryKey"] == ["id"]
    assert out[0]["foreignKeys"] == [{"columns": ["customer_id"], "targetTable": "customer", "targetColumns": ["id"]}]
    assert out[2]["primaryKey"] == ["b", "a"]
    assert "rows" not in out[0]
    assert schema.describe_tables(catalog, ["daily.events"])["tables"][0]["name"] == "daily.events"


def test_mysql_catalog_preserves_key_order_independent_of_column_order(monkeypatch):
    context = schema.Context(None, "test", "test", None, "mysql", "test")
    def query(ctx, sql, params, limit=501):
        assert params == {"table": "accounts"}
        if "information_schema.columns" in sql:
            assert "information_schema.key_column_usage" in sql and "k.constraint_name = 'PRIMARY'" in sql
            return [{"name": "account_id", "type": "int", "nullable": False, "primary_position": 2},
                    {"name": "tenant_id", "type": "int", "nullable": False, "primary_position": 1},
                    {"name": "note", "type": "text", "nullable": True, "primary_position": 0}]
        return []
    monkeypatch.setattr(schema, "_query", query)
    table = schema.describe_tables(context, ["accounts"])["tables"][0]
    assert [c["name"] for c in table["columns"]] == ["account_id", "tenant_id", "note"]
    assert table["primaryKey"] == ["tenant_id", "account_id"]
    assert all("primary_position" not in c for c in table["columns"])


@requires_mysql
@pytest.mark.integration
def test_mysql_catalog_reads_composite_primary_key_in_declared_order():
    import os
    conn = schema.core.Connection(key="mysql", url=os.environ["QUARRY_TEST_MYSQL_URL"], engine="mysql")
    context = schema.Context(conn, "test", "test", None, "mysql", "test")
    name = "qy_assistant_" + uuid4().hex
    schema.core.run_query(conn, f"CREATE TABLE {name} (account_id int, tenant_id int, note text, PRIMARY KEY (tenant_id, account_id))", allow_write=True)
    try:
        table = schema.describe_tables(context, [name])["tables"][0]
        assert [c["name"] for c in table["columns"]] == ["account_id", "tenant_id", "note"]
        assert table["primaryKey"] == ["tenant_id", "account_id"]
        assert table["foreignKeys"] == []
    finally:
        schema.core.run_query(conn, f"DROP TABLE {name}", allow_write=True)


def test_catalog_search_cap_and_unknown_names(catalog):
    assert schema.find_tables(catalog, "", 1)["capped"]
    assert schema.find_tables(catalog, "customer")["tables"] == ["customer"]
    assert schema.find_tables(catalog, "' OR 1=1 --")["tables"] == []
    with pytest.raises(QuarryError, match="not found"):
        schema.describe_tables(catalog, ["missing"])
    with pytest.raises(QuarryError):
        schema.describe_tables(catalog, ["orders"] * 9)


def test_connection_identity_rejects_workspace_env_drift(catalog, tmp_path):
    with pytest.raises(QuarryError, match="workspace"):
        schema.resolve_context(str(tmp_path / "other"), "shop", "dev")
    with pytest.raises(QuarryError, match="environment"):
        schema.resolve_context(catalog.workspace, "shop", "production")
    with sqlite3.connect(tmp_path / "shop.db") as db:
        db.execute("INSERT INTO customer VALUES (1, 'A')")
    assert schema.resolve_context(catalog.workspace, "shop", "dev").fingerprint == catalog.fingerprint


@requires_db
def test_postgres_catalog_resolves_qualified_tables_and_composite_relations(ws, pg_exec):
    namespace = "qy_assistant_" + uuid4().hex
    parent = f'{namespace}."Customer Profiles"'
    child = f'{namespace}."order.lines"'
    rc, _, error = pg_exec(f"CREATE SCHEMA {namespace}")
    assert rc == 0, error
    try:
        rc, _, error = pg_exec(f"""
            CREATE TABLE {parent} (
                customer_id integer,
                tenant_id integer,
                display_name text NOT NULL,
                PRIMARY KEY (tenant_id, customer_id)
            );
            CREATE TABLE {child} (
                line_id bigint,
                customer_id integer,
                tenant_id integer,
                amount numeric(12,2),
                PRIMARY KEY (line_id, tenant_id),
                FOREIGN KEY (tenant_id, customer_id)
                    REFERENCES {parent} (tenant_id, customer_id)
            );
            COMMENT ON COLUMN {child}.amount IS 'Amount before tax';
        """)
        assert rc == 0, error

        context = schema.resolve_context(str(ws), "testpg", "test")
        found = schema.find_tables(context, namespace)
        assert found == {"tables": [parent, child], "capped": False}
        tables = schema.describe_tables(context, [parent, child])["tables"]
        assert tables[0]["name"] == parent
        assert tables[0]["primaryKey"] == ["tenant_id", "customer_id"]
        assert tables[0]["foreignKeys"] == []
        assert tables[1]["name"] == child
        assert tables[1]["primaryKey"] == ["line_id", "tenant_id"]
        assert tables[1]["foreignKeys"] == [{
            "columns": ["tenant_id", "customer_id"],
            "targetSchema": namespace,
            "targetTable": "Customer Profiles",
            "targetColumns": ["tenant_id", "customer_id"],
        }]
        columns = {c["name"]: c for c in tables[1]["columns"]}
        assert list(columns) == ["line_id", "customer_id", "tenant_id", "amount"]
        assert columns["line_id"]["type"] == "bigint"
        assert columns["line_id"]["nullable"] is False
        assert columns["customer_id"]["nullable"] is True
        assert columns["amount"] == {
            "name": "amount", "type": "numeric(12,2)",
            "nullable": True, "comment": "Amount before tax",
        }
        assert all(not table["capped"] and "rows" not in table for table in tables)
    finally:
        rc, _, error = pg_exec(f"DROP SCHEMA {namespace} CASCADE")
        assert rc == 0, error
