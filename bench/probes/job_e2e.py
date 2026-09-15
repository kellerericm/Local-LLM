"""Phase 3a end-to-end: a generic job on sandbox/tune_me with the real model, killed mid-run and restarted.

    python -m bench.probes.job_e2e [--port 8812] [--minutes 90]

Drives the real server over HTTP like the UI does. Plan approval is automatic here (the script plays the user).
Done criterion (design §12, 3a): the job runs to completion and survives the server being killed partway.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable

GOAL = """Improve model.py so that `python evaluate.py` reports a score (RMSE) below 1.0. Read README.md for the rules.
Constraints: only change model.py; don't edit evaluate.py or data.csv; standard library only.
Work like a careful researcher: look at the data before choosing a model, change one thing at a time, and keep a log
of every experiment (hypothesis, change, score, kept or reverted) in experiments.md. Finish with the best model.py in
place and a short summary of what worked in experiments.md."""


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def start_server(port: int, data_dir: Path, logfile: Path) -> subprocess.Popen:
    out = open(logfile, "ab")
    proc = subprocess.Popen([PY, "-m", "localagent", "--no-browser", "--port", str(port), "--data-dir", str(data_dir)],
                            cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=2)
            return proc
        except httpx.HTTPError:
            time.sleep(0.5)
    raise RuntimeError("server did not start")


def kill_tree(proc: subprocess.Popen) -> None:
    parent = psutil.Process(proc.pid)
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8812)
    ap.add_argument("--minutes", type=float, default=90)
    args = ap.parse_args()

    run_dir = Path(r"D:\LocalAgent\bench-runs") / f"{dt.datetime.now():%Y%m%d-%H%M%S}_job_e2e"
    ws = run_dir / "tune_me"
    shutil.copytree(ROOT / "sandbox" / "tune_me", ws)
    protected = {p: (ws / p).read_bytes() for p in ("evaluate.py", "data.csv")}
    data_dir = run_dir / "data"
    server_log = run_dir / "server.log"
    base = f"http://127.0.0.1:{args.port}"
    c = httpx.Client(base_url=base, timeout=30)
    deadline = time.time() + args.minutes * 60
    result = {"run_dir": str(run_dir), "events": []}

    def note(event: str, **kw):
        log(event + (f" {kw}" if kw else ""))
        result["events"].append({"t": round(time.time()), "event": event, **kw})

    proc = start_server(args.port, data_dir, server_log)
    note("server started", pid=proc.pid)
    c.put("/api/settings", json={"thinking_budget": 2000, "resources": {"idle_unload_minutes": 0}})
    project = c.post("/api/projects", json={"name": "tune_me", "workspace_path": str(ws)}).json()
    job = c.post("/api/jobs", json={"project_id": project["id"], "title": "Tune the sensor model", "goal": GOAL,
                                    "budget": {"max_hours": 1.5, "max_steps": 300, "indefinite": False}}).json()
    job_id = job["id"]
    note("job created", job_id=job_id)

    killed = False
    last = None
    while time.time() < deadline:
        try:
            d = c.get(f"/api/jobs/{job_id}").json()
        except httpx.HTTPError as e:
            note("poll error", error=str(e))
            time.sleep(5)
            continue
        status = d["job"]["status"]
        tasks = d["tasks"]
        summary = (status, tuple((t["key"], t["status"], t["attempts"]) for t in tasks))
        if summary != last:
            note("state", status=status, reason=d["job"].get("status_reason"),
                 tasks=[f"{t['key']}:{t['status']}:{t['attempts']}" for t in tasks])
            last = summary
        if status == "awaiting_approval" and "plan" not in result:
            result["plan"] = [{k: t[k] for k in ("key", "parent_key", "title", "instructions", "done_when", "checks",
                                                 "depends_on")} for t in tasks]
            c.post(f"/api/jobs/{job_id}/approve")
            note("plan approved", n_tasks=len(tasks))
        elif status == "waiting_user":
            q = d["job"]["inputs"].get("pending_question") or next((t["question"] for t in tasks if t.get("question")), "")
            answer = "Use your best judgment within the README rules; no further input is available."
            task = next((t for t in tasks if t["status"] == "waiting_user"), None)
            c.post(f"/api/jobs/{job_id}/answer", json={"text": answer, "task_id": task["id"] if task else None})
            note("answered question", question=q)
        elif status in ("done", "failed", "cancelled") or (status == "paused" and killed):
            break
        # Play the user for approval pop-ups too: allow once, and record what was asked.
        for a in d.get("pending_approvals", []):
            c.post(f"/api/approvals/{a['id']}", json={"decision": "once"})
            note("approved request", summary=a["summary"], keys=a["keys"])

        # Kill the server once: after at least one task finished and another has been running a while.
        if not killed and any(t["status"] == "done" for t in tasks):
            running = [r for r in d["runs"] if r["status"] == "running" and r["kind"] == "task"]
            if running:
                msgs = c.get(f"/api/jobs/{job_id}/runs/{running[0]['id']}").json()["messages"]
                if sum(1 for m in msgs if m["role"] == "assistant") >= 2:
                    note("KILLING server mid-task", task_run=running[0]["id"], steps_so_far=len(msgs))
                    kill_tree(proc)
                    killed = True
                    time.sleep(3)
                    proc = start_server(args.port, data_dir, server_log)
                    note("server restarted", pid=proc.pid)
        time.sleep(5)

    d = c.get(f"/api/jobs/{job_id}").json()
    kill_tree(proc)
    score = subprocess.run([PY, str(ws / "evaluate.py")], capture_output=True, text=True).stdout.strip()
    holdout = subprocess.run([PY, str(ROOT / "sandbox" / "answer_keys" / "tune_me_holdout.py"), str(ws / "model.py")],
                             capture_output=True, text=True).stdout.strip()
    result.update({
        "killed_and_restarted": killed,
        "final_status": d["job"]["status"], "status_reason": d["job"].get("status_reason"),
        "usage": d["job"]["usage"],
        "tasks": [{k: t[k] for k in ("key", "title", "status", "attempts", "result_summary", "guidance", "checklist")}
                  for t in d["tasks"]],
        "context": [f"c{i['id']} ({i['author']}/{i.get('task_key')}): {i['text']}" for i in d.get("context", [])],
        "runs": [{k: r[k] for k in ("kind", "task_id", "attempt", "status", "outcome", "steps")} for r in d["runs"]],
        "interrupted_runs": sum(1 for r in d["runs"] if r["status"] == "interrupted"),
        "journal": [f"[{e.get('task_key') or '-'}] {e['text']}" for e in d["journal"]],
        "evaluate": score, "holdout": holdout,
        "forbidden_edits": any((ws / p).read_bytes() != b for p, b in protected.items()),
        "mirror_files": sorted(str(p.relative_to(ws)) for p in (ws / "jobs").rglob("*") if p.is_file()) if (ws / "jobs").exists() else [],
        "experiments_md": (ws / "experiments.md").read_text(encoding="utf-8")[:4000] if (ws / "experiments.md").exists() else None,
    })
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"FINAL status={result['final_status']} killed={killed} interrupted_runs={result['interrupted_runs']} "
        f"{score} | {holdout} | forbidden_edits={result['forbidden_edits']}")
    log(f"saved {run_dir / 'result.json'}")


if __name__ == "__main__":
    main()
