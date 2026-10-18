"""Assistant CLI input and output contracts without database/model requests."""
from __future__ import annotations

import io
import json

import pytest

from quarry import assistant, assistant_config, cli
from quarry.core import QuarryError
from test_assistant_provider import ai_home  # noqa: F401


def _args(*values):
    return cli.build_parser().parse_args(["ai", *values])


@pytest.mark.parametrize("auth", ["env", "none", "key"])
def test_interactive_configuration_blank_key_preserves_existing_auth(ai_home, monkeypatch, capsys, auth):
    monkeypatch.setenv("QUARRY_TEST_CLI_KEY", "test-only-secret")
    options = {"api_key_env": "QUARRY_TEST_CLI_KEY"} if auth == "env" else (
        {"no_key": True} if auth == "none" else {"api_key": "test-only-secret"})
    assistant_config.configure_model(base_url="http://localhost:1/v1", model="existing",
                                     bifrost=True, thinking="disabled", **options)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    monkeypatch.setattr("getpass.getpass", lambda prompt: "")
    args = _args("configure")
    assert args.func(args) == 0
    config = assistant_config.load_model_config()
    assert config.model == "existing" and config.bifrost and config.thinking == "disabled"
    assert config.api_key == ("" if auth == "none" else "test-only-secret")
    assert assistant_config.config_details()["auth"] == ("stored" if auth == "key" else auth)
    output = capsys.readouterr()
    assert "test-only-secret" not in output.out + output.err


def test_interactive_configuration_explicit_auth_skips_hidden_key_prompt(ai_home, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "local-model")
    monkeypatch.setattr("getpass.getpass", lambda prompt: pytest.fail("Explicit --no-key must not prompt for a key"))
    args = _args("configure", "--base-url", "http://localhost:1/v1", "--no-key", "--bifrost")
    assert args.func(args) == 0
    config = assistant_config.load_model_config()
    assert config.model == "local-model" and config.bifrost and config.api_key == ""


