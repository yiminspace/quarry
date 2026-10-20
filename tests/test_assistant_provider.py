"""Assistant model protocol, scope and CLI tests; all providers are local fakes."""
from __future__ import annotations

import io
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from quarry import assistant, assistant_config, assistant_store, cli, workspace
from quarry.core import QuarryError


@pytest.fixture
def ai_home(monkeypatch, tmp_path):
    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("QUARRY_AI_HOME", str(tmp_path / "history"))
    monkeypatch.delenv("QUARRY_AI_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def ai_workspace(ai_home):
    path = ai_home / "data.db"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT); CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id)); INSERT INTO users VALUES (1, 'unchanged');")
    ws = ai_home / "workspace"
    ws.mkdir()
    (ws / "connections.toml").write_text(f'[demo]\nurl = "sqlite:///{path}"\nengine = "sqlite"\nenv = "dev"\n')
    before = workspace.WS_LIST
    before_primary = workspace.WS
    workspace.configure_workspace(str(ws))
    try:
        yield ws
    finally:
        workspace.WS_LIST = before
        workspace.WS = before_primary


def _sse(*events, done=True):
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return (body + ("data: [DONE]\n\n" if done else "")).encode()


def _text_reply(text="This query reads users.", *, done=True, finish="stop"):
    return _sse({"choices": [{"delta": {"content": text}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": finish}]}, done=done)


@contextmanager
def fake_provider(replies):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append({"path": self.path, "headers": dict(self.headers),
                             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])) )})
            reply = replies[min(len(received) - 1, len(replies) - 1)]
            code, body = reply if isinstance(reply, tuple) else (200, reply)
            self.send_response(code)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _configure(url):
    return assistant_config.configure_model(base_url=url, model="test-model", no_key=True)


def _conversation():
    return assistant.create_conversation({"db": "demo", "env": "dev", "sql": "SELECT * FROM users"})


def _chat(conversation, **extra):
    return {"conversationId": conversation["id"], "revision": conversation["revision"],
            "sql": conversation["sql"], "prompt": "Limit this to 20 rows", "requestId": "request-1", **extra}


def test_configuration_private_key_and_preserved_sections(ai_home):
    config = ai_home / "config.toml"
    config.write_text('# keep\nworkspaces = ["~/workspace"]\n[tunnel]\nkeep_alive = true\n')
    result = assistant_config.configure_model(base_url="https://model.example/v1/", model="test",
                                              api_key="secret-for-test", bifrost=True, thinking="disabled")
    assert result["configured"] and result["api_key"] == "********"
    assert "secret-for-test" not in json.dumps(result)
    assert "secret-for-test" not in config.read_text()
    assert '# keep\nworkspaces = ["~/workspace"]\n[tunnel]\nkeep_alive = true' in config.read_text()
    secret = ai_home / "assistant-credentials.json"
    assert secret.stat().st_mode & 0o777 == 0o600
    assert assistant_config.load_model_config().api_key == "secret-for-test"
    assistant_config.configure_model(base_url="http://localhost:1234/v1", model="local", no_key=True)
    assert not secret.exists()
    assert config.read_text().count("[assistant]") == 1


@pytest.mark.parametrize("url", ["file:///tmp/model", "https://user:secret@model.example/v1", "https://model.example/v1?key=secret", "https://model.example/#secret", "https://model.example/\n"])
def test_config_rejects_embedded_secrets_and_invalid_url(ai_home, url):
    with pytest.raises(QuarryError):
        assistant_config.configure_model(base_url=url, model="test", no_key=True)
    assert not (ai_home / "config.toml").exists()


def test_environment_key_is_resolved_at_request_time(ai_home, monkeypatch):
    assistant_config.configure_model(base_url="https://model.example/v1", model="test", api_key_env="QUARRY_TEST_MODEL_KEY")
    assert assistant_config.config_status() == {"configured": False}
    monkeypatch.setenv("QUARRY_TEST_MODEL_KEY", "test-env-secret")
    assert assistant_config.config_status() == {"configured": True, "model": "test"}
    assert "test-env-secret" not in json.dumps(assistant_config.config_details())


def test_configure_does_not_overwrite_malformed_config(ai_home):
    config = ai_home / "config.toml"
    config.write_text("[broken\n")
    with pytest.raises(QuarryError, match="Cannot read"):
        _configure("http://localhost:1234/v1")
    assert config.read_text() == "[broken\n"


