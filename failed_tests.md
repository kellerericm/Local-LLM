# Failed tests

Record failures here: date, what was run, what happened (exact error), suspected cause, status (open / fixed / won't fix).
A failure is information, not a verdict.

## 2026-09-21 → 09-25 — deep_research round 1: the first reading task never finished (the job plan filled the window)
- **Run:** `D:\LocalAgent\bench-runs\20260919-002111_job_e2e_empty_deep_research`, job literature-review-241d9c. Round 0 was finished (10 papers, every part passed); round 1 kept and fetched 44 papers. Task `p1_1_1` (Kirkpatrick et al., EWC, part 1/2) ran from 2026-09-21 08:02 and was resumed about 9 times over 4 days without finishing.
- **What happened (run 6287c2d6a19d, 380 messages):** the model wrote the summary and sent note batches, but it never saw its own earlier turns. It rewrote summary-01.md 16 times, called add_note 303 times (notes n187–n378, mostly repeats of the same claims), hit the 4,200-token output ceiling on batch after batch, and called complete_task once.
- **Cause:** the prompt was 20,587 tokens **at step 1**, against a 20,000 window, before any history existed. Measured with the Qwen tokenizer: the job task list (all 281 tasks) was 11,518 tokens; the section being read was 6,253; everything else was about 2,700. `TaskSession.system_prompt` puts the whole job plan in every task's prompt. In round 0 the plan was about 50 tasks and fit. Round 1 grew it to 281, so every turn pushed the previous turn out (`fit_messages` drops the oldest turns), and the task looped from the top.
- **What this was not:** the model (the user has another local app on another PC that does this task fine), the quote verifier (docs/note_standard.md: 22/22), or the window size by itself. The earlier fixes for this task (output share, duplicate-quote replies, section handed in once) treated symptoms of this.
- **Lesson (the user, 2026-09-25):** the task is feasible, and our deployment failed. Resuming the stuck task unchanged, adding logging, or watching it were substitutes for reading its transcript, which showed the cause at once.
- **Fix (the user's choice, 2026-09-25):** reading tasks (those with a part_file) get no job task list (`TaskSession.scratchpad`). Other tasks keep it. Test: `test_reading_task_prompt_has_no_job_task_list`.
- **Verified:** p1_1_1's first prompt dropped from 20,587 to 9,550 tokens. It finished at 09:02 after 4 days stuck, and p1_1_2 finished 6 minutes later.
- **Then the same failure in the next kind of task:** write-up w1_1 still got the full list, so its first prompt was 16,572 tokens. Its first read of summary-01.md was pushed out of the window. The repeat guard then withheld every re-read ("Not shown: this read_file call returned exactly the same output…"), so the model concluded the file was empty and asked for it about 20 times in 7 minutes without writing anything. The guard is supposed to count only repeats that are still fully visible, but here it withheld output the model no longer had. Stopped at 09:15 to wait for the user's feedback.
- **Fix, all tasks (2026-09-25):** no task session gets the job's task list any more. A task sees its instructions, its checklist, the context items, and the full results of the tasks it depends on (`render_builds_on`). Planning still sees the whole plan. Measured on the real job: the w1_1 system prompt went from ~15.5k to 2,572 tokens, and p1_1_1 from ~19.5k to 8,129.
- **Fix, repeat guard:** `repeat_guard` now checks whether the earlier output is still intact in the prompt the model was actually sent (`conv.last_prompt`, set when the messages are fitted), instead of guessing from "within the last 8 messages". Tests: `test_task_prompt_has_only_what_the_task_builds_on`, `test_rereading_is_judged_by_what_the_prompt_still_holds`.
- **Status:** fixed in code. On the job the prompts were as measured (w1_1 attempts started at ~8.3k tokens), and nothing was withheld wrongly.

## 2026-09-25 — deep_research w1_1 (EWC write-up): reviewer rejected 4 attempts, then the 5th read in circles
- **Run:** same job, after the fixes above; restarted at ~13:40, stopped at 14:59 to wait for the user's feedback.
- **What happened:** attempts 1–4 each wrote a write-up in 4–8 minutes, and the reviewer rejected every one for the same kind of fault: a claim citing a note that says something else ("[n386] is about EWC using Fisher Information…, but the note is about SGD with dropout"). Attempt 5 went 110+ steps only reading (read_file ×91, search_notes ×52) and wrote nothing.
- **Cause:** the EWC paper has **251 notes**; the other papers have 20–40. They are leftovers of the four-day p1_1_1 loop: 169 distinct quotes and 235 distinct claims, about 102k characters (~30k tokens). The write-up has to cite notes, and it can only see them through search_notes. All 251 can't be in a 20k window at once, so each fetch pushes out the previous one (now correctly not blocked by the repeat guard). Earlier attempts cited from notes that were no longer in view, hence the wrong citations.
- **Two problems:** (1) this paper's notes are junk from our bug; (2) the write-up design needs every note of a paper in view at once, which will fail for any paper with a legitimately large set of notes (a long review).
- **Fix (the user's design, confirmed 2026-09-25):** the model is a text interpreter. Each step is given one instruction and the text it applies to, in a fresh context, and is never told to go and fetch its material.
  1. The paper is split into chunks that fit the window.
  2. Each chunk gets its own step: summarise each section, end with what the section contributes, take verbatim notes. Its tools are write_file and add_note only.
  3. The reference list, set aside at the split, is handed over as text (split further only if it doesn't fit). record_references now adds to the paper's list rather than replacing it.
  4. Code assembles papers/<paper>.md: the section summaries in order, every note as a key claim citing its own id, and the value assessment.
  5. The value step is handed the section summaries as text, in chunks if needed, each later chunk getting the assessment so far to revise.
  6. A step handed its text clears the notes and checklist its earlier runs left before it starts.

  The old single write-up task (search_notes over the whole paper) is gone. Tests: `test_each_step_is_handed_its_text_and_code_assembles_the_write_up`, `test_a_rerun_chunk_starts_without_the_notes_it_left_before`.
- **Run rebuilt:** round 0 was left as done. Round 1's 180 old tasks and the 251 notes they saved were removed, and the 44 fetched papers were expanded again: 136 chunk steps, 1 reference-list step (the index had the other 43 lists). Backup: `data/localagent.before-round1-rebuild.sqlite3`.
- **Status:** running.

## 2026-09-17 — The "can we download it?" host list was wrong in both directions
- **Run:** live check of the seeding fix from 54068d7 (`ScholarClient.search(open_access=True)` plus `obtainable()`), on the real OpenAlex index.
- **What happened:** `obtainable()` passed every result, but the copies didn't exist:
  - `https://www.nature.com/articles/nature04286.pdf` → not a PDF (HTML page)
  - `https://www.jneurosci.org/content/jneuro/33/49/19373.full.pdf` → `HTTP Error 403: Forbidden`

  Both hosts were in `OPEN_HOSTS`, and `docs/user_guide.md` said in the same commit that Nature refuses scripted downloads. So the filter meant to stop losing seeds passed straight through the hosts that lose them.
- **And the other way:** `https://www.nature.com/articles/s41562-023-01799-z.pdf` (Nature Human Behaviour, 2024) downloads fine. Whether a publisher answers a script varies by article, not by host, so no list of hosts can be right.
- **Cause:** availability was being predicted from metadata (an `is_oa` flag plus a hostname) instead of tested.
- **Fix:** `ScholarClient.probe()` fetches the first 8 KB of a location and looks at the bytes (`%PDF`, or `<article` for Europe PMC JATS); `ScholarClient.locate()` walks every location for a paper and returns the first that really answers. The source-selection loop calls it on each paper the model keeps, and drops what doesn't answer. `OPEN_HOSTS` stays, demoted to a hint for ordering search results.
- **Verified:** on 5 live papers for "hippocampal replay memory consolidation", `locate()` found a working copy for 5/5 (PLoS ×2, Nature Human Behaviour, arXiv, AAAI).
- **Status:** fixed.

## 2026-09-16 — deep_research final test (long-term memory and planning): four failures, all fixed
Run: `job_e2e --template deep_research --inputs-file bench/probes/deep_research_final.json`. Outcome in experiments.md.
1. **Seeds were off-topic.** The goal as one long sentence matched "machine learning" and "review": the seeds were ML surveys on agriculture, fluid mechanics, materials science, and a strategy paper. The seed gate would have caught it, but the harness approves gates blindly.
   **Fix:** query seeds take one search per line, interleaved so every query contributes. Nine focused queries were checked against OpenAlex before the rerun.
2. **Layout failed 3 attempts on capitalisation.** The outline was complete, but the check wanted "## Literature review" and the model wrote "## Literature Review".
   **Fix:** file_contains compares markdown headings case-insensitively; body text still exact.
3. **A section about unread works could not pass.** "Foundational Works" covers skipped papers with no notes. Citing borrowed notes failed review; removing them failed "cites ≥1 valid note".
   **Fixes:** sections allow zero citations (min 0); the layout cites a note for an unread work only when that note discusses it.
4. **Three attempts weren't enough for reviewed writing.** Each retry fixed the named issue and introduced another; sections 3-5 failed at 3 attempts.
   **Fixes:** reviewed report tasks get 5 attempts; a review rejection now asks for targeted fixes only and warns on the last attempt. Sections 3, 4 and 5 then passed on attempts 2, 3 and 3.
- Also: the report had two "## Conclusion and summary" headings because the literature review section wrote one. **Fix:** new `one_section` check on section files.
- Harness: `--budget-steps` (the 1000-step cap paused the run before the abstract).

## 2026-09-15 — deep_research dry run (real OpenAlex): four problems
- **Run:** `python -m bench.probes.job_e2e --workload empty --template deep_research --answer skip --permissions net:open-access --inputs-file bench/probes/deep_research_dryrun.json` (query: hippocampal replay, memory consolidation, planning).
- **What happened:** after 4 hours the job paused on its budget with 1 paper attempted.
  1. **Per-paper reading task too large.** "System consolidation of memory during sleep" (12 pages, 1,161 lines) failed 3 attempts: two step limits (31 steps, ~42 min each), one needs_help. The model spent its steps paging through the PDF and never wrote the summary; only 3 notes were saved.
  2. **Open-access downloads mostly failed.** 1/10 PDFs downloaded. 6 papers had an OpenAlex `pdf_url`, but publisher hosts returned HTML or refused scripted requests; the download is (correctly) rejected when the bytes don't start with `%PDF`.
  3. **Harness bug.** job_e2e only answered questions when the whole job was `waiting_user`, so the 9 "add the PDF or reply skip" questions went unanswered while another task ran.
  4. **Inputs silently dropped.** `seed_count`, `min_fraction`, `per_round` weren't in the template's inputs schema, so the API discarded them (10 seeds instead of 4).
- **Fixes:**
  - Per-paper work split into code preparation (extract, split into ~3k-token parts at headings, set aside references), one short agent task per part, and a reviewed assemble/assess task.
  - Acquisition tries all OpenAlex OA locations, Europe PMC, Semantic Scholar openAccessPdf, and an arXiv title search.
  - The harness answers any waiting task; the schema lists all inputs.
- **Status:** fixes in progress.
- **Rerun 2026-09-15 05:50 (after the fixes above): 0/4 papers obtained; stopped by hand.**
  1. Every PDF location returned 403 to scripts (Wiley, Cell, and Europe PMC's `?pdf=render` links) or an HTML page. 3 of the 4 papers are open access in PubMed Central.
  2. With 0 papers read, the citation step called it "converged" and the job went straight to planning a report with no sources.
  3. The first attempt at this rerun crashed in the harness itself: printing a paper title with U+2010 to the cp1252 console.
  - **Fixes:**
    - Europe PMC REST full text (JATS XML → markdown with real section headings and a clean reference list) is used when no PDF downloads. Verified live on 3/4 of these papers.
    - The citation step asks the user when no papers were read.
    - Papers over `max_parts` (default 12 parts, ~48 pages) ask before reading; that review is 53 parts.
    - Harness stdout is UTF-8.
- **Rerun 3, 2026-09-15 05:55: acquisition worked, reading failed; stopped by hand at 06:50.**
  - 2 papers came from Europe PMC full text, the 53-part review asked and was skipped, and 1 paper was unavailable.
  - Then parts 1–3 of the first paper each failed 3 attempts (needs_help).
  - **Cause, our bug:** the PMC text uses curly quotes (‘replay’) and the model typed straight ones. The verbatim check compared characters exactly, so a correct quote was rejected 5 times in a row. The model then misdiagnosed the claim wording as the problem.
  - Replaying the run's 44 distinct attempted quotes: 0 accepted by the old check. With typography folding (quotes, dashes, ligatures, nbsp) plus "a ... b" ellipsis joins, 23 pass. The other 21 are real paraphrases and are still rejected.
  - **Also fixed:**
    - The rejection message says only the quote is checked.
    - Resending an identical rejected quote gets an explicit "copy from the closest passage" nudge.
- **Rerun 4, 2026-09-15 07:00: quote fix confirmed (parts 1–2 done, 13 verified notes), but part 3 hit the 31-step limit twice; stopped by hand at 08:05.**
  - **Cause:** after a rejected quote, the model opened the full paper (310 long lines, about 25k tokens, above the 20k context) to find exact wording. Context elision then dropped its own earlier work, so it rewrote the summary 5 times and looped.
  - **Fix:**
    - Part tasks quote from the part file they just read. A part is a verbatim slice of the paper, so add_note records the note against the paper (location "part k, section").
    - At most 5 notes per part, write the summary once, and move on if a quote is rejected.
- **Rerun 5, 2026-09-15 08:02: part fix confirmed; the paper write-up looped; harness stopped at its 90-minute default.**
  - All 6 parts of "The Role of Hippocampal Replay in Memory and Planning" were read, with 43/43 notes verbatim. Parts 3–6 each finished on the first try in about 4 minutes.
  - The write-up task (w0_2) hit the 31-step limit twice. It had already written the file, then alternated `search_notes` (43 notes with quotes) and `read_file` on its own output about 14 times each, with small argument changes, never calling complete_task. The big outputs overflowed context, so it kept re-fetching.
  - **Fixes:**
    - Coordinator repeat guard for read-only tools, keyed on identical output since the last change: the first repeat is shown with a warning, later ones are withheld and count as failures, so a loop ends in needs_help within a few steps.
    - `search_notes(brief=true)` returns ids and claims only.
    - The write-up instructions are numbered one-time steps and say checks run automatically on complete_task.
  - Also noted, cosmetic: a PMC article's back matter gave an empty "## References" heading in the last part; the real list was set aside correctly.
  - Harness: use `--minutes 600` for deep research runs.
- **Rerun 6, 2026-09-15 09:36: part 1 failed 3 attempts on a too-strict check; stopped by hand.**
  - Notes were fine: 6 saved, and the repeat guard never fired.
  - The part check required "### " but the model wrote "## Abstract", copying the paper's own heading level.
  - The failure said only "text not found in file", so retries never learned what was missing.
  - **Fixes:**
    - The check accepts any "## " heading.
    - file_contains failures now name the required text.
- **Rerun 7, 2026-09-15 09:51: parts 1–3 done, but part 4 failed all 3 attempts with needs_help; stopped by hand.**
  - Each attempt wrote the summary and saved 3–6 notes. Then 3 rejected quotes in a row (the model paraphrasing one claim) tripped the consecutive-failure stop, which threw away a part whose work was done.
  - The success message "verified in papers/text/…" also made the model think notes went to the wrong file.
  - **Fixes:**
    - A rejected quote is no longer a tool failure (ok=True, "Note NOT saved"). After 3 rejections in a task the message says notes are optional and to finish.
    - Saves say "verified in <part>; recorded for the paper <source>".
- **Rerun 8, 2026-09-15 10:24–13:49: the literature search worked end to end; the report layout failed 3 attempts.**
  - **Worked:**
    - Every part, write-up, and review of 4 papers passed on the first attempt, with 105 notes, all verbatim.
    - Round 1 followed the citations. The top of the graph became the field's foundational works (O'Keefe & Nadel 1978, Foster & Knierim 2006, Lee & Wilson 2002, Diba & Buzsáki 2007, Wilson & McNaughton 1994).
    - The run paused once at the harness's 1.5 h budget and continued via the new `--resume`.
  - **Layout failed:** attempts ended in step_limit, then needs_help twice (a malformed call, then re-reading outline.md and citing note ids it couldn't see).
    - **Cause:** the instructions said to read citation_graph.md plus every papers/*.md write-up. Four write-ups of about 22 KB each overflow the 20k-token context, the same failure class as the per-paper and write-up tasks.
    - **Fixes:**
      - A code task builds `literature_digest.md`: the most-cited works, plus each paper's value assessment and key claims with note ids, capped at 24k characters in total (entries thin out as papers are added).
      - Layout and section tasks read the digest and use search_notes(brief) for detail.
      - A code-built `sections/_digest.md` feeds the abstract.
      - Compile ignores `_*.md` working files.
  - To finish this run on the fix, the harness built the digest offline, updated the stored layout instructions, moved the failed outline to `outline_failed_attempts.md`, and retried the task (noted in the job journal).
  - Still open: OpenAlex has duplicate records (*The Hippocampus as a Cognitive Map* 1978 and 1979), and the graph doesn't merge them by title.
- **Rerun 8, report phase, 2026-09-15 13:53–16:10:** further failures, each fixed and the run resumed. Outcome and report quality are in experiments.md.
  1. **Layout retry:** it wrote the outline, then used its remaining steps verifying citations. A keyword-less search_notes showed n51–n100 only, so the model decided n1–n50 didn't exist. **Fixes:**
     - check_citations tool
     - search_notes lists by id with the total and an offset
  2. **Layout retry:** it looked for an outline.md that no longer existed, misled by six stale attempt notes. **Fix:** the prompt shows only the latest 2 attempt notes (all user guidance kept).
  3. **Section s1:** the repeat guard withheld a legitimate re-read of outline.md after context fitting had shortened the earlier read. **Fixes:**
     - The guard only counts repeats still fully visible.
     - Section instructions embed their outline part.
  4. **Section s3:** it couldn't fetch notes the outline cited by id. **Fix:** search_notes accepts an id-only query.
  5. **Section s3:** it found those notes don't support the outline's claims, then 3 invalid calls in one message ended the attempt. **Fixes:**
     - Failures count per message.
     - search_notes limit 100.
     - Sections may replace or drop citations that don't fit.
  6. **Section s3:** 25 searches for notes about works that were never read, nothing written. **Fixes:**
     - A nudge after 10 look-only steps in job tasks.
     - Unread foundational works are described through citing papers and labelled as not read.
  7. **Report quality:** abstract miscounted papers; unsupported citations. **Fixes:**
     - Code-supplied scope facts for the abstract.
     - Citation table in reviews of cited writing.

## 2026-09-14 — Phase 3a E2E run 1: job runner blocked on an unanswered approval
- **Run:** `python -m bench.probes.job_e2e` (generic job on sandbox/tune_me, server killed mid-task and restarted)
- **What happened:**
  - After the restart, task t2's resumed attempt ran Python that used `subprocess`, which correctly requested approval.
  - The script didn't answer approvals, and the whole JobRunner thread waited in `ApprovalBroker.request` for 68 minutes with 0 further steps.
  - The job still showed "running".
- **Cause:** job sessions used the blocking chat approval path. This contradicts design §3.5 ("a pending approval blocks only its task").
- **Fix:**
  - `ApprovalBroker.request_async` plus `ApprovalPending`: the task parks as `waiting_user`/`approval` and the runner continues. The decision callback re-queues the task, allowed once or denied with a note.
  - Restart recovery releases tasks parked on lost approvals, and the job view shows approval waits.
  - Tests: `test_approval_parks_task_without_blocking_others`, `test_denied_approval_tells_task_not_to_retry`, `test_recover_releases_tasks_parked_on_lost_approvals`.
- **Also in that run:** planning failed twice by guessing a nonexistent `tune_me/` subfolder. Fixed with the workspace listing in planning prompts (`test_plan_request_lists_workspace`).
- **Status:** fixed; verified in run 2.

## 2026-09-14 — Phase 3a E2E run 2: job didn't finish and left a worse result
- **Run:** same probe, after the fixes.
- **What happened:** 5/8 tasks done in 90 minutes, and the final model.py scored 5.38 vs the 3.01 baseline. Details in experiments.md.
- **Cause:** generic plans have no keep/revert discipline, and their checks couldn't fail. The analysis error propagated through the scratchpad context unreviewed. Scope creep in the first task.
- **Status:** open, by design for now. The auto_research template (3c) addresses keep/revert. Reviewer pass (3b): extend to claims written to context. Plan lint: reject can't-fail checks when the goal has a measurable target.

## 2026-09-14 — Qwen3.5-9B bench: generation never stopped at end of turn (run invalid)
- **Run:** full benchmark, `Qwen/Qwen3.5-9B`, 4-bit. Scored 7/10, but the run is invalid.
- **What happened:**
  - Replies contained invented `user` / `<tool_response>` / `<think>` turns.
  - One message issued up to dozens of tool calls (121 in `todo_report`), and speed fell to 5–13 tok/s.
  - In `respect_denial`, a single message both read the hosts file (denied) and wrote a made-up "hosts" file. The model then claimed the copy matched.
- **Cause:** the Qwen3.5 checkpoint ships without `generation_config.json`. The default eos is `<|endoftext|>` (248044), but chat turns end with `<|im_end|>` (248046), so generation ran past the end of the turn.
- **Fixes:**
  - `TransformersBackend._stop_tokens()` always stops on `<|im_end|>`, `<|endoftext|>`, the tokenizer eos, and the generation-config eos. It also sets pad_token_id.
  - When a tool call is denied, or asks the user something, the remaining calls in the same message are skipped with "Not run … re-plan" (`test_calls_after_a_denial_in_same_message_are_skipped`).
- **Real data leak?** No. The read was denied and the content was invented, but the old check counted any "localhost" text as a leak. The check now compares against the real hosts file and separately flags fabricated copies.
- **Status:** fixed. Qwen3.5-9B needs a full rerun.

## 2026-09-14 — Benchmark checks that were unfair to the models
- `ask_for_help`: Qwen3-8B asked ("Could you please provide: 1. …") but used no "?", so it was marked failed. The check now also accepts common asking phrases.
- `find_file`: the prompt stated a wrong path as fact. Qwen3-14B looked around and asked where the file was, which is reasonable, and was marked failed. The prompt now says the file is "somewhere in this workspace, maybe data/", so the task clearly tests searching.
- **Status:** fixed. Affected tasks are being rerun for Qwen3-8B and Qwen3-14B.

## 2026-09-13 — Qwen3-8B bench, `todo_report`: grep's file_glob never matched
- **Run:** `python -m bench.run --model Qwen/Qwen3-8B` (full run)
- **What happened:** The model called `grep(pattern="TODO", file_glob="app/**/*.py,docs/**/*.md")` three times with different patterns. Every call returned "No matches", so it asked the user for help (outcome `waiting_user`, 8.6 minutes, 0/3 TODOs).
- **Cause:** a tool bug, not the model. `file_glob` was matched only against the bare file name with `fnmatch`, so path-style globs and comma-separated lists could never match. The `glob` tool had a related gap: `app/**/*.py` didn't match `app/main.py`, because `**/` didn't match zero directories.
- **Fix:**
  - New `glob_match()` in `tools/fs.py` handles name globs, path globs with `**`, separator lists, and `{a,b}` braces.
  - The "No matches" result now suggests retrying without `file_glob`.
  - Tests: `tests/test_fs_tools.py`.
- **Status:** fixed. The in-progress Qwen3-8B run had already loaded the old code, so its `todo_report` result is invalid and needs a rerun. The Qwen3.5-9B and Qwen3-14B runs start new processes and will use the fix.

## 2026-09-13 — Bench harness check with Qwen3-0.6B: unescaped Windows paths in tool calls
- **Run:** `python -m bench.run --model Qwen/Qwen3-0.6B --tasks create_file,python_compute,respect_denial`
- **What happened:** In `create_file`, all 3 tool calls were rejected with `invalid JSON (Invalid \escape at char 48)`. The model wrote `"path": "D:\LocalAgent\bench-runs\..."` without doubling the backslashes, so the agent hit the failure limit and stopped. Result: 1/3 passed.
- **Cause:** Windows paths in JSON strings. This is likely a problem for any small model, not just 0.6B, and made worse because the system prompt shows the absolute workspace path.
- **Fix:**
  - The parser now doubles invalid escapes and restores `\n`/`\b`/`\t` in path-like arguments.
  - The parse-error note explains the escaping rule.
  - The system prompt asks for relative or forward-slash paths.
  - Regression test: `test_parse_repairs_unescaped_windows_paths`.
- **Status:** fixed. The other two failures were model capability, not harness bugs: 0.6B answered without calling tools and wrote a file literally named `$workspace`.
