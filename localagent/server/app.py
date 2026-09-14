"""FastAPI app: REST for state, WebSocket for live events, static files for the UI.

The API is the product boundary: the browser UI uses it today, and a native shell can use it later.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import Settings
from ..safety.paths import normalize
from .runtime import Runtime

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ProjectIn(BaseModel):
    name: str
    workspace_path: str
    env_path: str | None = None
    toolsets: list[str] | None = None
    description: str = ""


class ProjectPatch(BaseModel):
    name: str | None = None
    workspace_path: str | None = None
    env_path: str | None = None
    toolsets: list[str] | None = None
    description: str | None = None


class ChatIn(BaseModel):
    project_id: str | None = None
    title: str = "New chat"


class ChatPatch(BaseModel):
    title: str | None = None
    project_id: str | None = None
    move_to_general: bool = False


class SendIn(BaseModel):
    text: str


class DecisionIn(BaseModel):
    decision: str


def _validate_env(env_path: str | None) -> None:
    if env_path and not (Path(env_path) / "python.exe").exists() and not (Path(env_path) / "bin" / "python").exists():
        raise HTTPException(400, f"No Python interpreter found in environment folder: {env_path}")


def create_app(settings: Settings, backend=None) -> FastAPI:
    rt = Runtime(settings, backend)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        rt.bus.bind(asyncio.get_running_loop())
        rt.start()
        yield
        await asyncio.to_thread(rt.shutdown)

    app = FastAPI(title="LocalAgent", lifespan=lifespan)
    app.state.runtime = rt

    # -- overview ----------------------------------------------------------
    @app.get("/api/state")
    def state():
        return {"projects": rt.store.list_projects(), "chats": rt.store.list_chats(),
                "running": rt.runs.running(), "pending_approvals": rt.approvals.pending(),
                "toolsets": rt.registry.toolsets()}

    @app.get("/api/status")
    def status():
        return {"model": rt.backend.status(), "resources": rt.resources.status(), "running": rt.runs.running()}

    # -- projects ----------------------------------------------------------
    @app.post("/api/projects")
    def create_project(body: ProjectIn):
        ws = Path(body.workspace_path)
        if not ws.is_absolute():
            raise HTTPException(400, "Workspace path must be absolute, e.g. D:\\Projects\\my-project")
        if any(normalize(p["workspace_path"]) == normalize(ws) for p in rt.store.list_projects()):
            raise HTTPException(400, "Another project already uses that folder.")
        _validate_env(body.env_path)
        ws.mkdir(parents=True, exist_ok=True)
        project = rt.store.create_project(body.name.strip() or ws.name, str(ws), body.env_path or None,
                                          body.toolsets, body.description)
        rt.bus.publish({"type": "state_changed"})
        return project

    @app.patch("/api/projects/{project_id}")
    def update_project(project_id: str, body: ProjectPatch):
        if not rt.store.get_project(project_id):
            raise HTTPException(404, "No such project")
        fields = body.model_dump(exclude_unset=True)
        if "workspace_path" in fields:
            if not Path(fields["workspace_path"]).is_absolute():
                raise HTTPException(400, "Workspace path must be absolute")
            Path(fields["workspace_path"]).mkdir(parents=True, exist_ok=True)
        if fields.get("env_path"):
            _validate_env(fields["env_path"])
        project = rt.store.update_project(project_id, **fields)
        rt.bus.publish({"type": "state_changed"})
        return project

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str):
        chats = [c["id"] for c in rt.store.list_chats() if c["project_id"] == project_id]
        if any(c in rt.runs.running() for c in chats):
            raise HTTPException(409, "Stop the project's running chats first.")
        rt.store.delete_project(project_id)      # removes its chats; never touches files on disk
        rt.bus.publish({"type": "state_changed"})
        return {"ok": True}

    # -- chats -------------------------------------------------------------
    @app.post("/api/chats")
    def create_chat(body: ChatIn):
        if body.project_id and not rt.store.get_project(body.project_id):
            raise HTTPException(404, "No such project")
        chat = rt.store.create_chat(body.project_id, body.title)
        rt.bus.publish({"type": "state_changed"})
        return chat

    @app.get("/api/chats/{chat_id}")
    def get_chat(chat_id: str):
        chat = rt.store.get_chat(chat_id)
        if not chat:
            raise HTTPException(404, "No such chat")
        return {"chat": chat, "messages": rt.store.list_messages(chat_id), "tasks": rt.store.get_tasks(chat_id),
                "running": chat_id in rt.runs.running(),
                "pending_approvals": [a for a in rt.approvals.pending() if a["chat_id"] == chat_id]}

    @app.patch("/api/chats/{chat_id}")
    def update_chat(chat_id: str, body: ChatPatch):
        if not rt.store.get_chat(chat_id):
            raise HTTPException(404, "No such chat")
        fields = body.model_dump(exclude_unset=True, exclude={"move_to_general"})
        if body.move_to_general:
            fields["project_id"] = None
        chat = rt.store.update_chat(chat_id, **fields)
        rt.bus.publish({"type": "state_changed"})
        return chat

    @app.delete("/api/chats/{chat_id}")
    def delete_chat(chat_id: str):
        if chat_id in rt.runs.running():
            raise HTTPException(409, "Stop the chat before deleting it.")
        rt.store.delete_chat(chat_id)
        rt.bus.publish({"type": "state_changed"})
        return {"ok": True}

    @app.post("/api/chats/{chat_id}/send")
    def send(chat_id: str, body: SendIn):
        chat = rt.store.get_chat(chat_id)
        if not chat:
            raise HTTPException(404, "No such chat")
        if not body.text.strip():
            raise HTTPException(400, "Empty message")
        if chat["title"] == "New chat":
            title = " ".join(body.text.split())[:60]
            rt.store.update_chat(chat_id, title=title)
            rt.bus.publish({"type": "state_changed"})
        if not rt.runs.start(chat_id, body.text):
            raise HTTPException(409, "This chat is already running.")
        return {"ok": True}

    @app.post("/api/chats/{chat_id}/cancel")
    def cancel(chat_id: str):
        return {"ok": rt.runs.cancel(chat_id)}

    # -- approvals ---------------------------------------------------------
    @app.post("/api/approvals/{approval_id}")
    def decide(approval_id: str, body: DecisionIn):
        try:
            ok = rt.approvals.resolve(approval_id, body.decision)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not ok:
            raise HTTPException(404, "That approval is no longer pending.")
        return {"ok": True}

    @app.get("/api/approval_rules")
    def rules():
        return rt.store.list_rules()

    @app.delete("/api/approval_rules")
    def delete_rule(scope: str, key: str):
        rt.store.delete_rule(scope, key)
        return {"ok": True}

    # -- settings & model --------------------------------------------------
    @app.get("/api/settings")
    def get_settings():
        return rt.settings.to_dict()

    @app.put("/api/settings")
    def put_settings(patch: dict):
        if patch.get("env_path"):
            _validate_env(patch["env_path"])
        if patch.get("quantization") not in (None, "none", "4bit", "8bit"):
            raise HTTPException(400, "quantization must be none, 4bit, or 8bit")
        return rt.update_settings(patch).to_dict()

    @app.post("/api/model/unload")
    def unload():
        ok = rt.backend.unload()
        rt.bus.publish({"type": "model_status", "status": rt.backend.status()})
        return {"ok": ok}

    # -- live events -------------------------------------------------------
    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        q = rt.bus.subscribe()
        try:
            while True:
                event = await q.get()
                await socket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            rt.bus.unsubscribe(q)

    # -- UI ----------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    return app
