"""Run a Python snippet in the project environment, gated by CommandPolicy.evaluate_python."""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from .process import env_for, format_result, run_process
from .registry import Tool, ToolContext, ToolResult


def python_exe(env_path: Path) -> Path:
    for candidate in (env_path / "python.exe", env_path / "bin" / "python", env_path / "Scripts" / "python.exe"):
        if candidate.exists():
            return candidate
    return Path("python")


def run_python(ctx: ToolContext, code: str, timeout_s: int | None = None) -> ToolResult:
    decision = ctx.policy.evaluate_python(code, ctx.guard)
    if decision.action == "deny":
        return ToolResult(f"Blocked by policy ({decision.summary}).", ok=False, denied=True)
    if decision.action == "ask":
        if not ctx.ask(decision.keys, "Run Python code", f"{code}\n\nWhy approval is needed: {decision.summary}"):
            return ToolResult(f"The user denied running this code ({decision.summary}). Adapt or ask the user.", ok=False, denied=True)
    tmp = Path(ctx.settings.tmp_dir)
    tmp.mkdir(parents=True, exist_ok=True)
    script = tmp / f"snippet_{int(time.time())}_{uuid.uuid4().hex[:6]}.py"
    script.write_text(code, encoding="utf-8")
    timeout = float(timeout_s or ctx.settings.tool_timeout_s)
    try:
        code_, output, status = run_process([str(python_exe(ctx.env_path)), "-u", str(script)],
                                            ctx.workspace, env_for(ctx.env_path), timeout, ctx.cancel)
    finally:
        script.unlink(missing_ok=True)
    text, ok = format_result(code_, output, status, timeout)
    return ToolResult(text, ok=ok)


TOOLS = [
    Tool("run_python",
         "Run a Python script in the project environment with the workspace as the working directory. "
         "Print what you want to see. Returns exit code and output.",
         {"type": "object", "properties": {
             "code": {"type": "string"},
             "timeout_s": {"type": "integer", "minimum": 1, "maximum": 3600}},
          "required": ["code"]},
         run_python, "python", timeout_s=3700),
]
