"""Conversations for job runs: planning sessions and task sessions (see coordinator/conversation.py)."""
from __future__ import annotations

import threading

from ..coordinator import prompts as base_prompts
from ..coordinator.conversation import Conversation
from ..tools.registry import ApprovalPending, Tool, ToolRegistry
from . import prompts
from .checks import describe
from .scratchpad import render_block
from .tools import (ADD_NOTE, CHECK_CITATIONS, COMPLETE_TASK, FAIL_TASK, JOB_ASK_USER, PROPOSE_PLAN, RECORD_REFERENCES,
                    SEARCH_NOTES, UPDATE_CHECKLIST, UPDATE_CONTEXT)

READ_ONLY_TOOLS = ("read_file", "read_document", "list_dir", "glob", "grep")
ATTEMPT_NOTES_SHOWN = 2


def recent_guidance(items: list[str]) -> tuple[list[str], int]:
    """Everything the user said, plus only the latest attempt notes: a long failure history crowds the context and
    describes files that may no longer exist (dry run 8: six notes about an outline that had been moved aside)."""
    attempt = [i for i, g in enumerate(items) if g.startswith(("A previous attempt", "Attempt "))]
    drop = set(attempt[:-ATTEMPT_NOTES_SHOWN])
    return [g for i, g in enumerate(items) if i not in drop], len(drop)


def job_project(store, job: dict) -> dict | None:
    """The project as a job's sessions see it. Templates may point a job at an isolated working copy
    (job.inputs.work_dir), e.g. auto-research experiments that must not touch the original until accepted."""
    project = store.get_project(job["project_id"])
    work_dir = (job.get("inputs") or {}).get("work_dir")
    if project and work_dir:
        project = {**project, "workspace_path": work_dir}
    return project


class JobApprover:
    """Approvals for unattended jobs.

    Keys pre-approved at launch pass, as do keys the user allowed once after an earlier request (consumed on use).
    Anything else becomes a non-blocking request: ApprovalPending parks the task and the runner moves on.
    """

    def __init__(self, broker, runner, job: dict, task_id: str | None):
        self.broker = broker
        self.runner = runner
        self.job = job
        self.task_id = task_id

    def request(self, chat_id, scope, keys, summary, detail, cancel=None, **kw) -> bool:
        jobs = self.runner.jobs
        job = jobs.get_job(self.job["id"]) or self.job
        permissions = set(job.get("permissions") or [])
        inputs = job.get("inputs") or {}
        granted = list(inputs.get("granted_once") or [])
        if keys and all(k in permissions or k in granted for k in keys):
            used = [k for k in keys if k not in permissions]
            if used:
                for k in used:
                    granted.remove(k)
                jobs.update_job(job["id"], inputs={**inputs, "granted_once": granted})
            return True
        label = f"{summary} (job: {job['title']})"
        approval_id = self.broker.request_async(
            chat_id, scope, keys, label, detail, job_id=job["id"],
            on_decision=lambda decision: self.runner.on_approval(job["id"], self.task_id, keys, summary, decision))
        if approval_id is None:
            return True
        raise ApprovalPending(approval_id, summary)

    def pending(self):
        return self.broker.pending()


class JobSession(Conversation):
    def __init__(self, runner, job: dict, run: dict):
        self.runner = runner
        self.jobs = runner.jobs
        self.store = runner.store
        self.job = job
        self.run = run
        self.result: dict | None = None
        self.steps = 0
        self.yield_seconds = 0.0
        self.interrupt_reason: str | None = None

    def event_fields(self) -> dict:
        return {"job_id": self.job["id"], "run_id": self.run["id"]}

    def project(self) -> dict | None:
        return job_project(self.store, self.job)

    def messages(self) -> list[dict]:
        return self.jobs.list_run_messages(self.run["id"])

    def add_message(self, role: str, content: str | None, **kw) -> dict:
        if role == "assistant" and kw.get("kind", "normal") == "normal":
            self.steps += 1
        return self.jobs.add_run_message(self.run["id"], role, content, **kw)

    def gen_overrides(self) -> dict:
        return self.job.get("gen_overrides") or {}

    def approvals(self, default):
        return JobApprover(default, self.runner, self.job, getattr(self, "task", {}).get("id"))

    def before_step(self, cancel: threading.Event) -> str | None:
        reason = self.runner.break_point(self, cancel)
        if reason:
            self.interrupt_reason = reason
        return reason


    def scratchpad(self, ctx, current: dict | None = None) -> str:
        """Rebuilt from the database for every model step, so updates show up immediately."""
        return render_block(self.jobs.list_tasks(self.job["id"]), self.jobs.list_context(self.job["id"]),
                            prompts.workspace_listing(ctx.workspace), current)


class PlanSession(JobSession):
    max_steps = 20

    def system_prompt(self, ctx) -> str:
        return (base_prompts.system_prompt(str(ctx.workspace), str(ctx.env_path), ctx.project) + "\n"
                + prompts.PLAN_BLOCK + "\n\n" + self.scratchpad(ctx))

    def tools(self, registry: ToolRegistry, ctx) -> list[Tool]:
        available = {t.name: t for t in registry.available(ctx)}
        return [available[n] for n in READ_ONLY_TOOLS if n in available] + [SEARCH_NOTES, UPDATE_CONTEXT, PROPOSE_PLAN,
                                                                             JOB_ASK_USER]


class TaskSession(JobSession):
    def __init__(self, runner, job: dict, run: dict, task: dict, max_steps: int):
        super().__init__(runner, job, run)
        self.task = task
        self.max_steps = max_steps

    def event_fields(self) -> dict:
        return {**super().event_fields(), "task_key": self.task["key"]}

    def system_prompt(self, ctx) -> str:
        t = self.jobs.get_task(self.task["id"]) or self.task
        self.task = t
        checks = ""
        if t["checks"]:
            checks = "\n**Automatic checks when you call complete_task:**\n" + "\n".join(
                f"- {describe(c)}" for c in t["checks"]) + "\n"
        guidance = ""
        if t["guidance"]:
            shown, omitted = recent_guidance(t["guidance"])
            guidance = ("\n## Notes from earlier attempts and the user\n" + "\n".join(f"- {g}" for g in shown)
                        + (f"\n- ({omitted} older attempt notes omitted; files may have changed since, so check the "
                           "workspace rather than trusting old descriptions of it.)" if omitted else "") + "\n")
        block = prompts.TASK_BLOCK.format(title=self.job["title"], goal=self.job["goal"],
                                          scratchpad=self.scratchpad(ctx, t),
                                          key=t["key"], task_title=t["title"], instructions=t["instructions"] or "-",
                                          done_when=t["done_when"] or "-", checks=checks, guidance=guidance)
        return base_prompts.system_prompt(str(ctx.workspace), str(ctx.env_path), ctx.project) + "\n" + block

    def tools(self, registry: ToolRegistry, ctx) -> list[Tool]:
        # The plan replaces the chat task list; job ask_user replaces the chat one.
        base = [t for t in registry.available(ctx) if t.name not in ("update_tasks", "ask_user")]
        extra = [RECORD_REFERENCES] if (self.task.get("params") or {}).get("paper") else []
        return base + [UPDATE_CHECKLIST, UPDATE_CONTEXT, ADD_NOTE, SEARCH_NOTES, CHECK_CITATIONS, *extra, COMPLETE_TASK, FAIL_TASK,
                       JOB_ASK_USER]
