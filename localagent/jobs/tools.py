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


def update_context(ctx: ToolContext, add: list[str] | None = None, remove: list[str] | None = None) -> ToolResult:
    from .scratchpad import CONTEXT_CHAR_LIMIT, ITEM_CHAR_LIMIT, context_chars, render_context

    conv = ctx.conversation
    jobs, job_id = conv.jobs, conv.job["id"]
    add = [a.strip() for a in (add or []) if a and a.strip()]
    too_long = [a for a in add if len(a) > ITEM_CHAR_LIMIT]
    if too_long:
        return ToolResult(f"Context items must be at most {ITEM_CHAR_LIMIT} characters each; split or shorten: "
                          f"{too_long[0][:80]}…", ok=False)
    ids = []
    for r in remove or []:
        try:
            ids.append(int(str(r).strip().lstrip("cC")))
        except ValueError:
            return ToolResult(f"Unknown context item id {r!r}; use ids like c12 from the scratchpad.", ok=False)
    current = [i for i in jobs.list_context(job_id) if i["id"] not in ids]
    if context_chars(current) + sum(len(a) for a in add) > CONTEXT_CHAR_LIMIT:
        return ToolResult(f"The context would exceed {CONTEXT_CHAR_LIMIT} characters. Consolidate first: remove items "
                          "that are outdated or merge related ones (remove=[ids], add=[merged text]). Current context:\n"
                          + render_context(jobs.list_context(job_id)), ok=False)
    removed = jobs.remove_context(job_id, ids) if ids else 0
    task_key = getattr(conv, "task", None) and conv.task["key"]
    for a in add:
        jobs.add_context(job_id, a, "agent", task_key)
    conv.runner._changed(job_id)
    return ToolResult(f"Context updated (+{len(add)}, -{removed}). Now:\n" + render_context(jobs.list_context(job_id)))


def update_checklist(ctx: ToolContext, items: list[dict]) -> ToolResult:
    from .scratchpad import render_checklist

    conv = ctx.conversation
    checklist = [{"text": str(i["text"]).strip()[:200], "done": bool(i.get("done"))} for i in items if str(i.get("text", "")).strip()]
    conv.jobs.update_task(conv.task["id"], checklist=checklist)
    conv.task["checklist"] = checklist
    conv.runner._changed(conv.job["id"])
    done = sum(1 for i in checklist if i["done"])
    return ToolResult(f"Checklist saved ({done}/{len(checklist)} done):\n{render_checklist(checklist)}")


def _source_name(ctx: ToolContext, path) -> str:
    try:
        return path.relative_to(ctx.guard.resolve(ctx.workspace)).as_posix()
    except ValueError:
        return str(path)


def add_note(ctx: ToolContext, claim: str, quote: str, source: str, location: str = "",
             tags: list[str] | None = None) -> ToolResult:
    from ..tools.documents_tool import doc_cache
    from .documents import closest_snippet, extract, find_quote

    conv = ctx.conversation
    path = ctx.check_path(source, "read")
    if not path.is_file():
        return ToolResult(f"Source file not found: {source}. Use the path of the document you read.", ok=False)
    try:
        text = extract(path, doc_cache(ctx)).text
    except Exception as e:
        return ToolResult(f"Couldn't read {source} to verify the quote: {e}", ok=False)
    if not find_quote(text, quote):
        near = closest_snippet(text, quote)
        hint = f" The closest passage is: \"{near}\"" if near else ""
        return ToolResult("Note not saved: the quote doesn't appear word for word in the source. Copy the exact words "
                          f"(a shorter exact quote is fine).{hint}", ok=False)
    task_key = getattr(conv, "task", None) and conv.task["key"]
    note = conv.jobs.add_note(conv.job["id"], _source_name(ctx, path), claim.strip(), quote.strip(), location.strip(),
                              tags, task_key)
    conv.runner._changed(conv.job["id"])
    return ToolResult(f"Saved note n{note['id']} (quote verified in {note['source']}). Cite it as [n{note['id']}].")


def search_notes(ctx: ToolContext, query: str = "", source: str = "", limit: int = 10) -> ToolResult:
    conv = ctx.conversation
    notes = conv.jobs.search_notes(conv.job["id"], query, source or None, limit)
    if not notes:
        return ToolResult("No matching notes." + (" Try fewer or different words." if query else ""))
    return ToolResult("\n".join(f"[n{n['id']}] ({n['source']}{', ' + n['location'] if n['location'] else ''}) "
                                f"{n['claim']} — \"{n['quote'][:300]}\"" for n in notes))


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

UPDATE_CONTEXT = Tool(
    "update_context",
    "Edit the job scratchpad's Context: facts, decisions, file locations, constraints, and dead ends that later tasks "
    "will need (e.g. 'Baseline score is 3.01', 'Degree-5 polynomial overfits; don't retry'). Not for progress; the "
    "task list tracks that. Add short items; remove outdated ones by id (c12). If it's full, merge items.",
    {"type": "object", "properties": {
        "add": {"type": "array", "items": {"type": "string"}, "description": "new items, one fact each"},
        "remove": {"type": "array", "items": {"type": "string"}, "description": "ids of items to remove, e.g. c3"}}},
    update_context, "job")

UPDATE_CHECKLIST = Tool(
    "update_checklist",
    "Write or update your checklist for this task: its sub-steps, each marked done or not. It's saved immediately and "
    "shown to any later attempt (after a retry, pause, or restart), so tick items off as soon as they're done.",
    {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {"text": {"type": "string"}, "done": {"type": "boolean"}},
        "required": ["text", "done"]}}}, "required": ["items"]},
    update_checklist, "job")

ADD_NOTE = Tool(
    "add_note",
    "Save one piece of evidence: a claim from a source, with an exact supporting quote. The quote is checked against "
    "the source text and rejected if it isn't word for word. Returns an id to cite as [n12].",
    {"type": "object", "properties": {
        "claim": {"type": "string", "minLength": 5, "description": "the point, in your words"},
        "quote": {"type": "string", "minLength": 8, "description": "exact words from the source"},
        "source": {"type": "string", "description": "path of the source document"},
        "location": {"type": "string", "description": "section, page, or line, e.g. 'p. 4' or 'Results'"},
        "tags": {"type": "array", "items": {"type": "string"}}},
     "required": ["claim", "quote", "source"]},
    add_note, "job")

SEARCH_NOTES = Tool(
    "search_notes",
    "Search this job's saved notes by keywords, optionally for one source. Returns note ids, claims, and quotes.",
    {"type": "object", "properties": {
        "query": {"type": "string"}, "source": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
    search_notes, "job")

JOB_ASK_USER = Tool(
    "ask_user",
    "Ask the user a question. Only this task waits for the answer; the rest of the job keeps running.",
    {"type": "object", "properties": {"question": {"type": "string", "minLength": 5}}, "required": ["question"]},
    job_ask_user, "job")
