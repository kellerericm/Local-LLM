"""Core tools that are always available: the task list and asking the user."""
from __future__ import annotations

from .registry import Tool, ToolContext, ToolResult

STATUSES = ["pending", "in_progress", "completed", "blocked"]


def update_tasks(ctx: ToolContext, tasks: list[dict]) -> ToolResult:
    items = [{"content": t["content"].strip(), "status": t["status"]} for t in tasks]
    ctx.store.set_tasks(ctx.chat_id, items)
    ctx.emit({"type": "tasks", "chat_id": ctx.chat_id, "tasks": items})
    counts = {s: sum(1 for t in items if t["status"] == s) for s in STATUSES}
    note = ""
    if counts["in_progress"] > 1:
        note = " Note: more than one task is in_progress; work on one at a time."
    return ToolResult("Task list updated: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v) + "." + note)


def ask_user(ctx: ToolContext, question: str) -> ToolResult:
    return ToolResult("Your question was shown to the user. The turn ends here; their reply will arrive as the next message.",
                      end_turn=True)


TOOLS = [
    Tool("update_tasks",
         "Replace the task list for this chat. Use it to plan multi-step work and keep progress visible: "
         "exactly one task in_progress at a time, mark completed immediately when done, blocked if stuck.",
         {"type": "object", "properties": {"tasks": {"type": "array", "items": {
             "type": "object",
             "properties": {"content": {"type": "string", "minLength": 1}, "status": {"enum": STATUSES}},
             "required": ["content", "status"]}}},
          "required": ["tasks"]},
         update_tasks, "core"),
    Tool("ask_user",
         "Ask the user a question and stop until they answer. Use when requirements are unclear, when you are stuck "
         "after trying, or when you need access or a decision. Say what you tried and what you need.",
         {"type": "object", "properties": {"question": {"type": "string", "minLength": 1}}, "required": ["question"]},
         ask_user, "core"),
]