def test_first_interactive_configuration_requires_key_or_explicit_no_key(ai_home, monkeypatch):
    answers = iter(["http://localhost:1/v1", "test-model"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt: "")
    args = _args("configure")
    with pytest.raises(QuarryError, match="API key is required"):
        args.func(args)
    assert not (ai_home / "config.toml").exists()
    assert not (ai_home / "assistant-credentials.json").exists()


@pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
def test_interactive_configuration_cancellation_preserves_existing_files(ai_home, monkeypatch, interrupt):
    assistant_config.configure_model(base_url="http://localhost:1/v1", model="existing", api_key="retained-secret")
    paths = [ai_home / "config.toml", ai_home / "assistant-credentials.json"]
    before = [path.read_bytes() for path in paths]
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def cancelled(prompt):
        raise interrupt()

    monkeypatch.setattr("builtins.input", cancelled)
    args = _args("configure")
    with pytest.raises(QuarryError, match="configuration cancelled"):
        args.func(args)
    assert [path.read_bytes() for path in paths] == before


def test_environment_auth_configuration_reports_missing_runtime_key(ai_home, monkeypatch, capsys):
    monkeypatch.delenv("QUARRY_TEST_CLI_UNSET_KEY", raising=False)
    args = _args("configure", "--base-url", "http://localhost:1/v1", "--model", "test-model",
                 "--api-key-env", "QUARRY_TEST_CLI_UNSET_KEY")
    assert args.func(args) == 0
    output = capsys.readouterr()
    assert "SQL assistant configured: test-model" in output.out
    assert "API key is not available in this environment yet." in output.err
    assert assistant_config.config_status() == {"configured": False}


@pytest.fixture
def fake_generation(ai_home, monkeypatch):
    assistant_config.configure_model(base_url="http://localhost:1/v1", model="test", no_key=True)
    created, requests, events = [], [], []

    def create(body):
        created.append(body)
        return {"id": "cli-conversation", "revision": 0}

    def stream(body):
        requests.append(body)
        yield from events

    monkeypatch.setattr(assistant, "create_conversation", create)
    monkeypatch.setattr(assistant, "stream_chat", stream)
    return created, requests, events


def test_explain_rejects_empty_input_before_creating_history(fake_generation):
    created, requests, _ = fake_generation
    args = _args("explain", "--db", "demo", "--sql", " \n ")
    with pytest.raises(QuarryError, match="Provide SQL"):
        args.func(args)
    assert created == requests == []


@pytest.mark.parametrize("source", ["file", "stdin"])
def test_explain_streams_text_and_finishes_with_one_newline(fake_generation, tmp_path, monkeypatch, capsys, source):
    created, requests, events = fake_generation
    sql = "SELECT '中文';"
    if source == "stdin":
        monkeypatch.setattr("sys.stdin", io.StringIO(sql))
        path = "-"
    else:
        path = tmp_path / "query.sql"
        path.write_text(sql)
    events.extend([{"type": "text_delta", "delta": "Reads "}, {"type": "text_delta", "delta": "a literal."},
                   {"type": "done", "conversation": {"id": "cli-conversation"}}])
    args = _args("explain", "--db", "demo", "--file", str(path))
    assert args.func(args) == 0
    output = capsys.readouterr()
    assert output.out == "Reads a literal.\n" and output.err == ""
    assert created[0]["sql"] == requests[0]["sql"] == sql
    assert requests[0]["explain"] is True
    assert requests[0]["prompt"] == assistant.DEFAULT_EXPLAIN_PROMPT


@pytest.mark.parametrize("action,format,partial", [("explain", "text", "Partial explanation"),
                                                   ("explain", "text", ""), ("sql", "json", "Partial explanation")])
def test_stream_error_does_not_emit_success_or_partial_sql(fake_generation, capsys, action, format, partial):
    _, _, events = fake_generation
    if partial:
        events.append({"type": "text_delta", "delta": partial})
    events.append({"type": "error", "error": "Fixture provider failed"})
    values = [action, "--db", "demo", "--sql", "SELECT 1", "--format", format]
    if action == "sql":
        values.append("Limit the query")
    args = _args(*values)
    with pytest.raises(QuarryError, match="Fixture provider failed"):
        args.func(args)
    output = capsys.readouterr()
    assert output.out == (partial + "\n" if partial and action == "explain" and format == "text" else "")
    assert output.err == ""


def test_generate_requires_done_before_printing_a_proposal(fake_generation, capsys):
    _, _, events = fake_generation
    events.extend([{"type": "text_delta", "delta": "Prepared a query."},
                   {"type": "sql_proposal", "sql": "SELECT 1"}])
    args = _args("sql", "--db", "demo", "Make a query")
    with pytest.raises(QuarryError, match="reply did not complete"):
        args.func(args)
    output = capsys.readouterr()
    assert output.out == output.err == ""


@pytest.mark.parametrize("format", ["sql", "json"])
def test_generation_clarification_is_never_printed_as_sql(fake_generation, capsys, format):
    _, _, events = fake_generation
    events.extend([{"type": "text_delta", "delta": "Which table should I use?"},
                   {"type": "done", "conversation": {"id": "cli-conversation"}}])
    args = _args("sql", "--db", "demo", "--format", format, "Find the records")
    assert args.func(args) == 0
    output = capsys.readouterr()
    if format == "sql":
        assert output.out == "" and output.err == "Which table should I use?\n"
    else:
        assert json.loads(output.out) == {"conversationId": "cli-conversation", "text": "Which table should I use?", "sql": None}
        assert output.err == ""


def test_configure_rejects_non_numeric_timeout_before_writing(ai_home, capsys):
    with pytest.raises(SystemExit) as failure:
        _args("configure", "--timeout", "soon")
    assert failure.value.code == 2
    assert "invalid int value" in capsys.readouterr().err
    assert not (ai_home / "config.toml").exists()
