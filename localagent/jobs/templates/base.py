"""Template interface.

A template can:
- build the initial plan in code (`initial_plan`), instead of a model planning session;
- run code tasks (`kind="code"`, dispatched to `handlers[task["handler"]]`);
- define gates (`kind="gate"`): the job waits for the user, then `on_gate` decides what happens;
- grow the plan when tasks finish (`on_task_done`), e.g. one task per outline section.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class HandlerResult:
    ok: bool
    summary: str = ""
    retry_guidance: str | None = None    # for failures that another attempt could fix
    wait_question: str | None = None     # park the task and ask the user (e.g. "add this PDF")


@dataclass
class Template:
    name: str
    label: str
    description: str
    inputs_schema: dict = field(default_factory=dict)
    handlers: dict[str, Callable] = field(default_factory=dict)

    def initial_plan(self, runner, job: dict) -> list[dict] | None:
        """Return tasks to skip the model planning session, or None to let the model plan."""
        return None

    def on_task_done(self, runner, job: dict, task: dict) -> None:
        pass

    def on_gate(self, runner, job: dict, task: dict, answer: str) -> str:
        """Decide a gate. Return 'approve' to continue, or 'revise' after reopening tasks with the user's feedback."""
        return "approve" if is_approval(answer) else "revise"


APPROVAL_WORDS = ("approve", "approved", "yes", "ok", "okay", "looks good", "lgtm", "go ahead", "continue", "proceed")


def is_approval(answer: str) -> bool:
    a = answer.strip().lower().rstrip(".!")
    return a in APPROVAL_WORDS or a.startswith(("approve", "looks good", "yes,", "yes "))
