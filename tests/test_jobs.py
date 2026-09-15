import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from localagent.backend.fake import EchoBackend, ScriptedBackend
from localagent.coordinator import Coordinator
from localagent.jobs.checks import run_checks
from localagent.jobs.models import JobStore
from localagent.jobs.planner import lint_plan, next_ready_leaf, normalize_plan
from localagent.jobs.runner import JobRunner
from localagent.safety import AutoApprover, CommandPolicy, PathGuard
from localagent.server import create_app
from localagent.tools import default_registry


def call(tool_name, **args):
    return f'<tool_call>{json.dumps({"name": tool_name, "arguments": args})}</tool_call>'


GOOD_PLAN = [
    {"id": "t1", "title": "Write data file", "instructions": "Create data.txt with the numbers 1 to 3.",
     "done_when": "data.txt exists and contains 3 lines", "checks": [{"type": "file_exists", "path": "data.txt"}]},
    {"id": "t2", "title": "Summarize", "instructions": "Write summary.md describing data.txt.",
     "done_when": "summary.md exists with a one-line summary", "depends_on": ["t1"],
     "checks": [{"type": "file_contains", "path": "summary.md", "text": "numbers"}]},
]


class FakeChats:
    def __init__(self, busy_calls=0):
        self.busy_calls = busy_calls

    def running(self):
        if self.busy_calls > 0:
            self.busy_calls -= 1
            return ["chat1"]
        return []


class Env:
    def __init__(self, store, settings, workspace, responses, chats=None, approver=None):
        settings.max_consecutive_failures = 3
        self.store, self.settings, self.workspace = store, settings, workspace
        self.jobs = JobStore(store)
        self.backend = ScriptedBackend(responses)
        self.events = []
        self.approver = approver or AutoApprover(allow=False)
        self.coord = Coordinator(self.backend, store, default_registry(), self.approver, lambda: settings,
                                 self.events.append)
        self.runner = JobRunner(self.jobs, store, self.coord, lambda: settings, self.events.append, self.approver,
                                chat_runs=chats or FakeChats())
        self.project = store.create_project("P", str(workspace))

    def job(self, **kw):
        return self.jobs.create_job(self.project["id"], kw.pop("title", "Test job"), kw.pop("goal", "Make files"), **kw)

    def plan(self, job_id, plan=GOOD_PLAN, approve=True):
        self.jobs.replace_plan(job_id, normalize_plan(plan))
        self.jobs.update_job(job_id, status="awaiting_approval")
        if approve:
            self.runner.approve_plan(job_id)

    def task(self, job_id, key):
        return next(t for t in self.jobs.list_tasks(job_id) if t["key"] == key)


@pytest.fixture
def env_factory(store, settings, workspace):
    return lambda responses, **kw: Env(store, settings, workspace, responses, **kw)


# ---------------------------------------------------------------- planner
def test_lint_accepts_good_plan_and_rejects_vague_or_broken_ones(workspace):
    guard, policy = PathGuard(workspace), CommandPolicy()
    assert lint_plan(normalize_plan(GOOD_PLAN), policy, guard) == []

    vague = normalize_plan([{"id": "a", "title": "Read docs", "instructions": "Read them",
                             "done_when": "The documents have been read"}])
    assert any("can't be checked" in e for e in lint_plan(vague))

    broken = normalize_plan([
        {"id": "a", "title": "A", "instructions": "x", "done_when": "a.txt exists here", "depends_on": ["b"]},
        {"id": "b", "title": "B", "instructions": "x", "done_when": "b.txt exists here", "depends_on": ["a"],
         "checks": [{"type": "teleport"}]},
        {"id": "c", "title": "C", "instructions": "x", "done_when": "c.txt exists here", "parent_id": "nope"},
    ])
    errors = " ".join(lint_plan(broken))
    assert "unknown type" in errors and "parent_id 'nope'" in errors

    cycle = normalize_plan([
        {"id": "a", "title": "A", "instructions": "x", "done_when": "a.txt exists here", "depends_on": ["b"]},
        {"id": "b", "title": "B", "instructions": "x", "done_when": "b.txt exists here", "depends_on": ["a"]}])
    assert any("cycle" in e for e in lint_plan(cycle))

    blocked = normalize_plan([{"id": "a", "title": "A", "instructions": "x", "done_when": "command succeeds ok",
                               "checks": [{"type": "command_ok", "command": "setx FOO 1"}]}])
    assert any("blocked by policy" in e for e in lint_plan(blocked, policy, guard))


