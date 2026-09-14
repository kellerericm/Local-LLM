import sqlite3
import time

from fastapi.testclient import TestClient

from localagent.backend.fake import EchoBackend, ScriptedBackend
from localagent.backend.model_profiles import effective_generation, profile_for, validate_generation
from localagent.coordinator import Coordinator
from localagent.safety import AutoApprover
from localagent.server import create_app
from localagent.store import Store
from localagent.tools import default_registry


def test_profiles_match_model_families():
    assert profile_for("Qwen/Qwen3.5-9B")["family"].startswith("Qwen3.5")
    assert profile_for("Qwen/Qwen3.6-27B")["family"].startswith("Qwen3.5")
    assert profile_for("Qwen/Qwen3-8B")["family"] == "Qwen3"
    assert profile_for("google/gemma-3-4b-it")["family"] == "Other model"
    fast = profile_for("Qwen/Qwen3.5-9B")["presets"]["fast"]
    assert fast["thinking"] is False and fast["presence_penalty"] == 1.5


def test_validate_generation_limits():
    assert validate_generation({"temperature": 0.7, "top_p": 0.9, "thinking_budget": 2048}) == []
    errors = validate_generation({"temperature": 5, "top_p": 0, "presence_penalty": "lots"})
    assert len(errors) == 3


def test_chat_overrides_win(settings):
    params = effective_generation(settings, {"temperature": 1.0, "thinking": False, "bogus": 1})
    assert params["temperature"] == 1.0 and params["thinking"] is False and "bogus" not in params
    assert params["top_k"] == settings.top_k


def test_coordinator_uses_chat_overrides_and_stores_usage(store, settings, workspace):
    p = store.create_project("P", str(workspace))
    chat = store.create_chat(p["id"])["id"]
    store.update_chat(chat, gen_overrides={"preset": "fast", "thinking": False, "temperature": 0.7, "thinking_budget": 512})

    class UsageBackend(ScriptedBackend):
        def generate(self, *a, **kw):
            yield from super().generate(*a, **kw)
            yield {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "thinking_tokens": 0, "answer_tokens": 20}}

    backend = UsageBackend(["Hello there."])
    events = []
    Coordinator(backend, store, default_registry(), AutoApprover(), lambda: settings, events.append).run(chat, "hi")
    sent = backend.calls[0]["params"]
    assert sent["thinking"] is False and sent["temperature"] == 0.7 and sent["thinking_budget"] == 512
    reply = store.list_messages(chat)[-1]
    assert reply["usage"]["prompt_tokens"] == 100 and reply["usage"]["context_tokens"] == settings.context_tokens
    assert any(e["type"] == "usage" for e in events)


def test_old_database_is_migrated(settings):
    # A database created before gen_overrides/usage existed.
    conn = sqlite3.connect(settings.db_path)
    conn.executescript("""
        CREATE TABLE chats(id TEXT PRIMARY KEY, project_id TEXT, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                           created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, role TEXT NOT NULL,
                              kind TEXT NOT NULL DEFAULT 'normal', content TEXT, reasoning TEXT, tool_calls TEXT,
                              tool_call_id TEXT, name TEXT, ok INTEGER, created_at REAL NOT NULL);
        INSERT INTO chats VALUES('old', NULL, 'Old chat', 'active', 1, 1);
        INSERT INTO messages(chat_id, role, content, created_at) VALUES('old', 'user', 'hello', 1);
    """)
    conn.commit()
    conn.close()
    s = Store(settings.db_path)
    assert s.get_chat("old")["gen_overrides"] == {}
    assert s.list_messages("old")[0]["usage"] is None
    s.update_chat("old", gen_overrides={"temperature": 0.3})
    assert s.get_chat("old")["gen_overrides"] == {"temperature": 0.3}
    s.close()


def test_settings_and_chat_override_api(settings):
    with TestClient(create_app(settings, EchoBackend())) as client:
        prof = client.get("/api/model/profile").json()
        assert prof["presets"] and "temperature" in prof["docs"] and prof["limits"]["top_p"]

        ok = client.put("/api/settings", json={"preset": "fast", "thinking": False, "thinking_budget": 1024,
                                               "resources": {"offload": "gpu_only"}})
        assert ok.status_code == 200 and ok.json()["resources"]["offload"] == "gpu_only"
        assert client.put("/api/settings", json={"temperature": 9}).status_code == 400
        assert client.put("/api/settings", json={"context_tokens": 999999}).status_code == 400
        assert client.put("/api/settings", json={"resources": {"offload": "disk"}}).status_code == 400

        chat = client.post("/api/chats", json={}).json()
        r = client.patch(f"/api/chats/{chat['id']}", json={"gen_overrides": {"temperature": 1.1, "junk": 1}})
        assert r.json()["gen_overrides"] == {"temperature": 1.1}
        assert client.patch(f"/api/chats/{chat['id']}", json={"gen_overrides": {"top_k": -3}}).status_code == 400
        assert client.patch(f"/api/chats/{chat['id']}", json={"gen_overrides": {}}).json()["gen_overrides"] == {}

        # Usage from the (fake) model is stored on the reply.
        client.post(f"/api/chats/{chat['id']}/send", json={"text": "hello"})
        for _ in range(100):
            data = client.get(f"/api/chats/{chat['id']}").json()
            if not data["running"]:
                break
            time.sleep(0.05)
        assert data["messages"][-1]["usage"]["completion_tokens"] > 0
