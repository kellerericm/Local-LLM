# Phase 3 design: long-running tasks ("jobs")

Status: **draft for review** · 2026-09-14
Related: CLAUDE.md (Design), open_questions.md, experiments.md (probe results), `sandbox/` (test workloads)

## 1. Problem
Milestone 1 handles one sitting: a chat turn runs until the model answers, asks, fails repeatedly, or hits 60 steps. Long tasks break that model in five ways:
1. **Context:** a 9B model with a 32k window can't hold hours of work, and the current overflow handling throws information away.
2. **Coherence:** small models drift over long transcripts. They forget the goal, repeat work, or quietly stop partway.
3. **Durability:** a crash, reboot, or model unload loses the run.
4. **Unattended operation:** nothing runs in the background, respects allowed hours, or keeps going while one part waits for the user.
5. **Quality control:** "done" is whatever the model says.

The target use cases from CLAUDE.md:
- **Research → report:** search documents while taking notes, then compile the notes into a report.
- **Auto-research:** iterative design improvement and experimentation on a script, formula, schematic, or similar.

## 2. Core principle
**The coordinator owns the structure; the model fills in bounded steps with fresh context.**

- The model never carries the whole job in its head. The job's state lives in the database and files.
- Each step gets a freshly built prompt containing only what that step needs: the goal, where it sits in the plan, its instructions, relevant notes, and relevant outputs.
- Progress is checkpointed after every tool call.
- "Done" is decided by checks where possible and by a separate reviewer pass otherwise, never by the worker's own say-so.

This matches how the benchmark went: Qwen3.5-9B is reliable on short, well-specified tool tasks (10/10) and weaker when it has to hold everything at once.

## 3. Concepts

### 3.1 Job
A persistent unit of long-running work belonging to a project.

| Field | Meaning |
|---|---|
| goal | What the user wants, in their words, plus clarified success criteria |
| template | `research_report`, `auto_research`, or `generic` (see §6) |
| inputs | Template parameters, e.g. source folders and output format, or target file and evaluator command |
| status | draft → awaiting_plan_approval → queued → running ⇄ paused → done / failed / cancelled. Also `waiting_user` when *every* runnable task is blocked on the user |
| budget | Max wall-clock hours, model steps, and tokens; optional deadline. On exhaustion the job stops and writes a progress report, rather than being cut off silently |
| schedule | `now` or `background_hours` (uses Settings → Resources) · priority vs. interactive chats |
| permissions | Pre-approved approval keys for this job (e.g. "network: allowed"), granted at launch so unattended runs don't stall |
| origin_chat | The chat it was created from. Progress summaries are posted back there |

### 3.2 Plan tree
Tasks form a tree, and only leaves are executed.

| Field | Meaning |
|---|---|
| title, instructions | What to do |
| done_when | A concrete completion condition, e.g. "notes/2021_report.md exists with ≥1 note per relevant claim; all quotes verbatim" |
| checks | Machine checks run before review (§5): file exists, command exits 0, quotes verbatim, JSON valid, score improved… |
| inputs / outputs | References to artifacts and note queries it reads, and artifacts it must produce |
| depends_on | Sibling task ids that must finish first |
| status | pending → ready → running → checking → done · failed · blocked · skipped |
| attempts | Tries so far, each with its run transcript and a failure summary; max 3 by default |
| result_summary | ≤150 words written when the task completes. It's what later tasks see instead of the transcript |

Planning rules, enforced by the coordinator:
- max depth 3, max 8 children per node, max ~60 leaves per plan
- every leaf needs `done_when`; dependencies must reference real ids
- a leaf must fit a fresh context in ≤ ~15 tool calls, otherwise it gets split

**Who writes the plan** (confirmed by probe P1, §13):
- **Templates** generate the structure in code, e.g. one "read and take notes" task per document, each with machine-checkable `done_when` and `checks`.
- **The model** fills in the parts that need judgment: the report outline, experiment ideas, or the whole tree for `generic` jobs.
- **Plan lint for model-written plans.** Reject and ask for a rewrite when a plan has:
  - vague `done_when` ("has been read", "identified"). Each condition must name an artifact, a note count, a command, or a check.
  - tasks that bundle several sources or deliverables
  - no step for conflicts or verification when the goal involves multiple sources

**Plan approval.** By default the user approves the initial plan and can rename, add, remove, or skip tasks first.

**Replanning** happens only at defined points:
- a task fails its attempts
- the reviewer rejects a result twice
- a template phase ends and hands off to the next

