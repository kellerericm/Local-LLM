# Open questions

## Even longer-term memory
There is probably some structure that can be added to the LLM, or modified inside it, to extend its memory. The analogy is a human's long-term memory versus short-term memory (like context). Its form is unknown and hasn't been researched yet.

Directions to look into (unevaluated):
- Retrieval over a persistent notes store (RAG). This is the cheapest option and needs no model changes.
- Learned memory tokens or a compressed summary state carried between sessions.
- Per-project LoRA adapters trained on accumulated notes. This ties into the specialist-model systems (Phase 4).
- Architectural memory modules (memory layers or key-value stores attached to the model's internals).

## Tool-call reliability of small local models
How much coordinator scaffolding (argument repair, retries, constrained decoding) is needed before an 8–14B model follows multi-step task lists reliably? The model benchmark should answer this.

## Pre-quantized copy of the default model
Qwen3.5-9B ships as bf16 (~18 GB) and is quantized to 4-bit on every load. That costs ~50 s and a ~19 GB spike in system RAM, which was enough to get another process killed during benchmarking. Saving a 4-bit copy to `D:\LocalAgent\models` once should cut both.
- Does `save_pretrained` round-trip a bnb-4bit *multimodal* checkpoint correctly in transformers 5, with the vision tower left unquantized?
- Is output identical? Spot-check against the bf16 load and rerun the benchmark.
- Where does the copy go, and how does the (future) model manager treat it: as a variant of the original model, or as its own entry?

## Long-context steps are extremely slow (priority)
In the Lake Veyra job run, a task whose prompt held a whole report plus sources took 64 minutes for 7 steps; one generation took over 15 minutes. Short-context steps take 20–40 s. Research jobs routinely have long contexts, so this blocks practical deep research.
- **Measure:** time to first token and tokens/s vs prompt length (2k, 8k, 16k, 32k) for Qwen3.5-9B 4-bit, with the model's GPU/CPU placement logged.
- **Suspects:** (a) the reference PyTorch implementation of the gated-delta-rule layers (below); (b) KV cache growth pushing the model partly onto CPU under the 14 GB VRAM cap; (c) the reasoning budget not bounding total output.
- **Options:** optimized kernels; a llama.cpp backend; a smaller context window per task (fit_messages budget), with long material read in pieces; a lower VRAM cap for weights with more room for KV; GPU-only offload mode so spillover fails loudly instead of silently slowing down.

## Optimized kernels for Qwen3.5's linear-attention layers
While running Qwen3.5-9B, transformers warns that `chunk_gated_delta_rule` / `fused_recurrent_gated_delta_rule` (needs `flash-linear-attention`) and `causal_conv1d_fn` / `causal_conv1d_update` (needs `causal_conv1d`) fall back to "much slower" reference PyTorch code. Both packages depend on Triton and CUDA builds, which are awkward on Windows.
- Is there a working path on Windows + Python 3.14? For example `triton-windows` plus `flash-linear-attention`, prebuilt wheels, or WSL.
- How much faster does it get? Qwen3.5-9B measured 14–22 tok/s vs 20–28 for the attention-only Qwen3-8B. The kernels may close or reverse that gap.
- Is a llama.cpp backend (native kernels for this architecture) the simpler route to speed?

## Thinking on vs. off (and reasoning budget) for agent work
Qwen3.5-9B passed 10/10 with thinking on. Unknowns:
- how much faster it is with thinking off
- what it costs in pass rate
- whether a reasoning budget (e.g. 1–2k tokens) gets most of the benefit at a fraction of the time

This probably differs between short tool steps and planning/synthesis steps, so the Phase 3 job runner may want different settings per step type. Measure with the benchmark (thinking on / off / budgeted) and later with long-running tasks.

## Approval process for paper downloads in unattended research jobs (return to later)
The `deep_research` template (design §6.4) downloads open-access papers while running unattended. Today's options: the job-level "network" pre-approval (broad), or per-download approval requests (each parks a paper until answered). Neither is right yet. To decide:
- **Scope:** allowlist specific hosts (arxiv.org, api.openalex.org, api.semanticscholar.org, known OA repositories) per job, rather than all network access? Should publisher domains need a separate approval?
- **Granularity:** approve the seed list and "follow citations" once, or each round's download list (with counts and sources shown)?
- **Limits and etiquette:** download caps per job, rate limiting and a polite User-Agent for APIs, and honoring terms of use and robots rules.
- **Provenance:** record where each file came from (URL, license or OA status, time) in the job, and show it in the report's bibliography.
- **Safety:** downloaded PDFs are untrusted input. Text extraction only, never executing embedded content. Size limits. Should instructions found inside papers be ignored (prompt-injection risk)?

## Sandbox strength
The command policy is pattern-based and best-effort. Is a stronger isolation layer worth it on Windows (Windows Sandbox, a restricted token, a separate low-privilege user account)?
