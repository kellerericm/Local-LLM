# Failed tests

Record failures here: date, what was run, what happened (exact error), suspected cause, status (open / fixed / won't fix).
A failure is information, not a verdict.

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