A replan may retry with guidance, split a task, insert a prerequisite, skip, or mark the task blocked and ask the user. Replans count against the budget, max 5 per job by default.

### 3.3 Task runs
Executing a leaf reuses the Milestone 1 agent loop (tool calls, validation, safety, approvals, timeouts) inside a **task-scoped conversation** instead of a chat.

Prompt layout for Qwen3.5-9B with a 32k window. Numbers are budgets; the coordinator trims to fit.

| Part | Budget |
|---|---|
| System prompt + tool schemas | ~3k |
| Job header: goal, success criteria, constraints | ≤500 |
| Plan outline, compressed, with "you are here" and sibling results | ≤1k |
| Task spec: instructions, `done_when`, checks, previous attempts' failure summaries | ≤1k |
| Retrieved notes (§4.2) | ≤4k |
| Input artifact excerpts | ≤4k |
| Working transcript (elided as it grows) | remainder, ~14k |
| Reply | ≤4k (reasoning budget per step type, see §9) |

Job-only tools added to the task's toolset:
- `add_note(claim, quote, source, location, tags)`: evidence with a verbatim quote, checked on write (§5).
- `search_notes(query, k)`: full-text search over this job's notes.
- `read_artifact` / `write_artifact`: files under the job folder, registered with a description.
- `complete_task(summary, outputs)`: ends the run and triggers checks → review.
- `fail_task(reason, what_would_help)`: honest failure. It feeds replanning and counts as a normal outcome, not an error.
- `ask_user(question)`: blocks **only this task**; other ready tasks keep running.

Transcripts are kept for audit, debugging, and Phase 4 training data, but never fed wholesale into later tasks.

### 3.4 Scheduler
A single `JobRunner` thread in the server:
- **One model, one step at a time.** Interactive chats preempt jobs *between model steps*. A job never makes you wait more than one generation (~seconds to a minute) for a chat reply.
- **Task choice:** picks the next ready leaf by depth-first order within a job and round-robin across running jobs.
- **Resource rules:** respects background hours, the GPU-busy pause (already token-level), and idle unload. Jobs keep the model loaded only while running.
- **Budgets:** checked before each step. A job about to exceed one finishes its current task, then writes a progress report and pauses.
- **Resume after restart:** state is committed after every tool result. Interrupted tasks go back to `ready` with an "interrupted; here's what exists" note built from their artifacts and the last transcript. Task instructions ask for idempotent, file-based outputs so reruns are safe.

### 3.5 Human in the loop
- **Gates:** plan approval (default on), template milestones (e.g. "approve report outline", on by default for `research_report`), and any `ask_user`.
- **Approvals** from job tasks go through the existing ApprovalBroker and project rules, plus the job's pre-approved permissions. A pending approval blocks only its task.
- **Progress** is posted to the origin chat at milestones and on completion or failure, and shown in the job view. Unattended jobs never silently spin: after the budget or 3 replans without progress, they stop and explain.

## 4. Memory: what persists outside the model

### 4.1 Where things live
The database is the source of truth. Human-readable mirrors are written into the project workspace so you can read along or take over by hand:

```
<workspace>/jobs/<job-slug>/
  job.md          goal, status, budget used, links (regenerated)
  plan.md         the plan tree with statuses (regenerated)
  journal.md      append-only timeline: tasks, decisions, replans, user input, failures
  notes/          one markdown file per source, generated from the notes table
  artifacts/      everything tasks produce (drafts, experiment copies, results)
  report/         final outputs
```

### 4.2 Notes store
- Table `notes(id, job_id, project_id, task_id, claim, quote, source_path, location, tags, verified, created_at)` with a SQLite **FTS5** index (verified available: SQLite 3.53).
- Retrieval is BM25 keyword search on the task's title and instructions plus the model's `search_notes` queries, capped by token budget. This needs no embedding model competing for VRAM. Embeddings and long-term memory are Phase 5.
- Notes are scoped to the job and can optionally be promoted to project-level notes for later jobs.

### 4.3 Journal
An append-only table plus a `journal.md` mirror. It holds what happened and why, in one line each:
- "T7 failed twice: quotes not verbatim → replanned: re-read with smaller excerpts"
- "User chose to include the budget memo"

It feeds progress reports, resume notes, replanning prompts, and later fine-tuning data.

### 4.4 Artifacts
Table `artifacts(id, job_id, task_id, path, kind, description, created_at)`. Tasks reference inputs and outputs by artifact id or path, and the plan UI links to them.

