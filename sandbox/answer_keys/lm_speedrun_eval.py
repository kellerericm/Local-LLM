"""Official, hidden scoring for sandbox/lm_speedrun. Kept outside the agent's workspace.

    python sandbox/answer_keys/lm_speedrun_eval.py <workspace> [--budget 300] [--mem-gb 3] [--split val|test] [--no-train]

1. Runs <workspace>/train.py with TIME_BUDGET_S=budget, a hard timeout of budget+90 s, and a CUDA memory cap applied
   before train.py's code runs.
2. Loads model.pt via train.load_model and scores bits-per-byte on hidden data from D:\\LocalAgent\\datasets\\lm_speedrun:
   val.bin (keep/revert decisions, 3-way split) or test.bin (default / final score).
Prints JSON: {"split", "bpb", "train_seconds", "timed_out", ...}.
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import torch

HIDDEN = Path(r"D:\LocalAgent\datasets\lm_speedrun")
ROOT = Path(__file__).resolve().parents[2]

LAUNCHER = """
import os, runpy, sys, torch
if torch.cuda.is_available():
    total = torch.cuda.get_device_properties(0).total_memory
    torch.cuda.set_per_process_memory_fraction(min(1.0, float(os.environ["MEM_CAP_GB"]) * 2**30 / total))
sys.argv = ["train.py"]
runpy.run_path("train.py", run_name="__main__")
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace")
    ap.add_argument("--budget", type=float, default=300)
    ap.add_argument("--mem-gb", type=float, default=3.0)
    ap.add_argument("--split", choices=["val", "test"], default="test")
    ap.add_argument("--no-train", action="store_true")
    args = ap.parse_args()
    ws = Path(args.workspace).resolve()
    result = {"split": args.split, "budget_s": args.budget}

    if not args.no_train:
        (ws / "model.pt").unlink(missing_ok=True)
        env = {**__import__("os").environ, "TIME_BUDGET_S": str(args.budget), "MEM_CAP_GB": str(args.mem_gb)}
        start = time.time()
        try:
            proc = subprocess.run([sys.executable, "-c", LAUNCHER], cwd=ws, env=env, capture_output=True, text=True,
                                  timeout=args.budget + 90)
            result.update(train_exit=proc.returncode, timed_out=False, train_log_tail=proc.stdout[-600:] + proc.stderr[-600:])
        except subprocess.TimeoutExpired:
            result.update(timed_out=True)
        result["train_seconds"] = round(time.time() - start, 1)
        if result.get("timed_out") or result.get("train_exit"):
            result["bpb"] = None
            print(json.dumps(result))
            return

    split_file = HIDDEN / f"{args.split}.bin"
    if not split_file.exists():
        result.update(bpb=None, error=f"{split_file} missing; run python -m bench.datasets.prepare_lm_speedrun")
        print(json.dumps(result))
        return
    sys.path.insert(0, str(ROOT / "sandbox" / "lm_speedrun"))       # for bits_per_byte
    from dev_eval import bits_per_byte
    spec = importlib.util.spec_from_file_location("candidate_train", ws / "train.py")
    train = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(train)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        model = train.load_model(ws / "model.pt").to(device)
        data = torch.frombuffer(bytearray(split_file.read_bytes()), dtype=torch.uint8).long()
        result["bpb"] = round(bits_per_byte(model, data, int(train.BLOCK_SIZE), device, max_bytes=500_000), 4)
    except Exception as e:
        result.update(bpb=None, error=f"{type(e).__name__}: {e}")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
