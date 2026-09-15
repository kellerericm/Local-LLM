"""Save a pre-quantized (bitsandbytes 4-bit) copy of a model, so later loads skip the ~19 GB RAM spike.

    python -m localagent.backend.quantize_copy --model Qwen/Qwen3.5-9B \
        --out "D:\\LocalAgent\\models\\local\\Qwen3.5-9B-bnb-4bit"

Steps: load with on-the-fly 4-bit quantization -> record greedy reference outputs -> save weights, config, tokenizer,
and processor files -> unload -> load the saved copy -> compare outputs and peak memory. Writes verify.json next to
the copy. Keep the model family name in the folder name, so the app still recognizes its presets.
"""
from __future__ import annotations

import argparse
import gc
import json
import time
from pathlib import Path

import psutil
import torch

from .transformers_backend import TransformersBackend

PROMPTS = [
    "In two sentences, explain what long-term potentiation is.",
    "List three differences between a hash map and a binary search tree.",
    "Write a Python function that returns the n-th Fibonacci number iteratively.",
]


def outputs(backend: TransformersBackend) -> list[str]:
    results = []
    for p in PROMPTS:
        text = ""
        for chunk in backend.generate([{"role": "user", "content": p}], None,
                                      {"thinking": False, "max_new_tokens": 64, "temperature": 0}):
            if isinstance(chunk, str):
                text += chunk
        results.append(text)
    return results


def peak_rss_during(fn):
    proc = psutil.Process()
    peak = [proc.memory_info().rss]
    import threading
    stop = threading.Event()

    def sample():
        while not stop.wait(0.5):
            peak[0] = max(peak[0], proc.memory_info().rss)

    t = threading.Thread(target=sample, daemon=True)
    t.start()
    start = time.time()
    try:
        fn()
    finally:
        stop.set()
        t.join()
    return round(peak[0] / 2**30, 1), round(time.time() - start)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen3.5-9B")
    ap.add_argument("--out", default=r"D:\LocalAgent\models\local\Qwen3.5-9B-bnb-4bit")
    ap.add_argument("--cache-dir", default=r"D:\LocalAgent\models")
    args = ap.parse_args()
    out = Path(args.out)
    report = {"source": args.model, "copy": str(out)}

    original = TransformersBackend(args.model, "4bit", 14.0, 8, args.cache_dir, offload="gpu_only")
    report["original_peak_ram_gb"], report["original_load_s"] = peak_rss_during(original.load)
    reference = outputs(original)
    out.mkdir(parents=True, exist_ok=True)
    original.model.save_pretrained(out, safe_serialization=True)
    original.tokenizer.save_pretrained(out)
    try:
        from transformers import AutoProcessor
        AutoProcessor.from_pretrained(args.model, cache_dir=args.cache_dir).save_pretrained(out)
    except Exception as e:           # text-only models have no processor
        report["processor_note"] = f"{type(e).__name__}: {e}"
    del original
    gc.collect()
    torch.cuda.empty_cache()

    copy = TransformersBackend(str(out), "4bit", 14.0, 8, None, offload="gpu_only")
    report["copy_peak_ram_gb"], report["copy_load_s"] = peak_rss_during(copy.load)
    candidate = outputs(copy)
    report["identical_outputs"] = [a == b for a, b in zip(reference, candidate)]
    report["samples"] = [{"prompt": p, "original": a[:200], "copy": b[:200]} for p, a, b in zip(PROMPTS, reference, candidate)]
    report["disk_gb"] = round(sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 2**30, 2)
    (out / "verify.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "samples"}, indent=2))


if __name__ == "__main__":
    main()
