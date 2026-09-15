"""Where an agent run's messages live and how its prompt is built.

The agent loop (loop.py) is the same for a chat turn and for a job task. What differs is captured here:
message storage, the system prompt, which tools are offered, generation overrides, event routing,
and break points where a run may be interrupted.
"""
from __future__ import annotations

import threading

from ..tools.registry import Tool, ToolRegistry
from . import prompts


class Conversation:
    chat_id: str | None = None
    max_steps: int | None = None        # None = settings.max_steps
    read_only_nudge: int | None = None  # after this many look-only steps in a row, tell the model to write something

    def event_fields(self) -> dict:
        """Fields added to every event this run emits, so the UI can route them."""
        raise NotImplementedError

    def project(self) -> dict | None:
        raise NotImplementedError

    def messages(self) -> list[dict]:
        raise NotImplementedError

    def add_message(self, role: str, content: str | None, **kw) -> dict:
        raise NotImplementedError

    def gen_overrides(self) -> dict:
        return {}

    def system_prompt(self, ctx) -> str:
        return prompts.system_prompt(str(ctx.workspace), str(ctx.env_path), ctx.project)

    def tools(self, registry: ToolRegistry, ctx) -> list[Tool]:
        return registry.available(ctx)

    def approvals(self, default):
        return default

    def wrap_up_tools(self, registry: ToolRegistry, ctx) -> list[Tool] | None:
        """Tools still offered in the final no-more-steps turn (None = text only). Sessions whose only useful
        output is a tool call, like a reviewer's verdict, return that tool here."""
        return None

    def before_step(self, cancel: threading.Event) -> str | None:
        """Called at each break point (before a model step). Return a reason string to interrupt the run."""
        return None


class ChatConversation(Conversation):
    def __init__(self, store, chat_id: str):
        self.store = store
        self.chat_id = chat_id

    def event_fields(self) -> dict:
        return {"chat_id": self.chat_id}

    def project(self) -> dict | None:
        chat = self.store.get_chat(self.chat_id)
        return self.store.get_project(chat["project_id"]) if chat else None

    def messages(self) -> list[dict]:
        return self.store.list_messages(self.chat_id)

    def add_message(self, role: str, content: str | None, **kw) -> dict:
        return self.store.add_message(self.chat_id, role, content, **kw)

    def gen_overrides(self) -> dict:
        return (self.store.get_chat(self.chat_id) or {}).get("gen_overrides") or {}
