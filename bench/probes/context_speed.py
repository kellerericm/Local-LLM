"""Measure prefill (time to first token) and decode speed vs prompt length for the local model.

    python -m bench.probes.context_speed [--model Qwen/Qwen3.5-9B] [--lengths 1000,4000,8000,16000]

Answers open_questions.md "Long-context steps are extremely slow": is it prompt length (attention kernels), or spill
to CPU? Logs GPU/CPU placement and peak VRAM for each length.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import torch  # noqa: E402

from localagent.backend.transformers_backend import TransformersBackend  # noqa: E402

FILLER = (ROOT / "sandbox" / "lake_veyra" / "2021_agricultural_runoff_report.txt").read_text(encoding="utf-8")


def prompt_of(tokenizer, n_tokens: int) -> str:
    text, ids = "", []
    while len(ids) < n_tokens:
        text += FILLER + "\n"
        ids = tokenizer(text, add_special_tokens=False)["input_ids"]
    return tokenizer.decode(ids[:n_tokens])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3.5-9B")
    ap.add_argument("--lengths", default="1000,4000,8000,16000")
    ap.add_argument("--new-tokens", type=int, default=64)
    ap.add_argument("--max-vram-gb", type=float, default=14.0)
    args = ap.parse_args()
    b = TransformersBackend(args.model, "4bit", args.max_vram_gb, 8, r"D:\LocalAgent\models")
    t = time.time()
    b.load()
    results = {"model": args.model, "load_s": round(time.time() - t), "placement": b.placement, "runs": []}
    print(json.dumps(results), flush=True)
    for n in [int(x) for x in args.lengths.split(",")]:
        torch.cuda.reset_peak_memory_stats()
        content = prompt_of(b.tokenizer, n) + "\n\nIn one sentence, what is this document about?"
        msgs = [{"role": "user", "content": content}]
        start = time.time()
        first = None
        usage = None
        for chunk in b.generate(msgs, None, {"thinking": False, "max_new_tokens": args.new_tokens, "temperature": 0.7,
                                             "top_p": 0.8, "top_k": 20}):
            if isinstance(chunk, dict):
                usage = chunk["usage"]
            elif first is None:
                first = time.time()
        end = time.time()
        row = {"prompt_tokens": usage["prompt_tokens"], "time_to_first_token_s": round((first or end) - start, 1),
               "decode_tokens": usage["completion_tokens"],
               "decode_tok_per_s": round(usage["completion_tokens"] / max(0.01, end - (first or end)), 1),
               "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2)}
        results["runs"].append(row)
        print(json.dumps(row), flush=True)
    out = Path(r"D:\LocalAgent\bench-runs") / f"{time.strftime('%Y%m%d-%H%M%S')}_context_speed.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
