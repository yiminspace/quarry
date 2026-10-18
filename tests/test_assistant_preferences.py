"""CLI-managed GUI reply preferences and operating-system language resolution."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from quarry import assistant_config, cli
from quarry.core import QuarryError


@pytest.fixture
def preferences_home(tmp_path, monkeypatch):
    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(assistant_config, "system_reply_language", lambda: "en")
    return tmp_path


def test_unset_preferences_do_not_require_a_model_or_create_a_file(preferences_home):
    assert assistant_config.gui_preferences() == {"replyLanguage": "system", "resolvedReplyLanguage": "en", "configured": False}
    assert not (preferences_home / "config.toml").exists()
    assert not assistant_config.config_status()["configured"]


@pytest.mark.parametrize("language", ["zh", "en", "system", "auto"])
def test_cli_can_update_preference_and_auto_is_a_system_alias(preferences_home, language):
    result = assistant_config.set_reply_language(language)
    expected = "system" if language == "auto" else language
    assert result == {"replyLanguage": expected, "resolvedReplyLanguage": "en" if expected == "system" else expected, "configured": True}
    assert assistant_config.set_reply_language("zh")["resolvedReplyLanguage"] == "zh"
    assert assistant_config.set_reply_language("system")["resolvedReplyLanguage"] == "en"
    assert tomllib.loads((preferences_home / "config.toml").read_text())["gui"]["assistant"]["reply_language"] == "system"


def test_legacy_auto_is_read_as_system_without_rewriting_config(preferences_home):
    path = preferences_home / "config.toml"
    path.write_text('[gui.assistant]\nreply_language="auto"\n')
    assert assistant_config.gui_preferences() == {"replyLanguage": "system", "resolvedReplyLanguage": "en", "configured": True}
    assert 'reply_language="auto"' in path.read_text()


@pytest.mark.parametrize("language", [None, "", "de", "ZH", True, [], {}])
def test_invalid_language_cannot_create_or_change_preferences(preferences_home, language):
    with pytest.raises(QuarryError, match="zh, en, or system"):
        assistant_config.set_reply_language(language)
    assert not (preferences_home / "config.toml").exists()


def test_preference_and_model_configuration_preserve_each_other_and_credentials(preferences_home):
    path = preferences_home / "config.toml"
    path.write_text('# Keep this comment\nworkspaces=["~/db"]\n[gui.assistant]\nwidth=42\n')
    assistant_config.configure_model(base_url="https://model.example/v1", model="first", api_key="existing-test-secret")
    credentials = preferences_home / "assistant-credentials.json"
    before_credentials = credentials.read_bytes()
    assistant_config.set_reply_language("zh")
    assistant_config.set_reply_language("auto")
    assert credentials.read_bytes() == before_credentials
    assistant_config.configure_model(base_url="https://model.example/v1", model="second", api_key="existing-test-secret")
    document = tomllib.loads(path.read_text())
    assert document["assistant"]["model"] == "second"
    assert document["gui"]["assistant"] == {"width": 42, "reply_language": "system"}
    assert document["workspaces"] == ["~/db"]
    assert "# Keep this comment" in path.read_text()
    assert "existing-test-secret" not in json.dumps(assistant_config.gui_preferences())
    assert credentials.read_bytes() == before_credentials


@pytest.mark.parametrize("model_header,gui_header", [
    ("[assistant] # model endpoint", "[gui.assistant] # reply preferences"),
    ('  ["assistant"]', '  ["gui"."assistant"]'),
    ("['assistant']", "['gui'.'assistant']"),
])
def test_model_and_language_updates_accept_valid_toml_headers(preferences_home, model_header, gui_header):
    path = preferences_home / "config.toml"
    path.write_text(f'workspaces=["~/db"]\n{model_header}\nbase_url="http://localhost:1/v1"\nmodel="before"\nauth="none"\n{gui_header}\nreply_language="en"\nwidth=42\n')
    assistant_config.configure_model(base_url="http://localhost:2/v1", model="after", api_key="new-test-secret")
    assistant_config.set_reply_language("zh")
    document = tomllib.loads(path.read_text())
    assert document["workspaces"] == ["~/db"]
    assert document["assistant"]["model"] == "after"
    assert document["gui"]["assistant"] == {"reply_language": "zh", "width": 42}
    assert assistant_config.load_model_config().api_key == "new-test-secret"


@pytest.mark.parametrize("original", [
    'workspaces=["~/db"]\nassistant={base_url="http://localhost:1/v1",model="before",auth="none"}\n',
    'note="""\n[assistant]\nnot a table\n[other]\n"""\n[assistant]\nbase_url="http://localhost:1/v1"\nmodel="before"\nauth="none"\n',
])
def test_unrepresentable_model_update_never_changes_config_or_credentials(preferences_home, original):
    path = preferences_home / "config.toml"
    path.write_text(original)
    credentials = preferences_home / "assistant-credentials.json"
    credentials.write_text('{"api_key":"existing-test-secret"}\n')
    before_config, before_credentials = path.read_bytes(), credentials.read_bytes()
    with pytest.raises(QuarryError, match="Cannot update model configuration"):
        assistant_config.configure_model(base_url="http://localhost:2/v1", model="after", api_key="replacement-test-secret")
    assert path.read_bytes() == before_config
    assert credentials.read_bytes() == before_credentials
    assert tomllib.loads(path.read_text())["assistant"]["model"] == "before"


def test_cli_language_show_change_and_auto_are_gui_only(preferences_home, capsys):
    parser = cli.build_parser()
    for index, (value, expected) in enumerate(((None, "system"), ("zh", "zh"), ("en", "en"), ("system", "system"), ("auto", "system"), (None, "system"))):
        args = parser.parse_args(["ai", "language"] + ([value] if value else []))
        assert args.func(args) == 0
        assert json.loads(capsys.readouterr().out) == {"replyLanguage": expected, "resolvedReplyLanguage": "en" if expected == "system" else expected, "configured": index > 0}
    assert not (preferences_home / "assistant-credentials.json").exists()
    assert not assistant_config.config_status()["configured"]


@pytest.mark.parametrize("language", ["en", "auto"])
def test_cli_can_repair_invalid_stored_language_without_losing_other_configuration(preferences_home, language, capsys):
    path = preferences_home / "config.toml"
    path.write_text('[assistant]\nmodel="existing"\n[gui.assistant]\nreply_language="invalid"\nwidth=42\n')
    with pytest.raises(QuarryError, match="Invalid GUI reply language"):
        assistant_config.gui_preferences()
    args = cli.build_parser().parse_args(["ai", "language", language])
    assert args.func(args) == 0
    expected = "system" if language == "auto" else language
    assert json.loads(capsys.readouterr().out) == {"replyLanguage": expected, "resolvedReplyLanguage": "en", "configured": True}
    assert tomllib.loads(path.read_text()) == {
        "assistant": {"model": "existing"},
        "gui": {"assistant": {"reply_language": expected, "width": 42}},
    }


@pytest.mark.parametrize("environment,expected", [
    ({"LC_ALL": "zh_CN.UTF-8", "LC_MESSAGES": "en_US", "LANG": "en_US"}, "zh"),
    ({"LC_MESSAGES": "zh-TW", "LANG": "en_US.UTF-8"}, "zh"),
    ({"LANG": "de_DE.UTF-8"}, "en"),
    ({"LC_ALL": "C.UTF-8", "LANG": "zh_CN.UTF-8"}, "zh"),
])
def test_system_language_uses_server_locale_environment(monkeypatch, environment, expected):
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(assistant_config, "_macos_language", lambda: pytest.fail("OS fallback is unnecessary"))
    assert assistant_config.system_reply_language() == expected


@pytest.mark.parametrize("languages,expected", [(["zh-Hans-CN", "en-CN"], "zh"), (["en-CN", "zh-Hans-CN"], "en")])
def test_macos_prefers_primary_system_language_over_region(monkeypatch, languages, expected):
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.setenv(key, "C.UTF-8")
    monkeypatch.setattr(assistant_config.sys, "platform", "darwin")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "/usr/bin/defaults":
            assert command[-1] == "AppleLanguages"
            return subprocess.CompletedProcess(command, 0, stdout=b"(language)")
        return subprocess.CompletedProcess(command, 0, stdout=plistlib.dumps(languages))
    monkeypatch.setattr(assistant_config.subprocess, "run", run)
    assert assistant_config.system_reply_language() == expected
    assert len(calls) == 2


def test_macos_applelocale_fallback_and_unavailable_os_locale(monkeypatch):
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(assistant_config.sys, "platform", "darwin")
    def run(command, **kwargs):
        if command[-1] == "AppleLanguages":
            raise subprocess.CalledProcessError(1, command)
        assert command[-1] == "AppleLocale"
        return subprocess.CompletedProcess(command, 0, stdout="zh_CN\n")
    monkeypatch.setattr(assistant_config.subprocess, "run", run)
    assert assistant_config.system_reply_language() == "zh"
    monkeypatch.setattr(assistant_config, "_macos_language", lambda: None)
    monkeypatch.setattr(assistant_config.locale, "getlocale", lambda: (None, None))
    assert assistant_config.system_reply_language() == "en"


def test_cross_process_model_and_language_updates_preserve_both(preferences_home):
    # Widen the read/modify/write window. A shared real file lock must retain
    # both independent settings whichever subprocess starts first.
    code = '''
import json,sys,time
from pathlib import Path
from quarry import assistant_config
home=Path(sys.argv[1]); action=sys.argv[2]
original=assistant_config._atomic_write
def slow_write(*args, **kwargs):
    time.sleep(.2)
    return original(*args, **kwargs)
assistant_config._atomic_write=slow_write
(home/(action+'.ready')).touch()
deadline=time.monotonic()+5
while not (home/'go').exists():
    if time.monotonic()>deadline: raise RuntimeError('test start timed out')
    time.sleep(.01)
if action == 'language':
    assistant_config.set_reply_language('zh')
else:
    assistant_config.configure_model(base_url='http://localhost:1/v1',model='test',no_key=True)
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    processes = [subprocess.Popen([sys.executable, "-c", code, str(preferences_home), action],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for action in ("language", "model")]
    try:
        deadline = time.monotonic() + 5
        while not all((preferences_home / (action + ".ready")).exists() for action in ("language", "model")):
            assert time.monotonic() < deadline
            time.sleep(.01)
        (preferences_home / "go").touch()
        for process in processes:
            _, error = process.communicate(timeout=5)
            assert process.returncode == 0, error
        assert assistant_config.gui_preferences()["replyLanguage"] == "zh"
        assert assistant_config.config_status() == {"configured": True, "model": "test"}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
