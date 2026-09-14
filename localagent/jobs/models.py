"""Persistence for jobs, plan tasks, task runs (with transcripts), and the journal.

Shares the main Store's SQLite connection and lock. Every state change is committed immediately,
which is what makes jobs resumable after a crash or restart.
"""
from __future__ import annotations

import json
import re
import time
import uuid

JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    origin_chat_id TEXT,
    title TEXT NOT NULL,
    slug TEXT NOT NULL,
    goal TEXT NOT NULL,
    template TEXT NOT NULL DEFAULT 'generic',
    inputs TEXT,
    status TEXT NOT NULL,
    status_reason TEXT,
    budget TEXT NOT NULL,
    usage TEXT NOT NULL,
    schedule TEXT NOT NULL DEFAULT 'now',
    permissions TEXT,
    gen_overrides TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS job_tasks(
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    parent_key TEXT,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    done_when TEXT NOT NULL DEFAULT '',
    checks TEXT,
    depends_on TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    result_summary TEXT,
    guidance TEXT,
    question TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(job_id, key)
);
CREATE TABLE IF NOT EXISTS task_runs(
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    task_id TEXT,
    kind TEXT NOT NULL,
    attempt INTEGER,
    status TEXT NOT NULL,
    outcome TEXT,
    summary TEXT,
    steps INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS run_messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'normal',
    content TEXT,
    reasoning TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    name TEXT,
    ok INTEGER,
    usage TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_messages_run ON run_messages(run_id, id);
CREATE TABLE IF NOT EXISTS journal(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    task_key TEXT,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

# Job statuses
PLANNING, AWAITING_APPROVAL, RUNNING, PAUSED, WAITING_USER, DONE, FAILED, CANCELLED = (
    "planning", "awaiting_approval", "running", "paused", "waiting_user", "done", "failed", "cancelled")
ACTIVE_JOB_STATUSES = {PLANNING, RUNNING, WAITING_USER}
TERMINAL_JOB_STATUSES = {DONE, FAILED, CANCELLED}

# Task statuses
T_PENDING, T_RUNNING, T_DONE, T_FAILED, T_SKIPPED, T_WAITING = (
    "pending", "running", "done", "failed", "skipped", "waiting_user")
T_FINISHED = {T_DONE, T_SKIPPED}

DEFAULT_BUDGET = {"max_hours": 4.0, "max_steps": 400, "indefinite": False}


def slugify(text: str, suffix: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "job"
    return f"{base}-{suffix}"


class JobStore:
    def __init__(self, store):
        self.s = store
        with store._lock:
            store._conn.executescript(JOB_SCHEMA)
            store._conn.commit()

    # -- jobs ---------------------------------------------------------------
    @staticmethod
    def _job_out(row: dict | None) -> dict | None:
        if row is None:
            return None
        for k, default in (("inputs", {}), ("budget", dict(DEFAULT_BUDGET)), ("usage", {}), ("permissions", []),
                           ("gen_overrides", {})):
            row[k] = json.loads(row[k]) if row.get(k) else default
        return row

    def create_job(self, project_id: str, title: str, goal: str, *, template: str = "generic",
                   inputs: dict | None = None, budget: dict | None = None, schedule: str = "now",
                   permissions: list[str] | None = None, origin_chat_id: str | None = None) -> dict:
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        self.s._exec(
            "INSERT INTO jobs(id,project_id,origin_chat_id,title,slug,goal,template,inputs,status,budget,usage,schedule,"
            "permissions,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, project_id, origin_chat_id, title, slugify(title, jid[:6]), goal, template, json.dumps(inputs or {}),
             PLANNING, json.dumps({**DEFAULT_BUDGET, **(budget or {})}), json.dumps({"seconds": 0.0, "steps": 0}),
             schedule, json.dumps(permissions or []), now, now))
        return self.get_job(jid)

    def get_job(self, job_id: str) -> dict | None:
        return self._job_out(self.s._one("SELECT * FROM jobs WHERE id=?", (job_id,)))

    def list_jobs(self, project_id: str | None = None) -> list[dict]:
        if project_id:
            rows = self.s._all("SELECT * FROM jobs WHERE project_id=? ORDER BY updated_at DESC", (project_id,))
        else:
            rows = self.s._all("SELECT * FROM jobs ORDER BY updated_at DESC")
        return [self._job_out(r) for r in rows]

    def update_job(self, job_id: str, **fields) -> dict | None:
        json_fields = {"inputs", "budget", "usage", "permissions", "gen_overrides"}
        allowed = json_fields | {"title", "goal", "status", "status_reason", "schedule", "finished_at"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                params.append(json.dumps(v) if k in json_fields else v)
        sets.append("updated_at=?")
        params.append(time.time())
        self.s._exec(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", (*params, job_id))
        return self.get_job(job_id)

    def add_usage(self, job_id: str, seconds: float, steps: int) -> dict:
        job = self.get_job(job_id)
        usage = job["usage"]
        usage["seconds"] = round(usage.get("seconds", 0) + seconds, 1)
        usage["steps"] = usage.get("steps", 0) + steps
        return self.update_job(job_id, usage=usage)

    def delete_job(self, job_id: str) -> None:
        self.s._exec("DELETE FROM jobs WHERE id=?", (job_id,))

    # -- tasks ----------------------------------------------------------------
    @staticmethod
    def _task_out(row: dict | None) -> dict | None:
        if row is None:
            return None
        for k in ("checks", "depends_on", "guidance"):
            row[k] = json.loads(row[k]) if row.get(k) else []
        return row

    def replace_plan(self, job_id: str, tasks: list[dict]) -> list[dict]:
        """Replace all tasks of a job with a validated plan (list of dicts with key/parent_key/...)."""
        now = time.time()
        with self.s._lock:
            self.s._conn.execute("DELETE FROM job_tasks WHERE job_id=?", (job_id,))
            for pos, t in enumerate(tasks):
                self.s._conn.execute(
                    "INSERT INTO job_tasks(id,job_id,key,parent_key,position,title,instructions,done_when,checks,depends_on,"
                    "status,max_attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:12], job_id, t["key"], t.get("parent_key") or None, pos, t["title"],
                     t.get("instructions", ""), t.get("done_when", ""), json.dumps(t.get("checks") or []),
                     json.dumps(t.get("depends_on") or []), T_PENDING, int(t.get("max_attempts", 3)), now, now))
            self.s._conn.commit()
        return self.list_tasks(job_id)

    def list_tasks(self, job_id: str) -> list[dict]:
        return [self._task_out(r) for r in
                self.s._all("SELECT * FROM job_tasks WHERE job_id=? ORDER BY position", (job_id,))]

    def get_task(self, task_id: str) -> dict | None:
        return self._task_out(self.s._one("SELECT * FROM job_tasks WHERE id=?", (task_id,)))

    def update_task(self, task_id: str, **fields) -> dict | None:
        json_fields = {"checks", "depends_on", "guidance"}
        allowed = json_fields | {"title", "instructions", "done_when", "status", "attempts", "max_attempts",
                                 "result_summary", "question"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                params.append(json.dumps(v) if k in json_fields else v)
        sets.append("updated_at=?")
        params.append(time.time())
        self.s._exec(f"UPDATE job_tasks SET {','.join(sets)} WHERE id=?", (*params, task_id))
        return self.get_task(task_id)

    def add_guidance(self, task_id: str, text: str, keep: int = 6) -> dict:
        task = self.get_task(task_id)
        guidance = (task["guidance"] + [text])[-keep:]
        return self.update_task(task_id, guidance=guidance)

    # -- runs -------------------------------------------------------------------
    def create_run(self, job_id: str, kind: str, task_id: str | None = None, attempt: int | None = None) -> dict:
        rid = uuid.uuid4().hex[:12]
        self.s._exec("INSERT INTO task_runs(id,job_id,task_id,kind,attempt,status,started_at) VALUES(?,?,?,?,?,?,?)",
                     (rid, job_id, task_id, kind, attempt, "running", time.time()))
        return self.get_run(rid)

    def get_run(self, run_id: str) -> dict | None:
        return self.s._one("SELECT * FROM task_runs WHERE id=?", (run_id,))

    def finish_run(self, run_id: str, status: str, outcome: str, summary: str | None, steps: int) -> None:
        self.s._exec("UPDATE task_runs SET status=?, outcome=?, summary=?, steps=?, ended_at=? WHERE id=?",
                     (status, outcome, summary, steps, time.time(), run_id))

    def list_runs(self, job_id: str, task_id: str | None = None) -> list[dict]:
        if task_id:
            return self.s._all("SELECT * FROM task_runs WHERE task_id=? ORDER BY started_at", (task_id,))
        return self.s._all("SELECT * FROM task_runs WHERE job_id=? ORDER BY started_at", (job_id,))

    def interrupted_runs(self) -> list[dict]:
        return self.s._all("SELECT * FROM task_runs WHERE status='running'")

    def add_run_message(self, run_id: str, role: str, content: str | None = None, *, kind: str = "normal",
                        reasoning: str | None = None, tool_calls: list[dict] | None = None,
                        tool_call_id: str | None = None, name: str | None = None, ok: bool | None = None,
                        usage: dict | None = None) -> dict:
        cur = self.s._exec(
            "INSERT INTO run_messages(run_id,role,kind,content,reasoning,tool_calls,tool_call_id,name,ok,usage,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, role, kind, content, reasoning, json.dumps(tool_calls) if tool_calls else None, tool_call_id,
             name, None if ok is None else int(ok), json.dumps(usage) if usage else None, time.time()))
        return self.s._message_out(self.s._one("SELECT * FROM run_messages WHERE id=?", (cur.lastrowid,)))

    def list_run_messages(self, run_id: str) -> list[dict]:
        return [self.s._message_out(r) for r in
                self.s._all("SELECT * FROM run_messages WHERE run_id=? ORDER BY id", (run_id,))]

    # -- journal -----------------------------------------------------------------
    def journal(self, job_id: str, kind: str, text: str, task_key: str | None = None) -> dict:
        cur = self.s._exec("INSERT INTO journal(job_id,task_key,kind,text,created_at) VALUES(?,?,?,?,?)",
                           (job_id, task_key, kind, text, time.time()))
        return self.s._one("SELECT * FROM journal WHERE id=?", (cur.lastrowid,))

    def list_journal(self, job_id: str, limit: int | None = None) -> list[dict]:
        if limit:
            rows = self.s._all("SELECT * FROM journal WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit))
            return list(reversed(rows))
        return self.s._all("SELECT * FROM journal WHERE job_id=? ORDER BY id", (job_id,))
