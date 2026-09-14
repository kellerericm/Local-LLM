# Failed tests

Record failures here: date, what was run, what happened (exact error), suspected cause, status (open / fixed / won't fix).
A failure is information, not a verdict.

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