def test_next_ready_leaf_respects_parent_dependencies():
    plan = normalize_plan([
        {"id": "g1", "title": "Group 1", "instructions": "-", "done_when": "-"},
        {"id": "a", "parent_id": "g1", "title": "A", "instructions": "x", "done_when": "a.txt exists here"},
        {"id": "g2", "title": "Group 2", "instructions": "-", "done_when": "-", "depends_on": ["g1"]},
        {"id": "b", "parent_id": "g2", "title": "B", "instructions": "x", "done_when": "b.txt exists here"},
    ])
    for i, t in enumerate(plan):
        t.update(position=i, status="pending")
    assert next_ready_leaf(plan)["key"] == "a"
    plan[1]["status"] = "running"
    assert next_ready_leaf(plan) is None          # b waits for all of g1
    plan[1]["status"] = "done"
    assert next_ready_leaf(plan)["key"] == "b"


def test_checks(workspace):
    (workspace / "out.json").write_text('{"a": 1}')
    guard, policy = PathGuard(workspace), CommandPolicy()
    checks = [{"type": "json_valid", "path": "out.json"}, {"type": "file_contains", "path": "out.json", "text": "\"a\""},
              {"type": "file_exists", "path": "missing.txt"}, {"type": "file_exists", "path": "..\\outside.txt"},
              {"type": "command_ok", "command": "exit 0"}, {"type": "command_ok", "command": "exit 3"}]
    results = run_checks(checks, workspace, workspace, guard, policy, ask=lambda *a: False)
    assert [r.ok for r in results] == [True, True, False, False, True, False]
    assert "outside the workspace" in results[3].detail


# ---------------------------------------------------------------- planning
def test_planning_session_retries_after_lint_and_waits_for_approval(env_factory, workspace):
    bad = [{"id": "t1", "title": "Read", "instructions": "Read things", "done_when": "has been read"}]
    env = env_factory([call("list_dir"), call("propose_plan", tasks=bad), call("propose_plan", tasks=GOOD_PLAN)])
    job = env.job()
    assert env.runner._tick() is True
    job = env.jobs.get_job(job["id"])
    assert job["status"] == "awaiting_approval"
    assert [t["key"] for t in env.jobs.list_tasks(job["id"])] == ["t1", "t2"]
    run = env.jobs.list_runs(job["id"])[0]
    tool_msgs = [m for m in env.jobs.list_run_messages(run["id"]) if m["role"] == "tool"]
    assert "not accepted" in tool_msgs[1]["content"]
    # Planning tools are read-only plus the job tools.
    tool_names = {t["function"]["name"] for t in env.backend.calls[0]["tools"]}
    assert tool_names == {"read_file", "list_dir", "glob", "grep", "update_context", "propose_plan", "ask_user"}
    folder = workspace / "jobs" / job["slug"]
    assert (folder / "README.md").exists() and "LocalAgent job" in (folder / "README.md").read_text(encoding="utf-8")
    assert "[t2] Summarize" in (folder / "plan.md").read_text(encoding="utf-8")
    assert env.runner._tick() is False             # nothing runs before approval


def test_planning_question_waits_for_answer(env_factory):
    env = env_factory([call("ask_user", question="Which folder has the data?"),
                       lambda msgs: call("propose_plan", tasks=GOOD_PLAN) if any(
                           "D:/data" in (m.get("content") or "") for m in msgs) else "no answer seen"])
    job = env.job()
    env.runner._tick()
    assert env.jobs.get_job(job["id"])["status"] == "waiting_user"
    env.runner.answer(job["id"], "It's in D:/data")
    env.runner._tick()
    assert env.jobs.get_job(job["id"])["status"] == "awaiting_approval"


# ---------------------------------------------------------------- execution
def test_tasks_run_checks_retry_and_job_completes(env_factory, workspace):
    env = env_factory([
        # t1: write the file and complete -> checks pass
        call("write_file", path="data.txt", content="1\n2\n3\n"), call("complete_task", summary="Wrote data.txt with 3 lines."),
        # t2 attempt 1: claims completion without the file -> check fails
        call("complete_task", summary="Summary written (not really)."),
        # t2 attempt 2: sees the failure guidance in its prompt, does it properly
        lambda msgs: (call("write_file", path="summary.md", content="Three numbers.")
                      if "checks failed" in msgs[0]["content"] else "guidance missing"),
        call("complete_task", summary="Wrote summary.md."),
    ])
    job = env.job()
    env.plan(job["id"])
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "done"
    env.runner._tick()
    t2 = env.task(job["id"], "t2")
    assert t2["status"] == "pending" and t2["attempts"] == 1 and "checks failed" in t2["guidance"][0]
    assert "Wrote data.txt with 3 lines" in env.backend.calls[2]["messages"][0]["content"]   # outline shows t1's result
    env.runner._tick()
    assert env.task(job["id"], "t2")["status"] == "done"
    env.runner._tick()
    assert env.jobs.get_job(job["id"])["status"] == "done"


