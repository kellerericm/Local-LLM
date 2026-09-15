import pytest

from test_jobs import Env, call


@pytest.fixture
def env_factory(store, settings, workspace):
    return lambda responses, **kw: Env(store, settings, workspace, responses, **kw)


def review(verdict="pass", **kw):
    return call("report_review", verdict=verdict, **kw)


OUTLINE = "# Lake report\n\n## Trends\n- TP fell [n1]\n\n## Conflicts and open questions\n- Loads disputed [n1] [n2]\n"


def test_research_report_end_to_end(env_factory, workspace):
    (workspace / "docs").mkdir()
    (workspace / "docs" / "survey.md").write_text("North basin phosphorus fell from 38 to 26 micrograms per litre.")
    (workspace / "docs" / "review.md").write_text("The creek contributes roughly 30 percent of the load.")
    (workspace / "docs" / "trail.txt").write_text("The trail closes in June.")
    env = env_factory([
        # r1 review.md (sorted order: review.md, survey.md, trail.txt)
        call("add_note", claim="Creek ~30%", quote="contributes roughly 30 percent", source="docs/review.md"),
        call("complete_task", summary="One note saved from review.md."),
        # r2 survey.md
        call("add_note", claim="TP fell", quote="fell from 38 to 26", source="docs/survey.md"),
        call("complete_task", summary="One note saved from survey.md."),
        # r3 trail.txt
        call("complete_task", summary="Trail notice is not relevant to phosphorus."),
        # outline + review
        call("write_file", path="outline.md", content=OUTLINE),
        call("complete_task", summary="Outline with two sections written."),
        review(),
        # sections (after gate) + reviews
        call("write_file", path="sections/01-trends.md", content="## Trends\nPhosphorus fell [n2]."),
        call("complete_task", summary="Trends section written."),
        review(),
        call("write_file", path="sections/02-conflicts-and-open-questions.md",
             content="## Conflicts and open questions\nThe creek share is disputed [n1]."),
        call("complete_task", summary="Conflicts section written."),
        review(),
        call("write_file", path="sections/00-summary.md", content="## Summary\nTP fell [n2]; loads disputed [n1]."),
        call("complete_task", summary="Summary written."),
        review(),
    ])
    job = env.jobs.create_job(env.project["id"], "Lake report", "How has phosphorus changed?",
                              template="research_report", inputs={"sources": "docs"})
    env.runner._tick()                                             # template builds the plan
    job = env.jobs.get_job(job["id"])
    assert job["status"] == "awaiting_approval"
    keys = [t["key"] for t in env.jobs.list_tasks(job["id"])]
    assert keys == ["read", "r1", "r2", "r3", "outline", "outline_gate"]
    env.runner.approve_plan(job["id"])
    for _ in range(4):                                             # r1, r2, r3, outline(+review)
        env.runner._tick()
    tasks = {t["key"]: t for t in env.jobs.list_tasks(job["id"])}
    assert tasks["r3"]["status"] == "done" and tasks["outline"]["status"] == "done"
    env.runner._tick()                                             # opens the gate
    gate = next(t for t in env.jobs.list_tasks(job["id"]) if t["key"] == "outline_gate")
    assert gate["status"] == "waiting_user" and gate["waiting_kind"] == "gate"
    env.runner._tick()                                             # settle -> waiting for user
    assert env.jobs.get_job(job["id"])["status"] == "waiting_user"
    env.runner.answer(job["id"], "approve", gate["id"])
    keys = [t["key"] for t in env.jobs.list_tasks(job["id"])]
    assert keys[-5:] == ["write", "s1", "s2", "summary", "compile"]
    for _ in range(6):
        env.runner._tick()
    report = (workspace / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# Lake report") and "## Summary" in report and "## Sources and evidence" in report
    assert report.index("## Summary") < report.index("## Trends")
    assert "“fell from 38 to 26”" in report and "docs/review.md" in report
    assert env.jobs.get_job(job["id"])["status"] == "done"
    kinds = [r["kind"] for r in env.jobs.list_runs(job["id"])]
    assert kinds.count("review") == 4 and "code" in kinds


def test_outline_gate_revision_reopens_outline(env_factory, workspace):
    (workspace / "a.md").write_text("Alpha fact about lakes here.")
    env = env_factory([
        call("add_note", claim="Alpha", quote="Alpha fact about lakes", source="a.md"),
        call("complete_task", summary="Note saved from a.md."),
        call("write_file", path="outline.md", content="# R\n\n## One\n- x [n1]\n## Two\n- y [n1]\n## Three\n- z [n1]\n"),
        call("complete_task", summary="Outline written."),
        review(),
        lambda msgs: (call("write_file", path="outline.md", content="# R\n\n## Merged\n- x [n1] [n1] [n1]\n")
                      if "asked for changes" in msgs[0]["content"] else "feedback missing"),
        call("complete_task", summary="Outline revised."),
        review(),
    ])
    job = env.jobs.create_job(env.project["id"], "R", "Question?", template="research_report")
    env.runner._tick()
    env.runner.approve_plan(job["id"])
    for _ in range(3):
        env.runner._tick()
    gate = next(t for t in env.jobs.list_tasks(job["id"]) if t["key"] == "outline_gate")
    env.runner.answer(job["id"], "Merge the three sections into one", gate["id"])
    outline = next(t for t in env.jobs.list_tasks(job["id"]) if t["key"] == "outline")
    assert outline["status"] == "pending" and "Merge the three sections" in outline["guidance"][-1]
    env.runner._tick()                                             # outline again (+review)
    env.runner._tick()                                             # gate reopens
    gate = next(t for t in env.jobs.list_tasks(job["id"]) if t["key"] == "outline_gate")
    assert gate["status"] == "waiting_user"


def test_reviewer_rejection_retries_and_removes_bad_context(env_factory, workspace):
    (workspace / "a.md").write_text("Baseline error is 3.01 on the evaluator.")
    env = env_factory([
        call("update_context", add=["The data has 66 peaks so frequency is 6.67"]),
        call("add_note", claim="Baseline", quote="Baseline error is 3.01", source="a.md"),
        call("complete_task", summary="Noted the baseline."),
        review("fail", issues=[{"problem": "Peak count is noise, not signal", "evidence": "data is smooth"}],
               bad_context_ids=["c1"]),
        lambda msgs: (call("complete_task", summary="Baseline noted; peak claim dropped.")
                      if "reviewer rejected" in msgs[0]["content"] else "guidance missing"),
        review("pass"),
    ])
    plan = [{"id": "t1", "title": "Note baseline", "instructions": "Take a note from a.md",
             "done_when": "One note saved from a.md about the baseline",
             "checks": [{"type": "notes_for_source", "source": "a.md"}]}]
    job = env.job()
    env.plan(job["id"], plan=plan)
    t1 = env.task(job["id"], "t1")
    env.jobs.s._exec("UPDATE job_tasks SET review=1 WHERE id=?", (t1["id"],))
    env.runner._tick()
    t1 = env.task(job["id"], "t1")
    assert t1["status"] == "pending" and "Peak count is noise" in t1["guidance"][-1]
    assert env.jobs.list_context(job["id"]) == []                   # unsupported claim removed
    env.runner._tick()
    assert env.task(job["id"], "t1")["status"] == "done"
    # the reviewer never sees the worker's conversation
    review_call = next(c for c in env.backend.calls if any(t["function"]["name"] == "report_review" for t in c["tools"]))
    assert not any(m["role"] == "tool" for m in review_call["messages"])


def test_reviewer_out_of_steps_still_records_a_verdict(env_factory, workspace):
    from localagent.jobs.review import ReviewSession
    (workspace / "a.md").write_text("Alpha fact about lakes here.\n" + "".join(f"line {i}\n" for i in range(40)))
    env = env_factory([
        call("add_note", claim="Alpha", quote="Alpha fact about lakes", source="a.md"),
        call("complete_task", summary="Saved the alpha note."),
        *[call("read_file", path="a.md", offset=i + 1, limit=1)                # reviewer dawdles (distinct reads)
          for i in range(ReviewSession.max_steps)],
        lambda msgs: (review("fail", issues=[{"problem": "ran out of time checking"}])
                      if "out of steps" in msgs[-1]["content"] else "no final prompt"),
        call("complete_task", summary="Saved the alpha note again."),
        review("pass"),
    ])
    plan = [{"id": "t1", "title": "Note", "instructions": "Take a note from a.md", "done_when": "One note saved from a.md here",
             "checks": [{"type": "notes_for_source", "source": "a.md"}]}]
    job = env.job()
    env.plan(job["id"], plan=plan)
    t1 = env.task(job["id"], "t1")
    env.jobs.s._exec("UPDATE job_tasks SET review=1 WHERE id=?", (t1["id"],))
    env.runner._tick()
    t1 = env.task(job["id"], "t1")
    assert t1["status"] == "pending" and "ran out of time checking" in t1["guidance"][-1]


def test_template_setup_failure_is_explained(env_factory):
    env = env_factory([])
    job = env.jobs.create_job(env.project["id"], "R", "Q?", template="research_report", inputs={"sources": "nothing-here"})
    (env.workspace / "nothing-here").mkdir()
    env.runner._tick()
    job = env.jobs.get_job(job["id"])
    assert job["status"] == "failed" and "No documents" in job["status_reason"]
