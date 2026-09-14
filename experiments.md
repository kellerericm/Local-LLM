# Experiments

Experiments underway. Each entry: date, question, setup, status, results, conclusion.
Move a finished experiment's conclusion into CLAUDE.md's design notes if it changes the design.

## 2026-09-13 — Python 3.14 compatibility of the GPU stack
- **Question:** Does the ML stack work on the existing `local-llm` env (Python 3.14.7)?
- **Setup:** torch 2.14.0+cu130, transformers 5.17.0, bitsandbytes 0.50.2, peft 0.20.0 on an RTX 4060 Ti 16 GB (driver 610.74).
- **Status:** done
- **Result:** `torch.cuda.is_available()` is True, and a bitsandbytes `Linear4bit` forward pass on CUDA works. No env rebuild needed. (Triton isn't available on Windows; that only affects flop counting and some compiled kernels.)

## 2026-09-13/14 — Model selection benchmark
- **Question:** Which locally runnable model (under 20 GB on disk, fits 16 GB VRAM) is the best default for agentic tool use?
- **Setup:**
  - Command: `python -m bench.run --model <id>`, 10 tasks (`bench/tasks.py`).
  - Settings: 4-bit bitsandbytes, thinking on, max 25 steps per task, RTX 4060 Ti 16 GB.
  - Raw results: `D:\LocalAgent\bench-runs\`.
- **Status:** done.

### Results (after harness fixes; see failed_tests.md)
| Model | Disk | Passed | Total time | tok/s | Load |
|---|---|---|---|---|---|
| **Qwen/Qwen3.5-9B** | ~19 GB (bf16, quantized at load) | **10/10** | **7.3 min** | 14–22 | ~50 s |
| unsloth/Qwen3-14B-bnb-4bit | ~10 GB (pre-quantized) | 9/10 | ~21 min | 20–26 | ~28 s |
| Qwen/Qwen3-8B | ~16 GB | 10/10* | ~27 min | 19–28 | ~45 s |

\*Qwen3-8B's 10/10 combines its first full run (6 valid passes) with reruns of the 4 tasks invalidated by harness bugs. Its `task_list` pass came from a rerun; in the first run it left the task statuses stale. Treat it as 9–10/10.

### Observations
- **Qwen3.5-9B** was the most efficient by far: it finished everything in about 7 minutes, versus 21–27 minutes for the others. Its generation is slower per token, but it thinks far less, so tasks finish much sooner. It tends to issue several tool calls per message.
  - Its first run was invalid: generation didn't stop at `<|im_end|>` because the checkpoint has no generation config. Fixed.
- **Qwen3-14B** reasons well but spends a long time thinking. Its only miss was sending Python code with literal `\n` instead of line breaks, twice. The Python tool now repairs that automatically.
- **Qwen3-8B** is capable, but its long thinking makes it the slowest overall (8.5 min on one search task).
- All three respected denials once batched calls after a denial were skipped, and all asked for help when information was missing.
- Loading a bf16 checkpoint with on-the-fly 4-bit quantization briefly uses ~19 GB of system RAM. That was enough to trigger Windows' low-memory process killing, with another app server running at the same time. Pre-quantized checkpoints avoid this.

### Conclusion
- **Default model: `Qwen/Qwen3.5-9B`**, set in `config.py`.
- **Runner-up:** `unsloth/Qwen3-14B-bnb-4bit`.
- **Cleanup (2026-09-14):** Qwen3-8B and Qwen3-14B were deleted from D: at the user's request, since they're the same family as the chosen model. Future comparisons should use a different family (older Llama instruct, small Gemma). Qwen3-0.6B stays only for quick backend smoke tests.
- **Caveat:** 10 short tasks can't separate good models well. Add longer, multi-hour tasks once the Phase 3 long-running-task system exists.

### Follow-ups
- Save a pre-quantized 4-bit copy of Qwen3.5-9B to D: to cut the RAM spike and load time.
- Measure thinking on vs off for Qwen3.5-9B (speed vs pass rate).
- llama.cpp backend comparison for speed (open question: is bnb 4-bit the bottleneck?).
