"""Tool definitions, the per-call context, and argument validation."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import jsonschema

from ..safety.paths import PathGuard, normalize


class ToolError(Exception):
    """An expected failure; the message is shown to the model."""

    def __init__(self, message: str, denied: bool = False):
        super().__init__(message)
        self.denied = denied


@dataclass
class ToolResult:
    content: str
    ok: bool = True
    end_turn: bool = False          # stop the agent loop and wait for the user
    denied: bool = False            # the user or the policy refused; later calls in the same message are skipped


@dataclass
class ToolContext:
    chat_id: str
    project: dict | None
    workspace: Path
    env_path: Path
    guard: PathGuard
    policy: Any                     # CommandPolicy
    approvals: Any                  # ApprovalBroker | AutoApprover
    store: Any                      # Store
    settings: Any                   # Settings
    cancel: threading.Event
    emit: Callable[[dict], None]
    awaiting_approval: bool = False  # tool timeouts don't count time spent waiting on the user

    @property
    def scope(self) -> str:
        return self.project["id"] if self.project else "general"

    @property
    def is_general(self) -> bool:
        return self.project is None

    def ask(self, keys: list[str], summary: str, detail: str) -> bool:
        self.awaiting_approval = True
        try:
            return self.approvals.request(self.chat_id, self.scope, keys, summary, detail, self.cancel)
        finally:
            self.awaiting_approval = False

    def check_path(self, path: str, mode: str) -> Path:
        """Resolve a path and make sure it may be accessed, asking the user if needed."""
        rp = self.guard.resolve(path)
        if self.guard.access(rp, mode) == "allow":
            return rp
        folder = rp if rp.is_dir() else rp.parent
        key = f"path-{mode}:{normalize(folder)}"
        verb = "Read" if mode == "read" else "Write to"
        if self.ask([key], f"{verb} a location outside the workspace", str(rp)):
            return rp
        raise ToolError(f"The user denied {mode} access to {rp}. Stay inside the workspace "
                        f"({self.workspace}) or ask the user how to proceed.", denied=True)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., ToolResult | str]
    toolset: str
    scope: str = "any"              # any | general | project
    timeout_s: int | None = None    # wrapper timeout; None = settings.tool_timeout_s

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


ALWAYS_ON_TOOLSETS = {"core"}
GENERAL_TOOLSETS = ["files", "shell", "python", "projects"]


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def available(self, ctx: ToolContext) -> list[Tool]:
        enabled = set(GENERAL_TOOLSETS if ctx.is_general else ctx.project["toolsets"]) | ALWAYS_ON_TOOLSETS
        out = []
        for t in self.tools.values():
            if t.toolset not in enabled:
                continue
            if t.scope == "general" and not ctx.is_general:
                continue
            if t.scope == "project" and ctx.is_general:
                continue
            out.append(t)
        return out

    def toolsets(self) -> list[str]:
        return sorted({t.toolset for t in self.tools.values()} - ALWAYS_ON_TOOLSETS - {"projects"})


def validate_args(tool: Tool, args: Any) -> str | None:
    if not isinstance(args, dict):
        return f"arguments must be a JSON object, got {type(args).__name__}"
    try:
        jsonschema.validate(args, tool.parameters)
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path)
        return f"{e.message}" + (f" (at '{where}')" if where else "")
    return None


def default_registry() -> ToolRegistry:
    from . import fs, projects, python_exec, shell, tasks

    reg = ToolRegistry()
    for module in (fs, shell, python_exec, tasks, projects):
        for tool in module.TOOLS:
            reg.register(tool)
    return reg