@pytest.mark.integration
def test_provider_stream_headers_and_complete(ai_home):
    with fake_provider([_text_reply()]) as (url, requests):
        config = assistant_config.ModelConfig(url, "test", "safe-test-key", True, "disabled")
        events = list(assistant.provider_stream(config, [{"role": "user", "content": "explain"}], threading.Event(), session_id="conversation-1"))
    assert events[-1] == {"type": "complete", "content": "This query reads users.", "tool_calls": []}
    headers = {key.lower(): value for key, value in requests[0]["headers"].items()}
    assert headers["authorization"] == "Bearer safe-test-key" and headers["x-bf-vk"] == "safe-test-key"
    assert headers["x-bf-passthrough-extra-params"] == "true"
    assert headers["x-bf-lh-session-id"] == "conversation-1"
    assert requests[0]["body"]["thinking"] == {"type": "disabled"}
    assert {tool["function"]["name"] for tool in requests[0]["body"]["tools"]} == {"find_tables", "describe_tables", "propose_sql"}


@pytest.mark.integration
@pytest.mark.parametrize("reply", [_text_reply("```sql\nDELETE FROM users;\n```", done=False), _text_reply("half", finish="length"), b"data: not json\n\n"])
def test_incomplete_stream_never_proposes_or_applies_sql(ai_workspace, reply):
    with fake_provider([reply]) as (url, _):
        _configure(url)
        conversation = _conversation()
        events = list(assistant.stream_chat(_chat(conversation)))
    assert events[-1]["type"] == "error"
    assert not any(event["type"] == "sql_proposal" for event in events)
    saved = assistant_store.get(conversation["id"])
    assert saved["sql"] == conversation["sql"] and saved["checkpoints"] == []
    assert saved["messages"][-1]["status"] == "interrupted"


def _tool_reply(name="describe_tables", args='{"tables":["users","orders"]}'):
    split = len(args) // 2
    return _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call1", "type": "function", "function": {"name": name, "arguments": args[:split]}}]}, "finish_reason": None}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[split:]}}]}, "finish_reason": "tool_calls"}]})


@pytest.mark.integration
def test_metadata_tool_loop_has_foreign_keys_and_sql_is_only_proposal(ai_workspace):
    sql = "SELECT u.name FROM users u JOIN orders o ON o.user_id = u.id LIMIT 20;"
    with fake_provider([_tool_reply(), _tool_reply("propose_sql", json.dumps({"sql": sql})), _text_reply("Use the declared user relationship.")]) as (url, requests):
        _configure(url)
        conversation = _conversation()
        events = list(assistant.stream_chat(_chat(conversation)))
    assert events[-1]["type"] == "done"
    assert len(requests) == 3
    tool_result = next(message for message in requests[1]["body"]["messages"] if message["role"] == "tool")
    metadata = json.loads(tool_result["content"])
    assert metadata["tables"][1]["foreignKeys"][0]["targetTable"] == "users"
    proposal = next(event for event in events if event["type"] == "sql_proposal")
    assert "JOIN orders" in proposal["sql"]
    saved = events[-1]["conversation"]
    assert saved["sql"] == "SELECT * FROM users" and saved["checkpoints"] == []
    assert saved["messages"][-1]["baseRevision"] == conversation["revision"]
    assert "unchanged" not in json.dumps(requests)  # business row values never enter model context


