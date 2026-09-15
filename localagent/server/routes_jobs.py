"""HTTP routes for long-running jobs."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..jobs.models import ACTIVE_JOB_STATUSES, AWAITING_APPROVAL, DEFAULT_BUDGET
from ..jobs.planner import lint_plan, normalize_plan
from ..jobs.scratchpad import CONTEXT_CHAR_LIMIT, ITEM_CHAR_LIMIT, context_chars

ALLOWED_PERMISSIONS = {"cmd:network", "cmd:package-install", "cmd:process-control"}


class JobIn(BaseModel):
    project_id: str
    title: str
    goal: str
    budget: dict | None = None             # {max_hours, max_steps, indefinite}
    schedule: str = "now"                  # now | background_hours
    permissions: list[str] = []
    origin_chat_id: str | None = None


class JobPatch(BaseModel):
    title: str | None = None
    goal: str | None = None
    budget: dict | None = None
    schedule: str | None = None
    permissions: list[str] | None = None


class AnswerIn(BaseModel):
    text: str
    task_id: str | None = None


class FeedbackIn(BaseModel):
    feedback: str


class PlanIn(BaseModel):
    tasks: list[dict]


class ContextIn(BaseModel):
    text: str


def _budget(b: dict | None) -> dict:
    budget = {**DEFAULT_BUDGET, **(b or {})}
    budget["indefinite"] = bool(budget.get("indefinite"))
    if not budget["indefinite"]:
        try:
            if budget.get("max_hours") is not None and float(budget["max_hours"]) <= 0:
                raise ValueError
            if budget.get("max_steps") is not None and int(budget["max_steps"]) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(400, "Budget hours and steps must be positive numbers (or choose indefinite).")
    return budget


def register(app: FastAPI, rt) -> None:
    runner, jobs = rt.job_runner, rt.jobs

    def act(fn, *args):
        try:
            return fn(*args)
        except ValueError as e:
            raise HTTPException(400, str(e))

    def job_or_404(job_id: str) -> dict:
        job = jobs.get_job(job_id)
        if not job:
            raise HTTPException(404, "No such job")
        return job

    @app.get("/api/jobs")
    def list_jobs(project_id: str | None = None):
        return jobs.list_jobs(project_id)

    @app.post("/api/jobs")
    def create_job(body: JobIn):
        project = rt.store.get_project(body.project_id)
        if not project:
            raise HTTPException(404, "No such project")
        if not body.title.strip() or not body.goal.strip():
            raise HTTPException(400, "A job needs a title and a goal.")
        if body.schedule not in ("now", "background_hours"):
            raise HTTPException(400, "schedule must be now or background_hours")
        bad = set(body.permissions) - ALLOWED_PERMISSIONS
        if bad:
            raise HTTPException(400, f"Unknown permissions: {', '.join(sorted(bad))}")
        job = jobs.create_job(body.project_id, body.title.strip(), body.goal.strip(), budget=_budget(body.budget),
                              schedule=body.schedule, permissions=body.permissions, origin_chat_id=body.origin_chat_id)
        jobs.journal(job["id"], "created", f"Job created: {job['goal']}")
        return runner._changed(job["id"], wake=True)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = job_or_404(job_id)
        return {"job": job, "tasks": jobs.list_tasks(job_id), "journal": jobs.list_journal(job_id, limit=300),
                "context": jobs.list_context(job_id), "context_limit": CONTEXT_CHAR_LIMIT,
                "runs": jobs.list_runs(job_id),
                "pending_approvals": [a for a in rt.approvals.pending() if a.get("job_id") == job_id]}

    @app.patch("/api/jobs/{job_id}")
    def update_job(job_id: str, body: JobPatch):
        job_or_404(job_id)
        fields = body.model_dump(exclude_unset=True)
        if "budget" in fields:
            fields["budget"] = _budget(fields["budget"])
        if "permissions" in fields and set(fields["permissions"] or []) - ALLOWED_PERMISSIONS:
            raise HTTPException(400, "Unknown permissions")
        jobs.update_job(job_id, **fields)
        return runner._changed(job_id, wake=True)

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        job = job_or_404(job_id)
        if job["status"] in ACTIVE_JOB_STATUSES:
            runner.cancel_job(job_id)
        jobs.delete_job(job_id)
        rt.bus.publish({"type": "job", "job_id": job_id, "project_id": job["project_id"], "deleted": True})
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/runs/{run_id}")
    def run_messages(job_id: str, run_id: str):
        run = jobs.get_run(run_id)
        if not run or run["job_id"] != job_id:
            raise HTTPException(404, "No such run")
        return {"run": run, "messages": jobs.list_run_messages(run_id)}

    @app.post("/api/jobs/{job_id}/approve")
    def approve(job_id: str):
        return act(runner.approve_plan, job_id)

    @app.post("/api/jobs/{job_id}/pause")
    def pause(job_id: str):
        return act(runner.pause, job_id)

    @app.post("/api/jobs/{job_id}/resume")
    def resume(job_id: str):
        return act(runner.resume, job_id)

    @app.post("/api/jobs/{job_id}/stop")
    def stop(job_id: str):
        return act(runner.stop_job, job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return act(runner.cancel_job, job_id)

    @app.post("/api/jobs/{job_id}/replan")
    def replan(job_id: str, body: FeedbackIn):
        return act(runner.request_replan, job_id, body.feedback)

    @app.post("/api/jobs/{job_id}/answer")
    def answer(job_id: str, body: AnswerIn):
        if not body.text.strip():
            raise HTTPException(400, "Empty answer")
        return act(runner.answer, job_id, body.text.strip(), body.task_id)

    @app.put("/api/jobs/{job_id}/plan")
    def replace_plan(job_id: str, body: PlanIn):
        """Write or edit the plan by hand (only before it's approved)."""
        job = job_or_404(job_id)
        if job["status"] not in (AWAITING_APPROVAL, "planning", "failed", "paused"):
            raise HTTPException(400, "The plan can only be replaced before the job runs.")
        plan = normalize_plan(body.tasks)
        errors = lint_plan(plan)
        if errors:
            raise HTTPException(400, " ".join(errors))
        jobs.replace_plan(job_id, plan)
        jobs.update_job(job_id, status=AWAITING_APPROVAL, status_reason="Review and approve the plan")
        jobs.journal(job_id, "plan", "Plan edited by the user.")
        return runner._changed(job_id)

    @app.post("/api/jobs/{job_id}/context")
    def add_context(job_id: str, body: ContextIn):
        job_or_404(job_id)
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Empty note")
        if len(text) > ITEM_CHAR_LIMIT:
            raise HTTPException(400, f"Keep each note under {ITEM_CHAR_LIMIT} characters")
        if context_chars(jobs.list_context(job_id)) + len(text) > CONTEXT_CHAR_LIMIT:
            raise HTTPException(400, "The scratchpad context is full; remove something first")
        item = jobs.add_context(job_id, text, "user")
        jobs.journal(job_id, "context", f"User added to the scratchpad: {text}")
        runner._changed(job_id)
        return item

    @app.delete("/api/jobs/{job_id}/context/{item_id}")
    def remove_context(job_id: str, item_id: int):
        job_or_404(job_id)
        if not jobs.remove_context(job_id, [item_id]):
            raise HTTPException(404, "No such note")
        runner._changed(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/tasks/{task_id}/retry")
    def retry_task(job_id: str, task_id: str):
        return act(runner.retry_task, job_id, task_id)

    @app.post("/api/jobs/{job_id}/tasks/{task_id}/skip")
    def skip_task(job_id: str, task_id: str):
        return act(runner.skip_task, job_id, task_id)

    @app.patch("/api/jobs/{job_id}/tasks/{task_id}")
    def edit_task(job_id: str, task_id: str, fields: dict):
        return act(runner.edit_task, job_id, task_id, fields)
