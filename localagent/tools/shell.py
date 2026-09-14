"""PowerShell command tool, gated by CommandPolicy."""
from __future__ import annotations

from .process import env_for, format_result, run_process
from .registry import Tool, ToolContext, ToolResult

PS_PREFIX = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $ProgressPreference = 'SilentlyContinue'; "


def run_shell(ctx: ToolContext, command: str, timeout_s: int | None = None) -> ToolResult:
    decision = ctx.policy.evaluate(command, ctx.guard)
    if decision.action == "deny":
        return ToolResult(f"Blocked by policy ({decision.summary}). This kind of change to the computer is not "
                          "allowed. Find another way or ask the user.", ok=False, denied=True)
    if decision.action == "ask":
        if not ctx.ask(decision.keys, "Run a shell command", f"{command}\n\nWhy approval is needed: {decision.summary}"):
            return ToolResult(f"The user denied this command ({decision.summary}). Do not retry it; adapt or ask the user.",
                              ok=False, denied=True)
    timeout = float(timeout_s or ctx.settings.tool_timeout_s)
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + command]
    code, output, status = run_process(argv, ctx.workspace, env_for(ctx.env_path), timeout, ctx.cancel)
    text, ok = format_result(code, output, status, timeout)
    return ToolResult(text, ok=ok)


TOOLS = [
    Tool("run_shell",
         "Run a Windows PowerShell command with the workspace as the working directory. `python` and `pip` refer to "
         "the project environment. Returns exit code and combined output. Avoid interactive commands.",
         {"type": "object", "properties": {
             "command": {"type": "string"},
             "timeout_s": {"type": "integer", "minimum": 1, "maximum": 3600, "description": "Default 300."}},
          "required": ["command"]},
         run_shell, "shell", timeout_s=3700),
]
