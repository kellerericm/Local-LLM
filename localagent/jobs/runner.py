"""JobRunner: schedules planning and task sessions, one model step at a time, and keeps jobs resumable.

Key behaviors (design §3.4, decisions §14):
- Chats interrupt jobs at break points: before each model step a job session waits while any chat is running.
- Pause takes effect at the next break point; Stop cancels the current step immediately.
- Every tool result is committed, so after a restart interrupted tasks simply go back to pending with a note.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from ..resources import in_hours
from ..safety.paths import PathGuard
from ..tools.registry import ApprovalPending
from . import prompts
from .checks import run_checks
from .mirrors import write_mirrors
from .models import (ACTIVE_JOB_STATUSES, AWAITING_APPROVAL, CANCELLED, DONE, FAILED, PAUSED, PLANNING, RUNNING,
                     T_DONE, T_FAILED, T_FINISHED, T_PENDING, T_RUNNING, T_SKIPPED, T_WAITING, TERMINAL_JOB_STATUSES,
                     WAITING_USER, JobStore)
from .planner import leaves, next_ready_leaf
from .review import ReviewSession, review_request
from .sessions import JobApprover, PlanSession, TaskSession, job_project
from .templates import HandlerResult, get_template

log = logging.getLogger(__name__)

TASK_MAX_STEPS = 30
PLAN_MAX_ATTEMPTS = 3
MAX_NUDGES = 2


class JobRunner:
    def __init__(self, jobs: JobStore, store, coordinator, settings_getter, emit, approvals, chat_runs=None):
        self.jobs = jobs
        self.store = store
        self.coordinator = coordinator
        self.settings_getter = settings_getter
        self.emit = emit
        self.approvals = approvals
        self.chat_runs = chat_runs
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._current: tuple[str, threading.Event] | None = None
        self._pause_requests: set[str] = set()
        self._rr = 0
        self._run_started = 0.0

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="job-runner")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._current:
                self._current[1].set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def wake(self) -> None:
        self._wake.set()

    def recover(self) -> None:
        """Runs left 'running' by a crash or shutdown become interrupted; their tasks go back to pending.
        Approval requests don't survive a restart, so tasks parked on one go back to pending too."""
        for job in self.jobs.list_jobs():
            changed = False
            for task in self.jobs.list_tasks(job["id"]):
                if task["status"] == T_WAITING and task.get("waiting_kind") == "approval":
                    self.jobs.update_task(task["id"], status=T_PENDING, question=None, waiting_kind=None)
                    self.jobs.add_guidance(task["id"], "An approval request was lost when the app restarted. If you "
                                                       "still need that action, it will be asked again.")
                    changed = True
            inputs = job.get("inputs") or {}
            if inputs.get("pending_approval"):
                inputs.pop("pending_approval")
                self.jobs.update_job(job["id"], inputs=inputs, status=PLANNING, status_reason=None)
                changed = True
            elif changed and job["status"] == WAITING_USER:
                self.jobs.update_job(job["id"], status=RUNNING, status_reason=None)
            if changed:
                self._changed(job["id"])
        for run in self.jobs.interrupted_runs():
            msgs = self.jobs.list_run_messages(run["id"])
            steps = sum(1 for m in msgs if m["role"] == "assistant")
            self.jobs.finish_run(run["id"], "interrupted", "interrupted", "The app stopped while this was running.", steps)
            if run["task_id"]:
                task = self.jobs.get_task(run["task_id"])
                if task and task["status"] == T_RUNNING:
                    self.jobs.update_task(task["id"], status=T_PENDING)
                    self.jobs.add_guidance(task["id"], interruption_note(msgs))
                    self.jobs.journal(run["job_id"], "resume", "Interrupted when the app stopped; will resume.",
                                      task["key"])
            self._changed(run["job_id"])

    # -- control (called from the API thread) -------------------------------------
    def approve_plan(self, job_id: str) -> dict:
        self._require(job_id, {AWAITING_APPROVAL})
        if not leaves(self.jobs.list_tasks(job_id)):
            raise ValueError("The plan has no tasks to run.")
        self.jobs.update_job(job_id, status=RUNNING, status_reason=None)
        self.jobs.journal(job_id, "approval", "Plan approved by the user. Starting.")
        return self._changed(job_id, wake=True)

    def request_replan(self, job_id: str, feedback: str) -> dict:
        self._require(job_id, {AWAITING_APPROVAL, PAUSED})
        job = self.jobs.get_job(job_id)
        inputs = job["inputs"]
        inputs.setdefault("answers", []).append({"question": "Feedback on the proposed plan", "answer": feedback})
        self.jobs.update_job(job_id, inputs=inputs, status=PLANNING, status_reason="Re-planning with your feedback")
        self.jobs.journal(job_id, "replan", f"User asked for a new plan: {feedback}")
        return self._changed(job_id, wake=True)

    def pause(self, job_id: str) -> dict:
        self._require(job_id, ACTIVE_JOB_STATUSES)
        with self._lock:
            executing = self._current is not None and self._current[0] == job_id
            if executing:
                self._pause_requests.add(job_id)
        if not executing:
            self.jobs.update_job(job_id, status=PAUSED, status_reason="Paused by you")
            self.jobs.journal(job_id, "control", "Paused by the user.")
        else:
            self.jobs.update_job(job_id, status_reason="Pausing after the current step…")
        return self._changed(job_id)

    def stop_job(self, job_id: str) -> dict:
        self._require(job_id, ACTIVE_JOB_STATUSES | {AWAITING_APPROVAL})
        with self._lock:
            if self._current and self._current[0] == job_id:
                self._current[1].set()
        self.jobs.update_job(job_id, status=PAUSED, status_reason="Stopped by you")
        self.jobs.journal(job_id, "control", "Stopped by the user.")
        return self._changed(job_id)

    def resume(self, job_id: str) -> dict:
        self._require(job_id, {PAUSED, FAILED})
        status = RUNNING if self.jobs.list_tasks(job_id) else PLANNING
        self.jobs.update_job(job_id, status=status, status_reason=None)
        self.jobs.journal(job_id, "control", "Resumed by the user.")
        return self._changed(job_id, wake=True)

    def cancel_job(self, job_id: str) -> dict:
        job = self.jobs.get_job(job_id)
        if not job or job["status"] in TERMINAL_JOB_STATUSES:
            raise ValueError("Job is already finished.")
        with self._lock:
            if self._current and self._current[0] == job_id:
                self._current[1].set()
        self.jobs.update_job(job_id, status=CANCELLED, status_reason="Cancelled by you", finished_at=time.time())
        self.jobs.journal(job_id, "control", "Cancelled by the user.")
        return self._changed(job_id)

    def answer(self, job_id: str, text: str, task_id: str | None = None) -> dict:
        job = self.jobs.get_job(job_id)
        if not job:
            raise ValueError("No such job.")
        if task_id:
            task = self.jobs.get_task(task_id)
            if not task or task["status"] != T_WAITING:
                raise ValueError("That task isn't waiting for an answer.")
            if task.get("waiting_kind") == "approval":
                raise ValueError("That task is waiting for an approval; decide in the approval dialog.")
            if task.get("waiting_kind") == "gate":
                return self._decide_gate(job, task, text)
            self.jobs.add_guidance(task_id, f"You asked the user: \"{task['question']}\" They answered: \"{text}\"")
            self.jobs.update_task(task_id, status=T_PENDING, question=None, waiting_kind=None)
            self.jobs.journal(job_id, "answer", f"User answered: {text}", task["key"])
            if job["status"] == WAITING_USER:
                self.jobs.update_job(job_id, status=RUNNING, status_reason=None)
        else:
            question = (job["inputs"] or {}).get("pending_question")
            if not question:
                raise ValueError("The job isn't waiting for an answer.")
            inputs = job["inputs"]
            inputs.setdefault("answers", []).append({"question": question, "answer": text})
            inputs.pop("pending_question", None)
            self.jobs.update_job(job_id, inputs=inputs, status=PLANNING, status_reason=None)
            self.jobs.journal(job_id, "answer", f"User answered the planning question: {text}")
        return self._changed(job_id, wake=True)

    def _decide_gate(self, job: dict, task: dict, text: str) -> dict:
        decision = get_template(job["template"]).on_gate(self, job, task, text)
        if decision == "approve":
            self.jobs.update_task(task["id"], status=T_DONE, question=None, waiting_kind=None,
                                  result_summary=f"Approved by the user: {text}"[:400])
            self.jobs.journal(job["id"], "gate", f"Approved: {text}", task["key"])
            self._task_done_hook(job, self.jobs.get_task(task["id"]))
        else:
            self.jobs.update_task(task["id"], status=T_PENDING, question=None, waiting_kind=None)
            self.jobs.journal(job["id"], "gate", f"Changes requested: {text}", task["key"])
        if self.jobs.get_job(job["id"])["status"] == WAITING_USER:
            self.jobs.update_job(job["id"], status=RUNNING, status_reason=None)
        return self._changed(job["id"], wake=True)

    def on_approval(self, job_id: str, task_id: str | None, keys: list[str], summary: str, decision: str) -> None:
        """Called when the user decides on a job's approval request (from the API thread)."""
        job = self.jobs.get_job(job_id)
        if not job or job["status"] in TERMINAL_JOB_STATUSES:
            return
        inputs = job["inputs"] or {}
        allowed = decision in ("once", "always")
        if allowed and decision == "once":
            inputs["granted_once"] = list(inputs.get("granted_once") or []) + list(keys)
        note = (f"The user approved: {summary}. You may do it now." if allowed else
                f"The user denied: {summary}. Don't try it again; find another way, or call fail_task and explain.")
        if task_id:
            task = self.jobs.get_task(task_id)
            if task and task["status"] == T_WAITING and task.get("waiting_kind") == "approval":
                self.jobs.add_guidance(task_id, note)
                self.jobs.update_task(task_id, status=T_PENDING, question=None, waiting_kind=None)
            status = RUNNING if job["status"] == WAITING_USER else job["status"]
            self.jobs.update_job(job_id, inputs=inputs, status=status,
                                 status_reason=None if status == RUNNING else job.get("status_reason"))
            self.jobs.journal(job_id, "approval", f"{'Approved' if allowed else 'Denied'}: {summary}",
                              task["key"] if task else None)
        else:
            inputs.pop("pending_approval", None)
            inputs.setdefault("answers", []).append({"question": f"May the planner {summary}?",
                                                     "answer": "Yes" if allowed else "No, don't do that."})
            status = PLANNING if job["status"] == WAITING_USER else job["status"]
            self.jobs.update_job(job_id, inputs=inputs, status=status, status_reason=None)
            self.jobs.journal(job_id, "approval", f"{'Approved' if allowed else 'Denied'} for planning: {summary}")
        self._changed(job_id, wake=True)

    def retry_task(self, job_id: str, task_id: str) -> dict:
        task = self._require_task(job_id, task_id, {T_FAILED, T_SKIPPED, T_DONE})
        self.jobs.update_task(task_id, status=T_PENDING, attempts=0)
        self.jobs.journal(job_id, "control", "User asked to retry this task.", task["key"])
        self._unblock(job_id)
        return self._changed(job_id, wake=True)

    def skip_task(self, job_id: str, task_id: str) -> dict:
        task = self._require_task(job_id, task_id, {T_PENDING, T_FAILED, T_WAITING})
        self.jobs.update_task(task_id, status=T_SKIPPED, question=None)
        self.jobs.journal(job_id, "control", "User skipped this task.", task["key"])
        self._unblock(job_id)
        return self._changed(job_id, wake=True)

    def edit_task(self, job_id: str, task_id: str, fields: dict) -> dict:
        self._require_task(job_id, task_id, {T_PENDING, T_FAILED})
        allowed = {k: v for k, v in fields.items() if k in ("title", "instructions", "done_when", "checks", "max_attempts")}
        self.jobs.update_task(task_id, **allowed)
        return self._changed(job_id)

    # -- break points (called from the job session's thread) ---------------------------
    def break_point(self, session, cancel: threading.Event) -> str | None:
        job_id = session.job["id"]
        with self._lock:
            if job_id in self._pause_requests:
                return "paused"
        job = self.jobs.get_job(job_id)
        if not job or job["status"] not in (RUNNING, PLANNING):
            return "stopped"
        if self._budget_exceeded(job, session):
            return "budget"
        # Chats come first: wait until no chat is running (decision 5).
        started = None
        while self.chat_runs is not None and self.chat_runs.running() and not cancel.is_set():
            if started is None:
                started = time.time()
                self.emit({"type": "job_activity", "job_id": job_id, "activity": "Waiting for a chat to finish…"})
            with self._lock:
                if job_id in self._pause_requests:
                    break
            time.sleep(0.25)
        if started is not None:
            session.yield_seconds += time.time() - started
            self.emit({"type": "job_activity", "job_id": job_id, "activity": ""})
        with self._lock:
            if job_id in self._pause_requests:
                return "paused"
        return None

    # -- scheduling loop -----------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self._tick()
            except Exception:
                log.exception("job runner tick failed")
                worked = False
            if not worked:
                self._wake.wait(2.0)
                self._wake.clear()

    def _tick(self) -> bool:
        active = [j for j in self.jobs.list_jobs() if j["status"] in (PLANNING, RUNNING)]
        active.sort(key=lambda j: j["created_at"])
        if not active:
            return False
        settings = self.settings_getter()
        n = len(active)
        for i in range(n):
            job = active[(self._rr + i) % n]
            if self._stop.is_set():
                return False
            if job["schedule"] == "background_hours" and not in_hours(settings.resources.background_hours):
                continue
            if job["status"] == PLANNING:
                self._rr = (self._rr + i + 1) % n
                template = get_template(job["template"])
                try:
                    plan = template.initial_plan(self, job)
                except Exception as e:
                    log.exception("template planning failed")
                    self.jobs.update_job(job["id"], status=FAILED, status_reason=f"Couldn't set up the job: {e}")
                    self.jobs.journal(job["id"], "failed", f"Template setup failed: {e}")
                    self._changed(job["id"])
                    return True
                if plan is None:
                    self._plan(job)
                else:
                    self.jobs.replace_plan(job["id"], plan)
                    self.jobs.update_job(job["id"], status=AWAITING_APPROVAL, status_reason="Review and approve the plan")
                    self.jobs.journal(job["id"], "plan", f"Plan prepared by the {template.label} template "
                                                         f"({len(leaves(self.jobs.list_tasks(job['id'])))} tasks).")
                    self._changed(job["id"])
                return True
            tasks = self.jobs.list_tasks(job["id"])
            if self._budget_exceeded(job):
                self._pause_for_budget(job["id"])
                continue
            leaf = next_ready_leaf(tasks)
            if leaf:
                self._rr = (self._rr + i + 1) % n
                self._run_task(job, leaf, tasks)
                return True
            self._settle(job, tasks)
        return False

    def _settle(self, job: dict, tasks: list[dict]) -> None:
        """No task is ready: the job is done, blocked on a failure, or waiting on the user."""
        leaf = leaves(tasks)
        if leaf and all(t["status"] in T_FINISHED for t in leaf):
            done = sum(1 for t in leaf if t["status"] == T_DONE)
            self.jobs.update_job(job["id"], status=DONE, status_reason=f"{done} of {len(leaf)} tasks done",
                                 finished_at=time.time())
            self.jobs.journal(job["id"], "done", f"Job finished: {done} tasks done, {len(leaf) - done} skipped.")
            self._changed(job["id"])
        elif any(t["status"] == T_FAILED for t in leaf):
            failed = [t for t in leaf if t["status"] == T_FAILED]
            names = ", ".join(f"[{t['key']}] {t['title']}" for t in failed[:3])
            self.jobs.update_job(job["id"], status=PAUSED,
                                 status_reason=f"Needs your decision: {names} failed. Retry, skip, or edit it.")
            self.jobs.journal(job["id"], "blocked", f"Paused: {names} failed after all attempts.")
            self._changed(job["id"])
        elif any(t["status"] == T_WAITING for t in leaf):
            waiting = [t for t in leaf if t["status"] == T_WAITING]
            kinds = {t.get("waiting_kind") or "question" for t in waiting}
            reason = ("Waiting for your approval" if kinds == {"approval"} else
                      "Waiting for your answer" if kinds == {"question"} else "Waiting for your answers and approvals")
            if job["status"] != WAITING_USER or job.get("status_reason") != reason:
                self.jobs.update_job(job["id"], status=WAITING_USER, status_reason=reason)
                self._changed(job["id"])

    def _unblock(self, job_id: str) -> None:
        """After retry/skip, a job that was only blocked on its tasks can continue."""
        job = self.jobs.get_job(job_id)
        blocked_on_tasks = job["status"] == PAUSED and (job.get("status_reason") or "").startswith("Needs your decision")
        if job["status"] in (WAITING_USER, DONE) or blocked_on_tasks:
            self.jobs.update_job(job_id, status=RUNNING, status_reason=None, finished_at=None)

    def _budget_exceeded(self, job: dict, session=None) -> bool:
        budget = job["budget"]
        if budget.get("indefinite"):
            return False
        usage = job["usage"]
        seconds = usage.get("seconds", 0)
        steps = usage.get("steps", 0)
        if session is not None:
            seconds += time.time() - self._run_started - session.yield_seconds
            steps += session.steps
        if budget.get("max_hours") and seconds >= float(budget["max_hours"]) * 3600:
            return True
        return bool(budget.get("max_steps")) and steps >= int(budget["max_steps"])

    def _pause_for_budget(self, job_id: str) -> None:
        job = self.jobs.get_job(job_id)
        if job["status"] == PAUSED:
            return
        usage = job["usage"]
        self.jobs.update_job(job_id, status=PAUSED, status_reason=(
            f"Budget reached ({usage.get('seconds', 0) / 3600:.1f} h, {usage.get('steps', 0)} steps). "
            "Raise the budget or set it to indefinite, then resume."))
        self.jobs.journal(job_id, "budget", "Paused: budget reached.")
        self._changed(job_id)

    # -- sessions --------------------------------------------------------------------------
    def _execute(self, session, first_message: str) -> str:
        job_id = session.job["id"]
        cancel = threading.Event()
        with self._lock:
            self._current = (job_id, cancel)
        self._run_started = time.time()
        self._changed(job_id)
        try:
            outcome = self.coordinator.run(session, first_message, cancel, user_kind="coordinator")
            nudges = 0
            while outcome == "done" and session.result is None and nudges < MAX_NUDGES and not cancel.is_set():
                nudges += 1
                outcome = self.coordinator.run(session, prompts.NUDGE, cancel, user_kind="coordinator")
        finally:
            with self._lock:
                self._current = None
            elapsed = max(0.0, time.time() - self._run_started - session.yield_seconds)
            self.jobs.add_usage(job_id, elapsed, session.steps)
        return outcome

    def _after_interrupt(self, job_id: str, reason: str | None) -> None:
        with self._lock:
            paused = job_id in self._pause_requests
            self._pause_requests.discard(job_id)
        if paused or reason == "paused":
            self.jobs.update_job(job_id, status=PAUSED, status_reason="Paused by you")
            self.jobs.journal(job_id, "control", "Paused by the user.")
        elif reason == "budget":
            self._pause_for_budget(job_id)

    def _plan(self, job: dict) -> None:
        attempt = sum(1 for r in self.jobs.list_runs(job["id"]) if r["kind"] == "plan" and r["outcome"] != "interrupted") + 1
        if attempt > PLAN_MAX_ATTEMPTS:
            self.jobs.update_job(job["id"], status=FAILED, status_reason=(
                f"Couldn't produce a valid plan in {PLAN_MAX_ATTEMPTS} attempts. Clarify the goal and resume."))
            self.jobs.journal(job["id"], "failed", "Planning failed repeatedly.")
            self._changed(job["id"])
            return
        run = self.jobs.create_run(job["id"], "plan", attempt=attempt)
        self.jobs.journal(job["id"], "plan", f"Planning (attempt {attempt}).")
        session = PlanSession(self, job, run)
        project = self.store.get_project(job["project_id"])
        listing = prompts.workspace_listing(Path(project["workspace_path"])) if project else ""
        outcome = self._execute(session, prompts.plan_request(job, listing))
        res = session.result
        if res and res["kind"] == "plan":
            tasks = self.jobs.replace_plan(job["id"], res["tasks"])
            self.jobs.finish_run(run["id"], "done", "plan", f"{len(tasks)} tasks", session.steps)
            current = self.jobs.get_job(job["id"])
            if current["status"] == PLANNING:
                self.jobs.update_job(job["id"], status=AWAITING_APPROVAL, status_reason="Review and approve the plan")
            self.jobs.journal(job["id"], "plan", f"Proposed a plan with {len(leaves(tasks))} tasks; waiting for approval.")
        elif res and res["kind"] == "ask":
            inputs = self.jobs.get_job(job["id"])["inputs"]
            inputs["pending_question"] = res["question"]
            self.jobs.update_job(job["id"], inputs=inputs, status=WAITING_USER, status_reason="Question about the goal")
            self.jobs.finish_run(run["id"], "done", "ask", res["question"], session.steps)
            self.jobs.journal(job["id"], "question", f"Asked: {res['question']}")
        elif res and res["kind"] == "approval":
            inputs = self.jobs.get_job(job["id"])["inputs"]
            inputs["pending_approval"] = res["summary"]
            self.jobs.update_job(job["id"], inputs=inputs, status=WAITING_USER,
                                 status_reason=f"Waiting for your approval: {res['summary']}")
            self.jobs.finish_run(run["id"], "done", "waiting_approval", res["summary"], session.steps)
            self.jobs.journal(job["id"], "approval", f"Planning needs approval: {res['summary']}")
        elif outcome in ("interrupted", "cancelled"):
            self.jobs.finish_run(run["id"], "interrupted", "interrupted", session.interrupt_reason, session.steps)
            self._after_interrupt(job["id"], session.interrupt_reason)
        else:
            self.jobs.finish_run(run["id"], "failed", outcome, self._last_text(run["id"]), session.steps)
            self.jobs.journal(job["id"], "plan", f"Planning attempt {attempt} didn't produce a valid plan ({outcome}).")
        self._changed(job["id"])

    def _run_task(self, job: dict, task: dict, tasks: list[dict]) -> None:
        if task["kind"] == "code":
            return self._run_code_task(job, task)
        if task["kind"] == "gate":
            prompt = task["params"].get("prompt") or task["title"]
            self.jobs.update_task(task["id"], status=T_WAITING, waiting_kind="gate", question=prompt)
            self.jobs.journal(job["id"], "gate", f"Waiting for your review: {prompt}", task["key"])
            self._changed(job["id"])
            return
        attempt = task["attempts"] + 1
        self.jobs.update_task(task["id"], status=T_RUNNING)
        run = self.jobs.create_run(job["id"], "task", task["id"], attempt)
        self.jobs.journal(job["id"], "start", f"Started: {task['title']} (attempt {attempt})", task["key"])
        remaining = None if job["budget"].get("indefinite") else max(
            1, int(job["budget"].get("max_steps") or 10**6) - job["usage"].get("steps", 0))
        # +1 so an exhausted budget is caught at a break point (pause) rather than as a step-limit failure.
        max_steps = min(TASK_MAX_STEPS, remaining + 1) if remaining else TASK_MAX_STEPS
        session = TaskSession(self, job, run, task, max_steps)
        outcome = self._execute(session, prompts.task_start(task))
        res = session.result
        task = self.jobs.get_task(task["id"])

        if res and res["kind"] == "complete":
            try:
                results = self._check(job, task, res["summary"])
            except ApprovalPending as e:
                res = {"kind": "approval", "approval_id": e.approval_id, "summary": f"{e.summary} (a task check)"}
                self.jobs.add_guidance(task["id"], f"You called complete_task with this summary: {session.result['summary']} "
                                                   "Its checks need approval to run; once approved, verify and call "
                                                   "complete_task again.")
                results = None
        if res and res["kind"] == "complete":
            failed = [r for r in results if not r.ok]
            review = None
            if not failed and task["review"]:
                review = self._review(job, task, res["summary"], run)
            if not failed and review is not None and not review["passed"]:
                self._attempt_failed(job, task, run, session, "review_rejected",
                                     "A reviewer rejected the previous attempt:\n" + review["issues_text"])
            elif not failed:
                self.jobs.update_task(task["id"], status=T_DONE, result_summary=res["summary"][:1200])
                self.jobs.finish_run(run["id"], "done", "complete", res["summary"], session.steps)
                passed = f" ({len(results)} checks passed)" if results else ""
                reviewed = " (reviewed)" if review else ""
                self.jobs.journal(job["id"], "done", f"Done{passed}{reviewed}: {res['summary'][:200]}", task["key"])
                self._task_done_hook(job, self.jobs.get_task(task["id"]))
            else:
                lines = "\n".join(r.line() for r in failed)
                self._attempt_failed(job, task, run, session, "checks_failed",
                                     f"A previous attempt called complete_task, but these checks failed:\n{lines}")
        elif res and res["kind"] == "fail":
            help_ = f" What would help: {res['what_would_help']}" if res.get("what_would_help") else ""
            self._attempt_failed(job, task, run, session, "gave_up",
                                 f"A previous attempt gave up: {res['reason']}.{help_}")
        elif res and res["kind"] == "ask":
            self.jobs.update_task(task["id"], status=T_WAITING, question=res["question"], waiting_kind="question")
            self.jobs.finish_run(run["id"], "done", "ask", res["question"], session.steps)
            self.jobs.journal(job["id"], "question", f"Asked the user: {res['question']}", task["key"])
        elif res and res["kind"] == "approval":
            self.jobs.update_task(task["id"], status=T_WAITING, question=f"Approval needed: {res['summary']}",
                                  waiting_kind="approval")
            self.jobs.add_guidance(task["id"], interruption_note(self.jobs.list_run_messages(run["id"])))
            self.jobs.finish_run(run["id"], "done", "waiting_approval", res["summary"], session.steps)
            self.jobs.journal(job["id"], "approval", f"Waiting for approval: {res['summary']}", task["key"])
        elif outcome in ("interrupted", "cancelled"):
            self.jobs.update_task(task["id"], status=T_PENDING)
            self.jobs.add_guidance(task["id"], interruption_note(self.jobs.list_run_messages(run["id"])))
            self.jobs.finish_run(run["id"], "interrupted", "interrupted", session.interrupt_reason or "stopped",
                                 session.steps)
            self.jobs.journal(job["id"], "interrupt", f"Interrupted ({session.interrupt_reason or 'stopped'}); "
                                                      "will resume from its files.", task["key"])
            self._after_interrupt(job["id"], session.interrupt_reason)
        else:
            last = self._last_text(run["id"])
            self._attempt_failed(job, task, run, session, outcome,
                                 f"A previous attempt ended without finishing ({outcome}). Its last message: {last[:600]}")
        self._changed(job["id"])

    def _check(self, job: dict, task: dict, summary: str = ""):
        if not task["checks"]:
            return []
        settings = self.settings_getter()
        project = job_project(self.store, self.jobs.get_job(job["id"]) or job)
        workspace = Path(project["workspace_path"])
        env_path = Path(project["env_path"] or settings.env_path)
        guard = PathGuard(workspace, env_path)
        approver = JobApprover(self.approvals, self, job, task["id"])
        ask = lambda keys, summary_, detail: approver.request(None, project["id"], keys, summary_, detail)  # noqa: E731
        return run_checks(task["checks"], workspace, env_path, guard, self.coordinator.policy, ask,
                          notes=lambda: self.jobs.list_notes(job["id"]), summary=summary,
                          paper=lambda key: self.jobs.get_paper(job["id"], key))

    # -- templates: code tasks, gates, reviews, plan growth ---------------------------------------
    def _task_done_hook(self, job: dict, task: dict) -> None:
        try:
            get_template(job["template"]).on_task_done(self, self.jobs.get_job(job["id"]), task)
        except Exception as e:
            log.exception("template on_task_done failed")
            self.jobs.journal(job["id"], "error", f"Template couldn't extend the plan after this task: {e}", task["key"])

    def _run_code_task(self, job: dict, task: dict) -> None:
        template = get_template(job["template"])
        handler = template.handlers.get(task["handler"] or "")
        self.jobs.update_task(task["id"], status=T_RUNNING)
        run = self.jobs.create_run(job["id"], "code", task["id"], task["attempts"] + 1)
        self.jobs.journal(job["id"], "start", f"Started: {task['title']}", task["key"])
        self._changed(job["id"])
        started = time.time()
        try:
            if handler is None:
                raise RuntimeError(f"Unknown handler {task['handler']!r} for template {template.name}")
            result = handler(self, self.jobs.get_job(job["id"]), task)
        except ApprovalPending as e:
            self.jobs.update_task(task["id"], status=T_WAITING, question=f"Approval needed: {e.summary}",
                                  waiting_kind="approval")
            self.jobs.finish_run(run["id"], "done", "waiting_approval", e.summary, 0)
            self.jobs.journal(job["id"], "approval", f"Waiting for approval: {e.summary}", task["key"])
            self._changed(job["id"])
            return
        except Exception as e:
            log.exception("code task %s failed", task["key"])
            result = HandlerResult(False, f"{type(e).__name__}: {e}", retry_guidance=str(e))
        finally:
            self.jobs.add_usage(job["id"], time.time() - started, 0)
        if result.wait_question:
            self.jobs.update_task(task["id"], status=T_WAITING, question=result.wait_question, waiting_kind="question")
            self.jobs.finish_run(run["id"], "done", "ask", result.wait_question, 0)
            self.jobs.journal(job["id"], "question", result.wait_question, task["key"])
        elif result.ok:
            self.jobs.update_task(task["id"], status=T_DONE, result_summary=result.summary[:1200])
            self.jobs.finish_run(run["id"], "done", "complete", result.summary, 0)
            self.jobs.journal(job["id"], "done", f"Done: {result.summary[:200]}", task["key"])
            self._task_done_hook(job, self.jobs.get_task(task["id"]))
        else:
            attempts = task["attempts"] + 1
            if result.retry_guidance:
                self.jobs.add_guidance(task["id"], result.retry_guidance)
            status = T_FAILED if attempts >= task["max_attempts"] else T_PENDING
            self.jobs.update_task(task["id"], status=status, attempts=attempts)
            self.jobs.finish_run(run["id"], "failed", "error", result.summary, 0)
            self.jobs.journal(job["id"], "failed" if status == T_FAILED else "retry", result.summary[:300], task["key"])
        self._changed(job["id"])

    def _review(self, job: dict, task: dict, summary: str, worker_run: dict) -> dict:
        new_context = [i for i in self.jobs.list_context(job["id"])
                       if i["task_key"] == task["key"] and i["created_at"] >= worker_run["started_at"]]
        run = self.jobs.create_run(job["id"], "review", task["id"], task["attempts"] + 1)
        session = ReviewSession(self, job, run, task)
        self.jobs.journal(job["id"], "review", "Reviewing the result.", task["key"])
        outcome = self._execute(session, review_request(job, task, summary, new_context))
        res = session.result
        if not res or res.get("kind") != "review":
            self.jobs.finish_run(run["id"], "failed", outcome, "No verdict", session.steps)
            self.jobs.journal(job["id"], "review", f"Review was inconclusive ({outcome}); accepting the result.",
                              task["key"])
            return {"passed": True, "issues_text": ""}
        bad_ids = []
        for raw in res.get("bad_context_ids") or []:
            try:
                bad_ids.append(int(str(raw).strip().lstrip("cC")))
            except ValueError:
                continue
        allowed = {i["id"] for i in new_context}
        removed = [i for i in bad_ids if i in allowed]
        if removed:
            self.jobs.remove_context(job["id"], removed)
            self.jobs.journal(job["id"], "review", f"Reviewer removed unsupported context items: "
                                                   f"{', '.join(f'c{i}' for i in removed)}", task["key"])
        issues = res.get("issues") or []
        issues_text = "\n".join(f"- {i.get('problem', '')}" + (f" (evidence: {i['evidence']})" if i.get("evidence") else "")
                                for i in issues) or "- (no details given)"
        passed = res.get("verdict") == "pass"
        self.jobs.finish_run(run["id"], "done", "pass" if passed else "fail", issues_text, session.steps)
        self.jobs.journal(job["id"], "review", ("Review passed." if passed else f"Review failed:\n{issues_text}")[:400],
                          task["key"])
        return {"passed": passed, "issues_text": issues_text}

    def _attempt_failed(self, job, task, run, session, outcome: str, guidance: str) -> None:
        attempts = task["attempts"] + 1
        self.jobs.add_guidance(task["id"], guidance)
        self.jobs.finish_run(run["id"], "failed", outcome, guidance, session.steps)
        if attempts >= task["max_attempts"]:
            self.jobs.update_task(task["id"], status=T_FAILED, attempts=attempts)
            self.jobs.journal(job["id"], "failed", f"Failed after {attempts} attempts. Last: {guidance[:300]}", task["key"])
        else:
            self.jobs.update_task(task["id"], status=T_PENDING, attempts=attempts)
            self.jobs.journal(job["id"], "retry", f"Attempt {attempts} didn't finish; will retry. {guidance[:300]}",
                              task["key"])

    # -- helpers -------------------------------------------------------------------------------
    def _last_text(self, run_id: str) -> str:
        for m in reversed(self.jobs.list_run_messages(run_id)):
            if m["role"] == "assistant" and m["content"]:
                return m["content"]
        return "(no message)"

    def _require(self, job_id: str, statuses: set[str]) -> dict:
        job = self.jobs.get_job(job_id)
        if not job:
            raise ValueError("No such job.")
        if job["status"] not in statuses:
            raise ValueError(f"Can't do that while the job is {job['status']}.")
        return job

    def _require_task(self, job_id: str, task_id: str, statuses: set[str]) -> dict:
        task = self.jobs.get_task(task_id)
        if not task or task["job_id"] != job_id:
            raise ValueError("No such task.")
        if task["status"] not in statuses:
            raise ValueError(f"Can't do that while the task is {task['status']}.")
        return task

    def _changed(self, job_id: str, wake: bool = False) -> dict:
        write_mirrors(self.jobs, self.store, job_id)
        job = self.jobs.get_job(job_id)
        if job:
            self.emit({"type": "job", "job_id": job_id, "project_id": job["project_id"], "status": job["status"],
                       "status_reason": job.get("status_reason")})
        if wake:
            self.wake()
        return job


def interruption_note(messages: list[dict]) -> str:
    """Tell the next attempt what the interrupted one already did."""
    done = []
    results = {m["tool_call_id"]: m for m in messages if m["role"] == "tool"}
    for m in messages:
        for c in m.get("tool_calls") or []:
            r = results.get(c["id"])
            arg = next((str(v) for k, v in c["arguments"].items() if k in ("path", "command", "pattern")), "")
            status = "ok" if r and r["ok"] else "failed" if r else "no result"
            done.append(f"{c['name']}({arg[:80]}) → {status}")
    if not done:
        return "A previous attempt was interrupted before it did anything."
    return ("A previous attempt was interrupted (stopped, paused, or the app restarted) after these actions: "
            + "; ".join(done[-12:]) + ". Check what already exists before redoing work.")
