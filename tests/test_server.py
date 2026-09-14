import time

from fastapi.testclient import TestClient

from localagent.backend.fake import EchoBackend
from localagent.server import create_app


def wait_idle(client, chat_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/chats/{chat_id}").json()
        if not data["running"]:
            return data
        time.sleep(0.05)
    raise AssertionError("chat never finished")


def wait_for_approval(client, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = client.get("/api/state").json()["pending_approvals"]
        if pending:
            return pending[0]
        time.sleep(0.05)
    raise AssertionError("no approval request")


def test_end_to_end_with_fake_model(settings, workspace):
    with TestClient(create_app(settings, EchoBackend())) as client:
        assert client.get("/").status_code == 200
        project = client.post("/api/projects", json={"name": "Demo", "workspace_path": str(workspace)}).json()
        (workspace / "readme.txt").write_text("hello")
        chat = client.post("/api/chats", json={"project_id": project["id"]}).json()

        with client.websocket_connect("/ws") as ws:
            assert client.post(f"/api/chats/{chat['id']}/send", json={"text": "/ls"}).status_code == 200
            types = set()
            while True:
                ev = ws.receive_json()
                types.add(ev["type"])
                if ev["type"] == "status" and ev["state"] == "idle":
                    break
            assert {"token", "message", "tool_start"} <= types

        data = wait_idle(client, chat["id"])
        tool = [m for m in data["messages"] if m["role"] == "tool"][0]
        assert tool["ok"] and "readme.txt" in tool["content"]
        assert data["chat"]["title"] == "/ls"

        # Reading outside the workspace triggers an approval; deny it.
        client.post(f"/api/chats/{chat['id']}/send", json={"text": "/outside"})
        approval = wait_for_approval(client)
        assert "outside the workspace" in approval["summary"]
        assert client.post(f"/api/approvals/{approval['id']}", json={"decision": "deny"}).status_code == 200
        data = wait_idle(client, chat["id"])
        denied = [m for m in data["messages"] if m["role"] == "tool"][-1]
        assert not denied["ok"] and "denied" in denied["content"]

        # Settings round-trip
        s = client.put("/api/settings", json={"max_steps": 12, "resources": {"cpu_threads": 4}}).json()
        assert s["max_steps"] == 12 and s["resources"]["cpu_threads"] == 4

        # Validation
        assert client.post("/api/projects", json={"name": "x", "workspace_path": "relative\\path"}).status_code == 400

    # Restart: state persists
    with TestClient(create_app(settings.__class__.from_dict({**settings.to_dict()}), EchoBackend())) as client:
        state = client.get("/api/state").json()
        assert [p["name"] for p in state["projects"]] == ["Demo"]
        assert any(c["id"] == chat["id"] for c in state["chats"])
        assert len(client.get(f"/api/chats/{chat['id']}").json()["messages"]) >= 6
        assert client.delete(f"/api/chats/{chat['id']}").status_code == 200
        assert client.get(f"/api/chats/{chat['id']}").status_code == 404
