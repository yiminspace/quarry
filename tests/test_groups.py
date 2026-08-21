"""g-P0 unit tests: connection groups, env-sets, and resolution (no network)."""

from __future__ import annotations

import os

import pytest

from quarry import core, workspace

CONNS = """
[blog]
url = "postgresql://u@127.0.0.1:5432/blog"
group = "acme"
env = "prod"

[shop_dev]
url = "postgresql://u@dev-host/shop"
group = "shop"
db = "shop"
env = "dev"

[shop_prod]
url = "postgresql://u@prod-host/shop"
group = "shop"
db = "shop"
env = "prod"

[shop_jp]
url = "postgresql://u@tokyo-host/shop"
group = "shop"
db = "shop"
env = "jp"

[cache_prod]
url = "postgresql://u@prod-host/cache"
group = "cache"
db = "cache"
env = "prod"

[cache_local]
url = "postgresql://u@127.0.0.1/cache"
group = "cache"
db = "cache"
env = "local"
"""


@pytest.fixture()
def ws(tmp_path):
    (tmp_path / "connections.toml").write_text(CONNS)
    workspace.configure_workspace(str(tmp_path))
    yield tmp_path


def test_direct_key_backward_compatible(ws):
    assert core.resolve_connection("blog").key == "blog"


def test_envset_defaults_to_dev(ws):
    # logical db "shop" with no --env -> dev
    assert core.resolve_connection("shop").key == "shop_dev"


def test_envset_explicit_prod(ws):
    assert core.resolve_connection("shop", env="prod").key == "shop_prod"
    assert core.resolve_connection("shop", env="jp").key == "shop_jp"


def test_key_plus_env_resolves_via_envset(ws):
    # legacy query with @db=<connection key> + --env still hits the right env member
    assert core.resolve_connection("shop_dev", env="jp").key == "shop_jp"
    assert core.resolve_connection("shop_jp", env="prod").key == "shop_prod"


def test_envset_unknown_env_errors(ws):
    with pytest.raises(core.QuarryError):
        core.resolve_connection("shop", env="staging")


def test_unknown_db_errors(ws):
    with pytest.raises(core.QuarryError):
        core.resolve_connection("nope")


def test_group_structure(ws):
    tree = core.group_connections()
    groups = {g["group"]: g for g in tree}
    assert set(groups) == {"acme", "shop", "cache"}
    # shop folder holds ONE logical db that is an env-set of 3
    shop = groups["shop"]["items"]
    assert len(shop) == 1
    assert shop[0]["db"] == "shop"
    assert shop[0]["is_env_set"] is True
    assert sorted(e["env"] for e in shop[0]["envs"]) == ["dev", "jp", "prod"]
    # no local env in the set -> registration order is preserved (issue #44)
    assert [e["env"] for e in shop[0]["envs"]] == ["dev", "prod", "jp"]
    # acme folder holds blog as a singleton
    assert groups["acme"]["items"][0]["db"] == "blog"


def test_local_env_always_sorts_first(ws):
    # "cache" registers prod before local -> group_connections() still puts
    # local first, so it's both the leftmost pill and the default pick
    # (envs.find(dev) || envs[0]) when there's no dev env.
    tree = core.group_connections()
    cache = next(g for g in tree if g["group"] == "cache")["items"][0]
    assert [e["env"] for e in cache["envs"]] == ["local", "prod"]


def test_ungrouped_env_member_inherits_its_only_sibling_group(tmp_path):
    (tmp_path / "connections.toml").write_text(
        '[queue_local]\nurl = "redis://localhost:6379/0"\nengine = "redis"\n'
        'group = "brain"\ndb = "queue"\nenv = "local"\n'
        '[queue]\nurl = "redis://dev.example.com:6379/0"\nengine = "redis"\nenv = "dev"\n',
        encoding="utf-8",
    )
    try:
        workspace.configure_workspace(str(tmp_path))
        tree = core.group_connections()
        queue_items = [item for group in tree for item in group["items"] if item["db"] == "queue"]
        assert len(queue_items) == 1
        assert [e["env"] for e in queue_items[0]["envs"]] == ["local", "dev"]
        assert next(g for g in tree if queue_items[0] in g["items"])["group"] == "brain"
        assert not any(g["group"] is None for g in tree)
    finally:
        workspace.configure_workspace(None)


def test_ungrouped_env_member_stays_visible_when_sibling_groups_conflict(tmp_path):
    (tmp_path / "connections.toml").write_text(
        '[queue_dev]\nurl = "redis://dev.example.com:6379/0"\nengine = "redis"\n'
        'group = "brain"\ndb = "queue"\nenv = "dev"\n'
        '[queue_prod]\nurl = "redis://prod.example.com:6379/0"\nengine = "redis"\n'
        'group = "ops"\ndb = "queue"\nenv = "prod"\n'
        '[queue_local]\nurl = "redis://localhost:6379/0"\nengine = "redis"\n'
        'db = "queue"\nenv = "local"\n',
        encoding="utf-8",
    )
    try:
        workspace.configure_workspace(str(tmp_path))
        tree = core.group_connections()
        assert {g["group"] for g in tree} == {"brain", "ops", None}
        assert next(g for g in tree if g["group"] is None)["items"][0]["envs"][0]["env"] == "local"
    finally:
        workspace.configure_workspace(None)


def test_inherited_group_uses_the_declaring_siblings_workspace_origin(tmp_path):
    ungrouped = tmp_path / "ungrouped"
    grouped = tmp_path / "grouped"
    ungrouped.mkdir()
    grouped.mkdir()
    (ungrouped / "connections.toml").write_text(
        '[queue]\nurl = "redis://dev.example.com:6379/0"\nengine = "redis"\nenv = "dev"\n',
        encoding="utf-8",
    )
    (grouped / "connections.toml").write_text(
        '[queue_local]\nurl = "redis://localhost:6379/0"\nengine = "redis"\n'
        'group = "brain"\ndb = "queue"\nenv = "local"\n',
        encoding="utf-8",
    )
    try:
        workspace.configure_workspace(f"{ungrouped}{os.pathsep}{grouped}")
        queue_group = next(g for g in core.group_connections() if g["group"] == "brain")
        assert queue_group["ws"] == str(grouped.resolve())
    finally:
        workspace.configure_workspace(None)


def test_prod_write_is_read_only_by_default():
    # engine-level: write blocked unless allow_write
    with pytest.raises(core.QuarryError):
        core.enforce_safety("delete from t", allow_write=False, max_rows=None)
    sql, _ = core.enforce_safety("delete from t", allow_write=True, max_rows=None)
    assert sql.startswith("delete")
