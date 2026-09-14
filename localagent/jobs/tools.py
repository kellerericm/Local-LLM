"""Tools that exist only inside job sessions: proposing a plan, finishing or failing a task, asking the user.

They record their result on the session's conversation (`ctx.conversation.result`) and end the turn;
the JobRunner decides what happens next.
"""
from __future__ import annotations

from ..tools.registry import Tool, ToolContext, ToolResult
from .planner import CHECK_TYPES, lint_plan, normalize_plan


def propose_plan(ctx: ToolContext, tasks: list[dict]) -> ToolResult:
    plan = normalize_plan(tasks)
    errors = lint_plan(plan, ctx.policy, ctx.guard)
    if errors:
        return ToolResult("The plan was not accepted. Fix these problems and call propose_plan again with the whole "
                          "plan:\n- " + "\n- ".join(errors), ok=False)
    ctx.conversation.result = {"kind": "plan", "tasks": plan}
    return ToolResult(f"Plan accepted ({len(plan)} tasks). It will be shown to the user for approval.", end_turn=True)


def complete_task(ctx: ToolContext, summary: str) -> ToolResult:
    ctx.conversation.result = {"kind": "complete", "summary": summary.strip()}
    return ToolResult("Recorded. The task's checks will run now.", end_turn=True)


def fail_task(ctx: ToolContext, reason: str, what_would_help: str = "") -> ToolResult:
    ctx.conversation.result = {"kind": "fail", "reason": reason.strip(), "what_would_help": what_would_help.strip()}
    return ToolResult("Recorded. Thank you for being clear about it.", end_turn=True)


def job_ask_user(ctx: ToolContext, question: str) -> ToolResult:
    ctx.conversation.result = {"kind": "ask", "question": question.strip()}
    return ToolResult("Your question was sent to the user. This task will wait for the answer; other tasks continue.",
                      end_turn=True)


_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"enum": sorted(CHECK_TYPES)},
        "path": {"type": "string", "description": "workspace-relative path (file_exists, file_contains, json_valid)"},
        "text": {"type": "string", "description": "text that must appear (file_contains)"},
        "command": {"type": "string", "description": "PowerShell command that must exit 0 (command_ok)"},
    },
    "required": ["type"],
}

PROPOSE_PLAN = Tool(
    "propose_plan",
    "Propose the job's plan as a list of tasks. Group related tasks under a parent with parent_id (max 3 levels, "
    "max 8 subtasks per parent). Only tasks without subtasks are executed, each in a fresh context with at most "
    "~15 tool calls, so keep them small and self-contained.",
    {"type": "object", "properties": {"tasks": {"type": "array", "minItems": 1, "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "short unique id, e.g. t1"},
            "parent_id": {"type": "string", "description": "id of the grouping task; omit for top level"},
            "title": {"type": "string"},
            "instructions": {"type": "string", "description": "what to do, including which files to read/write"},
            "done_when": {"type": "string", "description": "concrete and checkable: a file that will exist, what it "
                                                           "will contain, a number, a command that will succeed"},
            "depends_on": {"type": "array", "items": {"type": "string"}},
            "checks": {"type": "array", "items": _CHECK_SCHEMA,
                       "description": "automatic checks run when the task is marked complete"},
        },
        "required": ["id", "title", "instructions", "done_when"]}}},
     "required": ["tasks"]},
    propose_plan, "job")

COMPLETE_TASK = Tool(
    "complete_task",
    "Finish your task. Summary (≤150 words): what you did, where the results are, anything the next tasks must know. "
    "Later tasks only see this summary and your files, not this conversation. The task's checks run afterwards.",
    {"type": "object", "properties": {"summary": {"type": "string", "minLength": 10}}, "required": ["summary"]},
    complete_task, "job")

FAIL_TASK = Tool(
    "fail_task",
    "Stop this task honestly when you can't complete it: say why, and what would help (information, access, a "
    "different approach). Failing clearly is a good outcome; it lets the job adapt.",
    {"type": "object", "properties": {"reason": {"type": "string", "minLength": 5},
                                      "what_would_help": {"type": "string"}}, "required": ["reason"]},
    fail_task, "job")

JOB_ASK_USER = Tool(
    "ask_user",
    "Ask the user a question. Only this task waits for the answer; the rest of the job keeps running.",
    {"type": "object", "properties": {"question": {"type": "string", "minLength": 5}}, "required": ["question"]},
    job_ask_user, "job")
