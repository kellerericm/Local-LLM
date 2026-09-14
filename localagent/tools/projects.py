"""Project tools, available only in general (unaffiliated) chats."""
from __future__ import annotations

from pathlib import Path

from ..safety.paths import normalize
from .registry import Tool, ToolContext, ToolError, ToolResult


def list_projects(ctx: ToolContext) -> ToolResult:
    projects = ctx.store.list_projects()
    if not projects:
        return ToolResult("There are no projects yet.")
    return ToolResult("\n".join(f"- {p['name']} (id {p['id']}): {p['workspace_path']}"
                                + (f" — {p['description']}" if p["description"] else "") for p in projects))


def create_project(ctx: ToolContext, name: str, workspace_path: str, description: str = "",
                   move_this_chat: bool = True) -> ToolResult:
    ws = Path(workspace_path)
    if not ws.is_absolute():
        raise ToolError("workspace_path must be an absolute path, e.g. D:\\Projects\\my-report.")
    for p in ctx.store.list_projects():
        if normalize(p["workspace_path"]) == normalize(ws):
            raise ToolError(f"Project '{p['name']}' already uses that folder.")
    detail = (f"Name: {name}\nWorkspace folder: {ws}{'' if ws.exists() else ' (will be created)'}\n"
              f"Description: {description or '-'}\nMove this chat into the project: {'yes' if move_this_chat else 'no'}")
    if not ctx.ask([f"create-project:{normalize(ws)}"], "Create a project", detail):
        return ToolResult("The user declined to create this project. Ask what they would prefer.", ok=False, denied=True)
    ws.mkdir(parents=True, exist_ok=True)
    project = ctx.store.create_project(name, str(ws), description=description)
    if move_this_chat:
        ctx.store.update_chat(ctx.chat_id, project_id=project["id"])
    ctx.emit({"type": "state_changed"})
    moved = " This chat now belongs to it, so your workspace is now that folder." if move_this_chat else ""
    return ToolResult(f"Created project '{name}' at {ws}.{moved}")


TOOLS = [
    Tool("list_projects", "List the user's projects and their workspace folders.",
         {"type": "object", "properties": {}}, list_projects, "projects", scope="general"),
    Tool("create_project",
         "Create a project (a named workspace folder with its own chats). Requires the user's approval. "
         "By default this chat moves into the new project.",
         {"type": "object", "properties": {
             "name": {"type": "string", "minLength": 1},
             "workspace_path": {"type": "string", "description": "Absolute folder path; created if missing."},
             "description": {"type": "string"},
             "move_this_chat": {"type": "boolean"}},
          "required": ["name", "workspace_path"]},
         create_project, "projects", scope="general"),
]
