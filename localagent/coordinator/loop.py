"""The agent loop: generate → parse tool calls → validate → gate → execute → repeat.

Error handling philosophy: failures are fed back to the model as information. Consecutive failures
past a limit make the agent stop and ask the user for help instead of flailing.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from ..backend.base import GenerationCancelled
from ..backend.model_profiles import effective_generation
from ..backend.toolcall_parsers import FORMAT_REMINDER, get_parser
from ..safety.commands import CommandPolicy
from ..safety.paths import PathGuard
from ..tools.registry import Tool, ToolContext, ToolError, ToolRegistry, ToolResult, validate_args
from . import prompts
from .context import fit_messages

log = logging.getLogger(__name__)

Emit = Callable[[dict], None]


def to_model_messages(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r["kind"] == "error":
            continue
        if r["role"] == "assistant":
            m = {"role": "assistant", "content": r["content"] or ""}
            if r["tool_calls"]:
                m["tool_calls"] = [{"id": c["id"], "type": "function",
                                    "function": {"name": c["name"], "arguments": c["arguments"]}}
                                   for c in r["tool_calls"]]
            out.append(m)
        elif r["role"] == "tool":
            out.append({"role": "tool", "content": r["content"] or "", "tool_call_id": r["tool_call_id"],
                        "name": r["name"]})
        elif r["kind"] == "coordinator":
            out.append({"role": "user", "content": f"[coordinator] {r['content']}"})
        else:
            out.append({"role": r["role"], "content": r["content"] or ""})
    return out


class Coordinator:
    def __init__(self, backend, store, registry: ToolRegistry, approvals, settings_getter: Callable,
                 emit: Emit, resources=None):
        self.backend = backend
        self.store = store
        self.registry = registry
        self.approvals = approvals
        self.settings_getter = settings_getter
        self.emit = emit
        self.resources = resources
        self.policy = CommandPolicy()

    # -- public ------------------------------------------------------------
    def run(self, chat_id: str, user_text: str | None, cancel: threading.Event | None = None) -> str:
        """Run one user turn to completion. Returns the outcome:
        done | waiting_user | cancelled | needs_help | step_limit | error"""
        cancel = cancel or threading.Event()
        settings = self.settings_getter()
        if user_text is not None:
            msg = self.store.add_message(chat_id, "user", user_text)
            self.emit({"type": "message", "chat_id": chat_id, "message": msg})
        self._status(chat_id, "running")
        failures = 0
        outcome = "done"
        try:
            for step in range(settings.max_steps):
                if cancel.is_set():
                    outcome = "cancelled"
                    break
                if self.resources is not None:
                    self.resources.wait_until_clear(cancel, lambda s: self._status(chat_id, s))
                ctx = self._context(chat_id, cancel)
                tools = self.registry.available(ctx)
                text, usage = self._generate(chat_id, ctx, tools, cancel)
                if cancel.is_set():
                    if text.strip():
                        self._add(chat_id, "assistant", text.strip() + "\n\n[stopped by user]")
                    outcome = "cancelled"
                    break
                parsed = get_parser(settings.tool_call_format)(text, [t.schema() for t in tools])
                calls = [{"id": f"call_{time.time_ns()}_{i}", **c} for i, c in enumerate(parsed.tool_calls)]
                self._add(chat_id, "assistant", parsed.content, reasoning=parsed.reasoning or None,
                          tool_calls=calls or None, usage=usage)

                if parsed.errors:
                    failures += 1
                    for c in calls:     # every call needs a result, even the ones we skip
                        self._add(chat_id, "tool", "Not run: another tool call in the same message was malformed.",
                                  tool_call_id=c["id"], name=c["name"], ok=False)
                    self._add(chat_id, "user", prompts.parse_error_note(
                        parsed.errors, FORMAT_REMINDER.get(parsed.format, "")), kind="coordinator")
                    if failures >= settings.max_consecutive_failures:
                        outcome = self._wrap_up(chat_id, cancel, "needs_help")
                        break
                    continue

                if not calls:
                    if not parsed.content:
                        failures += 1
                        self._add(chat_id, "user", "Your reply was empty. Continue the task, or summarize "
                                  "and stop if you are done.", kind="coordinator")
                        if failures >= settings.max_consecutive_failures:
                            outcome = self._wrap_up(chat_id, cancel, "needs_help")
                            break
                        continue
                    outcome = "done"
                    break

                by_name = {t.name: t for t in tools}
                end_turn = False
                for i, call in enumerate(calls):
                    if cancel.is_set():
                        for c in calls[i:]:
                            self._add(chat_id, "tool", "Cancelled by the user.", tool_call_id=c["id"],
                                      name=c["name"], ok=False)
                        break
                    result = self._execute(ctx, by_name, call, settings)
                    failures = 0 if result.ok else failures + 1
                    self._add(chat_id, "tool", result.content, tool_call_id=call["id"], name=call["name"],
                              ok=result.ok)
                    end_turn = end_turn or result.end_turn
                    if result.denied or result.end_turn:
                        # Later calls were planned without knowing this outcome; make the model re-plan.
                        why = "was denied" if result.denied else "asked the user a question"
                        for c in calls[i + 1:]:
                            self._add(chat_id, "tool", f"Not run: an earlier tool call in this message {why}. "
                                      "Re-plan based on that result.", tool_call_id=c["id"], name=c["name"], ok=False)
                        break
                if cancel.is_set():
                    outcome = "cancelled"
                    break
                if end_turn:
                    outcome = "waiting_user"
                    break
                if failures >= settings.max_consecutive_failures:
                    outcome = self._wrap_up(chat_id, cancel, "needs_help")
                    break
            else:
                outcome = self._wrap_up(chat_id, cancel, "step_limit")
        except GenerationCancelled:
            outcome = "cancelled"
        except Exception as e:
            log.exception("agent run failed")
            self._add(chat_id, "system", f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[-3000:]}", kind="error")
            outcome = "error"
        finally:
            self._status(chat_id, "idle", outcome=outcome)
        return outcome

    # -- internals ---------------------------------------------------------
    def _status(self, chat_id: str, state: str, **extra) -> None:
        self.emit({"type": "status", "chat_id": chat_id, "state": state, **extra})

    def _add(self, chat_id: str, role: str, content: str | None, **kw) -> dict:
        msg = self.store.add_message(chat_id, role, content, **kw)
        self.emit({"type": "message", "chat_id": chat_id, "message": msg})
        return msg

    def _context(self, chat_id: str, cancel: threading.Event) -> ToolContext:
        settings = self.settings_getter()
        chat = self.store.get_chat(chat_id)
        project = self.store.get_project(chat["project_id"]) if chat else None
        if project:
            workspace = Path(project["workspace_path"])
            env_path = Path(project["env_path"] or settings.env_path)
        else:
            workspace = settings.general_workspace
            env_path = Path(settings.env_path)
        workspace.mkdir(parents=True, exist_ok=True)
        return ToolContext(chat_id=chat_id, project=project, workspace=workspace, env_path=env_path,
                           guard=PathGuard(workspace, env_path), policy=self.policy, approvals=self.approvals,
                           store=self.store, settings=settings, cancel=cancel, emit=self.emit)

    def _build_messages(self, ctx: ToolContext, tools: list[dict] | None, max_new_tokens: int,
                        extra: list[dict] = ()) -> list[dict]:
        system = {"role": "system", "content": prompts.system_prompt(str(ctx.workspace), str(ctx.env_path), ctx.project)}
        history = to_model_messages(self.store.list_messages(ctx.chat_id)) + list(extra)
        budget = ctx.settings.context_tokens - max_new_tokens
        return fit_messages([system] + history, budget, tools)

    def _generate(self, chat_id: str, ctx: ToolContext, tools: list[Tool] | None, cancel: threading.Event,
                  extra: list[dict] = ()) -> tuple[str, dict | None]:
        """Returns the raw text and usage stats (None when the backend doesn't report them)."""
        settings = ctx.settings
        chat = self.store.get_chat(chat_id) or {}
        params = effective_generation(settings, chat.get("gen_overrides"))
        schemas = [t.schema() for t in tools] if tools else None
        messages = self._build_messages(ctx, schemas, int(params["max_new_tokens"]), extra)
        parts: list[str] = []
        usage = None
        self.emit({"type": "generation_start", "chat_id": chat_id})
        for chunk in self.backend.generate(messages, schemas, params, adapter=None, cancel=cancel,
                                           on_status=lambda s: self._status(chat_id, s)):
            if isinstance(chunk, dict):
                usage = {**chunk["usage"], "context_tokens": settings.context_tokens,
                         "preset": params.get("preset"), "thinking": params.get("thinking")}
                self.emit({"type": "usage", "chat_id": chat_id, "usage": usage})
                continue
            parts.append(chunk)
            self.emit({"type": "token", "chat_id": chat_id, "text": chunk})
            if cancel.is_set():
                break
        self.emit({"type": "generation_end", "chat_id": chat_id})
        self._status(chat_id, "running")
        return "".join(parts), usage

    def _execute(self, ctx: ToolContext, by_name: dict[str, Tool], call: dict, settings) -> ToolResult:
        tool = by_name.get(call["name"])
        if tool is None:
            return ToolResult(f"Unknown tool '{call['name']}'. Available tools: {', '.join(sorted(by_name))}.", ok=False)
        err = validate_args(tool, call["arguments"])
        if err:
            return ToolResult(f"Invalid arguments for {tool.name}: {err}. Check the tool's parameter schema.", ok=False)
        self.emit({"type": "tool_start", "chat_id": ctx.chat_id, "call": call})
        box: list[ToolResult] = []

        def target():
            try:
                r = tool.fn(ctx, **call["arguments"])
                box.append(r if isinstance(r, ToolResult) else ToolResult(str(r)))
            except ToolError as e:
                box.append(ToolResult(str(e), ok=False, denied=e.denied))
            except TypeError as e:
                box.append(ToolResult(f"Bad arguments for {tool.name}: {e}", ok=False))
            except Exception as e:
                log.exception("tool %s crashed", tool.name)
                box.append(ToolResult(f"Tool {tool.name} crashed: {type(e).__name__}: {e}", ok=False))

        thread = threading.Thread(target=target, daemon=True, name=f"tool-{tool.name}")
        thread.start()
        limit = tool.timeout_s or settings.tool_timeout_s
        active = 0.0
        while thread.is_alive():
            thread.join(0.2)
            if not ctx.awaiting_approval:
                active += 0.2
            if active > limit:
                return ToolResult(f"{tool.name} did not finish within {limit}s and was abandoned. "
                                  "Try a smaller operation.", ok=False)
        return box[0] if box else ToolResult(f"{tool.name} returned nothing.", ok=False)

    def _wrap_up(self, chat_id: str, cancel: threading.Event, outcome: str) -> str:
        settings = self.settings_getter()
        note = prompts.WRAP_UP_FAILURES if outcome == "needs_help" else prompts.WRAP_UP_STEPS.format(steps=settings.max_steps)
        self._add(chat_id, "user", note, kind="coordinator")
        ctx = self._context(chat_id, cancel)
        try:
            text, usage = self._generate(chat_id, ctx, None, cancel)
        except GenerationCancelled:
            return "cancelled"
        parsed = get_parser(settings.tool_call_format)(text)
        content = parsed.content or "(The agent stopped without a summary.)"
        if parsed.tool_calls:
            content += "\n\n[tool calls in this summary were ignored]"
        self._add(chat_id, "assistant", content, reasoning=parsed.reasoning or None, usage=usage)
        return outcome
