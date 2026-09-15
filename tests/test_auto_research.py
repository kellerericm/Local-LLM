import json
import shutil
from pathlib import Path

import pytest

from test_jobs import Env, call

ROOT = Path(__file__).resolve().parents[1]

OVERFIT = '''def predict(x):
    # degree-5 polynomial fitted to the visible data
    return (0.9886818184160636 + 5.362510733451499 * x - 1.052015115117749 * x**2
            - 0.24993231665337934 * x**3 + 0.06794503238656513 * x**4 - 0.003692959585910316 * x**5)
'''
TRUE_FORM = '''import math


def predict(x):
    return 4.0 * math.sin(0.8 * x) + 0.5 * x + 1.5
'''


@pytest.fixture
def tune_ws(workspace):
    for name in ("model.py", "evaluate.py", "data.csv", "README.md"):
        shutil.copy2(ROOT / "sandbox" / "tune_me" / name, workspace / name)
    return workspace


def job_inputs(**extra):
    holdout = f'python "{ROOT / "sandbox" / "answer_keys" / "tune_me_holdout.py"}" "{{work}}\\model.py"'
    return {"targets": "model.py", "optimize_command": "python evaluate.py", "optimize_regex": r"score:\s*([\d.]+)",
            "direction": "lower", "split": "provided", "holdout_command": holdout,
            "holdout_regex": r"holdout_extrapolation:\s*([\d.]+)", "protected": "evaluate.py, data.csv",
            "max_experiments": "2", "plateau": "4", **extra}


def run_all(env, job_id, limit=30):
    for _ in range(limit):
        job = env.jobs.get_job(job_id)
        gate = next((t for t in env.jobs.list_tasks(job_id) if t["waiting_kind"] == "gate" and t["status"] == "waiting_user"), None)
        if gate:
            env.runner.answer(job_id, "approve", gate["id"])
        if job["status"] in ("done", "failed"):
            return job
        env.runner._tick()
    return env.jobs.get_job(job_id)


def test_overfit_is_reverted_true_improvement_is_kept_and_applied(store, settings, tune_ws):
    env = Env(store, settings, tune_ws, [
        call("write_file", path="model.py", content=OVERFIT), call("complete_task", summary="Hypothesis: a degree-5 polynomial fits the curve."),
        call("write_file", path="model.py", content=TRUE_FORM), call("complete_task", summary="Hypothesis: sine plus linear trend."),
        call("write_file", path="report.md", content="# Report\nSine won."), call("complete_task", summary="Report written."),
    ])
    job = env.jobs.create_job(env.project["id"], "Tune", "Lower the error", template="auto_research", inputs=job_inputs())
    env.runner._tick()
    env.runner.approve_plan(job["id"])
    job = run_all(env, job["id"])
    assert job["status"] == "done", job["status_reason"]
    st = job["inputs"]["ar_state"]
    e1, e2 = st["experiments"]
    assert e1["result"].startswith("reverted") and "overfitting warning" in e1["result"]
    assert e1["optimize"] < st["baseline_optimize"] and e1["holdout"] > st["baseline_holdout"]
    assert e2["result"] == "kept" and st["best_n"] == 2 and st["best_holdout"] < 0.5
    # the original project file was only changed at the end, after approval
    assert "math.sin(0.8 * x)" in (tune_ws / "model.py").read_text()
    log = (tune_ws / "jobs" / job["slug"] / "experiments.md").read_text(encoding="utf-8")
    assert "degree-5" in log and "kept" in log
    # the worker ran inside the isolated copy
    assert Path(job["inputs"]["work_dir"]).name == "work"


def test_protected_file_edit_is_reverted(store, settings, tune_ws):
    env = Env(store, settings, tune_ws, [
        call("write_file", path="evaluate.py", content="print('score: 0.0')"),
        call("complete_task", summary="Hypothesis: make the evaluator friendlier."),
    ])
    job = env.jobs.create_job(env.project["id"], "Tune", "Lower the error", template="auto_research",
                              inputs=job_inputs(max_experiments="1"))
    env.runner._tick()
    env.runner.approve_plan(job["id"])
    for _ in range(4):
        env.runner._tick()
    st = env.jobs.get_job(job["id"])["inputs"]["ar_state"]
    assert "protected files changed" in st["experiments"][0]["result"]
    work = Path(env.jobs.get_job(job["id"])["inputs"]["work_dir"])
    assert "score: 0.0" not in (work / "evaluate.py").read_text()


def test_80_20_split_hides_heldout_rows(store, settings, workspace):
    rows = ["x,y"] + [f"{i},{2 * i}" for i in range(100)]
    (workspace / "data.csv").write_text("\n".join(rows) + "\n")
    (workspace / "model.py").write_text("def predict(x):\n    return 2 * x\n")
    (workspace / "score.py").write_text(
        "import csv,sys\nfrom model import predict\npath=sys.argv[1] if len(sys.argv)>1 else 'data.csv'\n"
        "r=list(csv.DictReader(open(path)))\nprint('score:', sum(abs(predict(float(x['x']))-float(x['y'])) for x in r)/len(r))\n")
    env = Env(store, settings, workspace, [])
    job = env.jobs.create_job(env.project["id"], "Fit", "Fit y", template="auto_research", inputs={
        "targets": "model.py", "optimize_command": "python score.py", "split": "80/20", "data_file": "data.csv",
        "holdout_command": 'python score.py "{heldout}\\data.csv"', "protected": "score.py"})
    env.runner._tick()
    env.runner.approve_plan(job["id"])
    env.runner._tick()                                   # setup
    job = env.jobs.get_job(job["id"])
    work = Path(job["inputs"]["work_dir"])
    visible = (work / "data.csv").read_text().strip().splitlines()
    hidden = (work.parent / "heldout" / "data.csv").read_text().strip().splitlines()
    assert len(visible) - 1 == 80 and len(hidden) - 1 == 20
    assert set(visible[1:]).isdisjoint(hidden[1:])
    assert job["inputs"]["ar_state"]["baseline_holdout"] == 0.0
    assert json.loads((work.parent / "heldout" / "split.json").read_text())["train_rows"] == 80


def test_heldout_required_unless_explicitly_none(store, settings, workspace):
    env = Env(store, settings, workspace, [])
    job = env.jobs.create_job(env.project["id"], "X", "Y", template="auto_research",
                              inputs={"targets": "a.py", "optimize_command": "python a.py", "split": "provided"})
    env.runner._tick()
    job = env.jobs.get_job(job["id"])
    assert job["status"] == "failed" and "held-out evaluation is required" in job["status_reason"]