def test_full_job_to_done(env_factory, workspace):
    env = env_factory([
        call("write_file", path="data.txt", content="1\n2\n3\n"), call("complete_task", summary="Wrote data.txt."),
        call("write_file", path="summary.md", content="It lists numbers."), call("complete_task", summary="Wrote summary."),
    ])
    job = env.job()
    env.plan(job["id"])
    for _ in range(3):
        env.runner._tick()
    job = env.jobs.get_job(job["id"])
    assert job["status"] == "done" and job["usage"]["steps"] == 4
    journal = [e["text"] for e in env.jobs.list_journal(job["id"])]
    assert any("Job finished" in j for j in journal)
    # task sessions don't offer the chat task list; they offer the job tools
    names = {t["function"]["name"] for t in env.backend.calls[0]["tools"]}
    assert {"complete_task", "fail_task", "ask_user", "write_file"} <= names and "update_tasks" not in names
    system = env.backend.calls[2]["messages"][0]["content"]
    assert "YOUR TASK" in system and "Wrote data.txt." in system     # outline shows the earlier result


def test_checks_failure_feeds_next_attempt(env_factory):
    env = env_factory([
        call("complete_task", summary="Done without the file."),
        lambda msgs: call("write_file", path="data.txt", content="1\n2\n3\n") if "checks failed" in msgs[0]["content"] else "no",
        call("complete_task", summary="Now with the file."),
    ])
    job = env.job()
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    env.runner._tick()
    t1 = env.task(job["id"], "t1")
    assert t1["status"] == "pending" and t1["attempts"] == 1
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "done"


def test_repeated_failure_pauses_job_and_retry_resumes(env_factory):
    give_up = call("fail_task", reason="The data source is missing", what_would_help="Tell me where the data is")
    env = env_factory([give_up, give_up, give_up,
                       call("write_file", path="data.txt", content="x"), call("complete_task", summary="Worked this time.")])
    job = env.job()
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    for _ in range(3):
        env.runner._tick()
    t1 = env.task(job["id"], "t1")
    assert t1["status"] == "failed" and t1["attempts"] == 3
    assert "data source is missing" in t1["guidance"][-1]
    env.runner._tick()                          # settle
    job_row = env.jobs.get_job(job["id"])
    assert job_row["status"] == "paused" and "Needs your decision" in job_row["status_reason"]
    env.runner.retry_task(job["id"], t1["id"])
    assert env.jobs.get_job(job["id"])["status"] == "running"
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "done"


def test_ask_user_blocks_only_its_task(env_factory):
    plan = [dict(GOOD_PLAN[0]), {"id": "t3", "title": "Independent", "instructions": "Write other.txt",
                                 "done_when": "other.txt exists in the workspace",
                                 "checks": [{"type": "file_exists", "path": "other.txt"}]}]
    env = env_factory([call("ask_user", question="How many numbers?"),
                       call("write_file", path="other.txt", content="ok"), call("complete_task", summary="Wrote other."),
                       lambda msgs: call("write_file", path="data.txt", content="1") if "They answered" in msgs[0]["content"] else "no",
                       call("complete_task", summary="Wrote data.")])
    job = env.job()
    env.plan(job["id"], plan=plan)
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "waiting_user"
    env.runner._tick()
    assert env.task(job["id"], "t3")["status"] == "done"          # other work continued
    env.runner._tick()                                             # settle -> waiting_user
    assert env.jobs.get_job(job["id"])["status"] == "waiting_user"
    env.runner.answer(job["id"], "Just one", env.task(job["id"], "t1")["id"])
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "done"


def test_nudge_when_task_ends_without_complete(env_factory):
    env = env_factory([call("write_file", path="data.txt", content="1"), "All finished!",
                       call("complete_task", summary="Wrote data.txt.")])
    job = env.job()
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "done"
    run = env.jobs.list_runs(job["id"])[-1]
    assert any("without calling complete_task" in (m["content"] or "") for m in env.jobs.list_run_messages(run["id"]))


