"""Machine checks declared on tasks, run when a task claims completion.

Cheapest and most reliable form of verification (design §5). Paths must stay inside the workspace;
commands go through the same safety policy and approval flow as the agent's shell tool.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..safety.paths import is_within
from ..tools.registry import ApprovalPending
from ..tools.process import env_for, format_result, run_process
from ..tools.shell import PS_PREFIX

CHECK_TIMEOUT_S = 300


@dataclass
class CheckResult:
    check: dict
    ok: bool
    detail: str

    def line(self) -> str:
        c = self.check
        target = c.get("path") or c.get("command") or ""
        return f"{'PASS' if self.ok else 'FAIL'} {c.get('type')} {target}: {self.detail}"


def describe(check: dict) -> str:
    t = check.get("type")
    if t == "file_exists":
        return f"file {check.get('path')} exists and is not empty"
    if t == "file_contains":
        return f"file {check.get('path')} contains {check.get('text')!r}"
    if t == "json_valid":
        return f"file {check.get('path')} is valid JSON"
    if t == "command_ok":
        return f"command `{check.get('command')}` exits with code 0"
    return json.dumps(check)


def run_checks(checks: list[dict], workspace: Path, env_path: Path, guard, policy,
               ask: Callable[[list[str], str, str], bool], cancel: threading.Event | None = None) -> list[CheckResult]:
    results = []
    for c in checks:
        try:
            results.append(_run_one(c, workspace, env_path, guard, policy, ask, cancel))
        except ApprovalPending:
            raise                   # the task parks until the user decides
        except Exception as e:  # a broken check is a failed check, never a crash
            results.append(CheckResult(c, False, f"check error: {type(e).__name__}: {e}"))
    return results


def _path(check: dict, workspace: Path, guard) -> Path:
    p = guard.resolve(check["path"])
    if not is_within(p, workspace):
        raise ValueError(f"path {check['path']} is outside the workspace")
    return p


def _run_one(c: dict, workspace, env_path, guard, policy, ask, cancel) -> CheckResult:
    t = c.get("type")
    if t == "file_exists":
        p = _path(c, workspace, guard)
        if not p.exists():
            return CheckResult(c, False, "file not found")
        if p.is_file() and p.stat().st_size == 0:
            return CheckResult(c, False, "file is empty")
        return CheckResult(c, True, "exists")
    if t == "file_contains":
        p = _path(c, workspace, guard)
        if not p.is_file():
            return CheckResult(c, False, "file not found")
        text = p.read_text(encoding="utf-8", errors="replace")
        return CheckResult(c, c["text"] in text, "found" if c["text"] in text else "text not found in file")
    if t == "json_valid":
        p = _path(c, workspace, guard)
        if not p.is_file():
            return CheckResult(c, False, "file not found")
        try:
            json.loads(p.read_text(encoding="utf-8"))
            return CheckResult(c, True, "valid JSON")
        except json.JSONDecodeError as e:
            return CheckResult(c, False, f"invalid JSON: {e}")
    if t == "command_ok":
        decision = policy.evaluate(c["command"], guard)
        if decision.action == "deny":
            return CheckResult(c, False, f"blocked by policy ({decision.summary})")
        if decision.action == "ask" and not ask(decision.keys, "Run a task check command",
                                                f"{c['command']}\n\nWhy approval is needed: {decision.summary}"):
            return CheckResult(c, False, "approval denied")
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + c["command"]]
        code, output, status = run_process(argv, workspace, env_for(env_path), float(c.get("timeout_s", CHECK_TIMEOUT_S)),
                                           cancel)
        text, ok = format_result(code, output, status, float(c.get("timeout_s", CHECK_TIMEOUT_S)))
        return CheckResult(c, ok, text[-800:])
    return CheckResult(c, False, f"unknown check type {t!r}")
