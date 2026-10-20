"""Configuration rejection and persisted-history retry boundaries."""
import locale
import subprocess

import pytest

from quarry import assistant_config as config, assistant_store as store
from quarry.core import QuarryError


@pytest.fixture
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("QUARRY_AI_HOME", str(tmp_path / "history"))
    config.configure_model(base_url="https://model.example/v1", model="test", api_key="test-private-key")
    return tmp_path


@pytest.mark.parametrize("overrides", [
    {"base_url": None}, {"base_url": "https://model.example:not-a-port"},
    {"base_url": "https://[broken"}, {"model": ""}, {"model": "x" * 201},
    {"model": "unsafe\nmodel"}, {"thinking": "invalid"}, {"timeout": True},
    {"timeout": 301}, {"no_key": True}, {"api_key_env": "KEY"},
    {"api_key": None}, {"api_key": None, "api_key_env": "invalid-key-name"},
    {"api_key": ""}, {"api_key": "key\nheader"}, {"api_key": "x" * 8193},
])
def test_rejected_configuration_preserves_both_files(private_home, overrides):
    files = [private_home / "config.toml", private_home / "assistant-credentials.json"]
    before = [path.read_bytes() for path in files]
    arguments = dict(base_url="https://other.example/v1", model="new", api_key="replacement-test-key")
    with pytest.raises(QuarryError):
        config.configure_model(**(arguments | overrides))
    assert [path.read_bytes() for path in files] == before
    assert config.load_model_config().api_key == "test-private-key"


@pytest.mark.parametrize("settings", [
    'assistant = true',
    '[assistant]\nbase_url="https://model.example"\nmodel=42',
    '[assistant]\nbase_url="https://model.example"\nmodel="test"\nauth="unknown"',
    '[assistant]\nbase_url="https://model.example"\nmodel="test"\nauth="none"\ntimeout=0',
    '[assistant]\nbase_url="https://model.example"\nmodel="test"\nauth="none"\nthinking="bad"',
])
def test_invalid_saved_model_settings_are_not_reported_ready(private_home, settings):
    (private_home / "config.toml").write_text(settings)
    with pytest.raises(QuarryError):
        config.load_model_config()
    assert config.config_status() == {"configured": False}


@pytest.mark.parametrize("credentials", ["not json", "[]", '{}',
    '{"base_url":"https://other.example","api_key":"wrong-endpoint-key"}'])
def test_unusable_stored_credentials_never_fall_back_to_another_key(private_home, credentials):
    (private_home / "assistant-credentials.json").write_text(credentials)
    with pytest.raises(QuarryError, match="API key is unavailable"):
        config.load_model_config()
    assert config.config_details()["api_key"] is None


@pytest.mark.parametrize("settings", ['gui=true', '[gui]\nassistant=true'])
def test_malformed_gui_section_cannot_replace_model_configuration(private_home, settings):
    path = private_home / "config.toml"
    original = settings + "\n" + path.read_text()
    path.write_text(original)
    with pytest.raises(QuarryError, match="Invalid.*gui.assistant"):
        config.gui_preferences()
    with pytest.raises(QuarryError, match="Cannot update GUI reply language"):
        config.set_reply_language("zh")
    assert path.read_text() == original


def test_locale_detection_survives_unavailable_platform_preferences(monkeypatch):
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config.sys, "platform", "darwin")
    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(config.subprocess, "run", unavailable)
    monkeypatch.setattr(locale, "getlocale", lambda: (_ for _ in ()).throw(ValueError("unknown locale")))
    assert config.system_reply_language() == "en"


@pytest.fixture
def conversation(private_home):
    return store.create(db="shop", env="dev", workspace=str(private_home), sql="SELECT 1", fingerprint="test")


@pytest.mark.parametrize("role,content", [("tool", "text"), ("user", None), ("assistant", "x" * 200001)])
def test_invalid_message_leaves_history_unchanged(conversation, role, content):
    with pytest.raises(QuarryError):
        store.append_message(conversation["id"], role, content)
    assert store.get(conversation["id"]) == conversation