def test_budget_pauses_job(env_factory):
    env = env_factory([call("list_dir")] * 10)
    job = env.job(budget={"max_hours": 4, "max_steps": 3, "indefinite": False})
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    env.runner._tick()
    job_row = env.jobs.get_job(job["id"])
    assert job_row["status"] == "paused" and "Budget reached" in job_row["status_reason"]
    assert env.task(job["id"], "t1")["status"] == "pending"        # interrupted, not failed
    env.jobs.update_job(job["id"], budget={"indefinite": True})
    env.runner.resume(job["id"])
    assert env.jobs.get_job(job["id"])["status"] == "running"


def test_pause_takes_effect_at_break_point(env_factory):
    holder = {}

    def pause_then_list(msgs):
        holder["runner"].pause(holder["job_id"])
        return call("write_file", path="partial.txt", content="half")

    env = env_factory([pause_then_list, call("complete_task", summary="should not run yet")])
    holder["runner"] = env.runner
    job = env.job()
    holder["job_id"] = job["id"]
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    env.runner._tick()
    t1 = env.task(job["id"], "t1")
    assert (env.workspace / "partial.txt").exists()                # the in-flight step finished
    assert t1["status"] == "pending" and t1["attempts"] == 0
    assert "interrupted" in t1["guidance"][-1] and "write_file" in t1["guidance"][-1]
    assert env.jobs.get_job(job["id"])["status"] == "paused"
    assert len(env.backend.calls) == 1                              # no further model step


def test_stop_cancels_immediately(env_factory):
    holder = {}

    def stop_now(msgs):
        holder["runner"].stop_job(holder["job_id"])
        return "partial text " * 5

    env = env_factory([stop_now])
    holder["runner"] = env.runner
    job = env.job()
    holder["job_id"] = job["id"]
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "pending"
    job_row = env.jobs.get_job(job["id"])
    assert job_row["status"] == "paused" and job_row["status_reason"] == "Stopped by you"


def test_recover_after_restart(env_factory):
    env = env_factory([])
    job = env.job()
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    t1 = env.task(job["id"], "t1")
    env.jobs.update_task(t1["id"], status="running")
    run = env.jobs.create_run(job["id"], "task", t1["id"], 1)
    env.jobs.add_run_message(run["id"], "assistant", "", tool_calls=[{"id": "c1", "name": "write_file",
                                                                       "arguments": {"path": "data.txt", "content": "1"}}])
    env.jobs.add_run_message(run["id"], "tool", "Created", tool_call_id="c1", name="write_file", ok=True)
    fresh = JobRunner(env.jobs, env.store, env.coord, lambda: env.settings, env.events.append, env.approver)
    fresh.recover()
    t1 = env.task(job["id"], "t1")
    assert t1["status"] == "pending" and "write_file(data.txt) → ok" in t1["guidance"][-1]
    assert env.jobs.get_run(run["id"])["status"] == "interrupted"


def test_job_yields_to_running_chats(env_factory):
    env = env_factory([call("write_file", path="data.txt", content="1"), call("complete_task", summary="Wrote data.txt.")],
                      chats=FakeChats(busy_calls=4))
    job = env.job()
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "done"
    assert any(e["type"] == "job_activity" and "chat" in e["activity"] for e in env.events)


def test_job_permissions_preapprove_keys(env_factory):
    env = env_factory([call("run_shell", command="Invoke-WebRequest https://example.com -OutFile page.html -TimeoutSec 1"),
                       call("fail_task", reason="offline")], approver=AutoApprover(allow=False))
    job = env.job(permissions=["cmd:network"])
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    env.runner._tick()
    assert env.approver.requests == []            # network was pre-approved for this job


