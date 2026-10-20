"""Provider protocol failures must remain bounded and cannot change editor SQL."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from quarry import assistant, assistant_config, assistant_schema, assistant_store
from quarry.core import QuarryError
from test_assistant_provider import ai_home, ai_workspace, _chat, _configure, _conversation, _sse


@contextmanager
def endpoint(replies):
    """A real HTTP endpoint with explicitly supplied wire responses."""
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append({"method": "GET", "path": self.path})
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            requests.append({"method": "POST", "path": self.path,
                             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            reply = replies[min(len(requests) - 1, len(replies) - 1)]
            self.send_response(reply.get("status", 200))
            self.send_header("Content-Type", reply.get("type", "application/json"))
            self.send_header("Content-Length", str(len(reply["body"])))
            for key, value in reply.get("headers", {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(reply["body"])
            except (BrokenPipeError, ConnectionResetError):
                pass  # a bounded reader is allowed to reject the remaining body
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def call(name="find_tables", arguments='{"query":"users"}', identity="call-1"):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": arguments}}


def completion(content="Explanation.", calls=None, finish="stop"):
    return {"body": json.dumps({"choices": [{"message": {"content": content, "tool_calls": calls or []},
                                              "finish_reason": finish}]}).encode()}


def read_provider(url, *, cancel=None):
    return list(assistant.provider_stream(assistant_config.ModelConfig(url, "test-model"),
                                         [{"role": "user", "content": "Explain."}], cancel or threading.Event()))


def unchanged(conversation):
    saved = assistant_store.get(conversation["id"])
    assert saved["sql"] == conversation["sql"] and saved["checkpoints"] == []
    assert not any(m.get("sql") for m in saved["messages"] if m.get("status") == "interrupted")
    assert conversation["id"] not in assistant._ACTIVE
    return saved


def test_json_completion_and_metadata_roundtrip_are_supported(ai_workspace):
    replies = [completion(None, [call()], "tool_calls"), completion("The users table has an ID and name.")]
    with endpoint(replies) as (url, requests):
        _configure(url)
        conversation = _conversation()
        events = list(assistant.stream_chat(_chat(conversation, explain=True)))
    assert events[-1]["type"] == "done"
    assert [e["delta"] for e in events if e["type"] == "text_delta"] == ["The users table has an ID and name."]
    result = json.loads(next(m["content"] for m in requests[1]["body"]["messages"] if m["role"] == "tool"))
    assert result == {"tables": ["users"], "capped": False}
    unchanged(conversation)


@pytest.mark.parametrize("reply", [
    {"body": b" " * 120_001},
    completion([{"type": "text", "text": "not a supported content string"}]),
    completion("x" * 64_001),
    {"body": b'{"choices":[]}'},
    {"body": b'{"choices":[{"message":null}]}'},
    completion("incomplete", finish="length"),
    completion(None, [call()], "stop"),
    completion(None, [], "tool_calls"),
    completion(None, [call(identity="")], "tool_calls"),
    completion(None, [call(), call()], "tool_calls"),
    completion(None, [{**call(), "type": "shell"}], "tool_calls"),
    completion(None, [call(arguments={})], "tool_calls"),
    completion(None, [call(arguments="x" * 16_001)], "tool_calls"),
], ids=["body-limit", "content-type", "content-limit", "empty-choices", "null-message", "truncated",
        "unfinished-tools", "empty-tools", "missing-id", "duplicate-id", "unsupported-type", "arguments-type", "arguments-limit"])
def test_invalid_json_responses_never_complete_or_change_sql(ai_workspace, reply):
    with endpoint([reply]) as (url, _):
        _configure(url)
        conversation = _conversation()
        events = list(assistant.stream_chat(_chat(conversation)))
    assert events[-1]["type"] == "error"
    assert not any(e["type"] in ("sql_proposal", "done") for e in events)
    assert unchanged(conversation)["messages"][-1]["status"] == "interrupted"


@pytest.mark.parametrize("body", [
    b"data: " + b" " * 120_001 + b"\n\n",
    (b":" + b" " * 100_000 + b"\n") * 21,
    _sse({"error": {"message": "credential-secret"}}),
    _sse({"choices": [{"delta": {"content": ["unexpected"]}}]}),
    _sse(*[{"choices": [{"delta": {"content": "x" * 20_000}}]}] * 4),
    _sse({"choices": [{"delta": {"tool_calls": [{"index": True}]}}]}),
    _sse({"choices": [{"delta": {"tool_calls": [{"index": 16}]}}]}),
    _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": 42}]}}]}),
    _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": ["find_tables"]}}]}}]}),
    _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "x" * 16_001}}]}}]}),
], ids=["line-limit", "wire-limit", "provider-error", "text-type", "text-limit", "bool-index", "tool-index-limit", "id-type", "name-type", "argument-limit"])
def test_bad_sse_frames_are_bounded_and_safe(body):
    with endpoint([{"type": "text/event-stream", "body": body}]) as (url, _):
        with pytest.raises(QuarryError) as error:
            read_provider(url)
    assert "credential-secret" not in str(error.value)


def test_sse_comments_usage_and_multiline_data_preserve_text():
    body = b': heartbeat\n\nevent: message\ndata: {"choices": [\ndata: {"delta":{"content":"A"},"finish_reason":null}]}\n\n'
    body += _sse({"choices": [], "usage": {"total_tokens": 1}},
                 {"choices": [{"delta": {"content": "B"}, "finish_reason": "stop"}]})
    with endpoint([{"type": "text/event-stream", "body": body}]) as (url, _):
        events = read_provider(url)
    assert "".join(e.get("delta", "") for e in events) == "AB"
    assert events[-1] == {"type": "complete", "content": "AB", "tool_calls": []}


@pytest.mark.parametrize("status,expected", [(403, "authentication"), (429, "limit"), (500, "HTTP 500")])
def test_http_provider_errors_do_not_echo_remote_secrets(status, expected):
    with endpoint([{"status": status, "body": b"credential-secret"}]) as (url, _):
        with pytest.raises(QuarryError, match=expected) as error:
            read_provider(url)
    assert "credential-secret" not in str(error.value)


def test_redirect_cannot_forward_a_model_credential():
    with endpoint([completion()]) as (destination, leaked):
        with endpoint([{"status": 302, "body": b"", "headers": {"Location": destination + "/steal"}}]) as (url, sent):
            config = assistant_config.ModelConfig(url, "test", "private-test-key", bifrost=True)
            with pytest.raises(QuarryError, match="HTTP 302"):
                list(assistant.provider_stream(config, [], threading.Event()))
        assert len(sent) == 1 and leaked == []


@pytest.mark.parametrize("error", [URLError("credential-secret"), TimeoutError("credential-secret"), OSError("credential-secret")])
def test_connection_failures_hide_transport_details(monkeypatch, error):
    def fail(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(assistant, "build_opener", lambda *_args: SimpleNamespace(open=fail))
    with pytest.raises(QuarryError, match="Cannot reach") as caught:
        read_provider("http://localhost:1/v1")
    assert "credential-secret" not in str(caught.value)


@pytest.mark.parametrize("name,arguments", [
    ("find_tables", "not-json"), ("find_tables", "[]"),
    ("find_tables", '{"query":"users","sql":"DELETE FROM users"}'), ("find_tables", '{"query":42}'),
    ("describe_tables", '{"tables":[]}'), ("describe_tables", '{"tables":[null]}'),
    ("describe_tables", '{"tables":["users"],"sql":"SELECT secret"}'),
])
def test_invalid_catalog_arguments_are_safe_and_never_query_database(ai_workspace, monkeypatch, name, arguments):
    monkeypatch.setattr(assistant_schema, "find_tables", lambda *_a, **_k: pytest.fail("Invalid metadata request must not query"))
    monkeypatch.setattr(assistant_schema, "describe_tables", lambda *_a, **_k: pytest.fail("Invalid metadata request must not query"))
    with endpoint([completion(None, [call(name, arguments)], "tool_calls"), completion("Please check the table name.")]) as (url, requests):
        _configure(url)
        conversation = _conversation()
        events = list(assistant.stream_chat(_chat(conversation)))
    assert events[-1]["type"] == "done"
    metadata = json.loads(next(m["content"] for m in requests[1]["body"]["messages"] if m["role"] == "tool"))
    assert set(metadata) == {"error"} and "Metadata is unavailable" in metadata["error"]
    unchanged(conversation)


@pytest.mark.parametrize("arguments", ['{"sql":', '[]', '{"sql":"SELECT 2","execute":true}', '{"sql":""}', '{"sql":42}'])
def test_invalid_sql_proposal_never_reaches_apply(ai_workspace, arguments):
    with endpoint([completion(None, [call("propose_sql", arguments)], "tool_calls")]) as (url, _):
        _configure(url)
        conversation = _conversation()
        events = list(assistant.stream_chat(_chat(conversation)))
    assert events[-1]["type"] == "error"
    assert not any(e["type"] == "sql_proposal" for e in events)
    unchanged(conversation)


def test_catalog_errors_and_oversized_metadata_are_replaced_with_safe_tool_results(ai_workspace, monkeypatch):
    results = iter([QuarryError("postgres://user:credential-secret@host/db"), {"tables": ["x" * 40_001]}])
    def metadata(*_args, **_kwargs):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(assistant_schema, "find_tables", metadata)
    replies = [completion(None, [call()], "tool_calls"), completion(None, [call(identity="call-2")], "tool_calls"), completion("Use fewer tables.")]
    with endpoint(replies) as (url, requests):
        _configure(url)
        conversation = _conversation()
        events = list(assistant.stream_chat(_chat(conversation)))
    assert events[-1]["type"] == "done"
    assert "credential-secret" not in json.dumps(requests) + json.dumps(events)
    tools = [json.loads(m["content"]) for m in requests[-1]["body"]["messages"] if m["role"] == "tool"]
    assert "unavailable" in tools[0]["error"] and "context limit" in tools[1]["error"]
    unchanged(conversation)


def test_tool_and_round_limits_stop_without_applying_sql(ai_workspace, monkeypatch):
    for replies, expected in (([completion(None, [call(identity=f"id-{i}") for i in range(17)], "tool_calls")], "lookup limit"),
                              ([completion(None, [call()], "tool_calls")], "turn limit")):
        monkeypatch.setattr(assistant, "MAX_ROUNDS", 1)
        with endpoint(replies) as (url, _):
            _configure(url)
            conversation = _conversation()
            events = list(assistant.stream_chat(_chat(conversation)))
        assert expected in events[-1]["error"]
        unchanged(conversation)


def test_recent_history_is_preserved_but_interrupted_and_oversized_history_is_omitted(ai_workspace):
    conversation = _conversation()
    assistant_store.append_message(conversation["id"], "user", "old" * 50_000)
    assistant_store.append_message(conversation["id"], "system", "internal audit")
    assistant_store.append_message(conversation["id"], "assistant", "interrupted secret", status="interrupted")
    assistant_store.append_message(conversation["id"], "assistant", "Previous explanation", sql="SELECT 2", status="complete")
    with endpoint([completion()]) as (url, requests):
        _configure(url)
        events = list(assistant.stream_chat(_chat(conversation, prompt="Continue.")))
    assert events[-1]["type"] == "done"
    messages = requests[0]["body"]["messages"]
    assert messages[-1] == {"role": "user", "content": "Continue."}
    assert any("Previous explanation" in m["content"] and "SELECT 2" in m["content"] for m in messages)
    assert not any("internal audit" in m["content"] or "interrupted secret" in m["content"] or "oldold" in m["content"] for m in messages)
    unchanged(conversation)


def test_context_and_accumulated_reply_limits_fail_without_sql_changes(ai_workspace, monkeypatch):
    _configure("http://localhost:1/v1")
    conversation = _conversation()
    with monkeypatch.context() as patch:
        patch.setattr(assistant, "MAX_CONTEXT", 20)
        patch.setattr(assistant, "provider_stream", lambda *_a, **_k: pytest.fail("Oversized context must not be sent"))
        assert "context is too large" in list(assistant.stream_chat(_chat(conversation)))[-1]["error"]
    unchanged(conversation)
    def chunks(*_args, **_kwargs):
        yield {"type": "text_delta", "delta": "x" * 64_001}
    monkeypatch.setattr(assistant, "provider_stream", chunks)
    assert "response is too large" in list(assistant.stream_chat(_chat(conversation)))[-1]["error"]
    unchanged(conversation)


@pytest.mark.parametrize("failure,expected", [(None, "incomplete"), (RuntimeError("credential-secret"), "could not complete"), (TimeoutError("credential-secret"), "timed out")])
def test_provider_failure_records_interruption_and_releases_conversation(ai_workspace, monkeypatch, failure, expected):
    _configure("http://localhost:1/v1")
    conversation = _conversation()
    def broken(*_args, **_kwargs):
        yield {"type": "text_delta", "delta": "Partial"}
        if failure:
            raise failure
    monkeypatch.setattr(assistant, "provider_stream", broken)
    events = list(assistant.stream_chat(_chat(conversation)))
    assert expected in events[-1]["error"] and "credential-secret" not in json.dumps(events)
    assert unchanged(conversation)["messages"][-1]["content"] == "Partial"


def test_generation_slots_and_duplicate_chat_are_released_on_disconnect(ai_workspace):
    _configure("http://localhost:1/v1")
    conversations = [_conversation() for _ in range(5)]
    streams = []
    try:
        for conversation in conversations[:4]:
            stream = assistant.stream_chat(_chat(conversation))
            assert next(stream)["type"] == "request"
            streams.append(stream)
        duplicate = list(assistant.stream_chat(_chat(conversations[0], requestId="duplicate")))
        assert "already generating" in duplicate[-1]["error"]
        capacity = list(assistant.stream_chat(_chat(conversations[4])))
        assert "busy" in capacity[-1]["error"]
        assert assistant_store.get(conversations[4]["id"])["messages"] == []
    finally:
        for stream in streams:
            stream.close()
    for conversation in conversations:
        unchanged(conversation)


@pytest.mark.parametrize("conversation,request_id", [(None, "id"), ("", "id"), ("id", []), ("id", "")])
def test_cancel_rejects_invalid_ids(conversation, request_id):
    with pytest.raises(QuarryError):
        assistant.cancel_chat(conversation, request_id)


def test_cancelled_or_expired_provider_does_not_open_a_connection(monkeypatch):
    monkeypatch.setattr(assistant, "build_opener", lambda *_a: pytest.fail("No request after cancellation or deadline"))
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(QuarryError, match="stopped"):
        read_provider("http://localhost:1/v1", cancel=cancelled)
    monkeypatch.setattr(assistant, "MAX_STREAM_SECONDS", -1)
    with pytest.raises(QuarryError, match="time limit"):
        read_provider("http://localhost:1/v1")
    assert assistant.cancel_chat("unknown", "request") == {"cancelled": False}


def test_explanation_without_sql_does_not_start_or_save_turn(ai_workspace):
    _configure("http://localhost:1/v1")
    conversation = assistant.create_conversation({"db": "demo", "env": "dev", "sql": ""})
    events = list(assistant.stream_chat(_chat(conversation, explain=True)))
    assert "Write some SQL" in events[-1]["error"]
    assert unchanged(conversation)["messages"] == []


def test_failed_interruption_save_does_not_leak_error_or_hold_generation_slot(ai_workspace, monkeypatch):
    _configure("http://localhost:1/v1")
    conversation = _conversation()
    original_append = assistant_store.append_message
    def append(identity, role, content, **extra):
        if role == "assistant":
            raise OSError("disk failure: credential-secret")
        return original_append(identity, role, content, **extra)
    def fail(*_args, **_kwargs):
        raise RuntimeError("upstream failure: credential-secret")
    monkeypatch.setattr(assistant_store, "append_message", append)
    monkeypatch.setattr(assistant, "provider_stream", fail)
    events = list(assistant.stream_chat(_chat(conversation)))
    assert events[-1]["type"] == "error"
    assert "credential-secret" not in json.dumps(events)
    saved = unchanged(conversation)
    assert [m["role"] for m in saved["messages"]] == ["user"]
