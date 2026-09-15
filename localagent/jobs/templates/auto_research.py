"""auto_research: measured improvement loop with coordinator-owned keep/revert (design §6.2, decision 7).

Flow:
  setup (code): copy the workspace into jobs/<slug>/work, protect evaluator/data files, optionally make the 80/20
                split, measure the baseline on the optimization and held-out evaluators.
  e1 (agent):   investigate and leave ONE candidate change in the working copy; complete_task with the hypothesis.
  m1 (code):    run the held-out evaluator itself; check protected files; keep (snapshot) or revert; log the row.
  ... repeat until the experiment limit, a plateau, or the target; then
  report (agent) → accept gate (user) → apply (code: copy the best files back into the project).
The agent never sees the held-out data or its score history beyond what the coordinator writes into the log.
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
import re
import shutil
import time
from pathlib import Path

from ...tools.process import env_for, run_process
from ...tools.shell import PS_PREFIX
from . import register
from .base import HandlerResult, Template, is_approval
from .research_report import workspace_of

DEFAULTS = {"targets": "", "optimize_command": "", "optimize_regex": r"score:\s*([-\d.eE+]+)", "direction": "lower",
            "holdout_command": "", "holdout_regex": r"score:\s*([-\d.eE+]+)", "protected": "", "split": "provided",
            "data_file": "", "max_experiments": 12, "plateau": 4, "target": "", "timeout_s": 1800}
SKIP = {"jobs", "__pycache__", ".git", "node_modules", ".venv"}

EXPERIMENT = """Improve {targets} so that `{optimize_command}` reports a {better} score.

Current best (held-out) score: {best}. Baseline: {baseline}.
Rules: change only {targets}; don't edit {protected}. Your working folder is a private copy; the official score is
measured by the coordinator on held-out data you can't see, after you finish.

Experiment log so far (measured by the coordinator):
{log}

Do ONE experiment:
1. Investigate first: look at the data and the current code; test ideas cheaply (e.g. short runs, small analyses).
2. Form one hypothesis. Prefer simple, general improvements over tuning to the visible data; fitting noise will be
   caught by the held-out score and reverted.
3. Make the change and run `{optimize_command}` to check it.
4. Leave your best candidate for this hypothesis in place and call complete_task with: hypothesis, what you changed,
   and the score you saw. If the idea didn't help, restore the previous version before completing.
Record reusable findings (what helps, what doesn't) with update_context."""

REPORT = """The experiments are finished ({reason}). Write report.md in the working folder: the goal, the baseline and
final held-out scores, a table of every experiment from the log below (hypothesis, held-out score, kept or reverted),
what mattered, what didn't, the overfitting risk (compare optimization vs held-out scores), and suggested next steps.

{log}"""


def cfg(job: dict) -> dict:
    c = {**DEFAULTS, **{k: v for k, v in (job.get("inputs") or {}).items() if v not in (None, "")}}
    for k in ("max_experiments", "plateau", "timeout_s"):
        c[k] = int(c[k])
    c["targets_list"] = [t.strip() for t in str(c["targets"]).split(",") if t.strip()]
    c["protected_list"] = [t.strip() for t in str(c["protected"]).split(",") if t.strip()]
    return c


def work_dir(runner, job) -> Path:
    from ..mirrors import job_dir
    return job_dir(workspace_of(runner, job), job) / "work"


def best_dir(runner, job) -> Path:
    return work_dir(runner, job).parent / "best"


def hidden_dir(runner, job) -> Path:
    return work_dir(runner, job).parent / "heldout"


def better(a: float | None, b: float | None, direction: str) -> bool:
    if a is None:
        return False
    if b is None:
        return True
    return a < b if direction == "lower" else a > b


def run_metric(runner, job, command: str, regex: str, timeout_s: int) -> tuple[float | None, str]:
    work = work_dir(runner, job)
    cmd = command.replace("{work}", str(work)).replace("{heldout}", str(hidden_dir(runner, job)))
    settings = runner.settings_getter()
    project = runner.store.get_project(job["project_id"])
    env = env_for(Path(project["env_path"] or settings.env_path))
    code, output, status = run_process(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + cmd],
                                       work, env, float(timeout_s))
    matches = re.findall(regex, output)
    value = None
    if status == "ok" and matches:
        try:
            value = float(matches[-1] if isinstance(matches[-1], str) else matches[-1][0])
        except ValueError:
            value = None
    tail = output[-600:]
    if status != "ok":
        tail = f"({status}) {tail}"
    return value, tail


def file_hashes(root: Path, names: list[str]) -> dict:
    out = {}
    for n in names:
        p = root / n
        out[n] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return out


def snapshot(src: Path, dst: Path, files: list[str]) -> None:
    for f in files:
        s = src / f
        if s.is_file():
            (dst / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, dst / f)