def test_approval_parks_task_without_blocking_others(env_factory, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret-ish")
    plan = [
        {"id": "t1", "title": "Read outside file", "instructions": "Copy outside.txt into copy.txt",
         "done_when": "copy.txt exists in the workspace", "checks": [{"type": "file_exists", "path": "copy.txt"}]},
        {"id": "t2", "title": "Independent", "instructions": "Write other.txt",
         "done_when": "other.txt exists in the workspace", "checks": [{"type": "file_exists", "path": "other.txt"}]},
    ]
    approver = AutoApprover(allow=False)
    env = env_factory([
        call("read_file", path=str(outside)),                                        # t1 -> needs approval, parks
        call("write_file", path="other.txt", content="ok"), call("complete_task", summary="Wrote other.txt."),  # t2 runs
        call("read_file", path=str(outside)),                                        # t1 again: granted once
        call("write_file", path="copy.txt", content="secret-ish"), call("complete_task", summary="Copied the file."),
    ], approver=approver)
    job = env.job()
    env.plan(job["id"], plan=plan)
    env.runner._tick()
    t1 = env.task(job["id"], "t1")
    assert t1["status"] == "waiting_user" and t1["waiting_kind"] == "approval"
    assert approver.requests and approver.requests[0]["job_id"] == job["id"]
    env.runner._tick()
    assert env.task(job["id"], "t2")["status"] == "done"          # not blocked by t1's approval
    env.runner._tick()                                             # settle
    assert env.jobs.get_job(job["id"])["status_reason"] == "Waiting for your approval"
    with pytest.raises(ValueError):
        env.runner.answer(job["id"], "yes", t1["id"])              # approvals aren't answered as questions
    approver.decide(next(iter(approver.callbacks)), "once")
    t1 = env.task(job["id"], "t1")
    assert t1["status"] == "pending" and "approved" in t1["guidance"][-1]
    assert env.jobs.get_job(job["id"])["status"] == "running"
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "done"
    assert env.jobs.get_job(job["id"])["inputs"]["granted_once"] == []   # the one-time grant was used up
    assert len(approver.requests) == 1                                   # the retry didn't ask again


def test_denied_approval_tells_task_not_to_retry(env_factory, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    approver = AutoApprover(allow=False)
    env = env_factory([call("read_file", path=str(outside))], approver=approver)
    job = env.job()
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    env.runner._tick()
    approver.decide(next(iter(approver.callbacks)), "deny")
    t1 = env.task(job["id"], "t1")
    assert t1["status"] == "pending" and "denied" in t1["guidance"][-1] and "Don't try it again" in t1["guidance"][-1]


def test_recover_releases_tasks_parked_on_lost_approvals(env_factory):
    env = env_factory([])
    job = env.job()
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    t1 = env.task(job["id"], "t1")
    env.jobs.update_task(t1["id"], status="waiting_user", waiting_kind="approval", question="Approval needed: x")
    env.jobs.update_job(job["id"], status="waiting_user")
    JobRunner(env.jobs, env.store, env.coord, lambda: env.settings, env.events.append, env.approver).recover()
    t1 = env.task(job["id"], "t1")
    assert t1["status"] == "pending" and "lost when the app restarted" in t1["guidance"][-1]
    assert env.jobs.get_job(job["id"])["status"] == "running"


def test_plan_request_lists_workspace(env_factory, workspace):
    (workspace / "data.csv").write_text("x,y\n")
    (workspace / "src").mkdir()
    env = env_factory([call("propose_plan", tasks=GOOD_PLAN)])
    env.job()
    env.runner._tick()
    first_user = next(m for m in env.backend.calls[0]["messages"] if m["role"] == "user")["content"]
    assert "- src/" in first_user and "- data.csv" in first_user and "jobs/" not in first_user


# ---------------------------------------------------------------- scratchpad
def test_planner_context_reaches_every_task_and_mirror(env_factory, workspace):
    env = env_factory([
        call("update_context", add=["Inputs live in data.txt at the workspace root", "Never edit evaluate.py"]),
        call("propose_plan", tasks=GOOD_PLAN[:1]),
        lambda msgs: (call("write_file", path="data.txt", content="1")
                      if "Never edit evaluate.py" in msgs[0]["content"] else "context missing from prompt"),
        call("complete_task", summary="Wrote data.txt."),
    ])
    job = env.job()
    env.runner._tick()
    env.runner.approve_plan(job["id"])
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "done"
    items = env.jobs.list_context(job["id"])
    assert [i["author"] for i in items] == ["agent", "agent"] and items[0]["task_key"] is None
    text = (workspace / "jobs" / env.jobs.get_job(job["id"])["slug"] / "scratchpad.md").read_text(encoding="utf-8")
    assert "Never edit evaluate.py" in text and "[x] [t1] Write data file" in text


def test_context_tool_removes_and_enforces_cap(env_factory):
    from localagent.jobs.scratchpad import CONTEXT_CHAR_LIMIT
    env = env_factory([
        call("update_context", add=["old fact"]),
        lambda msgs: call("update_context", remove=["c1"], add=["new fact"]),
        call("update_context", add=["x" * 399] * (CONTEXT_CHAR_LIMIT // 399 + 1)),
        call("update_context", add=["y" * 500]),
        call("fail_task", reason="just testing the scratchpad"),
    ])
    job = env.job()
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    env.runner._tick()
    assert [i["text"] for i in env.jobs.list_context(job["id"])] == ["new fact"]
    run = env.jobs.list_runs(job["id"])[-1]
    results = [m["content"] for m in env.jobs.list_run_messages(run["id"]) if m["role"] == "tool"]
    assert "Consolidate first" in results[2] and "at most 400 characters" in results[3]


def test_checklist_survives_interruption_and_is_shown_on_resume(env_factory):
    holder = {}

    def tick_then_pause(msgs):
        holder["runner"].pause(holder["job_id"])
        return call("update_checklist", items=[{"text": "Write data.txt", "done": True},
                                               {"text": "Verify line count", "done": False}])

    def resumed(msgs):
        system = msgs[0]["content"]
        assert "[x] Write data.txt" in system and "[ ] Verify line count" in system, system[-800:]
        return call("write_file", path="data.txt", content="1\n2\n3\n")

    env = env_factory([tick_then_pause, resumed, call("complete_task", summary="Verified and done.")])
    holder["runner"] = env.runner
    job = env.job()
    holder["job_id"] = job["id"]
    env.plan(job["id"], plan=GOOD_PLAN[:1])
    env.runner._tick()
    t1 = env.task(job["id"], "t1")
    assert t1["status"] == "pending" and t1["checklist"][0]["done"] is True
    env.runner.resume(job["id"])
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "done"


def test_context_api(settings, workspace):
    with TestClient(create_app(settings, EchoBackend(), run_jobs=False)) as client:
        project = client.post("/api/projects", json={"name": "P", "workspace_path": str(workspace)}).json()
        job = client.post("/api/jobs", json={"project_id": project["id"], "title": "J", "goal": "g"}).json()
        item = client.post(f"/api/jobs/{job['id']}/context", json={"text": "Reports go in D:/Reports"}).json()
        detail = client.get(f"/api/jobs/{job['id']}").json()
        assert detail["context"][0]["author"] == "user" and detail["context_limit"] > 0
        assert client.post(f"/api/jobs/{job['id']}/context", json={"text": "z" * 500}).status_code == 400
        assert client.delete(f"/api/jobs/{job['id']}/context/{item['id']}").status_code == 200
        assert client.get(f"/api/jobs/{job['id']}").json()["context"] == []


# ---------------------------------------------------------------- API
def test_job_api(settings, workspace):
    with TestClient(create_app(settings, EchoBackend(), run_jobs=False)) as client:
        project = client.post("/api/projects", json={"name": "P", "workspace_path": str(workspace)}).json()
        assert client.post("/api/jobs", json={"project_id": project["id"], "title": "", "goal": "x"}).status_code == 400
        assert client.post("/api/jobs", json={"project_id": project["id"], "title": "J", "goal": "x",
                                              "permissions": ["cmd:everything"]}).status_code == 400
        job = client.post("/api/jobs", json={"project_id": project["id"], "title": "Report", "goal": "Write a report",
                                             "budget": {"indefinite": True}}).json()
        assert job["status"] == "planning" and job["budget"]["indefinite"] is True
        assert client.post(f"/api/jobs/{job['id']}/approve").status_code == 400     # no plan yet
        bad = client.put(f"/api/jobs/{job['id']}/plan", json={"tasks": [{"id": "a", "title": "A", "instructions": "x",
                                                                          "done_when": "it has been read"}]})
        assert bad.status_code == 400 and "can't be checked" in bad.json()["detail"]
        ok = client.put(f"/api/jobs/{job['id']}/plan", json={"tasks": GOOD_PLAN})
        assert ok.json()["status"] == "awaiting_approval"
        assert client.post(f"/api/jobs/{job['id']}/approve").json()["status"] == "running"
        detail = client.get(f"/api/jobs/{job['id']}").json()
        assert len(detail["tasks"]) == 2 and detail["journal"]
        assert client.post(f"/api/jobs/{job['id']}/pause").json()["status"] == "paused"
        assert client.post(f"/api/jobs/{job['id']}/resume").json()["status"] == "running"
        assert any(j["id"] == job["id"] for j in client.get("/api/state").json()["jobs"])
        assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
        assert client.get(f"/api/jobs/{job['id']}").status_code == 404
