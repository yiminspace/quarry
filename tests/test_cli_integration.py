"""In-process tests for the CLI command handlers (quarry.cli).

Every handler is exercised by building the real argparse parser and calling the
resolved `args.func(args)` exactly as `main()` does — so coverage counts the CLI
module directly (no subprocess). DB-backed handlers hit the real local Postgres
and are gated with @requires_db.

Scratch-table prefix: cli_tmp_  (dropped in the same test; customers/orders are
never mutated).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from quarry import cli, core, workspace
from quarry.core import (
    EXIT_FINGERPRINT_MISSING,
    EXIT_FINGERPRINT_STALE,
    EXIT_OK,
    EXIT_SAFETY_BLOCKED,
    EXIT_SQL_ERROR,
    EXIT_USAGE,
    Param,
    Query,
)

from conftest import TEST_DB_URL, requires_db


# ---------------------------------------------------------------------------
# The main()-equivalent runner: build parser, configure workspace, dispatch.
# ---------------------------------------------------------------------------

def run_cli(wsdir, *argv):
    parser = cli.build_parser()
    args = parser.parse_args(["--workspace", str(wsdir), *argv])
    workspace.configure_workspace(args.workspace)
    try:
        return args.func(args)
    except cli.QuarryError as e:
        return e.exit_code
    except SystemExit as e:  # err() raises SystemExit on a couple of unreachable paths
        return e.code if isinstance(e.code, int) else 1


def _seed_query(wsdir: Path, db: str, name: str, body: str) -> Path:
    qdir = wsdir / "queries" / db
    qdir.mkdir(parents=True, exist_ok=True)
    p = qdir / f"{name}.sql"
    p.write_text(body, encoding="utf-8")
    return p


# ===========================================================================
# Pure / mocked helpers (no DB)
# ===========================================================================

@pytest.mark.unit
class TestPureHelpers:
    def test_collect_args_params_merges_positional_and_flag(self):
        import argparse
        ns = argparse.Namespace(params=["a=1", "b=2"], param=["b=3", "c=4"])
        merged = cli.collect_args_params(ns)
        # --param overrides the positional 'params' on conflicting keys
        assert merged == {"a": "1", "b": "3", "c": "4"}

    def test_collect_args_params_defaults_empty(self):
        import argparse
        assert cli.collect_args_params(argparse.Namespace()) == {}

    def test_parse_param_cli_variants(self):
        assert cli._parse_param_cli("cid:int:required") == Param("cid", "int", True, None)
        assert cli._parse_param_cli("name:text:default=foo") == Param("name", "text", False, "foo")
        # no type -> defaults to text; no qualifier -> not required, no default
        assert cli._parse_param_cli("bare") == Param("bare", "text", False, None)
        # empty default is allowed and preserved
        assert cli._parse_param_cli("x:text:default=") == Param("x", "text", False, "")

    def test_parse_param_cli_unknown_qualifier(self):
        with pytest.raises(core.QuarryError) as ei:
            cli._parse_param_cli("x:int:bogus")
        assert ei.value.exit_code == EXIT_USAGE

    def test_format_query_file_roundtrips_through_parser(self, tmp_path):
        q = Query(
            name="q1", db="testpg", desc="hi there", tags=["a", "b"],
            params=[Param("cid", "int", True, None), Param("st", "text", False, "paid")],
            schema_sources=["schema.sql"], source_fingerprint="sha256:deadbeef",
            saved_at="2020-01-01T00:00:00Z", last_validated=None,
            sql="SELECT 1",
        )
        text = cli._format_query_file(q)
        assert "-- @name: q1" in text
        assert "-- @db: testpg" in text
        assert "-- @desc: hi there" in text
        assert "-- @tags: a, b" in text
        assert "-- @param: cid (int, required)" in text
        assert "-- @param: st (text, default=paid)" in text
        assert "-- @schema-source: schema.sql" in text
        assert "-- @source-fingerprint: sha256:deadbeef" in text
        # a missing trailing ';' is added
        assert text.rstrip().endswith("SELECT 1;")
        # and it parses back to an equivalent query
        p = tmp_path / "q1.sql"
        p.write_text(text, encoding="utf-8")
        back = core.parse_query_file(p)
        assert back.name == "q1" and back.db == "testpg"
        assert back.desc == "hi there" and back.tags == ["a", "b"]
        assert [pp.name for pp in back.params] == ["cid", "st"]
        assert back.sql == "SELECT 1;"

    def test_format_query_file_keeps_existing_semicolon(self):
        q = Query(name="q", db="d", sql="SELECT 1;")
        text = cli._format_query_file(q)
        assert text.rstrip().endswith("SELECT 1;")
        assert not text.rstrip().endswith(";;")

    def test_format_query_file_emits_last_validated(self):
        q = Query(name="q", db="d", saved_at="2020-01-01T00:00:00Z",
                  last_validated="2021-06-06T06:06:06Z", sql="SELECT 1")
        text = cli._format_query_file(q)
        assert "-- @last-validated: 2021-06-06T06:06:06Z" in text
        assert "-- @saved-at: 2020-01-01T00:00:00Z" in text

    def test_stamp_validated_replaces_existing_line(self, tmp_path):
        p = tmp_path / "s.sql"
        p.write_text(
            "-- @name: s\n-- @db: d\n-- @saved-at: 2020-01-01T00:00:00Z\n"
            "-- @last-validated: OLD\nSELECT 1;\n", encoding="utf-8")
        cli._stamp_validated(p, "2020-01-01T00:00:00Z")
        out = p.read_text()
        assert "@last-validated: OLD" not in out
        assert out.count("@last-validated") == 1

    def test_stamp_validated_inserts_after_saved_at(self, tmp_path):
        p = tmp_path / "s.sql"
        saved = "2020-01-01T00:00:00Z"
        p.write_text(f"-- @name: s\n-- @db: d\n-- @saved-at: {saved}\nSELECT 1;\n", encoding="utf-8")
        cli._stamp_validated(p, saved)
        out = p.read_text()
        assert "@last-validated" in out
        # inserted right after the saved-at line
        lines = out.splitlines()
        i = next(k for k, ln in enumerate(lines) if "@saved-at" in ln)
        assert "@last-validated" in lines[i + 1]

    def test_stamp_validated_noop_when_no_anchor(self, tmp_path):
        p = tmp_path / "s.sql"
        p.write_text("SELECT 1;\n", encoding="utf-8")
        cli._stamp_validated(p, None)  # no @last-validated, no saved_at
        assert p.read_text() == "SELECT 1;\n"

    # ---- _override_limit / _strip_limit (paren-aware) ----

    def test_override_limit_append_and_replace(self):
        assert cli._override_limit("SELECT * FROM t", 5) == "SELECT * FROM t\nLIMIT 5"
        assert cli._override_limit("SELECT * FROM t LIMIT 99", 5) == "SELECT * FROM t LIMIT 5"

    def test_override_limit_ignores_inner_limit(self):
        sql = "SELECT * FROM (SELECT id FROM t LIMIT 100) s"
        out = cli._override_limit(sql, 5)
        assert "LIMIT 100) s" in out and out.rstrip().endswith("LIMIT 5")

    def test_strip_limit_outer_only(self):
        assert cli._strip_limit("SELECT * FROM t LIMIT 100") == "SELECT * FROM t"
        inner = "SELECT * FROM (SELECT id FROM t LIMIT 100) s WHERE id > 5"
        assert cli._strip_limit(inner) == inner  # no top-level LIMIT

    def test_strip_limit_with_offset(self):
        assert cli._strip_limit("SELECT * FROM t LIMIT 10 OFFSET 3") == "SELECT * FROM t"

    def test_limit_inside_parens_is_not_toplevel(self):
        # the string-blanking is done by paren-depth only; a LIMIT nested in a
        # subquery (depth > 0) is never the outer limit.
        sql = "SELECT * FROM (SELECT id FROM t LIMIT 5) s"
        assert cli._last_toplevel_limit(sql) is None
        assert cli._strip_limit(sql) == sql

    def test_last_toplevel_limit_picks_outer(self):
        sql = "SELECT * FROM (SELECT id FROM t LIMIT 5) s LIMIT 20"
        m = cli._last_toplevel_limit(sql)
        assert m is not None and sql[m.start():m.end()] == "LIMIT 20"

    def test_paren_depths_ignores_parens_in_string_and_comment(self):
        # parens inside a string literal or a block comment do not raise depth,
        # so a trailing LIMIT after them is still seen as top-level.
        sql = "SELECT '(' AS a /* ( ( */ FROM t LIMIT 9"
        m = cli._last_toplevel_limit(sql)
        assert m is not None and sql[m.start():m.end()] == "LIMIT 9"

    def test_paren_depths_tracks_block_comment(self):
        depths = cli._paren_depths("a /* (deep) */ b")
        # parens inside the block comment do not raise depth
        assert max(depths) == 0

    def test_paren_depths_ignores_line_comment(self):
        # a '(' inside a -- line comment (which ends at the newline) is ignored,
        # but a real '(' on the next line still counts.
        sql = "a -- ( ( comment ends here\n(real)"
        depths = cli._paren_depths(sql)
        # depth returns to 0 after the real ')' closes
        assert depths[-1] == 0
        # the max depth of 1 comes only from the real paren on line 2
        assert max(depths) == 1

    # ---- _read_sql_file ----

    def test_read_sql_file_ok(self, tmp_path):
        p = tmp_path / "x.sql"
        p.write_text("SELECT 42;\n", encoding="utf-8")
        assert cli._read_sql_file(str(p)) == "SELECT 42;\n"

    def test_read_sql_file_missing_is_usage_error(self, tmp_path):
        with pytest.raises(core.QuarryError) as ei:
            cli._read_sql_file(str(tmp_path / "nope.sql"))
        assert ei.value.exit_code == EXIT_USAGE
        assert "cannot read --file" in str(ei.value)

    # ---- _confirm_prod_write (prod-write guard) ----

    def test_confirm_prod_write_non_prod_is_auto_ok(self):
        import argparse
        conn = core.Connection(key="k", url="postgresql://x/y", env="dev")
        args = argparse.Namespace(write=True, yes=False)
        assert cli._confirm_prod_write(conn, "DELETE FROM t", args) is True

    def test_confirm_prod_write_readonly_is_auto_ok(self):
        import argparse
        conn = core.Connection(key="k", url="postgresql://x/y", env="prod")
        args = argparse.Namespace(write=True, yes=False)
        # a read-only statement never needs confirmation, even on prod
        assert cli._confirm_prod_write(conn, "SELECT 1", args) is True

    def test_confirm_prod_write_yes_flag_skips_prompt(self):
        import argparse
        conn = core.Connection(key="k", url="postgresql://x/y", env="prod")
        args = argparse.Namespace(write=True, yes=True)
        assert cli._confirm_prod_write(conn, "DELETE FROM t", args) is True

    def test_confirm_prod_write_prompt_yes(self, monkeypatch):
        import argparse
        conn = core.Connection(key="k", url="postgresql://x/y", env="PROD")
        args = argparse.Namespace(write=True, yes=False)
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("y\n"))
        assert cli._confirm_prod_write(conn, "DELETE FROM t", args) is True

    def test_confirm_prod_write_prompt_no(self, monkeypatch):
        import argparse
        conn = core.Connection(key="k", url="postgresql://x/y", env="prod")
        args = argparse.Namespace(write=True, yes=False)
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("n\n"))
        assert cli._confirm_prod_write(conn, "DELETE FROM t", args) is False


# ===========================================================================
# connections management (needs a workspace on disk; add/set write to it)
# ===========================================================================

@pytest.mark.unit
class TestConnectionsMgmt:
    """These mutate connections.toml only inside the temp workspace. The add/set
    handlers call a connectivity test at the end unless --no-test is passed, so we
    always pass --no-test to keep them DB-free and deterministic."""

    def test_connections_list_table_and_json(self, wsdir, capsys):
        assert run_cli(wsdir, "connections", "list") == EXIT_OK
        out = capsys.readouterr().out
        assert "testpg" in out and "postgres" in out

        assert run_cli(wsdir, "connections", "list", "--format", "json") == EXIT_OK
        tree = json.loads(capsys.readouterr().out)
        item = tree[0]["items"][0]
        assert item["db"] == "testpg" and item["engine"] == "postgres"

    def test_connections_list_table_no_env(self, wsdir, capsys):
        # a plain connection with no `env` renders the non-env-set line (no labels)
        (wsdir / "connections.toml").write_text(
            '[plain]\nurl = "postgresql://localhost/x"\nengine = "postgres"\n', encoding="utf-8")
        assert run_cli(wsdir, "connections", "list") == EXIT_OK
        out = capsys.readouterr().out
        assert "plain" in out and "[" not in out.split("plain")[1].split("\n")[0]

    def test_connections_list_table_env_set_labels(self, wsdir, capsys):
        # two members sharing db=shop with distinct envs -> the env-labelled line
        (wsdir / "connections.toml").write_text(
            '[shop_dev]\nurl = "postgresql://localhost/x"\ndb="shop"\nenv="dev"\n'
            '[shop_prod]\nurl = "postgresql://localhost/y"\ndb="shop"\nenv="prod"\n',
            encoding="utf-8")
        assert run_cli(wsdir, "connections", "list") == EXIT_OK
        out = capsys.readouterr().out
        assert "[dev]" in out and "[prod]" in out

    def test_connections_add_and_duplicate(self, wsdir, capsys):
        rc = run_cli(wsdir, "connections", "add", "extra", "--url",
                     "postgresql://localhost:5432/quarry_test", "--no-test", "--notes", "hi",
                     "--force")  # shares host:port with the seeded testpg connection
        assert rc == EXIT_OK
        assert "added connection [extra]" in capsys.readouterr().out
        # the connection is now visible + persisted
        conns = core.load_connections()
        assert "extra" in conns and conns["extra"].notes == "hi"
        # adding it again is a usage error (add refuses to overwrite)
        rc = run_cli(wsdir, "connections", "add", "extra", "--url",
                     "postgresql://localhost:5432/quarry_test", "--no-test", "--force")
        assert rc == EXIT_USAGE

    def test_connections_add_invalid_key(self, wsdir):
        rc = run_cli(wsdir, "connections", "add", "1bad", "--url",
                     "postgresql://localhost:5432/quarry_test", "--no-test")
        assert rc == EXIT_USAGE

    def test_connections_set_upsert(self, wsdir, capsys):
        # set on a brand-new key (upsert) — requires --url
        # shares host:port with the seeded testpg connection -> needs --force
        rc = run_cli(wsdir, "connections", "set", "s1", "--url",
                     "postgresql://localhost:5432/quarry_test",
                     "--engine", "postgres", "--notes", "n", "--no-test", "--force")
        assert rc == EXIT_OK
        assert "added connection [s1]" in capsys.readouterr().out
        # update an existing key: change notes only
        rc = run_cli(wsdir, "connections", "set", "s1", "--notes", "changed", "--no-test", "--force")
        assert rc == EXIT_OK
        assert "updated connection [s1]" in capsys.readouterr().out
        assert core.load_connections()["s1"].notes == "changed"

    def test_connections_set_infers_engine_on_url_change(self, wsdir):
        run_cli(wsdir, "connections", "set", "m1", "--url",
                "mysql://u:p@localhost:3306/db", "--no-test")
        assert core.load_connections()["m1"].engine == "mysql"

    def test_connections_set_region_and_env(self, wsdir):
        run_cli(wsdir, "connections", "set", "r1", "--url",
                "postgresql://localhost/x", "--region", "us-east-1",
                "--env", "prod", "--no-test", "--force")
        c = core.load_connections()["r1"]
        assert c.region == "us-east-1" and c.env == "prod"

    def test_connections_add_with_region_and_env(self, wsdir):
        run_cli(wsdir, "connections", "add", "a1", "--url",
                "postgresql://localhost/x", "--region", "eu-west-1",
                "--env", "dev", "--no-test", "--force")
        c = core.load_connections()["a1"]
        assert c.region == "eu-west-1" and c.env == "dev"

    def test_connections_add_accepts_explicit_db_and_group(self, wsdir):
        run_cli(wsdir, "connections", "add", "shop_dev", "--url",
                "postgresql://dev.example.com/shop", "--env", "dev",
                "--db", "shop", "--group", "commerce", "--no-test")
        c = core.load_connections()["shop_dev"]
        assert c.logical_db == "shop" and c.group == "commerce"

    def test_connections_add_inherits_env_set_identity(self, wsdir):
        (wsdir / "connections.toml").write_text(
            '[shop_dev]\nurl = "postgresql://dev.example.com/shop"\n'
            'env = "dev"\ndb = "shop"\ngroup = "commerce"\n',
            encoding="utf-8",
        )
        run_cli(wsdir, "connections", "add", "shop_prod", "--url",
                "postgresql://prod.example.com/shop", "--env", "prod", "--no-test")
        c = core.load_connections()["shop_prod"]
        assert c.logical_db == "shop" and c.group == "commerce"

    def test_connections_set_can_heal_identity_from_sibling(self, wsdir):
        (wsdir / "connections.toml").write_text(
            '[queue_local]\nurl = "redis://localhost:6379/0"\nengine = "redis"\n'
            'env = "local"\ndb = "queue"\ngroup = "brain"\n'
            '[queue]\nurl = "redis://dev.example.com:6379/0"\nengine = "redis"\nenv = "dev"\n',
            encoding="utf-8",
        )
        run_cli(wsdir, "connections", "set", "queue", "--notes", "healed", "--no-test")
        c = core.load_connections()["queue"]
        assert c.logical_db == "queue" and c.group == "brain"

    # -- issue #94: configurable per-connection query timeout ---------------

    def test_connections_add_with_timeout(self, wsdir):
        run_cli(wsdir, "connections", "add", "t1", "--url",
                "postgresql://localhost/x", "--timeout", "45", "--no-test", "--force")
        assert core.load_connections()["t1"].timeout == 45

    def test_connections_set_updates_timeout(self, wsdir):
        run_cli(wsdir, "connections", "set", "t2", "--url",
                "postgresql://localhost/x", "--no-test", "--force")
        assert core.load_connections()["t2"].timeout is None
        run_cli(wsdir, "connections", "set", "t2", "--timeout", "90", "--no-test", "--force")
        assert core.load_connections()["t2"].timeout == 90

    # -- issue #76: local-misconfig / port-conflict guardrails --------------

    def _seed_local_shadow_db(self, wsdir):
        (wsdir / "connections.toml").write_text(
            (wsdir / "connections.toml").read_text(encoding="utf-8")
            + '\n[shop_local]\nurl = "postgresql://127.0.0.1:5433/shop"\nenv = "local"\n'
              'local_image = "postgres:16-alpine"\n',
            encoding="utf-8",
        )

    def test_connections_add_port_conflict_blocked_without_force(self, wsdir):
        # message content (occupant key + local_image) is covered at the
        # core.check_connection_write level; here we only check the CLI wiring
        self._seed_local_shadow_db(wsdir)
        rc = run_cli(wsdir, "connections", "add", "shop", "--url",
                     "postgresql://localhost:5433/shop", "--no-test")
        assert rc == EXIT_USAGE
        assert "shop" not in core.load_connections()

    def test_connections_add_port_conflict_forced(self, wsdir):
        self._seed_local_shadow_db(wsdir)
        rc = run_cli(wsdir, "connections", "add", "shop", "--url",
                     "postgresql://localhost:5433/shop", "--no-test", "--force")
        assert rc == EXIT_OK
        assert "shop" in core.load_connections()

    def test_connections_add_loopback_without_ssh_warns(self, wsdir, capsys):
        rc = run_cli(wsdir, "connections", "add", "svc", "--url",
                     "postgresql://127.0.0.1:5555/svc", "--no-test")
        assert rc == EXIT_OK
        err = capsys.readouterr().err
        assert "loopback" in err and "--ssh-host" in err

    def test_connections_add_loopback_with_ssh_host_suppresses_warning(self, wsdir, capsys):
        rc = run_cli(wsdir, "connections", "add", "svc2", "--url",
                     "postgresql://127.0.0.1:5556/svc", "--ssh-host", "bastion.example.com",
                     "--ssh-user", "ubuntu", "--no-test")
        assert rc == EXIT_OK
        assert "loopback" not in capsys.readouterr().err
        c = core.load_connections()["svc2"]
        assert c.ssh_host == "bastion.example.com" and c.ssh_user == "ubuntu"

    def test_connections_add_env_local_naming_hint(self, wsdir, capsys):
        rc = run_cli(wsdir, "connections", "add", "shop", "--url",
                     "postgresql://db.internal.example.com:5432/shop", "--env", "local", "--no-test")
        assert rc == EXIT_OK
        assert "shop_local" in capsys.readouterr().err

    def test_connections_add_env_local_suffix_no_hint(self, wsdir, capsys):
        rc = run_cli(wsdir, "connections", "add", "shop_local", "--url",
                     "postgresql://db.internal.example.com:5432/shop", "--env", "local", "--no-test")
        assert rc == EXIT_OK
        assert "naming convention" not in capsys.readouterr().err

    def test_connections_set_new_key_without_url_errors(self, wsdir):
        rc = run_cli(wsdir, "connections", "set", "brandnew", "--notes", "x", "--no-test")
        assert rc == EXIT_USAGE

    def test_connections_set_invalid_key(self, wsdir):
        rc = run_cli(wsdir, "connections", "set", "9bad", "--url",
                     "postgresql://localhost/x", "--no-test")
        assert rc == EXIT_USAGE

    def test_connections_remove_with_yes(self, wsdir, capsys):
        run_cli(wsdir, "connections", "set", "goner", "--url",
                "postgresql://localhost/x", "--no-test", "--force")
        assert "goner" in core.load_connections()
        rc = run_cli(wsdir, "connections", "remove", "goner", "--yes")
        assert rc == EXIT_OK
        assert "removed connection [goner]" in capsys.readouterr().out
        assert "goner" not in core.load_connections()

    def test_connections_remove_missing(self, wsdir):
        assert run_cli(wsdir, "connections", "remove", "ghost", "--yes") == EXIT_USAGE

    def test_connections_remove_prompt_confirm(self, wsdir, monkeypatch):
        run_cli(wsdir, "connections", "set", "byebye", "--url",
                "postgresql://localhost/x", "--no-test", "--force")
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("y\n"))
        assert run_cli(wsdir, "connections", "remove", "byebye") == EXIT_OK  # no --yes -> prompt
        assert "byebye" not in core.load_connections()


# ===========================================================================
# saved-query metadata handlers that don't need a DB
# ===========================================================================

@pytest.mark.unit
class TestListDescribeAudit:
    def test_list_empty_and_populated(self, wsdir, capsys):
        assert run_cli(wsdir, "list") == EXIT_OK
        assert "(no saved queries)" in capsys.readouterr().out

        _seed_query(wsdir, "testpg", "q1",
                    "-- @name: q1\n-- @db: testpg\n-- @desc: d1\n-- @tags: t1, t2\n"
                    "-- @param: cid (int, required)\nSELECT 1;\n")
        assert run_cli(wsdir, "list") == EXIT_OK
        out = capsys.readouterr().out
        assert "q1" in out and "cid" in out and "t1" in out

    def test_list_text_query_without_desc_or_tags(self, wsdir, capsys):
        # a query with neither desc nor tags exercises the false branches of the
        # text renderer (only name + params printed).
        _seed_query(wsdir, "testpg", "plainq", "-- @name: plainq\n-- @db: testpg\nSELECT 1;\n")
        assert run_cli(wsdir, "list") == EXIT_OK
        out = capsys.readouterr().out
        assert "plainq" in out and "params:" in out
        assert "desc:" not in out and "tags:" not in out

    def test_list_json_and_filters(self, wsdir, capsys):
        _seed_query(wsdir, "testpg", "qa", "-- @name: qa\n-- @db: testpg\n-- @tags: x\nSELECT 1;\n")
        _seed_query(wsdir, "testpg", "qb", "-- @name: qb\n-- @db: testpg\n-- @tags: y\nSELECT 2;\n")
        assert run_cli(wsdir, "list", "--format", "json") == EXIT_OK
        rows = json.loads(capsys.readouterr().out)
        assert {r["name"] for r in rows} == {"qa", "qb"}
        # --tag filter
        assert run_cli(wsdir, "list", "--tag", "x", "--format", "json") == EXIT_OK
        assert [r["name"] for r in json.loads(capsys.readouterr().out)] == ["qa"]
        # --db filter (no match)
        assert run_cli(wsdir, "list", "--db", "nope", "--format", "json") == EXIT_OK
        assert json.loads(capsys.readouterr().out) == []

    def test_describe_text_and_json(self, wsdir, capsys):
        _seed_query(wsdir, "testpg", "d1",
                    "-- @name: d1\n-- @db: testpg\n-- @desc: the desc\n-- @tags: t\n"
                    "-- @param: cid (int, required)\nSELECT 1;\n")
        assert run_cli(wsdir, "describe", "d1") == EXIT_OK
        out = capsys.readouterr().out
        assert "name:" in out and "the desc" in out and "---- SQL ----" in out

        assert run_cli(wsdir, "describe", "d1", "--format", "json") == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj["name"] == "d1" and obj["sql"] == "SELECT 1;"
        assert obj["params"][0]["name"] == "cid" and obj["params"][0]["required"] is True

    def test_describe_text_no_params_no_sources(self, wsdir, capsys):
        _seed_query(wsdir, "testpg", "bare", "-- @name: bare\n-- @db: testpg\nSELECT 1;\n")
        assert run_cli(wsdir, "describe", "bare") == EXIT_OK
        out = capsys.readouterr().out
        assert "params:" in out and "(none)" in out
        assert "(unset)" in out  # fingerprint / saved-at unset

    def test_describe_text_with_schema_sources_and_tags(self, wsdir, capsys):
        _seed_query(wsdir, "testpg", "full",
                    "-- @name: full\n-- @db: testpg\n-- @tags: alpha, beta\n"
                    "-- @schema-source: a.sql\n-- @schema-source: b.sql\nSELECT 1;\n")
        assert run_cli(wsdir, "describe", "full") == EXIT_OK
        out = capsys.readouterr().out
        assert "tags:" in out and "alpha" in out
        assert "schema-sources:" in out and "a.sql" in out and "b.sql" in out

    def test_fingerprint_no_source(self, wsdir, capsys):
        _seed_query(wsdir, "testpg", "ns", "-- @name: ns\n-- @db: testpg\nSELECT 1;\n")
        assert run_cli(wsdir, "fingerprint", "ns") == EXIT_OK
        assert "no @schema-source" in capsys.readouterr().out
        assert run_cli(wsdir, "fingerprint", "ns", "--format", "json") == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj["stale"] is None and obj["sources"] == []

    def test_fingerprint_missing_source_exit_code(self, wsdir, capsys):
        _seed_query(wsdir, "testpg", "fm",
                    "-- @name: fm\n-- @db: testpg\n-- @schema-source: /no/such/schema.sql\n"
                    "-- @source-fingerprint: sha256:abc\nSELECT 1;\n")
        rc = run_cli(wsdir, "fingerprint", "fm")
        assert rc == EXIT_FINGERPRINT_MISSING
        assert "MISSING" in capsys.readouterr().out

    def test_fingerprint_stale_and_fresh(self, wsdir, capsys, tmp_path):
        schema = tmp_path / "schema.sql"
        schema.write_text("CREATE TABLE t (id int);\n", encoding="utf-8")
        # stale: recorded fingerprint doesn't match the file's actual one
        _seed_query(wsdir, "testpg", "st",
                    f"-- @name: st\n-- @db: testpg\n-- @schema-source: {schema}\n"
                    "-- @source-fingerprint: sha256:0000000000000000\nSELECT 1;\n")
        rc = run_cli(wsdir, "fingerprint", "st")
        assert rc == EXIT_FINGERPRINT_STALE
        assert "stale" in capsys.readouterr().out

        # fresh: stamp the real fingerprint
        actual, _ = core.compute_fingerprint([str(schema)])
        _seed_query(wsdir, "testpg", "fr",
                    f"-- @name: fr\n-- @db: testpg\n-- @schema-source: {schema}\n"
                    f"-- @source-fingerprint: {actual}\nSELECT 1;\n")
        rc = run_cli(wsdir, "fingerprint", "fr", "--format", "json")
        assert rc == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj["stale"] is False

    def test_audit_text_and_json(self, wsdir, capsys, tmp_path):
        schema = tmp_path / "sc.sql"
        schema.write_text("CREATE TABLE t (id int);\n", encoding="utf-8")
        actual, _ = core.compute_fingerprint([str(schema)])
        _seed_query(wsdir, "testpg", "fresh",
                    f"-- @name: fresh\n-- @db: testpg\n-- @schema-source: {schema}\n"
                    f"-- @source-fingerprint: {actual}\nSELECT 1;\n")
        _seed_query(wsdir, "testpg", "nosrc", "-- @name: nosrc\n-- @db: testpg\nSELECT 2;\n")
        _seed_query(wsdir, "testpg", "miss",
                    "-- @name: miss\n-- @db: testpg\n-- @schema-source: /no/such.sql\n"
                    "-- @source-fingerprint: sha256:zz\nSELECT 3;\n")
        _seed_query(wsdir, "testpg", "stale",
                    f"-- @name: stale\n-- @db: testpg\n-- @schema-source: {schema}\n"
                    "-- @source-fingerprint: sha256:1111111111111111\nSELECT 4;\n")
        assert run_cli(wsdir, "audit", "--format", "json") == EXIT_OK
        rows = {r["name"]: r["status"] for r in json.loads(capsys.readouterr().out)}
        assert rows == {"fresh": "fresh", "nosrc": "no-source",
                        "miss": "missing-source", "stale": "stale"}
        # text path renders all four with the aligned columns
        assert run_cli(wsdir, "audit") == EXIT_OK
        out = capsys.readouterr().out
        assert "fresh" in out and "no-source" in out and "missing-source" in out

    def test_audit_empty(self, wsdir, capsys):
        assert run_cli(wsdir, "audit") == EXIT_OK
        assert "(no saved queries)" in capsys.readouterr().out

    def test_remove_prompt_confirm_yes(self, wsdir, monkeypatch, capsys):
        p = _seed_query(wsdir, "testpg", "rmy", "-- @name: rmy\n-- @db: testpg\nSELECT 1;\n")
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("yes\n"))
        assert run_cli(wsdir, "remove", "rmy") == EXIT_OK  # no --yes -> prompt
        assert not p.exists()

    def test_remove_prompt_abort(self, wsdir, monkeypatch, capsys):
        p = _seed_query(wsdir, "testpg", "rmn", "-- @name: rmn\n-- @db: testpg\nSELECT 1;\n")
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("n\n"))
        assert run_cli(wsdir, "remove", "rmn") == EXIT_USAGE  # aborted
        assert p.exists()  # not removed

    def test_connections_remove_prompt_abort(self, wsdir, monkeypatch):
        run_cli(wsdir, "connections", "set", "keeper", "--url",
                "postgresql://localhost/x", "--no-test", "--force")
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("n\n"))
        assert run_cli(wsdir, "connections", "remove", "keeper") == EXIT_USAGE
        assert "keeper" in core.load_connections()  # abort kept it


# ===========================================================================
# workspace list/add/remove — MUST isolate $QUARRY_CONFIG away from the real file
# ===========================================================================

@pytest.mark.unit
class TestWorkspaceCmds:
    def test_workspace_list_empty(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        assert run_cli(wsdir, "workspace", "list") == EXIT_OK
        out = capsys.readouterr().out
        assert "config:" in out

    def test_workspace_list_json(self, wsdir, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "c.toml"
        monkeypatch.setenv("QUARRY_CONFIG", str(cfg))
        assert run_cli(wsdir, "workspace", "list", "--format", "json") == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj["workspaces"] == [] and "config" in obj

    def test_workspace_add_then_list_then_remove(self, wsdir, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "c.toml"
        monkeypatch.setenv("QUARRY_CONFIG", str(cfg))
        target = tmp_path / "ws_a"
        target.mkdir()
        (target / "connections.toml").write_text('[k]\nurl="postgresql://x/y"\n', encoding="utf-8")

        assert run_cli(wsdir, "workspace", "add", str(target)) == EXIT_OK
        add_out = capsys.readouterr().out
        assert "已加入 workspace" in add_out
        assert str(target) in workspace.config_workspaces()

        # adding the same dir again is a no-op (dedup by resolved path)
        assert run_cli(wsdir, "workspace", "add", str(target)) == EXIT_OK
        assert "未重复添加" in capsys.readouterr().out

        # remove it
        assert run_cli(wsdir, "workspace", "remove", str(target)) == EXIT_OK
        assert "已移除" in capsys.readouterr().out
        assert str(target) not in workspace.config_workspaces()

    def test_workspace_add_missing_dir_warns(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        ghost = tmp_path / "does_not_exist"
        assert run_cli(wsdir, "workspace", "add", str(ghost)) == EXIT_OK
        assert "目录不存在" in capsys.readouterr().out

    def test_workspace_add_dir_without_connections_warns(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        bare = tmp_path / "bare_ws"
        bare.mkdir()  # exists but has no connections.toml
        assert run_cli(wsdir, "workspace", "add", str(bare)) == EXIT_OK
        assert "没有 connections.toml" in capsys.readouterr().out

    def test_workspace_remove_not_found(self, wsdir, tmp_path, monkeypatch):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        assert run_cli(wsdir, "workspace", "remove", str(tmp_path / "nope")) == EXIT_USAGE


# ===========================================================================
# proxy — `qy proxy [status|on|off]` + `--no-proxy` override (issue #96)
# ===========================================================================

@pytest.mark.unit
class TestProxyCmds:
    def test_proxy_status_no_discovery_no_workspaces_enabled(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        from quarry import proxy as proxy_mod
        monkeypatch.setattr(proxy_mod, "discover_proxy", lambda: None)
        assert run_cli(wsdir, "proxy") == EXIT_OK
        out = capsys.readouterr().out
        assert "未探测到代理" in out
        assert "未启用" in out

    def test_proxy_status_json_includes_discovery_and_workspaces(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        from quarry import proxy as proxy_mod
        monkeypatch.setattr(
            proxy_mod, "discover_proxy",
            lambda: proxy_mod.ProxyInfo(host="127.0.0.1", port=7890, source="system"),
        )
        assert run_cli(wsdir, "proxy", "status", "--format", "json") == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj["discovered"] == {"host": "127.0.0.1", "port": 7890, "source": "system"}
        assert any(w["home"] == str(wsdir.resolve()) for w in obj["workspaces"])
        assert all(w["enabled"] is False for w in obj["workspaces"])

    def test_proxy_status_text_lists_active_tunnels(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        from quarry import tunnel as tunnel_mod
        monkeypatch.setattr(tunnel_mod, "list_tunnels", lambda: [
            {"ssh_target": "ec2-user@bastion.example.com:22", "db_target": "db.internal:5432",
             "local_port": 54321, "proxied": True, "proxy": "127.0.0.1:6152", "alive": True},
        ])
        assert run_cli(wsdir, "proxy") == EXIT_OK
        out = capsys.readouterr().out
        assert "活跃隧道" in out
        assert "bastion.example.com" in out and "54321" in out and "127.0.0.1:6152" in out

    def test_proxy_status_text_no_tunnels(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        from quarry import tunnel as tunnel_mod
        monkeypatch.setattr(tunnel_mod, "list_tunnels", lambda: [])
        assert run_cli(wsdir, "proxy") == EXIT_OK
        assert "(无)" in capsys.readouterr().out

    def test_proxy_status_json_includes_tunnels(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        from quarry import tunnel as tunnel_mod
        fake_tunnels = [
            {"ssh_target": "ec2-user@bastion.example.com:22", "db_target": "db.internal:5432",
             "local_port": 54321, "proxied": False, "proxy": None, "alive": True},
        ]
        monkeypatch.setattr(tunnel_mod, "list_tunnels", lambda: fake_tunnels)
        assert run_cli(wsdir, "proxy", "status", "--format", "json") == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj["tunnels"] == fake_tunnels

    def test_proxy_on_off_persist_and_are_read_back(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        assert run_cli(wsdir, "proxy", "on") == EXIT_OK
        assert "开启代理" in capsys.readouterr().out
        assert workspace.is_proxy_enabled(wsdir) is True

        # a fresh status call reflects the persisted toggle
        assert run_cli(wsdir, "proxy") == EXIT_OK
        assert "已启用" in capsys.readouterr().out

        assert run_cli(wsdir, "proxy", "off") == EXIT_OK
        assert "关闭代理" in capsys.readouterr().out
        assert workspace.is_proxy_enabled(wsdir) is False

    def test_proxy_on_explicit_workspace_flag(self, wsdir, tmp_path, monkeypatch):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        other = tmp_path / "other_ws"
        other.mkdir()
        assert run_cli(wsdir, "proxy", "on", "--workspace", str(other)) == EXIT_OK
        assert workspace.is_proxy_enabled(other) is True
        assert workspace.is_proxy_enabled(wsdir) is False

    def test_no_proxy_flag_overrides_enabled_toggle_for_one_call(self, wsdir, tmp_path, monkeypatch):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        workspace.set_proxy_enabled(str(wsdir), True)
        captured = {}

        def fake_execute_sql(**kwargs):
            captured.update(kwargs)
            return EXIT_OK

        monkeypatch.setattr(core, "execute_sql", fake_execute_sql)
        rc = run_cli(wsdir, "exec", "testpg", "--sql", "SELECT 1", "--no-proxy")
        assert rc == EXIT_OK
        assert captured["use_proxy"] is False

    def test_without_no_proxy_flag_defers_to_persisted_toggle(self, wsdir, tmp_path, monkeypatch):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        workspace.set_proxy_enabled(str(wsdir), True)
        captured = {}

        def fake_execute_sql(**kwargs):
            captured.update(kwargs)
            return EXIT_OK

        monkeypatch.setattr(core, "execute_sql", fake_execute_sql)
        rc = run_cli(wsdir, "exec", "testpg", "--sql", "SELECT 1")
        assert rc == EXIT_OK
        assert captured["use_proxy"] is None

    def test_run_strict_no_proxy_flag_threads_into_validate_query(self, wsdir, tmp_path, monkeypatch):
        """PR #98 review (r1-3): `qy run --strict --no-proxy` must also skip the
        proxy for the strict-mode EXPLAIN pre-check, not just the real query —
        previously core.validate_query() had no use_proxy parameter at all."""
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        workspace.set_proxy_enabled(str(wsdir), True)
        _seed_query(wsdir, "testpg", "sok", "-- @name: sok\n-- @db: testpg\nSELECT 1 AS ok;\n")
        captured = {}

        def fake_validate_query(q, conn, **kwargs):
            captured.update(kwargs)
            return EXIT_OK

        def fake_execute_sql(**kwargs):
            return EXIT_OK

        monkeypatch.setattr(core, "validate_query", fake_validate_query)
        monkeypatch.setattr(core, "execute_sql", fake_execute_sql)
        rc = run_cli(wsdir, "run", "sok", "--strict", "--no-proxy")
        assert rc == EXIT_OK
        assert captured["use_proxy"] is False

    def test_run_strict_without_no_proxy_defers_to_persisted_toggle(self, wsdir, tmp_path, monkeypatch):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        workspace.set_proxy_enabled(str(wsdir), True)
        _seed_query(wsdir, "testpg", "sok", "-- @name: sok\n-- @db: testpg\nSELECT 1 AS ok;\n")
        captured = {}

        def fake_validate_query(q, conn, **kwargs):
            captured.update(kwargs)
            return EXIT_OK

        def fake_execute_sql(**kwargs):
            return EXIT_OK

        monkeypatch.setattr(core, "validate_query", fake_validate_query)
        monkeypatch.setattr(core, "execute_sql", fake_execute_sql)
        rc = run_cli(wsdir, "run", "sok", "--strict")
        assert rc == EXIT_OK
        assert captured["use_proxy"] is None


# ===========================================================================
# proxy fallback notice (issue #101) — stderr hint when a proxied workspace's
# resolved connection actually runs direct
# ===========================================================================

def _seed_ssh_conn(wsdir: Path) -> None:
    (wsdir / "connections.toml").write_text(
        f'[testpg]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "test"\n'
        'ssh_host = "bastion.example.com"\nssh_user = "ec2-user"\n',
        encoding="utf-8",
    )


@pytest.mark.unit
class TestProxyFallbackNotice:
    def test_not_discovered_reason_printed_to_stderr(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        workspace.set_proxy_enabled(str(wsdir), True)
        _seed_ssh_conn(wsdir)
        from quarry import proxy as proxy_mod
        monkeypatch.setattr(proxy_mod, "discover_proxy", lambda: None)
        monkeypatch.setattr(core, "execute_sql", lambda **kwargs: EXIT_OK)

        rc = run_cli(wsdir, "exec", "testpg", "--sql", "SELECT 1")
        assert rc == EXIT_OK
        assert "none was discovered" in capsys.readouterr().err

    def test_port_unreachable_reason_printed_to_stderr(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        workspace.set_proxy_enabled(str(wsdir), True)
        _seed_ssh_conn(wsdir)
        from quarry import proxy as proxy_mod
        monkeypatch.setattr(
            proxy_mod, "discover_proxy",
            lambda: proxy_mod.ProxyInfo(host="127.0.0.1", port=6152, source="system"),
        )
        monkeypatch.setattr(proxy_mod, "_port_listening", lambda host, port, timeout=0.3: False)
        monkeypatch.setattr(core, "execute_sql", lambda **kwargs: EXIT_OK)

        rc = run_cli(wsdir, "exec", "testpg", "--sql", "SELECT 1")
        assert rc == EXIT_OK
        err_out = capsys.readouterr().err
        assert "nothing is listening" in err_out and "127.0.0.1:6152" in err_out

    def test_exception_list_reason_printed_to_stderr(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        workspace.set_proxy_enabled(str(wsdir), True)
        _seed_ssh_conn(wsdir)
        from quarry import proxy as proxy_mod
        monkeypatch.setattr(
            proxy_mod, "discover_proxy",
            lambda: proxy_mod.ProxyInfo(host="127.0.0.1", port=6152, source="system",
                                        exceptions=["bastion.example.com"]),
        )
        monkeypatch.setattr(core, "execute_sql", lambda **kwargs: EXIT_OK)

        rc = run_cli(wsdir, "exec", "testpg", "--sql", "SELECT 1")
        assert rc == EXIT_OK
        assert "exceptions list" in capsys.readouterr().err

    def test_no_proxy_flag_suppresses_notice(self, wsdir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        workspace.set_proxy_enabled(str(wsdir), True)
        _seed_ssh_conn(wsdir)
        from quarry import proxy as proxy_mod
        monkeypatch.setattr(proxy_mod, "discover_proxy", lambda: None)
        monkeypatch.setattr(core, "execute_sql", lambda **kwargs: EXIT_OK)

        rc = run_cli(wsdir, "exec", "testpg", "--sql", "SELECT 1", "--no-proxy")
        assert rc == EXIT_OK
        assert capsys.readouterr().err == ""

    def test_disabled_toggle_prints_no_notice(self, wsdir, tmp_path, monkeypatch, capsys):
        """The workspace proxy toggle is off entirely — nothing to report, unlike
        the `not_discovered`/`port_unreachable`/`exception_list` cases which all
        assume the toggle is on but the actual routing still fell back direct."""
        monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "c.toml"))
        _seed_ssh_conn(wsdir)
        monkeypatch.setattr(core, "execute_sql", lambda **kwargs: EXIT_OK)

        rc = run_cli(wsdir, "exec", "testpg", "--sql", "SELECT 1")
        assert rc == EXIT_OK
        assert capsys.readouterr().err == ""


# ===========================================================================
# gui / mcp — stub out the real servers; assert they are dispatched correctly
# ===========================================================================

@pytest.mark.unit
class TestGuiMcpDispatch:
    def test_cmd_gui_invokes_serve(self, wsdir, monkeypatch):
        import quarry.gui as gui
        captured = {}

        def fake_serve(*, host, port, ws_path, open_browser):
            captured.update(host=host, port=port, ws_path=ws_path, open_browser=open_browser)
            return 0

        monkeypatch.setattr(gui, "serve", fake_serve)
        rc = run_cli(wsdir, "gui", "--host", "0.0.0.0", "--port", "9999", "--no-open")
        assert rc == 0
        assert captured["host"] == "0.0.0.0"
        assert captured["port"] == 9999
        assert captured["ws_path"] == str(wsdir)
        assert captured["open_browser"] is False

    def test_cmd_gui_defaults(self, wsdir, monkeypatch):
        import quarry.gui as gui
        captured = {}
        monkeypatch.setattr(gui, "serve", lambda **kw: (captured.update(kw), 0)[1])
        assert run_cli(wsdir, "gui") == 0
        assert captured["host"] == "127.0.0.1" and captured["port"] == 8765
        assert captured["open_browser"] is True  # not --no-open

    def test_cmd_mcp_invokes_serve(self, wsdir, monkeypatch):
        import quarry.mcp as mcp
        captured = {}

        def fake_serve(ws_path, allow_write=False):
            captured.update(ws_path=ws_path, allow_write=allow_write)
            return 0

        monkeypatch.setattr(mcp, "serve", fake_serve)
        assert run_cli(wsdir, "mcp", "--write") == 0
        assert captured["ws_path"] == str(wsdir)
        assert captured["allow_write"] is True

    def test_cmd_mcp_default_readonly(self, wsdir, monkeypatch):
        import quarry.mcp as mcp
        captured = {}
        monkeypatch.setattr(mcp, "serve", lambda ws, allow_write=False: (captured.update(
            ws=ws, allow_write=allow_write), 0)[1])
        assert run_cli(wsdir, "mcp") == 0
        assert captured["allow_write"] is False


# ===========================================================================
# DB-backed integration tests (in-process against the real Postgres)
# ===========================================================================

@requires_db
@pytest.mark.integration
class TestExecRunDB:
    def test_exec_json(self, wsdir, capsys):
        rc = run_cli(wsdir, "exec", "testpg", "--sql",
                     "SELECT count(*) AS n FROM customers", "--format", "json")
        assert rc == EXIT_OK
        captured = capsys.readouterr()
        assert json.loads(captured.out)[0]["n"] == 3
        # issue #105: elapsed/download-size/avg-speed summary goes to stderr
        # only, keeping stdout pipeable (postgres approximates the size, so
        # it's marked with '≈').
        assert "ms · downloaded ≈" in captured.err
        assert "avg speed ≈" in captured.err

    def test_exec_ndjson(self, wsdir, capsys):
        rc = run_cli(wsdir, "exec", "testpg", "--sql",
                     "SELECT id FROM customers ORDER BY id", "--format", "ndjson")
        assert rc == EXIT_OK
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert [json.loads(ln)["id"] for ln in lines] == [1, 2, 3]

    def test_exec_csv(self, wsdir, capsys):
        rc = run_cli(wsdir, "exec", "testpg", "--sql",
                     "SELECT 1 AS a, 2 AS b", "--format", "csv")
        assert rc == EXIT_OK
        assert capsys.readouterr().out.splitlines()[0] == "a,b"

    def test_exec_table(self, wsdir, capsys):
        rc = run_cli(wsdir, "exec", "testpg", "--sql",
                     "SELECT name FROM customers ORDER BY id", "--format", "table")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "name" in out and "Alice" in out and "----" in out

    def test_exec_max_rows_exact(self, wsdir, capsys):
        rc = run_cli(wsdir, "exec", "testpg", "--sql",
                     "SELECT generate_series(1,10) AS n", "--max-rows", "4", "--format", "ndjson")
        assert rc == EXIT_OK
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) == 4  # exactly N, not N+1

    def test_exec_timeout_flag_cancels_slow_query(self, wsdir):
        # issue #94: --timeout reaches the server-side statement_timeout backstop
        parser = cli.build_parser()
        args = parser.parse_args(["--workspace", str(wsdir), "exec", "testpg", "--sql",
                                  "SELECT pg_sleep(3)", "--timeout", "1", "--format", "json"])
        workspace.configure_workspace(args.workspace)
        with pytest.raises(core.QuarryError) as ei:
            args.func(args)
        assert ei.value.exit_code == EXIT_SQL_ERROR
        msg = str(ei.value).lower()
        assert "statement timeout" in msg
        assert "--timeout" in msg

    def test_exec_write_blocked_exit_8(self, wsdir, pg_exec, capsys):
        # prove the write did NOT run: create a scratch table, block a delete, confirm intact
        pg_exec("DROP TABLE IF EXISTS cli_tmp_wb; "
                "CREATE TABLE cli_tmp_wb(id int); INSERT INTO cli_tmp_wb VALUES (1),(2);")
        try:
            rc = run_cli(wsdir, "exec", "testpg", "--sql", "DELETE FROM cli_tmp_wb")
            assert rc == EXIT_SAFETY_BLOCKED
            _, out, _ = pg_exec("SELECT count(*) FROM cli_tmp_wb")
            assert out.strip() == "2"  # delete was blocked, rows still there
        finally:
            pg_exec("DROP TABLE IF EXISTS cli_tmp_wb")

    def test_exec_multi_statement_blocked(self, wsdir):
        rc = run_cli(wsdir, "exec", "testpg", "--sql", "SELECT 1; DROP TABLE customers")
        assert rc == EXIT_SAFETY_BLOCKED

    def test_exec_write_flag_lifts_safety_block(self, wsdir, pg_exec):
        # --write must pass enforce_safety (no EXIT_SAFETY_BLOCKED). The CLI's
        # json/csv/table path then wraps the statement in `FROM (<sql>)`, which
        # Postgres rejects for a bare INSERT — so we get a plain SQL error (3),
        # NOT the safety block (8). The distinction proves --write took effect.
        pg_exec("DROP TABLE IF EXISTS cli_tmp_w2; CREATE TABLE cli_tmp_w2(id int)")
        try:
            rc = run_cli(wsdir, "exec", "testpg", "--sql",
                         "INSERT INTO cli_tmp_w2 VALUES (7)", "--write", "--format", "json")
            assert rc != EXIT_SAFETY_BLOCKED
            assert rc == EXIT_SQL_ERROR
        finally:
            pg_exec("DROP TABLE IF EXISTS cli_tmp_w2")

    def test_exec_from_file(self, wsdir, tmp_path, capsys):
        f = tmp_path / "q.sql"
        f.write_text("SELECT 5 AS five", encoding="utf-8")
        rc = run_cli(wsdir, "exec", "testpg", "--file", str(f), "--format", "json")
        assert rc == EXIT_OK
        assert json.loads(capsys.readouterr().out)[0]["five"] == 5

    def test_exec_missing_file(self, wsdir):
        rc = run_cli(wsdir, "exec", "testpg", "--file", str(wsdir / "nope.sql"))
        assert rc == EXIT_USAGE

    def test_exec_missing_sql_and_file(self, wsdir):
        assert run_cli(wsdir, "exec", "testpg") == EXIT_USAGE

    def test_exec_with_params(self, wsdir, capsys):
        rc = run_cli(wsdir, "exec", "testpg", "--sql",
                     "SELECT :cid::int AS c", "--param", "cid=42", "--format", "json")
        assert rc == EXIT_OK
        assert json.loads(capsys.readouterr().out)[0]["c"] == 42

    def test_exec_unknown_db(self, wsdir):
        assert run_cli(wsdir, "exec", "ghostdb", "--sql", "SELECT 1") == EXIT_USAGE

    def test_exec_sql_error_maps_to_exit_3(self, wsdir):
        # a read-only but invalid query raises QuarryError inside execute_sql,
        # caught by _execute and returned as EXIT_SQL_ERROR.
        rc = run_cli(wsdir, "exec", "testpg", "--sql",
                     "SELECT * FROM cli_tmp_absolutely_no_such_table", "--format", "json")
        assert rc == EXIT_SQL_ERROR


@requires_db
@pytest.mark.integration
class TestRunDB:
    def test_run_saved_query(self, wsdir, capsys):
        _seed_query(wsdir, "testpg", "allc",
                    "-- @name: allc\n-- @db: testpg\n"
                    "SELECT id, name FROM customers ORDER BY id;\n")
        rc = run_cli(wsdir, "run", "allc", "--format", "json")
        assert rc == EXIT_OK
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == 3 and rows[0]["name"] == "Alice"

    def test_run_with_limit(self, wsdir, capsys):
        _seed_query(wsdir, "testpg", "lim",
                    "-- @name: lim\n-- @db: testpg\n"
                    "SELECT id FROM customers ORDER BY id LIMIT 100;\n")
        rc = run_cli(wsdir, "run", "lim", "--limit", "1", "--format", "json")
        assert rc == EXIT_OK
        assert len(json.loads(capsys.readouterr().out)) == 1

    def test_run_full_strips_limit(self, wsdir, capsys):
        _seed_query(wsdir, "testpg", "fl",
                    "-- @name: fl\n-- @db: testpg\n"
                    "SELECT id FROM customers ORDER BY id LIMIT 1;\n")
        rc = run_cli(wsdir, "run", "fl", "--full", "--format", "json")
        assert rc == EXIT_OK
        assert len(json.loads(capsys.readouterr().out)) == 3  # LIMIT 1 removed

    def test_run_strict_passes(self, wsdir, capsys):
        _seed_query(wsdir, "testpg", "sok",
                    "-- @name: sok\n-- @db: testpg\nSELECT 1 AS ok;\n")
        rc = run_cli(wsdir, "run", "sok", "--strict", "--format", "json")
        assert rc == EXIT_OK
        assert json.loads(capsys.readouterr().out)[0]["ok"] == 1

    def test_run_strict_fails_on_bad_sql(self, wsdir):
        _seed_query(wsdir, "testpg", "sbad",
                    "-- @name: sbad\n-- @db: testpg\nSELECT * FROM cli_tmp_nonexistent_zzz;\n")
        rc = run_cli(wsdir, "run", "sbad", "--strict")
        assert rc == core.EXIT_STRICT_DRIFT

    def test_run_with_required_param(self, wsdir, capsys):
        _seed_query(wsdir, "testpg", "byid",
                    "-- @name: byid\n-- @db: testpg\n-- @param: cid (int, required)\n"
                    "SELECT name FROM customers WHERE id = :cid;\n")
        rc = run_cli(wsdir, "run", "byid", "--param", "cid=1", "--format", "json")
        assert rc == EXIT_OK
        assert json.loads(capsys.readouterr().out)[0]["name"] == "Alice"

    def test_run_missing_required_param(self, wsdir):
        _seed_query(wsdir, "testpg", "needp",
                    "-- @name: needp\n-- @db: testpg\n-- @param: cid (int, required)\n"
                    "SELECT :cid::int AS c;\n")
        assert run_cli(wsdir, "run", "needp") == EXIT_USAGE


@requires_db
@pytest.mark.integration
class TestSaveValidateRemoveEdit:
    def test_save_roundtrip_and_validate(self, wsdir, capsys):
        rc = run_cli(wsdir, "save", "s_roundtrip", "--db", "testpg",
                     "--desc", "custs", "--sql", "SELECT id FROM customers ORDER BY id")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "saved" in out and "validated against testpg" in out
        target = wsdir / "queries" / "testpg" / "s_roundtrip.sql"
        assert target.exists()
        # validation stamped @last-validated
        assert "@last-validated" in target.read_text()

    def test_save_already_exists(self, wsdir):
        run_cli(wsdir, "save", "dup", "--db", "testpg", "--sql", "SELECT 1", "--no-validate")
        rc = run_cli(wsdir, "save", "dup", "--db", "testpg", "--sql", "SELECT 2", "--no-validate")
        assert rc == EXIT_USAGE
        # --overwrite lets it through
        rc = run_cli(wsdir, "save", "dup", "--db", "testpg", "--sql", "SELECT 2",
                     "--no-validate", "--overwrite")
        assert rc == EXIT_OK

    def test_save_no_validate_skips_db(self, wsdir, capsys):
        rc = run_cli(wsdir, "save", "nov", "--db", "testpg",
                     "--sql", "SELECT 1", "--no-validate")
        assert rc == EXIT_OK
        assert "validated against" not in capsys.readouterr().out

    def test_save_with_params(self, wsdir):
        rc = run_cli(wsdir, "save", "withp", "--db", "testpg", "--no-validate",
                     "--param", "cid:int:required", "--param", "st:text:default=paid",
                     "--sql", "SELECT :cid::int AS c")
        assert rc == EXIT_OK
        q = core.load_query("withp")
        assert [p.name for p in q.params] == ["cid", "st"]
        assert q.params[0].required and q.params[1].default == "paid"

    def test_save_empty_sql(self, wsdir):
        assert run_cli(wsdir, "save", "empty", "--db", "testpg", "--sql", "   ") == EXIT_USAGE

    def test_save_no_sql_no_file(self, wsdir):
        assert run_cli(wsdir, "save", "nada", "--db", "testpg") == EXIT_USAGE

    def test_save_from_file(self, wsdir, tmp_path):
        f = tmp_path / "body.sql"
        f.write_text("SELECT id FROM customers", encoding="utf-8")
        rc = run_cli(wsdir, "save", "fromfile", "--db", "testpg",
                     "--file", str(f), "--no-validate")
        assert rc == EXIT_OK
        assert "SELECT id FROM customers" in core.load_query("fromfile").sql

    def test_validate_resolves_logical_db(self, wsdir, capsys):
        # env-set: connection key shop_dev has db=testpg-equivalent logical name.
        (wsdir / "connections.toml").write_text(
            f'[shop_dev]\nurl = "{TEST_DB_URL}"\nengine="postgres"\ndb="shop"\nenv="test"\n',
            encoding="utf-8")
        _seed_query(wsdir, "shop", "ping", "-- @name: ping\n-- @db: shop\nSELECT 1 AS ok;\n")
        rc = run_cli(wsdir, "validate", "ping")
        assert rc == EXIT_OK
        assert "schema OK" in capsys.readouterr().out
        # validating stamps @last-validated into the file
        assert "@last-validated" in (wsdir / "queries" / "shop" / "ping.sql").read_text()

    def test_validate_bad_query_returns_error(self, wsdir):
        _seed_query(wsdir, "testpg", "vbad",
                    "-- @name: vbad\n-- @db: testpg\nSELECT * FROM cli_tmp_ghost_zzz;\n")
        assert run_cli(wsdir, "validate", "vbad") == EXIT_SQL_ERROR

    def test_validate_replaces_existing_last_validated(self, wsdir):
        p = _seed_query(wsdir, "testpg", "vre",
                        "-- @name: vre\n-- @db: testpg\n-- @last-validated: OLDSTAMP\nSELECT 1 AS ok;\n")
        assert run_cli(wsdir, "validate", "vre") == EXIT_OK
        text = p.read_text()
        assert "OLDSTAMP" not in text and text.count("@last-validated") == 1

    def test_save_validation_failure_warns(self, wsdir, capsys):
        # a syntactically-invalid query: saved to disk, then validation fails ->
        # warning printed and the validate rc is returned (not EXIT_OK).
        rc = run_cli(wsdir, "save", "svfail", "--db", "testpg",
                     "--sql", "SELECT * FROM cli_tmp_no_such_table_zzz")
        assert rc == EXIT_SQL_ERROR
        assert (wsdir / "queries" / "testpg" / "svfail.sql").exists()  # saved anyway
        assert "validation failed" in capsys.readouterr().err

    def test_remove_with_yes(self, wsdir, capsys):
        p = _seed_query(wsdir, "testpg", "rmme", "-- @name: rmme\n-- @db: testpg\nSELECT 1;\n")
        assert p.exists()
        rc = run_cli(wsdir, "remove", "rmme", "--yes")
        assert rc == EXIT_OK
        assert not p.exists()
        assert "removed" in capsys.readouterr().out

    def test_remove_missing(self, wsdir):
        assert run_cli(wsdir, "remove", "ghost_query_zzz", "--yes") == EXIT_USAGE

    def test_edit_uses_editor_env(self, wsdir, monkeypatch):
        _seed_query(wsdir, "testpg", "ed", "-- @name: ed\n-- @db: testpg\nSELECT 1;\n")
        seen = {}

        def fake_call(cmd):
            seen["cmd"] = cmd
            return 0

        monkeypatch.setenv("EDITOR", "true")
        monkeypatch.setattr(cli.subprocess, "call", fake_call)
        rc = run_cli(wsdir, "edit", "ed")
        assert rc == 0
        assert seen["cmd"][0] == "true"
        assert seen["cmd"][1].endswith("ed.sql")


@requires_db
@pytest.mark.integration
class TestDescribeTableAndConnTestDB:
    def test_describe_table_json(self, wsdir, capsys):
        rc = run_cli(wsdir, "describe-table", "testpg", "customers", "--format", "json")
        assert rc == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj["table"] == "customers"
        colnames = {c["column_name"] for c in obj["columns"]}
        assert {"id", "name", "email", "created_at"} <= colnames

    def test_describe_table_text(self, wsdir, capsys):
        rc = run_cli(wsdir, "describe-table", "testpg", "customers", "--format", "text")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        # psql \d+ output includes the table title and column names
        assert "customers" in out and "email" in out

    def test_describe_table_text_cache_hit_skips_psql(self, wsdir, monkeypatch, capsys):
        # `qy schema`/describe-table's default (unflagged) form is postgres
        # --format text; issue #97 review flagged that it bypassed the shared
        # cache entirely. A second call for the same table must not shell out
        # to psql again.
        from quarry import cli

        rc1 = run_cli(wsdir, "describe-table", "testpg", "customers", "--format", "text")
        assert rc1 == EXIT_OK
        first = capsys.readouterr().out
        assert "email" in first

        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda *a, **k: pytest.fail("must not shell out to psql on a cache hit"))
        rc2 = run_cli(wsdir, "describe-table", "testpg", "customers", "--format", "text")
        assert rc2 == EXIT_OK
        assert capsys.readouterr().out == first

    def test_describe_table_text_handles_quote_in_name(self, wsdir, capsys):
        # The text path shells out to `psql -c '\d+ "<name>"'`; psql's own metacommand
        # parsing swallows a stray quote without a crash and still returns 0.
        rc = run_cli(wsdir, "describe-table", "testpg", 'customers"; DROP', "--format", "text")
        assert rc == EXIT_OK
        # customers table is what psql resolves the quoted-identifier prefix to
        assert "customers" in capsys.readouterr().out

    def test_describe_table_json_quote_is_not_a_crash(self, wsdir, capsys):
        # The JSON path now goes through core.cached_columns, which binds the
        # table name as a query parameter (issue #97) instead of splicing it
        # into the SQL text — a quote is just part of a nonexistent table name,
        # never a SQL syntax error or a Python crash.
        rc = run_cli(wsdir, "describe-table", "testpg", "cust'omers", "--format", "json")
        assert rc == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj == {"table": "cust'omers", "columns": []}

    def test_connections_test_ok(self, wsdir, capsys):
        rc = run_cli(wsdir, "connections", "test", "testpg")
        assert rc == EXIT_OK
        assert "connected to quarry_test" in capsys.readouterr().out


@requires_db
@pytest.mark.integration
class TestPing:
    def test_ping_single_ok(self, wsdir, capsys):
        rc = run_cli(wsdir, "ping", "testpg")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "✓ testpg (postgres): ok" in out
        assert "ms" in out

    def test_ping_single_json_shape(self, wsdir, capsys):
        rc = run_cli(wsdir, "ping", "testpg", "--format", "json")
        assert rc == EXIT_OK
        obj = json.loads(capsys.readouterr().out)
        assert obj["key"] == "testpg"
        assert obj["engine"] == "postgres"
        assert obj["ok"] is True
        assert obj["error"] is None
        assert isinstance(obj["elapsed_ms"], int)

    def test_ping_all_single_connection(self, wsdir, capsys):
        rc = run_cli(wsdir, "ping", "--all")
        assert rc == EXIT_OK
        out = capsys.readouterr().out
        assert "✓ testpg" in out
        assert "1/1 reachable" in out

    def test_ping_unreachable_connection_exits_1_with_reason(self, wsdir, capsys):
        # localhost:1 refuses instantly (no listener) — a fast, real connection
        # failure, no mocking or a real timeout wait needed.
        with (wsdir / "connections.toml").open("a", encoding="utf-8") as f:
            f.write('\n[bad]\nurl = "postgresql://localhost:1/nosuchdb"\nengine = "postgres"\n')
        rc = run_cli(wsdir, "ping", "bad", "--timeout", "3")
        assert rc == 1
        out = capsys.readouterr().out
        assert "✗ bad (postgres): fail" in out
        assert "connection" in out.lower()  # failure reason surfaced, not just "fail"

    def test_ping_all_mixed_reachability_exits_1(self, wsdir, capsys):
        with (wsdir / "connections.toml").open("a", encoding="utf-8") as f:
            f.write('\n[bad]\nurl = "postgresql://localhost:1/nosuchdb"\nengine = "postgres"\n')
        rc = run_cli(wsdir, "ping", "--all", "--timeout", "3")
        assert rc == 1
        out = capsys.readouterr().out
        assert "✓ testpg" in out
        assert "✗ bad" in out
        assert "1/2 reachable" in out

    def test_ping_requires_connection_or_all(self, wsdir):
        assert run_cli(wsdir, "ping") == EXIT_USAGE

    def test_ping_rejects_connection_and_all_together(self, wsdir):
        assert run_cli(wsdir, "ping", "testpg", "--all") == EXIT_USAGE

    def test_ping_unknown_connection_is_usage_error(self, wsdir):
        assert run_cli(wsdir, "ping", "no_such_connection") == EXIT_USAGE


@pytest.mark.unit
class TestDescribeTableUnsupported:
    def test_describe_table_unsupported_engine(self, wsdir):
        # redis/neptune reject describe-table before any tunnel/DB access, so this
        # is a pure usage error and needs no live engine.
        (wsdir / "connections.toml").write_text(
            '[cache]\nurl = "redis://localhost:6379/0"\nengine = "redis"\n', encoding="utf-8")
        assert run_cli(wsdir, "describe-table", "cache", "sometable") == EXIT_USAGE


@pytest.mark.integration
@requires_db
class TestProdWriteGuard:
    def test_exec_prod_write_aborted_by_prompt(self, wsdir, monkeypatch, pg_exec):
        # a write against a prod-tagged connection prompts; answering 'n' aborts
        # (EXIT_USAGE) and the write never reaches the DB.
        (wsdir / "connections.toml").write_text(
            f'[prodpg]\nurl = "{TEST_DB_URL}"\nengine = "postgres"\nenv = "prod"\n',
            encoding="utf-8")
        pg_exec("DROP TABLE IF EXISTS cli_tmp_prod; CREATE TABLE cli_tmp_prod(id int); "
                "INSERT INTO cli_tmp_prod VALUES (1);")
        try:
            monkeypatch.setattr(cli.sys, "stdin", io.StringIO("n\n"))
            rc = run_cli(wsdir, "exec", "prodpg", "--sql",
                         "DELETE FROM cli_tmp_prod", "--write")
            assert rc == EXIT_USAGE  # aborted
            _, out, _ = pg_exec("SELECT count(*) FROM cli_tmp_prod")
            assert out.strip() == "1"  # untouched
        finally:
            pg_exec("DROP TABLE IF EXISTS cli_tmp_prod")
