"""Run the agentic benchmark against one model.

    python -m bench.run --model Qwen/Qwen3-8B [--quant 4bit] [--tasks create_file,fix_bug] [--no-thinking]

Results go to D:\\LocalAgent\\bench-runs\\<timestamp>_<model>\\ (results.json, summary.md, per-task transcripts).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localagent.backend.worker import WorkerBackend  # noqa: E402
from localagent.config import Settings, ensure_dirs  # noqa: E402
from localagent.coordinator import Coordinator  # noqa: E402
from localagent.safety import AutoApprover  # noqa: E402
from localagent.store import Store  # noqa: E402
from localagent.tools import default_registry  # noqa: E402

from bench.tasks import TASKS  # noqa: E402

RUNS_DIR = Path(r"D:\LocalAgent\bench-runs")


class TimedBackend:
    """Wraps a backend to measure generation time and output size."""

    def __init__(self, inner):
        self.inner = inner
        self.gen_seconds = 0.0
        self.chars = 0
        self.first_load_seconds = None

    def generate(self, messages, tools, params, adapter=None, cancel=None, on_status=None):
        loading = [None]

        def status(s):
            if s == "loading_model":
                loading[0] = time.time()
            elif s == "generating" and loading[0] and self.first_load_seconds is None:
                self.first_load_seconds = time.time() - loading[0]
            if on_status:
                on_status(s)

        started = None
        for chunk in self.inner.generate(messages, tools, params, adapter, cancel, status):
            if started is None:
                started = time.time()
            if isinstance(chunk, str):
                self.chars += len(chunk)
            yield chunk
        if started:
            self.gen_seconds += time.time() - started

    def __getattr__(self, name):
        return getattr(self.inner, name)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--quant", default="4bit", choices=["none", "4bit", "8bit"])
    ap.add_argument("--tasks", help="comma-separated task ids (default: all)")
    ap.add_argument("--no-thinking", action="store_true")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--task-minutes", type=float, default=15)
    ap.add_argument("--max-vram-gb", type=float, default=14.5)
    args = ap.parse_args()

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / f"{stamp}_{args.model.replace('/', '_')}{'_nothink' if args.no_thinking else ''}"
    run_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=str(run_dir / "data"), model_id=args.model, quantization=args.quant,
                        thinking=not args.no_thinking, max_steps=args.max_steps, tool_timeout_s=120)
    settings.resources.max_vram_gb = args.max_vram_gb
    settings.resources.pause_when_gpu_busy = False
    ensure_dirs(settings)
    store = Store(settings.db_path)
    spec = {"model_id": settings.model_id, "quantization": settings.quantization,
            "max_vram_gb": settings.resources.max_vram_gb, "cpu_threads": settings.resources.cpu_threads,
            "cache_dir": settings.models_dir}
    backend = TimedBackend(WorkerBackend(lambda: spec))
    selected = [t for t in TASKS if not args.tasks or t.id in args.tasks.split(",")]
    results = []
    try:
        for task in selected:
            ws = run_dir / "workspaces" / task.id
            if ws.exists():
                shutil.rmtree(ws)
            ws.mkdir(parents=True)
            task.setup(ws)
            project = store.create_project(task.id, str(ws))
            chat = store.create_chat(project["id"], task.id)["id"]
            approver = AutoApprover(allow=task.allow_approvals)
            events = []
            coord = Coordinator(backend, store, default_registry(), approver, lambda: settings, events.append)
            cancel = threading.Event()
            timer = threading.Timer(args.task_minutes * 60, cancel.set)
            timer.start()
            before_s, before_c = backend.gen_seconds, backend.chars
            t0 = time.time()
            print(f"[{task.id}] running…", flush=True)
            outcome = coord.run(chat, task.prompt, cancel)
            timer.cancel()
            wall = time.time() - t0
            msgs = store.list_messages(chat)
            assistant = [m for m in msgs if m["role"] == "assistant"]
            tool_msgs = [m for m in msgs if m["role"] == "tool"]
            bad_calls = sum(1 for m in msgs if m["kind"] == "coordinator" and "could not be understood" in (m["content"] or "")) + \
                sum(1 for m in tool_msgs if (m["content"] or "").startswith(("Invalid arguments", "Unknown tool", "Bad arguments")))
            final = assistant[-1]["content"] if assistant else ""
            info = {"outcome": outcome, "final": final, "tasks": store.get_tasks(chat), "approvals": len(approver.requests)}
            if cancel.is_set() and outcome == "cancelled":
                info["outcome"] = "timeout"
            try:
                passed, note = task.check(ws, info)
            except Exception as e:
                passed, note = False, f"check crashed: {e}"
            gen_s, chars = backend.gen_seconds - before_s, backend.chars - before_c
            row = {"task": task.id, "passed": passed, "note": note, "outcome": info["outcome"], "steps": len(assistant),
                   "tool_calls": len(tool_msgs), "tool_failures": sum(1 for m in tool_msgs if not m["ok"]),
                   "malformed_or_invalid_calls": bad_calls, "wall_s": round(wall, 1),
                   "approx_tokens_per_s": round(chars / 3.5 / gen_s, 1) if gen_s else None}
            results.append(row)
            print(f"[{task.id}] {'PASS' if passed else 'FAIL'} ({note}) outcome={row['outcome']} steps={row['steps']} "
                  f"wall={row['wall_s']}s tok/s≈{row['approx_tokens_per_s']}", flush=True)
            (run_dir / f"transcript_{task.id}.json").write_text(json.dumps(msgs, indent=2, default=str), encoding="utf-8")
    finally:
        backend.unload(force=True)
        store.close()

    passed = sum(r["passed"] for r in results)
    summary = {"model": args.model, "quant": args.quant, "thinking": not args.no_thinking, "date": stamp,
               "passed": passed, "total": len(results), "load_seconds": backend.first_load_seconds,
               "total_wall_s": round(sum(r["wall_s"] for r in results), 1), "results": results}
    (run_dir / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = [f"### {args.model} ({args.quant}, thinking {'on' if not args.no_thinking else 'off'}) — {passed}/{len(results)} passed",
             f"Load ≈ {summary['load_seconds'] and round(summary['load_seconds'])}s, total wall {summary['total_wall_s']}s", "",
             "| task | pass | outcome | steps | tool calls | failures | bad calls | wall s | tok/s≈ | note |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['task']} | {'✅' if r['passed'] else '❌'} | {r['outcome']} | {r['steps']} | {r['tool_calls']} | "
                     f"{r['tool_failures']} | {r['malformed_or_invalid_calls']} | {r['wall_s']} | {r['approx_tokens_per_s']} | "
                     f"{str(r['note'])[:80].replace('|', '/')} |")
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nSaved to {run_dir}")


if __name__ == "__main__":
    main()