def render_log(state: dict, direction: str) -> str:
    rows = state.get("experiments") or []
    if not rows:
        return "(no experiments yet)"
    lines = ["| # | Hypothesis | Optimization score | Held-out score | Result |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['n']} | {r['hypothesis'][:140].replace('|', '/')} | {r.get('optimize')} | {r.get('holdout')} | "
                     f"{r['result']} |")
    return "\n".join(lines)


def write_log(runner, job, state: dict, direction: str) -> None:
    path = work_dir(runner, job).parent / "experiments.md"
    head = [f"# Experiment log: {job['title']}", "",
            f"Direction: {direction} is better. Baseline held-out: {state.get('baseline_holdout')}. "
            f"Best held-out: {state.get('best_holdout')} (experiment {state.get('best_n', 0)}).", ""]
    path.write_text("\n".join(head) + render_log(state, direction) + "\n", encoding="utf-8")


def state_of(job: dict) -> dict:
    return (job.get("inputs") or {}).get("ar_state") or {}


def save_state(runner, job, state: dict) -> None:
    fresh = runner.jobs.get_job(job["id"])
    runner.jobs.update_job(job["id"], inputs={**(fresh.get("inputs") or {}), "ar_state": state})


# ---------------------------------------------------------------- plan
def _initial_plan(runner, job):
    c = cfg(job)
    missing = [k for k in ("targets", "optimize_command") if not c[k]]
    if missing:
        raise ValueError(f"Auto-research needs: {', '.join(missing)}")
    if c["split"] == "provided" and not c["holdout_command"]:
        raise ValueError("A held-out evaluation is required: give a held-out command, or choose split 80/20 with a data "
                         "file. To run without one, set split to 'none' (the report will say so).")
    if c["split"] not in ("provided", "80/20", "none") and not re.fullmatch(r"\d{2}/\d{2}", str(c["split"])):
        raise ValueError("split must be provided, 80/20 (or another NN/NN you chose), or none")
    if c["split"] not in ("provided", "none") and not c["data_file"]:
        raise ValueError(f"split {c['split']} needs data_file (a CSV to split)")
    if c["direction"] not in ("lower", "higher"):
        raise ValueError("direction must be lower or higher")
    return [{"key": "setup", "title": "Set up the experiment workspace and measure the baseline", "kind": "code",
             "handler": "setup", "instructions": "-", "done_when": "baseline measured"}]


def add_experiment(runner, job, n: int) -> None:
    runner.jobs.append_tasks(job["id"], [
        {"key": f"e{n}", "title": f"Experiment {n}: investigate and propose", "params": {"n": n},
         "instructions": "(built from the live experiment log when the task starts)",
         "done_when": "one candidate change is in place and described in the summary",
         "depends_on": [f"m{n - 1}" if n > 1 else "setup"]},
        {"key": f"m{n}", "title": f"Experiment {n}: measure, keep or revert", "kind": "code", "handler": "measure",
         "params": {"n": n}, "depends_on": [f"e{n}"], "instructions": "-", "done_when": "held-out score logged"},
    ])


def refresh_experiment_instructions(runner, job, task) -> None:
    c = cfg(job)
    st = state_of(job)
    text = EXPERIMENT.format(targets=", ".join(c["targets_list"]), optimize_command=c["optimize_command"],
                             better="lower" if c["direction"] == "lower" else "higher",
                             best=st.get("best_holdout"), baseline=st.get("baseline_holdout"),
                             protected=", ".join(c["protected_list"]) or "evaluation and data files",
                             log=render_log(st, c["direction"]))
    runner.jobs.update_task(task["id"], instructions=text)


