"""The job scratchpad: a persistent working document shown at the top of every job prompt (design §3.6).

- Context: facts, decisions, file locations, dead ends. Separate items, maintained by the model
  (update_context) and the user. Capped, so it stays small enough to always include.
- Task list: generated from the plan, with checkboxes and one-line results.
- Current task checklist: the task's own sub-steps (update_checklist), kept across retries and restarts.
"""
from __future__ import annotations

from .planner import children_map

CONTEXT_CHAR_LIMIT = 6000        # ~1.5k tokens
ITEM_CHAR_LIMIT = 400
TASK_LIST_CHAR_LIMIT = 4000

BOX = {"done": "[x]", "skipped": "[-]", "failed": "[!]", "waiting_user": "[?]", "running": "[>]", "pending": "[ ]"}


def context_chars(items: list[dict]) -> int:
    return sum(len(i["text"]) for i in items)


def render_context(items: list[dict]) -> str:
    if not items:
        return "(empty; add facts later tasks will need with update_context)"
    lines = []
    for i in items:
        origin = "user" if i["author"] == "user" else (i["task_key"] or "planner")
        lines.append(f"- [c{i['id']}] {i['text']}  ({origin})")
    return "\n".join(lines)


def render_task_list(tasks: list[dict], current_key: str | None = None) -> str:
    if not tasks:
        return "(no plan yet)"
    kids = children_map(tasks)
    lines: list[str] = []

    def walk(parent, depth):
        for t in sorted(kids.get(parent, []), key=lambda x: x["position"]):
            indent = "  " * depth
            if t["key"] in kids:
                lines.append(f"{indent}{t['title']} [{t['key']}]")
            else:
                box = "[>]" if t["key"] == current_key else BOX.get(t["status"], "[ ]")
                line = f"{indent}{box} [{t['key']}] {t['title']}"
                if t["status"] == "done" and t.get("result_summary"):
                    line += f" — {t['result_summary'][:160]}"
                if t["key"] == current_key:
                    line += "   ← YOUR TASK"
                lines.append(line)
            walk(t["key"], depth + 1)

    walk(None, 0)
    text = "\n".join(lines)
    if len(text) > TASK_LIST_CHAR_LIMIT:
        idx = next((i for i, l in enumerate(lines) if "← YOUR TASK" in l), 0)
        text = "… (earlier tasks omitted) …\n" + "\n".join(lines[max(0, idx - 12): idx + 12]) + "\n… (later tasks omitted) …"
    return text


def render_checklist(items: list[dict]) -> str:
    if not items:
        return "(none yet; for multi-step work, write one with update_checklist and tick items off as you go)"
    return "\n".join(f"{'[x]' if i.get('done') else '[ ]'} {i['text']}" for i in items)


def render_block(tasks: list[dict], context: list[dict], listing: str, current: dict | None = None) -> str:
    parts = ["## Job scratchpad (persistent, shared by every task of this job)",
             "### Workspace top level", listing or "(unavailable)",
             f"### Context ({context_chars(context)}/{CONTEXT_CHAR_LIMIT} characters used)", render_context(context),
             "### Task list", render_task_list(tasks, current["key"] if current else None)]
    if current is not None:
        parts += ["### Your checklist for this task", render_checklist(current.get("checklist") or [])]
    return "\n".join(parts)


def render_markdown(job: dict, tasks: list[dict], context: list[dict]) -> str:
    lines = [f"# Scratchpad: {job['title']}", "",
             "The job's working memory. The agent reads it at the start of every task. Edit the context from the "
             "job view in LocalAgent; this file is regenerated.", "", "## Context", render_context(context), "",
             "## Task list", "```", render_task_list(tasks), "```"]
    with_lists = [t for t in tasks if t.get("checklist")]
    if with_lists:
        lines += ["", "## Task checklists"]
        for t in with_lists:
            lines += [f"### [{t['key']}] {t['title']} ({t['status']})", "```", render_checklist(t["checklist"]), "```"]
    return "\n".join(lines) + "\n"
