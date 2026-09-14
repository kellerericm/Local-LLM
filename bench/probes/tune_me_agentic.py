"""P4b: the tune_me experiment loop, but as a tool-using agent run instead of one-shot proposals.

    python -m bench.probes.tune_me_agentic [--model Qwen/Qwen3.5-9B] [--thinking-budget 2000] [--max-steps 40]

Tests the Phase 3 design change: "investigate & propose" steps get tools (read data, run Python, run the
evaluator) rather than guessing from a prompt excerpt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from localagent.backend.worker import WorkerBackend  # noqa: E402
from localagent.config import Settings, ensure_dirs  # noqa: E402
from localagent.coordinator import Coordinator  # noqa: E402
from localagent.safety import AutoApprover  # noqa: E402
from localagent.store import Store  # noqa: E402
from localagent.tools import default_registry  # noqa: E402

PROMPT = """Improve model.py so that `python evaluate.py` reports a lower score (RMSE). Read README.md for the rules.

Work like a careful researcher:
1. Look at the data first (all of it, e.g. with a short Python analysis) before guessing a model.
2. Run evaluate.py to get the baseline.
3. Try one idea at a time. After each change run evaluate.py. Keep the change only if the score went down; otherwise restore the previous model.py.
4. Keep a short log in experiments.md: hypothesis, change, score, kept or reverted.
Stop when the score is below 0.35 or after about 6 experiments, then summarize what worked."""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3.5-9B")
    ap.add_argument("--thinking-budget", type=int, default=2000)
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--minutes", type=float, default=30)
    args = ap.parse_args()

    run_dir = Path(r"D:\LocalAgent\bench-runs") / f"{dt.datetime.now():%Y%m%d-%H%M%S}_tune_me_agentic"
    ws = run_dir / "tune_me"
    shutil.copytree(ROOT / "sandbox" / "tune_me", ws)
    evaluator_before = (ws / "evaluate.py").read_bytes() + (ws / "data.csv").read_bytes()

    settings = Settings(data_dir=str(run_dir / "data"), model_id=args.model, max_steps=args.max_steps,
                        thinking_budget=args.thinking_budget, tool_timeout_s=120)
    settings.resources.pause_when_gpu_busy = False
    ensure_dirs(settings)
    store = Store(settings.db_path)
    chat = store.create_chat(store.create_project("tune_me", str(ws))["id"])["id"]
    spec = {"model_id": settings.model_id, "quantization": "4bit", "max_vram_gb": 14.5, "cpu_threads": 8,
            "cache_dir": settings.models_dir, "offload": "auto", "max_cpu_ram_gb": 12}
    backend = WorkerBackend(lambda: spec)
    events = []

    def emit(ev):
        if ev["type"] == "message" and ev["message"]["role"] == "tool":
            m = ev["message"]
            snippet = (m["content"] or "").replace("\n", " ")[:120]
            print(f"  tool {m['name']}: {'ok' if m['ok'] else 'FAIL'} {snippet}", flush=True)
        events.append(ev)

    cancel = threading.Event()
    timer = threading.Timer(args.minutes * 60, cancel.set)
    timer.start()
    t0 = time.time()
    try:
        outcome = Coordinator(backend, store, default_registry(), AutoApprover(allow=False), lambda: settings,
                              emit).run(chat, PROMPT, cancel)
    finally:
        timer.cancel()
        backend.unload(force=True)
    wall = time.time() - t0

    out = subprocess.run([sys.executable, str(ws / "evaluate.py")], capture_output=True, text=True).stdout
    m = re.search(r"score:\s*([\d.]+|inf)", out)
    final = float(m.group(1)) if m else None
    msgs = store.list_messages(chat)
    usage = [x["usage"] for x in msgs if x.get("usage")]
    result = {
        "outcome": outcome, "final_score": final, "baseline": 3.0136, "floor": 0.2919, "wall_s": round(wall),
        "model_steps": sum(1 for x in msgs if x["role"] == "assistant"),
        "tool_calls": sum(1 for x in msgs if x["role"] == "tool"),
        "evaluator_runs": sum(1 for x in msgs if x["role"] == "assistant" and x["tool_calls"]
                              and "evaluate.py" in json.dumps(x["tool_calls"])),
        "forbidden_edits": (ws / "evaluate.py").read_bytes() + (ws / "data.csv").read_bytes() != evaluator_before,
        "thinking_tokens": sum(u["thinking_tokens"] for u in usage),
        "answer_tokens": sum(u["answer_tokens"] for u in usage),
        "budget_hits": sum(1 for u in usage if u.get("thinking_budget_hit")),
        "experiments_md": (ws / "experiments.md").read_text(encoding="utf-8")[:3000] if (ws / "experiments.md").exists() else None,
        "final_model_py": (ws / "model.py").read_text(encoding="utf-8"),
        "final_message": msgs[-1]["content"] if msgs else None,
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "transcript.json").write_text(json.dumps(msgs, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("experiments_md", "final_model_py", "final_message")}, indent=2))
    print(f"saved {run_dir}")
    store.close()


if __name__ == "__main__":
    main()
