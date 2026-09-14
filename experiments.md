# Experiments

Experiments underway. Each entry: date, question, setup, status, results, conclusion.
Move a finished experiment's conclusion into CLAUDE.md's design notes if it changes the design.

## 2026-09-13 — Python 3.14 compatibility of the GPU stack
- **Question:** Does the ML stack work on the existing `local-llm` env (Python 3.14.7)?
- **Setup:** torch 2.14.0+cu130, transformers 5.17.0, bitsandbytes 0.50.2, peft 0.20.0 on an RTX 4060 Ti 16 GB (driver 610.74).
- **Status:** done
- **Result:** `torch.cuda.is_available()` is True, and a bitsandbytes `Linear4bit` forward pass on CUDA works. No env rebuild needed. (Triton isn't available on Windows; that only affects flop counting and some compiled kernels.)

## Model selection benchmark (planned)
- **Question:** Which locally runnable model (under 20 GB on disk, fits 16 GB VRAM) is the best default for agentic tool use?
- **Setup:** `python -m bench.run --model <id>` on the tasks in `bench/tasks.py`.
- **Status:** not started. Waiting on the model downloads.
