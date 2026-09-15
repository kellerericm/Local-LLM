"""Prompt text for job planning and task sessions."""
from __future__ import annotations

PLAN_BLOCK = """
# You are planning a long-running job
You don't do the work now. You write the plan that a worker will execute later, one task at a time.

How the plan will be run:
- Only tasks without subtasks are executed. Each runs in a **fresh context** with at most ~15 tool calls, using the
  same tools you see here plus file writing, shell, and Python.
- A task sees the job goal, the plan outline, its own instructions, and the summaries and files of finished tasks.
  It does **not** see other tasks' conversations. So tasks must pass results through files.
- The user approves the plan before anything runs.

Rules for a good plan:
- One deliverable per task. Don't bundle "read all documents" into one task; make one task per document or small group.
- `done_when` must be checkable: name the file that will exist, what it will contain, a number, or a command that will
  succeed. "The file has been read" is not checkable.
- Add `checks` wherever possible: file_exists, file_contains, json_valid, command_ok.
- Use depends_on when a task needs another task's output.
- Include a step to verify the final result, and a step to reconcile conflicting information when there are
  multiple sources.
- Max 3 levels deep, max 8 subtasks per parent, at most 60 executable tasks.

You may look around the workspace first (list_dir, glob, grep, read_file) so the plan names real files.
If the goal is too unclear to plan, use ask_user. Then call propose_plan. If it's rejected, fix the listed
problems and call it again with the whole plan."""

TASK_BLOCK = """
# You are doing one task of a longer job
**Job:** {title}
**Goal:** {goal}

## Plan (▶ = your task)
{outline}

## Your task: [{key}] {task_title}
**Instructions:** {instructions}

**Done when:** {done_when}
{checks}
{guidance}
## How to work on a job task
- Do only this task. Other tasks run separately, in their own sessions.
- Later tasks see only your summary and the files you create, not this conversation. Put results in files in the
  workspace, and mention their paths in your summary.
- Check what already exists before you start: an earlier attempt may have done part of the work.
- When the task is finished and you've verified it, call **complete_task** with a short summary.
- If you can't finish it, call **fail_task** and say why and what would help. That's a useful outcome, not a failure
  of yours.
- If you need information only the user has, call **ask_user**. This task waits; the rest of the job continues."""

NUDGE = ("You replied without calling complete_task or fail_task. If the task is done and verified, call "
         "complete_task with a summary. If you can't finish it, call fail_task. Otherwise, continue working.")


def workspace_listing(workspace, limit: int = 40) -> str:
    """Top level of the workspace, so plans name real paths (the first real-model run guessed a subfolder)."""
    try:
        entries = sorted((p for p in workspace.iterdir() if p.name not in ("jobs", "__pycache__", ".git")),
                         key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return ""
    rows = [f"- {p.name}/" if p.is_dir() else f"- {p.name} ({p.stat().st_size} bytes)" for p in entries[:limit]]
    if len(entries) > limit:
        rows.append(f"- … and {len(entries) - limit} more")
    return "\n".join(rows) or "(empty)"


def plan_request(job: dict, listing: str = "") -> str:
    parts = [f"Plan this job.\n\n**Title:** {job['title']}\n**Goal:** {job['goal']}"]
    if listing:
        parts.append("**The workspace (your current directory) contains, at the top level:**\n" + listing +
                     "\nUse paths relative to the workspace, exactly as listed.")
    budget = job["budget"]
    if budget.get("indefinite"):
        parts.append("**Budget:** no limit. Plan for thoroughness, but keep tasks small.")
    else:
        parts.append(f"**Budget:** about {budget.get('max_hours')} hours and {budget.get('max_steps')} model steps "
                     "in total. Plan accordingly.")
    answers = (job.get("inputs") or {}).get("answers") or []
    if answers:
        parts.append("**The user's answers to your earlier questions:**\n" +
                     "\n".join(f"- Q: {a['question']}\n  A: {a['answer']}" for a in answers))
    return "\n\n".join(parts)


def task_start(task: dict) -> str:
    return f"Start task [{task['key']}]: {task['title']}"