@pytest.mark.parametrize("sql", [None, "x" * 200001])
def test_invalid_sql_cannot_replace_a_persisted_draft(conversation, sql):
    with pytest.raises(QuarryError):
        store.update(conversation["id"], conversation["revision"], sql)
    assert store.get(conversation["id"]) == conversation


def test_duplicate_reply_cannot_create_another_history_item(conversation):
    saved = store.append_message(conversation["id"], "assistant", "Done", id="reply")
    with pytest.raises(store.Conflict, match="already been saved"):
        store.append_message(conversation["id"], "assistant", "Duplicate", id="reply")
    assert store.get(conversation["id"]) == saved


def test_noop_proposal_does_not_create_a_checkpoint(conversation):
    saved = store.append_message(conversation["id"], "assistant", "Already correct", id="reply", sql="SELECT 1")
    applied = store.apply(saved["id"], saved["revision"], "SELECT 1", "reply")
    assert applied["checkpoints"] == []
    assert applied["revision"] == saved["revision"]
    assert "checkpointId" not in applied["messages"][-1]


@pytest.mark.parametrize("explicit", [False, True])
def test_applied_proposal_retry_preserves_checkpoint_and_history(conversation, explicit):
    saved = store.append_message(conversation["id"], "assistant", "Changed", id="reply", sql="SELECT 2", baseRevision=0)
    applied = store.apply(saved["id"], saved["revision"], "SELECT 2", "reply")
    for revision in (saved["revision"], applied["revision"]):
        assert store.apply(saved["id"], revision, "SELECT 2", "reply", explicit=explicit) == applied
    assert store.get(saved["id"]) == applied


@pytest.mark.parametrize("revision", [True, -1, 999])
def test_apply_retry_rejects_invalid_revision(conversation, revision):
    saved = store.append_message(conversation["id"], "assistant", "Changed", id="reply", sql="SELECT 2")
    applied = store.apply(saved["id"], 0, "SELECT 2", "reply")
    with pytest.raises(store.Conflict):
        store.apply(saved["id"], revision, "SELECT 2", "reply", explicit=True)
    assert store.get(saved["id"]) == applied


def test_apply_retry_never_overwrites_a_newer_saved_draft(conversation):
    saved = store.append_message(conversation["id"], "assistant", "Changed", id="reply", sql="SELECT 2")
    applied = store.apply(saved["id"], 0, "SELECT 2", "reply")
    draft = store.update(saved["id"], applied["revision"], "SELECT 3 -- handwritten")
    with pytest.raises(store.Conflict):
        store.apply(saved["id"], saved["revision"], "SELECT 2", "reply", explicit=True)
    assert store.get(saved["id"]) == draft
    with pytest.raises(store.Conflict, match="already been applied"):
        store.apply(saved["id"], draft["revision"], "SELECT 2", "reply")
    reapplied = store.apply(saved["id"], draft["revision"], "SELECT 2", "reply", explicit=True)
    assert reapplied["sql"] == "SELECT 2"
    assert len(reapplied["checkpoints"]) == 2
    assert reapplied["checkpoints"][-1]["beforeSql"] == draft["sql"]


def test_unknown_history_targets_do_not_modify_existing_conversation(conversation):
    with pytest.raises(QuarryError, match="id is required"):
        store.get(None)
    with pytest.raises(store.NotFound, match="Conversation not found"):
        store.get("missing")
    with pytest.raises(store.NotFound, match="Checkpoint not found"):
        store.restore(conversation["id"], 0, "missing")
    assert store.get(conversation["id"]) == conversation


@pytest.mark.parametrize("revision", [True, -1, 999])
def test_restore_retry_rejects_invalid_revision_even_after_restoration(conversation, revision):
    saved = store.append_message(conversation["id"], "assistant", "Changed", id="reply", sql="SELECT 2")
    applied = store.apply(saved["id"], 0, "SELECT 2", "reply")
    checkpoint = applied["checkpoints"][0]["id"]
    restored = store.restore(saved["id"], applied["revision"], checkpoint)
    with pytest.raises(store.Conflict):
        store.restore(saved["id"], revision, checkpoint)
    assert store.get(saved["id"]) == restored
