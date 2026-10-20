"""Assistant HTTP contracts against a real local GUI server and SQLite workspace."""
from __future__ import annotations

import json
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from quarry import assistant, assistant_config, assistant_store


@pytest.fixture
def model_config(monkeypatch, tmp_path):
    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "ai-config.toml"))
    assistant_config.configure_model(base_url="http://localhost:1/v1", model="api-test", no_key=True)


def _stream(client, body):
    request = Request(client.base + "/api/assistant/chat", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
        assert response.headers["Content-Type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in response if line.strip()]


def _conversation(client):
    code, conversation = client.post("/api/assistant/conversations", {"db": "notes", "env": "dev", "sql": "SELECT * FROM entries"})
    assert code == 200
    return conversation


def test_assistant_config_read_only_no_key_in_response(sqlite_gui_server, model_config, monkeypatch):
    status, body = sqlite_gui_server.get("/api/assistant/config")
    assert status == 200 and body == {"configured": True, "model": "api-test"}
    status, _ = sqlite_gui_server.post("/api/assistant/config", {"api_key": "must-not-save"})
    assert status == 404
    status, _ = sqlite_gui_server.post("/api/assistant/conversations", {"db": "notes", "sql": "SELECT 1"}, headers={"Origin": "https://evil.example"})
    assert status == 403


def test_assistant_conversation_persistence_and_cas_http(sqlite_gui_server):
    client = sqlite_gui_server
    original = _conversation(client)
    assert original["connectionAvailable"] is True and original["guiWorkspace"]
    status, listing = client.get("/api/assistant/conversations")
    assert status == 200 and original["id"] in [item["id"] for item in listing["conversations"]]
    status, updated = client.post("/api/assistant/conversations/update", {"id": original["id"], "revision": 0, "sql": "SELECT 2"})
    assert status == 200 and updated["revision"] == 1
    status, _ = client.post("/api/assistant/conversations/update", {"id": original["id"], "revision": 0, "sql": "SELECT 3"})
    assert status == 409
    status, reopened = client.get("/api/assistant/conversations?id=" + original["id"])
    assert status == 200 and reopened["sql"] == "SELECT 2"
    status, _ = client.get("/api/assistant/conversations?id=missing")
    assert status == 404


def test_assistant_stream_and_checkpoint_endpoints(sqlite_gui_server, model_config, monkeypatch):
    client = sqlite_gui_server
    conversation = _conversation(client)
    calls = []
    def provider(config, messages, cancel, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            yield {"type": "complete", "content": "", "tool_calls": [
                {"id": "proposal", "type": "function", "function": {"name": "propose_sql", "arguments": '{"sql":"SELECT id FROM entries LIMIT 2"}'}}]}
        else:
            yield {"type": "text_delta", "delta": "Selected IDs and capped the result."}
            yield {"type": "complete", "content": "Selected IDs and capped the result.", "tool_calls": []}
    monkeypatch.setattr(assistant, "provider_stream", provider)
    events = _stream(client, {"conversationId": conversation["id"], "requestId": "http-request", "revision": 0,
                              "sql": conversation["sql"], "prompt": "Only IDs, limit 2"})
    assert events[0]["type"] == "request" and events[-1]["type"] == "done"
    assert all(event["requestId"] == "http-request" for event in events)
    proposal = next(event for event in events if event["type"] == "sql_proposal")
    assert events[-1]["conversation"]["sql"] == conversation["sql"]
    status, applied = client.post("/api/assistant/conversations/apply", {"id": conversation["id"], "revision": 0,
                                                                       "sql": proposal["sql"], "messageId": proposal["messageId"]})
    assert status == 200 and applied["revision"] == 1 and len(applied["checkpoints"]) == 1
    status, restored = client.post("/api/assistant/conversations/restore", {"id": conversation["id"], "revision": 1,
                                                                          "checkpointId": applied["checkpoints"][0]["id"]})
    assert status == 200 and restored["sql"] == conversation["sql"]
    assert len(restored["checkpoints"]) == 1
    assert restored["checkpoints"][0]["restorations"][0]["beforeSql"] == proposal["sql"]
    assert "checkpointId" not in restored["messages"][-1]
    for revision in (1, restored["revision"]):
        status, repeated = client.post("/api/assistant/conversations/restore", {
            "id": conversation["id"], "revision": revision,
            "checkpointId": applied["checkpoints"][0]["id"],
        })
        assert status == 200 and repeated == restored
    status, rows = client.post("/api/query", {"db": "notes", "env": "dev", "sql": "SELECT COUNT(*) AS n FROM entries"})
    assert status == 200 and rows["rows"] == [{"n": 250}]


def test_assistant_http_rejects_malformed_and_forged_requests(sqlite_gui_server, model_config):
    client = sqlite_gui_server
    conversation = _conversation(client)
    status, _ = client.post("/api/assistant/chat", {"conversationId": []})
    assert status == 400
    status, _ = client.post("/api/assistant/conversations/update", {"id": conversation["id"], "revision": 0, "sql": []})
    assert status == 400
    status, _ = client.post("/api/assistant/conversations/apply", {"id": conversation["id"], "revision": 0,
                                                                 "sql": "DELETE FROM entries", "messageId": "invented"})
    assert status == 400
    status, body = client.post("/api/assistant/cancel", {"conversationId": conversation["id"], "requestId": "missing"})
    assert status == 200 and body == {"cancelled": False}


def test_old_connection_history_remains_readable_but_chat_rejected(sqlite_gui_server, model_config, monkeypatch):
    client = sqlite_gui_server
    conversation = _conversation(client)
    monkeypatch.setattr(assistant_store, "connection_fingerprint", lambda _id: "old-connection")
    status, body = client.get("/api/assistant/conversations?id=" + conversation["id"])
    assert status == 200 and body["connectionAvailable"] is False and body["guiWorkspace"] is None
    events = _stream(client, {"conversationId": conversation["id"], "revision": 0, "sql": conversation["sql"], "prompt": "Explain"})
    assert events[-1]["type"] == "error" and "connection changed" in events[-1]["error"]


def test_gui_preferences_are_read_only_and_default_to_server_language(sqlite_gui_server, tmp_path, monkeypatch):
    monkeypatch.setenv("QUARRY_CONFIG", str(tmp_path / "preferences.toml"))
    monkeypatch.setattr(assistant_config, "system_reply_language", lambda: "zh")
    client = sqlite_gui_server
    assert client.get("/api/assistant/preferences") == (200, {"replyLanguage": "system", "resolvedReplyLanguage": "zh", "configured": False})
    for body in ({}, {"replyLanguage": "en"}, {"replyLanguage": "zh"}, {"replyLanguage": "system"}):
        status, _ = client.post("/api/assistant/preferences", body)
        assert status == 404
    assert not (tmp_path / "preferences.toml").exists()
    assistant_config.set_reply_language("en")
    assert client.get("/api/assistant/preferences") == (200, {"replyLanguage": "en", "resolvedReplyLanguage": "en", "configured": True})
    assistant_config.set_reply_language("auto")  # The CLI's shared operation.
    assert client.get("/api/assistant/preferences") == (200, {"replyLanguage": "system", "resolvedReplyLanguage": "zh", "configured": True})
    assert client.get("/api/assistant/config") == (200, {"configured": False})


def test_http_chat_uses_gui_preference_independently_of_request_body(sqlite_gui_server, model_config, monkeypatch):
    assistant_config.set_reply_language("en")
    captured = []
    def provider(config, messages, cancel, **kwargs):
        captured.append(messages)
        yield {"type": "text_delta", "delta": "An explanation."}
        yield {"type": "complete", "content": "An explanation.", "tool_calls": []}
    monkeypatch.setattr(assistant, "provider_stream", provider)
    conversation = _conversation(sqlite_gui_server)
    events = _stream(sqlite_gui_server, {"conversationId": conversation["id"], "revision": 0,
                                       "sql": conversation["sql"], "prompt": "解释一下", "gui": False})
    assert events[-1]["type"] == "done"
    assert any(m["content"].startswith("GUI reply language preference: reply in English.") for m in captured[0])
    assert captured[0][-1]["content"] == "解释一下"