@pytest.mark.integration
def test_unsupported_model_tool_is_rejected_before_any_execution(ai_workspace, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No model-supplied SQL may execute")
    monkeypatch.setattr(assistant, "_tool", forbidden)
    with fake_provider([_tool_reply("exec_sql", '{"sql":"DELETE FROM users"}')]) as (url, _):
        _configure(url)
        events = list(assistant.stream_chat(_chat(_conversation())))
    assert events[-1]["type"] == "error" and "unsupported tool" in events[-1]["error"]


@pytest.mark.integration
def test_authentication_failure_never_echoes_provider_body_or_key(ai_workspace):
    with fake_provider([(401, b'{"error":"secret-for-test and credential URL"}')]) as (url, _):
        assistant_config.configure_model(base_url=url, model="test", api_key="secret-for-test")
        events = list(assistant.stream_chat(_chat(_conversation())))
    assert "authentication failed" in events[-1]["error"]
    assert "secret-for-test" not in json.dumps(events)


def test_unconfigured_chat_creates_no_message_and_no_model_request(ai_workspace, monkeypatch):
    monkeypatch.setattr(assistant, "provider_stream", lambda *a, **kw: pytest.fail("Model must not run"))
    conversation = _conversation()
    events = list(assistant.stream_chat(_chat(conversation)))
    assert "qy ai configure --help" in events[0]["error"]
    assert assistant_store.get(conversation["id"])["messages"] == []


def test_changed_connection_cannot_reuse_conversation(ai_workspace, monkeypatch):
    _configure("http://localhost:1/v1")
    conversation = _conversation()
    monkeypatch.setattr(assistant_store, "connection_fingerprint", lambda _id: "replaced")
    monkeypatch.setattr(assistant, "provider_stream", lambda *a, **kw: pytest.fail("Model must not run"))
    events = list(assistant.stream_chat(_chat(conversation)))
    assert "connection changed" in events[0]["error"]


def test_stop_preserves_interrupted_message_and_releases_conversation(ai_workspace, monkeypatch):
    _configure("http://localhost:1/v1")
    conversation = _conversation()
    def chunks(config, messages, cancel, **kwargs):
        yield {"type": "text_delta", "delta": "partial"}
        yield {"type": "complete", "content": "```sql\nSELECT 9;\n```", "tool_calls": []}
    monkeypatch.setattr(assistant, "provider_stream", chunks)
    stream = assistant.stream_chat(_chat(conversation))
    assert next(stream)["type"] == "request"
    assert next(stream)["type"] == "text_delta"
    assert assistant.cancel_chat(conversation["id"], "request-1") == {"cancelled": True}
    assert list(stream)[-1]["type"] == "error"
    assert conversation["id"] not in assistant._ACTIVE
    assert assistant_store.get(conversation["id"])["messages"][-1]["status"] == "interrupted"


def test_cli_configuration_and_show_mask_stdin_key(ai_home, monkeypatch, capsys):
    parser = cli.build_parser()
    monkeypatch.setattr("sys.stdin", io.StringIO("cli-secret\n"))
    args = parser.parse_args(["ai", "configure", "--base-url", "https://model.example/v1", "--model", "test", "--api-key-stdin", "--bifrost", "--thinking", "disabled"])
    assert args.func(args) == 0
    args = parser.parse_args(["ai", "show"])
    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "cli-secret" not in output and "********" in output


def test_cli_bare_configure_is_interactive_and_key_is_hidden(ai_home, monkeypatch, capsys):
    answers = iter(["https://model.example/v1", "interactive-model", "y"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt: "interactive-secret")
    args = cli.build_parser().parse_args(["ai", "configure"])
    assert args.func(args) == 0
    config = assistant_config.load_model_config()
    assert config.model == "interactive-model" and config.bifrost
    assert config.api_key == "interactive-secret"
    assert "interactive-secret" not in capsys.readouterr().out


def test_cli_bare_configure_without_terminal_is_actionable(ai_home, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    args = cli.build_parser().parse_args(["ai", "configure"])
    with pytest.raises(QuarryError, match="configure --help"):
        args.func(args)
    assert not (ai_home / "config.toml").exists()


@pytest.mark.integration
def test_cli_generates_sql_without_execution_or_saved_query_edits(ai_workspace, capsys):
    with fake_provider([_tool_reply("propose_sql", '{"sql":"SELECT 123;"}'), _text_reply("Prepared the query.")]) as (url, _):
        _configure(url)
        args = cli.build_parser().parse_args(["ai", "sql", "--db", "demo", "--env", "dev", "Make a query"])
        assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == "SELECT 123;"
    assert not (ai_workspace / "queries").exists()


@pytest.mark.integration
def test_cli_explain_reads_stdin_and_never_proposes(ai_workspace, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("SELECT * FROM users"))
    with fake_provider([_text_reply("Reads every user.")]) as (url, requests):
        _configure(url)
        args = cli.build_parser().parse_args(["ai", "explain", "--db", "demo", "--env", "dev", "--file", "-", "--format", "json"])
        assert args.func(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["text"] == "Reads every user." and out["sql"] is None
    assert "SELECT * FROM users" in requests[0]["body"]["messages"][1]["content"]
    assert requests[0]["body"]["messages"][-1]["content"] == "请用中文解读这段 SQL。"


@pytest.mark.integration
@pytest.mark.parametrize("prompt", ["", "Explain this SQL in English."])
def test_explanation_preserves_the_default_prompt_and_explicit_requests(ai_workspace, prompt):
    conversation = _conversation()
    with fake_provider([_text_reply("这是 SQL 解读。")]) as (url, requests):
        _configure(url)
        events = list(assistant.stream_chat(_chat(conversation, prompt=prompt, explain=True)))
    assert events[-1]["type"] == "done"
    assert not any(event["type"] == "sql_proposal" for event in events)
    messages = requests[0]["body"]["messages"]
    assert messages[-1]["content"] == (prompt or "请用中文解读这段 SQL。")
    assert events[-1]["conversation"]["sql"] == conversation["sql"]


@pytest.mark.integration
@pytest.mark.parametrize("language,expected", [("zh", "Simplified Chinese"), ("en", "English"), ("system", "Simplified Chinese"), ("auto", "Simplified Chinese"), (None, "Simplified Chinese")])
def test_gui_reply_language_applies_to_handwritten_prompts_without_rewriting_them(ai_workspace, language, expected, monkeypatch):
    monkeypatch.setattr(assistant_config, "system_reply_language", lambda: "zh")
    if language:
        assistant_config.set_reply_language(language)
    prompt = "Explain the join. Please reply in Japanese."
    with fake_provider([_text_reply("Explanation.")]) as (url, requests):
        _configure(url)
        events = list(assistant.stream_chat(_chat(_conversation(), prompt=prompt, gui=False), gui=True))
    assert events[-1]["type"] == "done"
    messages = requests[0]["body"]["messages"]
    assert messages[0] == {"role": "system", "content": assistant.SYSTEM_PROMPT}
    assert messages[-1] == {"role": "user", "content": prompt}
    rules = [m["content"] for m in messages if m["role"] == "system" and m["content"].startswith("GUI reply language preference:")]
    if expected:
        assert len(rules) == 1 and f"reply in {expected}." in rules[0]
        assert "explicitly requests a different reply language" in rules[0]
    else:
        assert rules == []


@pytest.mark.integration
def test_gui_preference_is_reloaded_but_cli_and_body_flags_cannot_enable_it(ai_workspace, capsys):
    assistant_config.set_reply_language("zh")
    with fake_provider([_text_reply("Explanation.")]) as (url, requests):
        _configure(url)
        # No trusted GUI adapter flag: a forged JSON property has no effect.
        events = list(assistant.stream_chat(_chat(_conversation(), prompt="Explain.", gui=True)))
        assert events[-1]["type"] == "done"
        args = cli.build_parser().parse_args(["ai", "sql", "--db", "demo", "--env", "dev", "Explain this SQL"])
        assert args.func(args) == 0
        assistant_config.set_reply_language("en")
        events = list(assistant.stream_chat(_chat(_conversation(), prompt="Explain."), gui=True))
        assert events[-1]["type"] == "done"
    assert "GUI reply language preference:" not in json.dumps(requests[:2])
    assert "GUI reply language preference: reply in English." in json.dumps(requests[2])


@pytest.mark.integration
def test_empty_gui_explanation_prompt_does_not_override_reply_language(ai_workspace):
    assistant_config.set_reply_language("en")
    with fake_provider([_text_reply("Explanation.")]) as (url, requests):
        _configure(url)
        events = list(assistant.stream_chat(_chat(_conversation(), prompt="", explain=True), gui=True))
    assert events[-1]["type"] == "done"
    messages = requests[0]["body"]["messages"]
    assert messages[-1]["content"] == "Explain the current SQL."
    assert any(m["content"].startswith("GUI reply language preference: reply in English.") for m in messages)


@pytest.mark.integration
def test_chat_code_examples_never_become_editor_proposals(ai_workspace):
    with fake_provider([_text_reply("For example:\n```sql\nSELECT 1;\n```")]) as (url, _):
        _configure(url)
        events = list(assistant.stream_chat(_chat(_conversation(), prompt="What does SELECT mean?")))
    assert events[-1]["type"] == "done"
    assert not any(event["type"] == "sql_proposal" for event in events)
    assert "sql" not in events[-1]["conversation"]["messages"][-1]


@pytest.mark.integration
@pytest.mark.parametrize("explain,truncated", [(True, False), (False, True)])
def test_explanation_and_interrupted_final_reply_reject_pending_proposal(ai_workspace, explain, truncated):
    replies = [_tool_reply("propose_sql", '{"sql":"SELECT 9;"}'), _text_reply("Prepared.", done=not truncated)]
    with fake_provider(replies) as (url, requests):
        _configure(url)
        conversation = _conversation()
        events = list(assistant.stream_chat(_chat(conversation, explain=explain)))
    assert events[-1]["type"] == "error"
    assert not any(event["type"] == "sql_proposal" for event in events)
    if explain:
        assert all(tool["function"]["name"] != "propose_sql" for tool in requests[0]["body"]["tools"])
    assert assistant_store.get(conversation["id"])["sql"] == conversation["sql"]


def test_stale_revision_does_not_create_spurious_interrupted_message(ai_workspace):
    _configure("http://localhost:1/v1")
    conversation = _conversation()
    assistant_store.update(conversation["id"], conversation["revision"], "SELECT 99")
    events = list(assistant.stream_chat(_chat(conversation)))
    assert events[-1]["type"] == "error"
    assert assistant_store.get(conversation["id"])["messages"] == []