## 5. Verification
The order is cheapest and most reliable first:

1. **Machine checks** declared on the task:
   - `file_exists`, `min_notes`, `quotes_verbatim` (whitespace-normalized match against the source)
   - `command_ok` (exit 0 within a timeout), `json_schema`
   - `score_improved` (for auto-research), `no_forbidden_edits` (e.g. evaluator unchanged)
2. **Reviewer pass:** a *fresh-context* model call that sees only `done_when`, the outputs, and the evidence, never the worker's transcript. It returns `pass` or `fail` with specific issues via a `report_review` tool. It's used where machine checks can't judge, e.g. whether a report section is supported by its notes and covers the outline.
3. **User review** at gates.

A failure goes back to the worker as the next attempt's guidance. Repeated failure triggers replanning (§3.2).

## 6. Templates

### 6.1 `research_report`
**Inputs:** question, source folders/files, output format (Markdown by default), optional length and audience.

| Phase | Task shape | Who does it | Checks |
|---|---|---|---|
| 1 Inventory | List sources; extract text (txt/md now; PDF/DOCX with `pypdf` / `python-docx`); chunk large files | Code | Every source has a text cache |
| 2 Triage | One task per source: skim the first chunk and rate relevance with a reason | Model, cheap settings (thinking off or small budget) | Rating recorded |
| 3 Deep read | One task per relevant source (per chunk group if large): `add_note` for each relevant claim | Model, reasoning budget ~1k (P2: 13/13 verbatim, but 186 s on one document unbudgeted) | `quotes_verbatim`, ≥1 note or an explicit "nothing relevant" |
| 4 Synthesize | Cluster notes; draft outline; flag contradictions between sources | Model + reviewer | Outline covers the question; every conflict found is listed · **gate: user approves outline** |
| 5 Draft | One task per section: retrieve notes, write with `[note-id]` citations | Model | Every citation resolves; quotes verbatim |
| 6 Verify | Reviewer per section: claims supported by cited notes? contradictions presented, not flattened? | Model (fresh) | Pass, or back to phase 5 with issues |
| 7 Compile | Assemble the report; convert note ids to source references; add a sources list | Code + model (intro/summary) | File exists; all references resolve |

### 6.2 `auto_research`
Revised after probes P4 and P4b (§13):
- One-shot proposals failed.
- Tool-using investigation succeeded, but the model's own log was incomplete, and it overfit the only score it could see.

**Inputs:**
- target file(s) and allowed files, libraries, and time per run
- **optimization evaluator:** a command that prints a metric, plus which direction is better
- **validation evaluator (required):** a separate check the proposing agent can't see or run.
  - Examples: held-out data, a different test set, extrapolation cases, physical sanity limits, or tests.
  - If the user has none, the template helps create one at setup, e.g. reserving a random 20% of the data. That split is done by code, not the model.
- stopping rule: budget, a target validation metric, or a plateau (N rounds with no validation improvement)

**Loop (code-driven; the model works inside step 2 only):**
1. **Setup:**
   - copy targets into `artifacts/work/`; the original is untouched until the user accepts
   - hide the validation data from the working copy
   - run both evaluators for a baseline
2. **Investigate & propose.** A *tool-using task session*, not a one-shot prompt. The agent can:
   - read the working copy
   - run Python analyses on the visible data
   - run the optimization evaluator as often as it likes
   - edit allowed files
   It ends with `propose_experiment(hypothesis, expected_effect)` when the working copy holds its candidate.

   Guidance the prompt always includes: analyze all visible data before guessing; prefer simple explanations; beware fitting noise; one idea per experiment. Budget per session: ~15 steps.
3. **Measure (authoritative).**
   - The coordinator itself runs both evaluators on the candidate and checks `no_forbidden_edits`.
   - It records every run to the experiment log: hypothesis, diff, optimization score, validation score, time.
   - The model's own notes are supplementary; P4b's self-written log left out a broken attempt.
4. **Decide:**
   - **Keep** only if the *validation* metric improves (optionally within a tolerance, while optimization also improves).
   - **Otherwise revert** to the best version.
   - **Overfitting warning:** a large gap between optimization and validation scores is flagged in the log and fed into the next proposal.
5. **Reflect.** Every K rounds, a fresh-context summary of what has and hasn't worked goes at the head of the log. This keeps the prompt small.
6. **Stop** on the stopping rule. The model writes a final report: best result on both metrics, what mattered, what didn't, and the overfitting risk. The user reviews a diff before anything is copied back.

