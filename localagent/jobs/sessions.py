"""Conversations for job runs: planning sessions and task sessions (see coordinator/conversation.py)."""
from __future__ import annotations

import threading

from ..coordinator import prompts as base_prompts
from ..coordinator.conversation import Conversation
from ..tools.registry import Tool, ToolRegistry
from . import prompts
from .checks import describe
from .tools import COMPLETE_TASK, FAIL_TASK, JOB_ASK_USER, PROPOSE_PLAN

READ_ONLY_TOOLS = ("read_file", "list_dir", "glob", "grep")


class JobApprover:
    """Approvals for unattended jobs: keys the user pre-approved at launch pass; others go to the user."""

    def __init__(self, broker, job: dict):
        self.broker = broker
        self.job = job

    def request(self, chat_id, scope, keys, summary, detail, cancel=None, **kw) -> bool:
        if keys and all(k in (self.job.get("permissions") or []) for k in keys):
            return True
        return self.broker.request(chat_id, scope, keys, f"{summary} (job: {self.job['title']})", detail, cancel,
                                   job_id=self.job["id"])

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
        return self.store.get_project(self.job["project_id"])

    def messages(self) -> list[dict]:
        return self.jobs.list_run_messages(self.run["id"])

    def add_message(self, role: str, content: str | None, **kw) -> dict:
        if role == "assistant" and kw.get("kind", "normal") == "normal":
            self.steps += 1
        return self.jobs.add_run_message(self.run["id"], role, content, **kw)

    def gen_overrides(self) -> dict:
        return self.job.get("gen_overrides") or {}

    def approvals(self, default):
        return JobApprover(default, self.job)

    def before_step(self, cancel: threading.Event) -> str | None:
        reason = self.runner.break_point(self, cancel)
        if reason:
            self.interrupt_reason = reason
        return reason


class PlanSession(JobSession):
    max_steps = 20

    def system_prompt(self, ctx) -> str:
        return base_prompts.system_prompt(str(ctx.workspace), str(ctx.env_path), ctx.project) + "\n" + prompts.PLAN_BLOCK

    def tools(self, registry: ToolRegistry, ctx) -> list[Tool]:
        available = {t.name: t for t in registry.available(ctx)}
        return [available[n] for n in READ_ONLY_TOOLS if n in available] + [PROPOSE_PLAN, JOB_ASK_USER]


class TaskSession(JobSession):
    def __init__(self, runner, job: dict, run: dict, task: dict, outline: str, max_steps: int):
        super().__init__(runner, job, run)
        self.task = task
        self.outline = outline
        self.max_steps = max_steps

    def event_fields(self) -> dict:
        return {**super().event_fields(), "task_key": self.task["key"]}

    def system_prompt(self, ctx) -> str:
        t = self.task
        checks = ""
        if t["checks"]:
            checks = "\n**Automatic checks when you call complete_task:**\n" + "\n".join(
                f"- {describe(c)}" for c in t["checks"]) + "\n"
        guidance = ""
        if t["guidance"]:
            guidance = "\n## Notes from earlier attempts and the user\n" + "\n".join(f"- {g}" for g in t["guidance"]) + "\n"
        block = prompts.TASK_BLOCK.format(title=self.job["title"], goal=self.job["goal"], outline=self.outline,
                                          key=t["key"], task_title=t["title"], instructions=t["instructions"] or "-",
                                          done_when=t["done_when"] or "-", checks=checks, guidance=guidance)
        return base_prompts.system_prompt(str(ctx.workspace), str(ctx.env_path), ctx.project) + "\n" + block

    def tools(self, registry: ToolRegistry, ctx) -> list[Tool]:
        # The plan replaces the chat task list; job ask_user replaces the chat one.
        base = [t for t in registry.available(ctx) if t.name not in ("update_tasks", "ask_user")]
        return base + [COMPLETE_TASK, FAIL_TASK, JOB_ASK_USER]