# ---------------------------------------------------------------- handlers
def handle_setup(runner, job, task) -> HandlerResult:
    c = cfg(job)
    ws = workspace_of(runner, job)
    work = work_dir(runner, job)
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(ws, work, ignore=lambda d, names: [n for n in names if n in SKIP])
    held = hidden_dir(runner, job)
    held.mkdir(parents=True, exist_ok=True)
    if c["split"] not in ("provided", "none"):
        train_pct = int(str(c["split"]).split("/")[0])
        data = work / c["data_file"]
        with open(data, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        header, body = rows[0], rows[1:]
        rng = random.Random(int(time.time()))
        seed = rng.randrange(10**9)
        random.Random(seed).shuffle(body)
        cut = len(body) * train_pct // 100
        for path, part in ((work / c["data_file"], body[:cut]), (held / Path(c["data_file"]).name, body[cut:])):
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows([header] + part)
        (held / "split.json").write_text(json.dumps({"split": c["split"], "seed": seed, "train_rows": cut,
                                                      "heldout_rows": len(body) - cut}), encoding="utf-8")
    protected = c["protected_list"]
    best = best_dir(runner, job)
    snapshot(work, best, c["targets_list"])
    opt, opt_tail = run_metric(runner, job, c["optimize_command"], c["optimize_regex"], c["timeout_s"])
    hold, hold_tail = (run_metric(runner, job, c["holdout_command"], c["holdout_regex"], c["timeout_s"])
                       if c["holdout_command"] else (opt if c["split"] == "none" else None, ""))
    if opt is None:
        return HandlerResult(False, "The optimization command didn't produce a score.",
                             retry_guidance=f"Baseline optimization run output: {opt_tail}")
    if hold is None and c["split"] != "none":
        return HandlerResult(False, "The held-out evaluation didn't produce a score.",
                             retry_guidance=f"Held-out run output: {hold_tail}")
    inputs = {**(runner.jobs.get_job(job["id"])["inputs"] or {}), "work_dir": str(work)}
    runner.jobs.update_job(job["id"], inputs=inputs)
    state = {"baseline_optimize": opt, "baseline_holdout": hold, "best_holdout": hold, "best_optimize": opt,
             "best_n": 0, "experiments": [], "protected_hashes": file_hashes(work, protected), "since_improvement": 0}
    save_state(runner, runner.jobs.get_job(job["id"]), state)
    job = runner.jobs.get_job(job["id"])
    write_log(runner, job, state, c["direction"])
    runner.jobs.add_context(job["id"], f"Baseline: optimization score {opt}, held-out score {hold} "
                                       f"({c['direction']} is better). Measured by the coordinator.", "agent", "setup")
    if c["split"] == "none":
        runner.jobs.add_context(job["id"], "No held-out evaluation: results may not generalize.", "agent", "setup")
    return HandlerResult(True, f"Working copy ready; baseline optimization {opt}, held-out {hold}.")


def handle_measure(runner, job, task) -> HandlerResult:
    c = cfg(job)
    st = state_of(job)
    n = int(task["params"]["n"])
    work = work_dir(runner, job)
    exp_task = next(t for t in runner.jobs.list_tasks(job["id"]) if t["key"] == f"e{n}")
    hypothesis = (exp_task.get("result_summary") or "(no summary)").strip()
    violations = [f for f, h in file_hashes(work, c["protected_list"]).items() if h != st["protected_hashes"].get(f)]
    row = {"n": n, "hypothesis": hypothesis, "optimize": None, "holdout": None}
    if violations:
        snapshot(best_dir(runner, job), work, c["targets_list"])
        row["result"] = f"reverted: protected files changed ({', '.join(violations)})"
        runner.jobs.journal(job["id"], "experiment", f"Experiment {n} edited protected files; reverted.", task["key"])
        # protected files are restored from the original workspace copy
        snapshot(workspace_of(runner, job), work, violations)
    else:
        row["optimize"], _ = run_metric(runner, job, c["optimize_command"], c["optimize_regex"], c["timeout_s"])
        if c["holdout_command"]:
            row["holdout"], tail = run_metric(runner, job, c["holdout_command"], c["holdout_regex"], c["timeout_s"])
        else:
            row["holdout"], tail = row["optimize"], ""
        if better(row["holdout"], st["best_holdout"], c["direction"]):
            snapshot(work, best_dir(runner, job), c["targets_list"])
            st.update(best_holdout=row["holdout"], best_optimize=row["optimize"], best_n=n, since_improvement=0)
            row["result"] = "kept"
        else:
            snapshot(best_dir(runner, job), work, c["targets_list"])
            st["since_improvement"] = st.get("since_improvement", 0) + 1
            row["result"] = "reverted" + ("" if row["holdout"] is not None else " (no held-out score)")
        if row["optimize"] is not None and row["holdout"] is not None and st.get("baseline_optimize") and \
                st.get("baseline_holdout"):
            opt_gain = row["optimize"] - st["baseline_optimize"]
            hold_gain = row["holdout"] - st["baseline_holdout"]
            improving_opt = (opt_gain < 0) if c["direction"] == "lower" else (opt_gain > 0)
            improving_hold = (hold_gain < 0) if c["direction"] == "lower" else (hold_gain > 0)
            if improving_opt and not improving_hold:
                row["result"] += " — overfitting warning: better on visible data, not on held-out"
    st["experiments"].append(row)
    save_state(runner, job, st)
    job = runner.jobs.get_job(job["id"])
    write_log(runner, job, st, c["direction"])
    runner.jobs.add_context(job["id"], f"Experiment {n}: {hypothesis[:160]} → held-out {row['holdout']} ({row['result']}).",
                            "agent", task["key"])
    return HandlerResult(True, f"Experiment {n}: held-out {row['holdout']} ({row['result']}). "
                               f"Best so far {st['best_holdout']} (experiment {st['best_n']}).")


def stop_reason(c: dict, st: dict) -> str | None:
    n = len(st.get("experiments") or [])
    if c["target"] not in ("", None) and st.get("best_holdout") is not None:
        t = float(c["target"])
        if (st["best_holdout"] <= t) if c["direction"] == "lower" else (st["best_holdout"] >= t):
            return f"reached the target {t}"
    if n >= c["max_experiments"]:
        return f"ran {n} experiments (the limit)"
    if st.get("since_improvement", 0) >= c["plateau"]:
        return f"no improvement in the last {c['plateau']} experiments"
    return None


def handle_apply(runner, job, task) -> HandlerResult:
    c = cfg(job)
    ws = workspace_of(runner, job)
    snapshot(best_dir(runner, job), ws, c["targets_list"])
    report = work_dir(runner, job) / "report.md"
    if report.exists():
        shutil.copy2(report, work_dir(runner, job).parent / "report.md")
    st = state_of(job)
    return HandlerResult(True, f"Copied the best version (experiment {st.get('best_n')}, held-out {st.get('best_holdout')}) "
                               f"of {', '.join(c['targets_list'])} into the project.")


class AutoResearch(Template):
    def initial_plan(self, runner, job):
        return _initial_plan(runner, job)

    def on_task_done(self, runner, job, task):
        c = cfg(job)
        key = task["key"]
        if key == "setup":
            add_experiment(runner, job, 1)
        elif key.startswith("m"):
            st = state_of(runner.jobs.get_job(job["id"]))
            reason = stop_reason(c, st)
            if reason:
                runner.jobs.append_tasks(job["id"], [
                    {"key": "report", "title": "Write the experiment report", "depends_on": [key],
                     "instructions": REPORT.format(reason=reason, log=render_log(st, c["direction"])),
                     "done_when": "report.md exists with the experiment table and conclusions",
                     "checks": [{"type": "file_exists", "path": "report.md"}]},
                    {"key": "accept", "title": "Your decision: apply the best version?", "kind": "gate",
                     "depends_on": ["report"],
                     "params": {"prompt": f"Experiments finished ({reason}). Best held-out score {st.get('best_holdout')} "
                                          f"vs baseline {st.get('baseline_holdout')}. Review report.md and "
                                          "jobs/<job>/experiments.md. Reply 'approve' to copy the best version into "
                                          "the project, or 'no' to leave the project unchanged."}},
                    {"key": "apply", "title": "Apply the best version", "kind": "code", "handler": "apply",
                     "depends_on": ["accept"], "instructions": "-", "done_when": "files copied"},
                ])
                runner.jobs.journal(job["id"], "plan", f"Stopping experiments: {reason}.")
            else:
                add_experiment(runner, job, int(task["params"]["n"]) + 1)

    def on_gate(self, runner, job, task, answer):
        if task["key"] == "accept" and not is_approval(answer):
            apply = next(t for t in runner.jobs.list_tasks(job["id"]) if t["key"] == "apply")
            runner.jobs.update_task(apply["id"], status="skipped")
            return "approve"          # the gate closes; applying is skipped
        return "approve" if is_approval(answer) else "revise"


AUTO_RESEARCH = register(AutoResearch(
    name="auto_research",
    label="Auto-research (experiments)",
    description="Runs one experiment at a time on a private copy: the agent proposes a change, LocalAgent measures it on "
                "held-out data and keeps or reverts it, until a limit, a plateau, or the target. You decide whether "
                "to apply the best version.",
    inputs_schema={
        "targets": {"type": "string", "label": "Files the agent may change (comma-separated)", "default": ""},
        "optimize_command": {"type": "string", "label": "Command the agent uses to score (prints the score)", "default": ""},
        "optimize_regex": {"type": "string", "label": "Regex capturing its score", "default": DEFAULTS["optimize_regex"]},
        "direction": {"enum": ["lower", "higher"], "label": "Better scores are", "default": "lower"},
        "split": {"type": "string", "label": "Held-out data: 80/20 (auto), provided, or none", "default": "80/20"},
        "data_file": {"type": "string", "label": "CSV to split (for 80/20)", "default": ""},
        "holdout_command": {"type": "string", "label": "Held-out evaluation command (uses {work}, {heldout})", "default": ""},
        "holdout_regex": {"type": "string", "label": "Regex capturing the held-out score", "default": DEFAULTS["holdout_regex"]},
        "protected": {"type": "string", "label": "Files the agent must not change", "default": ""},
        "max_experiments": {"type": "string", "label": "Max experiments", "default": "12"},
        "plateau": {"type": "string", "label": "Stop after N experiments without improvement", "default": "4"},
        "target": {"type": "string", "label": "Stop when the held-out score reaches (optional)", "default": ""},
    },
    handlers={"setup": handle_setup, "measure": handle_measure, "apply": handle_apply},
))
