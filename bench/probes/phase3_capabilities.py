"""Probe whether the local model can fill the roles the Phase 3 design gives it.

    python -m bench.probes.phase3_capabilities [--model Qwen/Qwen3.5-9B] [--thinking-budget 3000]

P1 plan:       break a research goal into a checkable task plan (structured tool call)
P2 notes:      extract claims with verbatim quotes from a source (citation accuracy)
P3 critic:     find planted factual errors in a draft against notes
P4 experiment: propose code changes to improve a score; loop 3 rounds with real evaluator feedback

Writes D:\\LocalAgent\\bench-runs\\<stamp>_phase3_probes\\results.json and prints a summary.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from localagent.backend.toolcall_parsers import parse  # noqa: E402
from localagent.backend.transformers_backend import TransformersBackend  # noqa: E402

SANDBOX = ROOT / "sandbox"
CORPUS = SANDBOX / "lake_veyra"
OUT = Path(r"D:\LocalAgent\bench-runs")


def tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties, "required": required}}}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


class Runner:
    def __init__(self, backend, params):
        self.backend, self.params = backend, params

    def ask(self, system, user, tools):
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        text, usage = "", None
        for ch in self.backend.generate(msgs, tools, self.params):
            if isinstance(ch, dict):
                usage = ch["usage"]
            else:
                text += ch
        return parse(text, tools), usage


# ---------------------------------------------------------------- P1
PLAN_TOOL = tool("propose_plan", "Propose the task plan for this job.", {
    "tasks": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "parent_id": {"type": "string", "description": "empty for top-level"},
        "title": {"type": "string"}, "instructions": {"type": "string"},
        "done_when": {"type": "string", "description": "a concrete, checkable completion condition"},
        "depends_on": {"type": "array", "items": {"type": "string"}}},
        "required": ["id", "title", "instructions", "done_when"]}}}, ["tasks"])


def p1_plan(r: Runner):
    files = "\n".join(f"- {p.name} ({p.stat().st_size} bytes)" for p in sorted(CORPUS.iterdir()))
    user = (
        "Job goal: Write a report answering: How has phosphorus pollution in Lake Veyra changed since 2019, "
        "what drives it, and what is being done?\n\nSource documents in the workspace:\n" + files +
        "\n\nTools the worker will have for each task: read_file, grep, write_file, add_note(claim, quote, source), "
        "search_notes(query).\nEach task will run in a fresh context and must be small: at most ~15 tool calls. "
        "Later tasks only see earlier tasks' notes and output files, not their conversations.\n"
        "Propose the plan with propose_plan.")
    parsed, usage = r.ask("You plan work for an autonomous research agent. Plans must be concrete and checkable.",
                          user, [PLAN_TOOL])
    calls = [c for c in parsed.tool_calls if c["name"] == "propose_plan"]
    tasks = calls[0]["arguments"].get("tasks", []) if calls else []
    ids = {t.get("id") for t in tasks}
    bad_deps = [d for t in tasks for d in (t.get("depends_on") or []) if d not in ids]
    text = json.dumps(tasks).lower()
    return {"valid_call": bool(calls), "errors": parsed.errors, "n_tasks": len(tasks),
            "all_have_done_when": all(t.get("done_when") for t in tasks),
            "dangling_dependencies": bad_deps,
            "mentions_citations_or_quotes": any(w in text for w in ("quote", "citation", "cite")),
            "mentions_contradiction_or_conflict": any(w in text for w in ("contradict", "conflict", "disagree", "discrepan")),
            "mentions_verification": any(w in text for w in ("verify", "check", "review")),
            "plan": tasks, "usage": usage}


# ---------------------------------------------------------------- P2
NOTE_TOOL = tool("add_note", "Record one claim from the source with a verbatim supporting quote.", {
    "claim": {"type": "string"}, "quote": {"type": "string", "description": "copied exactly from the source"},
    "source": {"type": "string"}}, ["claim", "quote", "source"])


def p2_notes(r: Runner):
    results = []
    for name in ("2021_agricultural_runoff_report.txt", "2023_consultant_review.md", "trail_maintenance_notice.txt"):
        doc = (CORPUS / name).read_text(encoding="utf-8")
        user = (f"Research question: How has phosphorus pollution in Lake Veyra changed since 2019, what drives it, "
                f"and what is being done?\n\nSource: {name}\n-----\n{doc}\n-----\n"
                "Record every claim from this source that is relevant to the question, one add_note call per claim. "
                "The quote must be copied exactly from the source. If nothing is relevant, record no notes and say so.")
        parsed, usage = r.ask("You extract evidence for a research report. Never paraphrase inside quotes.", user, [NOTE_TOOL])
        notes = [c["arguments"] for c in parsed.tool_calls if c["name"] == "add_note"]
        verbatim = [norm(n.get("quote", "")) in norm(doc) and len(n.get("quote", "")) > 8 for n in notes]
        results.append({"source": name, "n_notes": len(notes), "verbatim_ok": sum(verbatim),
                        "bad_quotes": [n.get("quote") for n, ok in zip(notes, verbatim) if not ok],
                        "errors": parsed.errors, "notes": notes, "usage": usage})
    return results


# ---------------------------------------------------------------- P3
VERIFY_TOOL = tool("report_review", "Report the review result.", {
    "verdict": {"type": "string", "enum": ["pass", "fail"]},
    "issues": {"type": "array", "items": {"type": "object", "properties": {
        "claim": {"type": "string"}, "problem": {"type": "string"}, "evidence": {"type": "string"}}}}},
    ["verdict", "issues"])

NOTES_FOR_REVIEW = """[n1] 2019_baseline_survey.md: "Mean total phosphorus (TP) in the north basin was 38 µg/L during the sampling season."
[n2] 2024_monitoring_update.md: "North basin mean TP: 26 µg/L (29 in 2022)."
[n3] 2024_monitoring_update.md: "Algal bloom days: 22."
[n4] 2021_agricultural_runoff_report.txt: "we estimate that Tamsin Creek contributes about 55% of the external phosphorus load to Lake Veyra"
[n5] 2023_consultant_review.md: "we estimate that Tamsin Creek contributes roughly 30% of the external phosphorus load, not the 55% reported in 2021"
[n6] meeting_minutes_2024_03.md: "The Council will run a two-year aeration pilot in the north basin starting summer 2025"
"""
DRAFT = ("Since 2019, north basin phosphorus has fallen from 38 µg/L to 22 µg/L [n1][n2]. Tamsin Creek is the dominant "
         "source, contributing 55% of the external load, a figure all studies agree on [n4]. In response, the Council "
         "will begin a two-year aeration pilot in summer 2025 [n6].")


def p3_critic(r: Runner):
    user = (f"Notes (the only allowed evidence):\n{NOTES_FOR_REVIEW}\nDraft paragraph:\n{DRAFT}\n\n"
            "Check every factual claim in the draft against the notes. Report problems with report_review.")
    parsed, usage = r.ask("You are a strict fact-checker. Only the notes count as evidence.", user, [VERIFY_TOOL])
    calls = [c for c in parsed.tool_calls if c["name"] == "report_review"]
    args = calls[0]["arguments"] if calls else {}
    issues_text = json.dumps(args.get("issues", [])).lower()
    caught_number = "22" in issues_text and ("26" in issues_text or "bloom" in issues_text)
    caught_contradiction = "30" in issues_text or "agree" in issues_text or "consultant" in issues_text
    false_alarm_aeration = "aeration" in issues_text and "2025" in issues_text and "wrong" in issues_text
    return {"valid_call": bool(calls), "verdict": args.get("verdict"), "caught_wrong_number": caught_number,
            "caught_ignored_contradiction": caught_contradiction, "false_alarm_on_correct_claim": false_alarm_aeration,
            "issues": args.get("issues"), "usage": usage}


# ---------------------------------------------------------------- P4
EXP_TOOL = tool("propose_experiment", "Propose one change to model.py.", {
    "hypothesis": {"type": "string"}, "new_model_py": {"type": "string", "description": "the complete new file"}},
    ["hypothesis", "new_model_py"])


def score(model_code: str, work: Path) -> float:
    (work / "model.py").write_text(model_code, encoding="utf-8")
    out = subprocess.run([sys.executable, str(work / "evaluate.py")], capture_output=True, text=True, timeout=60).stdout
    m = re.search(r"score:\s*([\d.]+|inf)", out)
    return float(m.group(1)) if m else float("inf")


def p4_experiments(r: Runner, run_dir: Path, rounds: int = 3):
    src = SANDBOX / "tune_me"
    work = run_dir / "tune_me"
    shutil.copytree(src, work, dirs_exist_ok=True)
    data_head = "\n".join((src / "data.csv").read_text().splitlines()[:26:2])
    best_code = (src / "model.py").read_text()
    best = score(best_code, work)
    log = [{"round": 0, "hypothesis": "baseline", "score": best}]
    for i in range(1, rounds + 1):
        history = "\n".join(f"- round {e['round']}: {e['hypothesis']} → score {e['score']}" for e in log)
        user = (f"Task: improve model.py so evaluate.py reports a lower score (RMSE). Standard library only.\n\n"
                f"Current best model.py (score {best}):\n```python\n{best_code}\n```\n\n"
                f"Every other row of data.csv (first 26 rows):\n{data_head}\n\nExperiment log so far:\n{history}\n\n"
                "Propose ONE experiment with propose_experiment. Learn from the log; don't repeat failed ideas.")
        parsed, usage = r.ask("You run careful, incremental experiments.", user, [EXP_TOOL])
        calls = [c for c in parsed.tool_calls if c["name"] == "propose_experiment"]
        if not calls:
            log.append({"round": i, "hypothesis": "(no valid proposal)", "score": None, "errors": parsed.errors})
            continue
        code, hyp = calls[0]["arguments"].get("new_model_py", ""), calls[0]["arguments"].get("hypothesis", "")
        s = score(code, work)
        kept = s < best
        if kept:
            best, best_code = s, code
        else:
            score(best_code, work)
        log.append({"round": i, "hypothesis": hyp[:300], "score": s, "kept": kept,
                    "thinking_tokens": usage and usage["thinking_tokens"], "seconds": usage and usage["seconds"]})
    return {"baseline": log[0]["score"], "best": best, "log": log, "best_code": best_code}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3.5-9B")
    ap.add_argument("--thinking-budget", type=int, default=3000)
    ap.add_argument("--only", help="comma list of p1,p2,p3,p4")
    args = ap.parse_args()
    run_dir = OUT / f"{dt.datetime.now():%Y%m%d-%H%M%S}_phase3_probes"
    run_dir.mkdir(parents=True, exist_ok=True)
    backend = TransformersBackend(args.model, "4bit", 14.0, 8, r"D:\LocalAgent\models")
    t = time.time()
    backend.load()
    print(f"loaded in {time.time() - t:.0f}s", flush=True)
    r = Runner(backend, {"thinking": True, "thinking_budget": args.thinking_budget, "max_new_tokens": 8000,
                         "temperature": 0.6, "top_p": 0.95, "top_k": 20})
    only = set((args.only or "p1,p2,p3,p4").split(","))
    results = {"model": args.model, "thinking_budget": args.thinking_budget}
    for key, fn in (("p1", lambda: p1_plan(r)), ("p2", lambda: p2_notes(r)), ("p3", lambda: p3_critic(r)),
                    ("p4", lambda: p4_experiments(r, run_dir))):
        if key in only:
            t = time.time()
            results[key] = fn()
            results[key + "_seconds"] = round(time.time() - t)
            print(f"{key} done in {results[key + '_seconds']}s", flush=True)
            (run_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved {run_dir / 'results.json'}")


if __name__ == "__main__":
    main()
