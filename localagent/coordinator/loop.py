"""The agent loop: generate → parse tool calls → validate → gate → execute → repeat.

Error handling philosophy: failures are fed back to the model as information. Consecutive failures
past a limit make the agent stop and ask the user for help instead of flailing.

The loop runs over a Conversation (conversation.py): a chat turn or a job task session.
"""
from __future__ import annotations

import hashlib
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
from ..tools.registry import (ApprovalPending, Tool, ToolContext, ToolError, ToolRegistry, ToolResult,
                              validate_args)
from . import prompts
from .context import fit_messages
from .conversation import ChatConversation, Conversation

log = logging.getLogger(__name__)

# Tools that only look at things. Repeating one of these with identical arguments, when nothing was changed since,
# returns the same result; a small model with elided context can loop on them (dry run 5: 14 alternating
# search_notes/read_file calls without writing anything).
READ_ONLY_TOOLS = {"read_file", "list_dir", "glob", "grep", "read_document", "search_notes", "check_citations", "list_projects"}


def repeat_guard(seen: dict[str, list[int]], name: str, result: ToolResult, step: int) -> ToolResult:
    """Keyed on the output, not the arguments: the model varies limits and empty queries while looping. The first
    repeat is shown with a warning; later ones are withheld and count as failures, so a loop ends in needs_help."""
    key = name + ":" + hashlib.sha1(result.content.encode("utf-8", "replace")).hexdigest()
    earlier = seen.setdefault(key, [])
    earlier.append(step)
    if len(earlier) == 1:
        return result
    steps = ", ".join(str(s + 1) for s in earlier[:-1])
    if len(earlier) == 2:
        return ToolResult(f"(This is exactly the same output you got at step {steps}; nothing has changed since. "
                          "Don't fetch it again: use it and move on.)\n" + result.content)
    return ToolResult(f"Not shown: this {name} call returned exactly the same output as at steps {steps}, and nothing "
                      "has changed since. Stop re-checking and act on what you know: write the file, make the change, "
                      "or finish (for a task, call complete_task; its checks run automatically).", ok=False)

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
    def run(self, target: str | Conversation, user_text: str | None, cancel: threading.Event | None = None,
            user_kind: str = "normal") -> str:
        """Run one turn to completion. `target` is a chat id or a Conversation. Returns the outcome:
        done | waiting_user | cancelled | interrupted | needs_help | step_limit | error"""
        conv = ChatConversation(self.store, target) if isinstance(target, str) else target
        cancel = cancel or threading.Event()
        settings = self.settings_getter()
        max_steps = conv.max_steps or settings.max_steps
        if user_text is not None:
            self._add(conv, "user", user_text, kind=user_kind)
        self._status(conv, "running")
        failures = 0
        outcome = "done"
        seen: dict[str, list[int]] = {}      # read-only call key -> steps it ran at, since the last change
        try:
            for step in range(max_steps):
                if cancel.is_set():
                    outcome = "cancelled"
                    break
                if self.resources is not None:
                    self.resources.wait_until_clear(cancel, lambda s: self._status(conv, s))
                reason = conv.before_step(cancel)
                if reason:
                    self._status(conv, "interrupted", reason=reason)
                    outcome = "interrupted"
                    break
                ctx = self._context(conv, cancel)
                tools = conv.tools(self.registry, ctx)
                text, usage = self._generate(conv, ctx, tools, cancel)
                if cancel.is_set():
                    if text.strip():
                        self._add(conv, "assistant", text.strip() + "\n\n[stopped by user]")
                    outcome = "cancelled"
                    break
                parsed = get_parser(settings.tool_call_format)(text, [t.schema() for t in tools])
                calls = [{"id": f"call_{time.time_ns()}_{i}", **c} for i, c in enumerate(parsed.tool_calls)]
                self._add(conv, "assistant", parsed.content, reasoning=parsed.reasoning or None,
                          tool_calls=calls or None, usage=usage)

                if parsed.errors:
                    failures += 1
                    for c in calls:     # every call needs a result, even the ones we skip
                        self._add(conv, "tool", "Not run: another tool call in the same message was malformed.",
                                  tool_call_id=c["id"], name=c["name"], ok=False)
                    self._add(conv, "user", prompts.parse_error_note(
                        parsed.errors, FORMAT_REMINDER.get(parsed.format, "")), kind="coordinator")
                    if failures >= settings.max_consecutive_failures:
                        outcome = self._wrap_up(conv, cancel, "needs_help", max_steps)
                        break
                    continue

                if not calls:
                    if not parsed.content:
                        failures += 1
                        self._add(conv, "user", "Your reply was empty. Continue the task, or summarize "
                                  "and stop if you are done.", kind="coordinator")
                        if failures >= settings.max_consecutive_failures:
                            outcome = self._wrap_up(conv, cancel, "needs_help", max_steps)
                            break
                        continue
                    outcome = "done"
                    break

                by_name = {t.name: t for t in tools}
                end_turn = False
                for i, call in enumerate(calls):
                    if cancel.is_set():
                        for c in calls[i:]:
                            self._add(conv, "tool", "Cancelled by the user.", tool_call_id=c["id"],
                                      name=c["name"], ok=False)
                        break
                    result = self._execute(ctx, by_name, call, settings)
                    if call["name"] in READ_ONLY_TOOLS and result.ok:
                        result = repeat_guard(seen, call["name"], result, step)
                    elif call["name"] not in READ_ONLY_TOOLS and result.ok:
                        seen.clear()                 # something may have changed: earlier reads are stale
                    failures = 0 if result.ok else failures + 1
                    self._add(conv, "tool", result.content, tool_call_id=call["id"], name=call["name"],
                              ok=result.ok)
                    end_turn = end_turn or result.end_turn
                    if result.denied or result.end_turn:
                        # Later calls were planned without knowing this outcome; make the model re-plan.
                        why = "was denied" if result.denied else "ended the turn"
                        for c in calls[i + 1:]:
                            self._add(conv, "tool", f"Not run: an earlier tool call in this message {why}. "
                                      "Re-plan based on that result.", tool_call_id=c["id"], name=c["name"], ok=False)
                        break
                if cancel.is_set():
                    outcome = "cancelled"
                    break
                if end_turn:
                    outcome = "waiting_user"
                    break
                if failures >= settings.max_consecutive_failures:
                    outcome = self._wrap_up(conv, cancel, "needs_help", max_steps)
                    break
            else:
                outcome = self._wrap_up(conv, cancel, "step_limit", max_steps)
        except GenerationCancelled:
            outcome = "cancelled"
        except Exception as e:
            log.exception("agent run failed")
            self._add(conv, "system", f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[-3000:]}", kind="error")
            outcome = "error"
        finally:
            self._status(conv, "idle", outcome=outcome)
        return outcome

    # -- internals ---------------------------------------------------------
    def _status(self, conv: Conversation, state: str, **extra) -> None:
        self.emit({"type": "status", **conv.event_fields(), "state": state, **extra})

    def _add(self, conv: Conversation, role: str, content: str | None, **kw) -> dict:
        msg = conv.add_message(role, content, **kw)
        self.emit({"type": "message", **conv.event_fields(), "message": msg})
        return msg

    def _context(self, conv: Conversation, cancel: threading.Event) -> ToolContext:
        settings = self.settings_getter()
        project = conv.project()
        if project:
            workspace = Path(project["workspace_path"])
            env_path = Path(project["env_path"] or settings.env_path)
        else:
            workspace = settings.general_workspace
            env_path = Path(settings.env_path)
        workspace.mkdir(parents=True, exist_ok=True)
        return ToolContext(chat_id=conv.chat_id, project=project, workspace=workspace, env_path=env_path,
                           guard=PathGuard(workspace, env_path), policy=self.policy,
                           approvals=conv.approvals(self.approvals), store=self.store, settings=settings,
                           cancel=cancel, emit=self.emit, conversation=conv)

    def _build_messages(self, conv: Conversation, ctx: ToolContext, tools: list[dict] | None,
                        max_new_tokens: int) -> list[dict]:
        system = {"role": "system", "content": conv.system_prompt(ctx)}
        history = to_model_messages(conv.messages())
        budget = ctx.settings.context_tokens - max_new_tokens
        return fit_messages([system] + history, budget, tools)

    def _generate(self, conv: Conversation, ctx: ToolContext, tools: list[Tool] | None,
                  cancel: threading.Event) -> tuple[str, dict | None]:
        """Returns the raw text and usage stats (None when the backend doesn't report them)."""
        settings = ctx.settings
        params = effective_generation(settings, conv.gen_overrides())
        schemas = [t.schema() for t in tools] if tools else None
        messages = self._build_messages(conv, ctx, schemas, int(params["max_new_tokens"]))
        parts: list[str] = []
        usage = None
        fields = conv.event_fields()
        self.emit({"type": "generation_start", **fields})
        for chunk in self.backend.generate(messages, schemas, params, adapter=None, cancel=cancel,
                                           on_status=lambda s: self._status(conv, s)):
            if isinstance(chunk, dict):
                usage = {**chunk["usage"], "context_tokens": settings.context_tokens,
                         "preset": params.get("preset"), "thinking": params.get("thinking")}
                self.emit({"type": "usage", **fields, "usage": usage})
                continue
            parts.append(chunk)
            self.emit({"type": "token", **fields, "text": chunk})
            if cancel.is_set():
                break
        self.emit({"type": "generation_end", **fields})
        self._status(conv, "running")
        return "".join(parts), usage

    def _execute(self, ctx: ToolContext, by_name: dict[str, Tool], call: dict, settings) -> ToolResult:
        tool = by_name.get(call["name"])
        if tool is None:
            return ToolResult(f"Unknown tool '{call['name']}'. Available tools: {', '.join(sorted(by_name))}.", ok=False)
        err = validate_args(tool, call["arguments"])
        if err:
            return ToolResult(f"Invalid arguments for {tool.name}: {err}. Check the tool's parameter schema.", ok=False)
        self.emit({"type": "tool_start", **ctx.conversation.event_fields(), "call": call})
        box: list[ToolResult] = []

        def target():
            try:
                r = tool.fn(ctx, **call["arguments"])
                box.append(r if isinstance(r, ToolResult) else ToolResult(str(r)))
            except ToolError as e:
                box.append(ToolResult(str(e), ok=False, denied=e.denied))
            except ApprovalPending as e:
                ctx.conversation.result = {"kind": "approval", "approval_id": e.approval_id, "summary": e.summary}
                box.append(ToolResult(f"This needs the user's approval ({e.summary}). The task will pause here and "
                                      "continue after they decide.", end_turn=True))
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

    def _wrap_up(self, conv: Conversation, cancel: threading.Event, outcome: str, max_steps: int) -> str:
        settings = self.settings_getter()
        note = prompts.WRAP_UP_FAILURES if outcome == "needs_help" else prompts.WRAP_UP_STEPS.format(steps=max_steps)
        ctx = self._context(conv, cancel)
        final_tools = conv.wrap_up_tools(self.registry, ctx)
        if final_tools:
            names = ", ".join(t.name for t in final_tools)
            note = f"You're out of steps. Call {names} now with your conclusion so far. Don't call any other tool."
        self._add(conv, "user", note, kind="coordinator")
        try:
            text, usage = self._generate(conv, ctx, final_tools, cancel)
        except GenerationCancelled:
            return "cancelled"
        parsed = get_parser(settings.tool_call_format)(text, [t.schema() for t in final_tools or []])
        by_name = {t.name: t for t in final_tools or []}
        allowed = [c for c in parsed.tool_calls if c["name"] in by_name]
        if allowed:
            calls = [{"id": f"call_{time.time_ns()}_{i}", **c} for i, c in enumerate(allowed[:1])]
            self._add(conv, "assistant", parsed.content, reasoning=parsed.reasoning or None, tool_calls=calls,
                      usage=usage)
            result = self._execute(ctx, by_name, calls[0], settings)
            self._add(conv, "tool", result.content, tool_call_id=calls[0]["id"], name=calls[0]["name"], ok=result.ok)
            return outcome
        content = parsed.content or "(The agent stopped without a summary.)"
        if parsed.tool_calls:
            content += "\n\n[tool calls in this summary were ignored]"
        self._add(conv, "assistant", content, reasoning=parsed.reasoning or None, usage=usage)
        return outcome
