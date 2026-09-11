"""Query ownership and skill browsing entry points, without a live database."""

import json
import os
import subprocess
import sys

import pytest

from quarry import workspace


def configure_existing(paths):
    from pathlib import Path
    for path in paths.split(os.pathsep):
        Path(path).mkdir(parents=True, exist_ok=True)
    workspace.configure_workspace(paths)


def invoke(tmp_path, *args):
    env = dict(os.environ, QUARRY_CONFIG=str(tmp_path / "config.toml"))
    env.pop("QUARRY_QUERIES_DIR", None)
    env.pop("QUARRY_CONNECTIONS_FILE", None)
    return subprocess.run([sys.executable, "-m", "quarry.cli", *map(str, args)],
                          env=env, capture_output=True, text=True, timeout=15, check=False)


def make_skill(tmp_path):
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("test skill")
    return skill


def test_save_visible_in_skill_and_link_recreated(tmp_path):
    skill = make_skill(tmp_path)
    ws = tmp_path / "acme"
    ws.mkdir()
    (ws / "connections.toml").write_text('[db]\nurl="postgresql://localhost/example"\n')
    prefix = ("--workspace", ws, "--skill-dir", skill)
    result = invoke(tmp_path, *prefix, "save", "one", "--db", "db", "--sql", "SELECT 1", "--no-validate")
    assert result.returncode == 0, result.stderr
    source = ws / "queries/db/one.sql"
    link = skill / "queries/acme"
    assert not (ws / "queries").is_symlink()
    assert link.is_symlink() and link.resolve() == ws / "queries"
    assert (link / "db/one.sql").read_text() == source.read_text()
    assert not (link / "connections.toml").exists()
    for remove in (False, True):
        if remove:
            link.unlink()
        result = invoke(tmp_path, *prefix, "list", "--format", "json")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)[0]["name"] == "one"
        assert link.is_symlink()
    duplicate = invoke(tmp_path, *prefix, "save", "one", "--db", "db", "--sql", "SELECT 2", "--no-validate")
    assert duplicate.returncode != 0
    assert "SELECT 1" in source.read_text()


def test_multiple_workspaces_and_conflict_preserves_files(tmp_path):
    skill = make_skill(tmp_path)
    first, second = tmp_path / "one", tmp_path / "two"
    configure_existing(os.pathsep.join(map(str, (first, second))))
    workspace.ensure_skill_links(str(skill))
    assert (skill / "queries/one").resolve() == first / "queries"
    assert (skill / "queries/two").resolve() == second / "queries"
    (skill / "queries/two").unlink()
    (skill / "queries/two").mkdir()
    note = skill / "queries/two/note.sql"
    note.write_text("SELECT 42")
    with pytest.raises(ValueError, match="already exists"):
        workspace.ensure_skill_links(str(skill))
    assert note.read_text() == "SELECT 42"


def test_duplicate_workspace_names_do_not_choose_silently(tmp_path):
    skill = make_skill(tmp_path)
    configure_existing(os.pathsep.join(map(str, (tmp_path / "a/db", tmp_path / "b/db"))))
    with pytest.raises(ValueError, match="duplicate workspace name"):
        workspace.ensure_skill_links(str(skill))
    assert not (skill / "queries").exists()


def test_foreign_symlink_is_not_replaced(tmp_path):
    skill = make_skill(tmp_path)
    configure_existing(str(tmp_path / "acme"))
    (skill / "queries").mkdir()
    link = skill / "queries/acme"
    link.symlink_to(tmp_path / "old-location")
    with pytest.raises(ValueError, match="points elsewhere"):
        workspace.ensure_skill_links(str(skill))
    assert link.readlink() == tmp_path / "old-location"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "..", "nested/name", "bad\nheader"])
@pytest.mark.parametrize("field", ["name", "db"])
def test_save_rejects_path_and_header_injection(tmp_path, name, field):
    result = invoke(tmp_path, "--workspace", tmp_path / "acme", "save", name if field == "name" else "one",
                    "--db", name if field == "db" else "db", "--sql", "SELECT 1", "--no-validate")
    assert result.returncode != 0
    assert "filename component" in result.stderr
    assert not (tmp_path / "acme/queries").exists()


def test_link_failure_stops_command_cleanly(tmp_path):
    result = invoke(tmp_path, "--workspace", tmp_path / "acme", "--skill-dir", tmp_path,
                    "list", "--format", "json")
    assert result.returncode != 0
    assert "SKILL.md" in result.stderr and "Traceback" not in result.stderr
    assert not result.stdout


def test_skill_inside_query_directory_cannot_create_recursive_link(tmp_path):
    ws = tmp_path / "acme"
    queries = ws / "queries"
    queries.mkdir(parents=True)
    skill = make_skill(queries)
    configure_existing(str(ws))
    with pytest.raises(ValueError, match="recursive"):
        workspace.ensure_skill_links(str(skill))
    assert not (skill / "queries").exists()


def test_symlinked_skill_queries_root_is_not_followed(tmp_path):
    skill = make_skill(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (skill / "queries").symlink_to(foreign, target_is_directory=True)
    configure_existing(str(tmp_path / "acme"))
    with pytest.raises(ValueError, match="real directory"):
        workspace.ensure_skill_links(str(skill))
    assert list(foreign.iterdir()) == []


def test_concurrent_link_creation_is_idempotent(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from threading import Barrier

    skill = make_skill(tmp_path)
    configure_existing(str(tmp_path / 'acme'))
    barrier = Barrier(2)
    original = Path.symlink_to

    def simultaneous(self, target, target_is_directory=False):
        barrier.wait(timeout=5)
        return original(self, target, target_is_directory=target_is_directory)

    monkeypatch.setattr(Path, 'symlink_to', simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(workspace.ensure_skill_links, str(skill)) for _ in range(2)]
        for call in calls:
            call.result(timeout=10)
    assert (skill / 'queries/acme').resolve() == tmp_path / 'acme/queries'


def test_missing_workspace_is_not_recreated_for_read_only_list(tmp_path, capsys, monkeypatch):
    from quarry import cli
    skill = make_skill(tmp_path)
    missing = tmp_path / 'unmounted/workspace'
    monkeypatch.setattr(sys, 'argv', ['qy', '--workspace', str(missing), '--skill-dir', str(skill), 'list'])
    assert cli.main() != 0
    assert 'missing or unavailable' in capsys.readouterr().err
    assert not missing.parent.exists()
    assert not (skill / 'queries').exists()


def test_link_refuses_query_file_and_filesystem_root(tmp_path):
    skill = make_skill(tmp_path)
    ws = tmp_path / 'acme'
    configure_existing(str(ws))
    (ws / 'queries').write_text('keep')
    with pytest.raises(ValueError, match='not a directory'):
        workspace.ensure_skill_links(str(skill))
    assert (ws / 'queries').read_text() == 'keep'
    from pathlib import Path
    workspace.configure_workspace(Path(tmp_path.anchor).as_posix())
    with pytest.raises(ValueError, match='filesystem-root'):
        workspace.ensure_skill_links(str(skill))


def test_conflicting_entry_created_during_linking_is_preserved(tmp_path, monkeypatch):
    from pathlib import Path
    skill = make_skill(tmp_path)
    configure_existing(str(tmp_path / 'acme'))
    def competing_file(self, target, target_is_directory=False):
        self.write_text('another writer')
        raise FileExistsError()
    monkeypatch.setattr(Path, 'symlink_to', competing_file)
    with pytest.raises(ValueError, match='conflicts'):
        workspace.ensure_skill_links(str(skill))
    assert (skill / 'queries/acme').read_text() == 'another writer'