Evidence for the validation requirement (P4b): the agent reached 0.327 on the visible data (floor 0.29) with a degree-5 polynomial. On hidden data it scored 0.37 inside the range but **15.6 on extrapolation, 5× worse than the untouched baseline line (2.95)**. The true function scores 0.31 on both.

### 6.3 `generic`
The model proposes the whole plan tree (validated against the planning rules), and the same run → check → review cycle applies. This is the fallback when no template fits.

## 7. Creating and watching jobs (UI)
- **From a chat:** a new `propose_job` tool lets the model suggest turning a request into a job, with goal, template, inputs, budget, and permissions. You see a card and choose *Create job* or *Edit*.
- **Directly:** Project → *New job* opens a form per template.
- **Sidebar:** each project gets a *Jobs* list with status dots, next to its chats.
- **Job view:**
  - header: goal, status, budget bars, and controls (start, pause, resume, cancel, approve plan)
  - **plan tree** with statuses; a task expands to its runs, transcript, and check/review results; you can edit pending tasks
  - **Notes** tab: searchable, each note links to its source and location
  - **Artifacts** tab, plus a diff view for auto-research
  - **Journal** timeline
  - **Questions** panel for pending `ask_user` items and approvals

## 8. Data model changes
New tables: `jobs`, `tasks`, `task_runs`, `run_messages` (same shape as `messages`, keyed by run), `notes` + `notes_fts`, `artifacts`, `journal`.
They're added with the existing migration mechanism, and existing tables are unchanged.

Code layout:
```
localagent/jobs/
  models.py      dataclasses + store methods
  runner.py      JobRunner (scheduling, budgets, resume)
  planner.py     plan validation, replanning prompts
  task_session.py  task-scoped conversation for the agent loop
  checks.py      machine checks
  review.py      reviewer pass
  memory.py      notes/FTS, journal, artifacts, markdown mirrors
  templates/     research_report.py, auto_research.py, generic.py
  tools.py       add_note, search_notes, artifacts, complete/fail_task, propose_job
```
The coordinator loop gets a small refactor: a `Conversation` interface (load messages, append, build prompt, allowed tools, end conditions) so chats and task runs share the same loop.

## 9. Model settings per step type
Uses the per-request generation settings added this week:

| Step type | Suggested default |
|---|---|
| triage, simple extraction | Fast preset (thinking off) |
| deep read, drafting | Thinking — coding preset, reasoning budget ~2k |
| planning, synthesis, review | Thinking — general or coding, reasoning budget ~4k |
| auto-research proposals | Thinking — coding, reasoning budget ~3k |

These are starting points to be tuned with measurements (open_questions.md: thinking on/off). Templates can override them per phase.

## 10. Failure handling summary
| Situation | Response |
|---|---|
| Malformed tool call / invalid args / tool error | Existing loop feedback; counts toward the task's consecutive-failure limit |
| Task hits its step cap or failure limit | Attempt ends with a failure summary → retry with that guidance (≤3 attempts) |
| Checks or reviewer reject | Next attempt gets the specific issues |
| Attempts exhausted | Replan (split, add prerequisite, skip, or ask the user) |
| `fail_task` with "what would help" | Recorded as an honest outcome; replan considers it; may become a user question |
| Approval pending or `ask_user` | Only that task blocks; others continue; job shows *waiting on you* when nothing else can run |
| Denied approval | Task told not to work around it; replan must find another route or ask |
| Budget nearly exhausted | Finish current task → progress report → pause |
| No progress over 3 replans | Stop, write "what I tried / where I'm stuck / what would help" |
| Crash, restart, unload | Resume from the last committed tool result (§3.4) |
| GPU busy or outside background hours | Pause between (or within) steps; resume automatically |

## 11. Evaluation
Extend `bench/` with long-horizon checks built on `sandbox/`:
- **research_report on `sandbox/lake_veyra`**, scored against `sandbox/answer_keys/lake_veyra.json`:
  - required-fact recall
  - citation accuracy (quotes verbatim, citations resolve)
  - contradiction handled (both estimates presented)
  - irrelevant source ignored
  - time, model steps, user interventions
- **auto_research on `sandbox/tune_me`:**
  - best score reached vs. baseline 3.01 and floor 0.29
  - **hidden holdout and extrapolation scores** (`answer_keys/tune_me_holdout.py`): the true function scores ~0.31 on both
  - rounds to reach below 1.0; no forbidden edits; revert correctness; experiment log completeness
