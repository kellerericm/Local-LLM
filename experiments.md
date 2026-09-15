# Experiments

Experiments underway. Each entry: date, question, setup, status, results, conclusion.
Move a finished experiment's conclusion into CLAUDE.md's design notes if it changes the design.

## 2026-09-13 — Python 3.14 compatibility of the GPU stack
- **Question:** Does the ML stack work on the existing `local-llm` env (Python 3.14.7)?
- **Setup:** torch 2.14.0+cu130, transformers 5.17.0, bitsandbytes 0.50.2, peft 0.20.0 on an RTX 4060 Ti 16 GB (driver 610.74).
- **Status:** done
- **Result:** `torch.cuda.is_available()` is True, and a bitsandbytes `Linear4bit` forward pass on CUDA works. No env rebuild needed. (Triton isn't available on Windows; that only affects flop counting and some compiled kernels.)

## 2026-09-15 — Phase 3b validation: research_report template on sandbox/lake_veyra (Qwen3.5-9B, pre-quantized copy)
- **Question:** Does the research_report template meet the 3b criterion (recall ≥ 80%, 100% verbatim quotes) with the real model, including a mid-task kill?
- **Setup:**
  - Command: `python -m bench.probes.job_e2e --workload lake_veyra --template research_report --minutes 180 --budget-hours 3`.
  - The script approves gates like a user.
  - Raw results: `D:\LocalAgent\bench-runs\20260914-235043_job_e2e_lake_veyra_research_report\`.
- **Status:** done. **Passed.**
- **Results:**
  - 17/17 tasks done in 1 h 45 min (213 model steps). The server was killed during r2; the task resumed and completed.
  - **Fact recall 11/11**; both conflicting estimates presented; irrelevant trail notice not cited; 7 sources cited.
  - **Quotes: 35/35 verbatim**, independently re-verified against the sources. 32 of the notes are cited in the 16k-character report.
  - **Reviewer:** 9 reviews: 5 pass, 2 fail, 2 ran out of steps (accepted by default).
    - Both failures were real errors, fixed on retry: section s5 missed citations for its key explanation; the summary cited the 2022 value (29) as evidence for "26 by 2024".
- **Fixes from this run:** the reviewer gets 20 steps and a budget hint, and the no-steps-left wrap-up now offers `report_review`, so it always records a verdict (`test_reviewer_out_of_steps_still_records_a_verdict`).
- **Speed:** reading and note-taking ran 1–2.5 min per document; drafting and reviewing sections 6–23 min each, which is the bulk of the time.

## 2026-09-14 — Speed vs prompt length (Qwen3.5-9B, 4-bit, RTX 4060 Ti 16 GB)
- **Question:** Why did a long-context job step take 15+ minutes?
- **Setup:** `python -m bench.probes.context_speed`; thinking off; 32–64 new tokens; model fully on GPU (7.2 GB of weights).
- **Results:**

  | Prompt tokens | Time to first token | Decode tok/s | Peak VRAM |
  |---|---|---|---|
  | 1,023 | 1.3 s | 18.2 | 7.7 GB |
  | 4,023 | 2.5 s | 18.6 | 8.5 GB |
  | 8,023 | 4.9 s | 16.1 | 9.5 GB |
  | 16,023 | 10.1 s | 19.3 | 11.7 GB |
  | 22,023 | 15.6 s | 20.1 | 13.3 GB |
  | **26,023** | **176.2 s** | 18.5 | 14.3 GB |
  | 30,023 | 315.4 s | 17.2 | 15.4 GB |

- **Conclusion:**
  - The attention fallback kernels are not the bottleneck: prefill scales roughly linearly and decode is flat.
  - Once peak memory passes what's left of the 16 GB card (~14 GB), the Windows driver spills to system RAM and prefill becomes ~11× slower.
  - Default context window set to 20,000 tokens.

## 2026-09-14 — Phase 3a end-to-end: generic job on sandbox/lake_veyra (Qwen3.5-9B)
- **Question:** Does a generic job suited to plan-following (document report) complete with the real model and survive a mid-task kill?
- **Setup:**
  - Command: `python -m bench.probes.job_e2e --workload lake_veyra --minutes 120 --budget-hours 2`, with the server run from a code snapshot.
  - Raw results: `D:\LocalAgent\bench-runs\20260914-220032_job_e2e_lake_veyra\`.
- **Status:** done. **Passed.**
- **Results:**
  - 5-task plan; the server was killed during t2 and restarted. The job finished with status `done` (5/5), 30 model steps, 1 interrupted run.
  - report.md scored against the answer key:
    - **fact recall 10/11** (missed only the 2022 dry-year caveat)
    - **both conflicting estimates** presented (55% vs 30%), with the methodological reason explained
    - irrelevant trail notice not cited; 7 relevant sources cited by file name
- **Problem: speed on long context.**
  - t1–t4 took ~9 minutes combined (about 20–40 s per step).
  - **t5 (review the whole report) took 64 minutes for 7 steps**; one generation ran over 15 minutes. Its context held the full report plus re-read sources.
  - VRAM read 14.2 GB against a 14.5 GB cap during that step.
  - Leading suspects: the reference (non-kernel) implementation of Qwen3.5's gated-delta-rule attention, which scales badly with prompt length, and/or KV growth pushing layers to CPU.
  - Tracked in open_questions.md. This must be fixed before long research jobs are practical.

## 2026-09-14 — Phase 3a end-to-end: generic job on sandbox/tune_me (Qwen3.5-9B)
- **Question:** Does a generic job run to completion with the real model, survive a server kill mid-task, and produce a good result?
- **Setup:**
  - Script: `bench/probes/job_e2e.py`, driving the real server over HTTP and playing the user (approves the plan, answers questions and approvals).
  - Settings: reasoning budget 2000; job budget 1.5 h / 300 steps; 90-minute test limit.
  - Raw results: `D:\LocalAgent\bench-runs\20260914-184352_job_e2e\` (run 1), `…\20260914-202308_job_e2e\` (run 2).
- **Run 1 (before fixes):**
  - Planning failed twice by guessing a `tune_me/` subfolder.
  - After the kill and restart, the resumed task asked for approval, and **the whole runner blocked on it for 68 minutes**.
  - Led to non-blocking job approvals, a workspace listing in planning prompts, and the scratchpad.
- **Run 2 (with fixes):**
  - Infrastructure ✅:
    - planned on the first try (8 tasks)
    - kill and restart mid-task: the task resumed with its checklist and completed
    - an approval parked task t2 while t3 ran; approving re-queued t2, which completed
    - the scratchpad was used: 13 context items, checklists on every task
  - Outcome ❌: not finished in 90 minutes (5/8 tasks, 130 steps, ~39 s/step), and **final model.py scored 5.38, worse than the 3.01 baseline**.
- **Why the result was bad:**
  1. **No keep/revert.** Each "try X" task overwrote model.py whatever its score.
  2. **Checks that can't fail.** Most tasks used `command_ok: python evaluate.py`, which exits 0 regardless of score. Plan lint accepted it, and "New score recorded" passed the concreteness heuristic because it mentions "score".
  3. **Wrong analysis propagated.** t2 counted noise local maxima as 66 peaks (frequency ~6.67 vs a true ~0.13 cycles/unit) and wrote it to context. Later tasks trusted it, and no reviewer questioned it.
  4. **Scope creep.** t1 ("get baseline") ran experiments, hit the 30-step limit, and recorded a misleading context note.
- **Conclusions:**
  - 3a's mechanics (resume, non-blocking approvals, scratchpad, chat/job coexistence) work with the real model.
  - Generic jobs are not suitable for optimization work. That needs the `auto_research` template (3c): coordinator-owned keep/revert, measured scores, a held-out split.
  - 3b's reviewer should check claims written to context, not just final outputs.
  - Plan lint should reject checks that can't fail when the goal has a measurable target.
  - Speed (~39 s/step) makes long jobs slow; the kernel question in open_questions.md matters more now.

## 2026-09-14 — Phase 3 capability probes (Qwen3.5-9B)
- **Question:** Can the default model fill the roles the Phase 3 job design gives it (planning, note-taking with quotes, reviewing, experimenting)?
- **Setup:**
  - Scripts: `bench/probes/phase3_capabilities.py` (P1–P4) and `bench/probes/tune_me_agentic.py` (P4b).
  - Workloads: `sandbox/`. Settings: 4-bit, thinking on, reasoning budget 3000 (2000 for P4b).
  - Raw results: `D:\LocalAgent\bench-runs\20260914-101039_phase3_probes\`, `…\20260914-102404_tune_me_agentic\`.
- **Status:** done.
- **Results:**
  - **P1 planning:** a valid plan, but shallow: 5 flat tasks, vague "done when" conditions, and no step for conflicting sources. (63 s)
  - **P2 notes:** 13/13 quotes verbatim, and correctly no notes from the irrelevant document. Slow: 186 s on one short document with 2.2k thinking tokens.
  - **P3 reviewer:** caught both planted errors, no false alarms. (51 s)
  - **P4 one-shot experiments:** no improvement over 3 rounds (guessed from a data excerpt).
  - **P4b tool-using experiments:** 3.01 → 0.327 in 6.5 min. But its own log omitted a broken attempt, and hidden checks show overfitting: 0.37 inside the data range, 15.6 on extrapolation (baseline 2.95, true function 0.31).
- **Conclusions (applied to docs/design/phase3_long_running_tasks.md):**
  - Templates build plan structure in code; model-written plans get linted.
  - Note-taking and review are reliable enough to build on.
  - Auto-research proposals must be tool-using sessions. The coordinator owns the experiment log and decides keep/revert on a validation check the agent can't see.
- **Follow-ups:**
  - Measure deep-read speed and accuracy with a ~1k reasoning budget.
  - Optimized linear-attention kernels (open_questions.md).

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