- **Resume test:** kill the server mid-job, restart, and confirm the job completes with no duplicated notes or lost progress.
- **Preemption test:** chat latency while a job runs.

## 12. Build order
| Step | Scope | Done when |
|---|---|---|
| 3a | Data model; `Conversation` refactor; task sessions; `generic` template; plan validation; checks; JobRunner with budgets and resume; minimal job view (plan tree, controls, journal) | A generic job on `sandbox/tune_me` runs to completion and survives a restart |
| 3b | Notes + FTS5; artifacts; markdown mirrors; reviewer pass; `research_report` template (txt/md); outline gate | Lake Veyra report meets the answer key's recall ≥ 80% with 100% verbatim quotes |
| 3c | `auto_research` template; coordinator-owned experiment log; validation evaluator + data split helper; diff review | tune_me reaches < 1.0 on the hidden extrapolation check (not just the visible score) within budget, with no forbidden edits |
| 3d | `propose_job` from chat; background hours; preemption tuning; PDF/DOCX extraction; progress posts; long-horizon bench | Benchmarks recorded in experiments.md |

## 13. Evidence: capability probes on Qwen3.5-9B (2026-09-14)
Script: `bench/probes/phase3_capabilities.py`. Raw results: `D:\LocalAgent\bench-runs\20260914-101039_phase3_probes\`. Settings: 4-bit, thinking on, reasoning budget 3000.

| Probe | What it tested | Result | Design consequence |
|---|---|---|---|
| P1 plan | Decompose the Lake Veyra report goal into a checkable plan | Valid tool call, but shallow: 5 flat tasks grouping several documents each, `done_when` like "has been read", no step for conflicting sources. 63 s | Templates own structure; model plans only judgment parts; plan lint (§3.2) |
| P2 notes | Extract claims with verbatim quotes from 3 sources | **13/13 quotes verbatim**; 0 notes from the irrelevant trail notice (correct). But 186 s on one short document (2.2k thinking tokens) | Note-taking is reliable. Deep-read tasks get a tighter reasoning budget (~1k), to be measured |
| P3 reviewer | Find planted errors in a draft against notes | **Caught both** (26 vs 22 µg/L; the ignored 55% vs 30% conflict), no false alarms. 51 s | The fresh-context reviewer pass is viable |
| P4 experiments (one-shot) | 3 rounds of propose → evaluate → keep/revert from a prompt excerpt of the data | **No improvement:** 3.01 → 22.9, 59.4, 3.58 (all reverted). It guessed functional forms from 13 rows instead of analyzing the data. (The probe only showed it an early slice.) | Proposals must be tool-using investigations, not one-shot guesses |
| P4b experiments (tool-using agent) | Same problem as a normal agent run with file/Python/shell tools (`bench/probes/tune_me_agentic.py`) | **3.01 → 0.327** in 6.5 min, 20 steps, 21 tool calls, no forbidden edits. It analyzed all the data, fit a degree-5 polynomial, broke the model once (47,714) and fixed it. Its `experiments.md` omitted the broken attempt. Hidden check (`sandbox/answer_keys/tune_me_holdout.py`): 0.37 inside the range, **15.6 on extrapolation** (baseline line 2.95; true function 0.31) | Tool-using sessions work. The coordinator must own the experiment log and run a validation evaluator the agent can't see; keep/revert decisions use validation (§6.2) |

**Speed note:** Qwen3.5-9B's linear-attention layers are running on slow reference kernels. Missing `flash-linear-attention` / `causal_conv1d` is tracked in open_questions.md; fixing it would shorten every job.

## 14. Decisions needed from you
1. **Plan approval:** required before a job starts (recommended), or just shown while the job starts right away?
2. **Job files:** visible `jobs/` folder in the workspace (recommended, human-legible), or hidden under `.agent/`?
3. **Document formats for 3b:** txt/md first, then PDF and DOCX in 3d (recommended)? Anything else you rely on, e.g. scanned PDFs needing OCR, HTML, spreadsheets?
4. **Report output:** Markdown only at first (recommended), or also DOCX/PDF?
5. **Chat vs. job priority:** chats always preempt jobs between steps (recommended), or should jobs get a guaranteed share?
6. **Default budgets:** e.g. 4 hours / 400 model steps per job unless changed?
7. **Auto-research validation:** a separate validation check is required before an auto-research job can start (recommended; P4b shows why). If you don't provide one, the template holds out 20% of the data automatically. Or is a warning enough?
