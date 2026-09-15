"""Reviewer pass (design §5): a fresh-context model session that judges a task's result without seeing the
worker's conversation. It also vets facts the worker added to the shared scratchpad context (E2E run 2 showed an
unchecked wrong analysis spreading through context)."""
from __future__ import annotations

import re
from pathlib import Path

from ..coordinator import prompts as base_prompts
from ..tools.registry import Tool, ToolContext, ToolRegistry, ToolResult
from .sessions import JobSession
from .tools import CHECK_CITATIONS, SEARCH_NOTES

REVIEW_TOOLS = ("read_file", "read_document", "list_dir", "glob", "grep")

REVIEW_BLOCK = """
# You are a reviewer
Another agent just finished one task of a longer job. You did not do the work. Decide whether the result is
acceptable. Be strict about real problems and fair about everything else: don't fail work for style or for things
the task didn't ask for.

Check, using the read tools to look at the actual files and notes:
1. Is the task's "done when" condition really met?
2. Is the output correct and supported? For research writing: every factual claim must be supported by the quotes of
   the notes it cites, and sources that disagree must be presented as disagreeing, not flattened into one view.
3. For each context item the worker added: is it something measured or read (supported), or a guess stated as
   fact? List unsupported ones in bad_context_ids.

Be efficient: you have about 15 tool calls. Check the claims that matter most rather than every word, then call
report_review exactly once. For a fail, give specific, fixable issues with evidence."""


def report_review(ctx: ToolContext, verdict: str, issues: list[dict] | None = None,
                  bad_context_ids: list[str] | None = None) -> ToolResult:
    ctx.conversation.result = {"kind": "review", "verdict": verdict, "issues": issues or [],
                               "bad_context_ids": bad_context_ids or []}
    return ToolResult("Review recorded.", end_turn=True)


REPORT_REVIEW = Tool(
    "report_review",
    "Record your review. verdict 'pass' or 'fail'; issues: specific problems with evidence; bad_context_ids: context "
    "items (e.g. c12) that are guesses or wrong.",
    {"type": "object", "properties": {
        "verdict": {"enum": ["pass", "fail"]},
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "problem": {"type": "string"}, "evidence": {"type": "string"}}, "required": ["problem"]}},
        "bad_context_ids": {"type": "array", "items": {"type": "string"}}},
     "required": ["verdict"]},
    report_review, "job")


class ReviewSession(JobSession):
    max_steps = 20

    def wrap_up_tools(self, registry: ToolRegistry, ctx) -> list[Tool]:
        return [REPORT_REVIEW]

    def __init__(self, runner, job: dict, run: dict, task: dict):
        super().__init__(runner, job, run)
        self.task = task

    def event_fields(self) -> dict:
        return {**super().event_fields(), "task_key": self.task["key"], "review": True}

    def system_prompt(self, ctx) -> str:
        return base_prompts.system_prompt(str(ctx.workspace), str(ctx.env_path), ctx.project) + "\n" + REVIEW_BLOCK

    def tools(self, registry: ToolRegistry, ctx) -> list[Tool]:
        available = {t.name: t for t in registry.available(ctx)}
        return [available[n] for n in REVIEW_TOOLS if n in available] + [SEARCH_NOTES, CHECK_CITATIONS, REPORT_REVIEW]


CITATION_TABLE_CHARS = 9000


def citation_table(workspace: Path, paths: list[str], notes: list[dict], limit: int = CITATION_TABLE_CHARS) -> str:
    """Each cited sentence next to the note it cites, so a reviewer can judge support without looking every note up.
    Dry run 8: citations to real but unrelated notes (Bellman-backup notes cited for the 'cognitive map') passed
    checks and most reviews."""
    by_id = {n["id"]: n for n in notes}
    rows: list[str] = []
    for rel in paths:
        f = workspace / rel
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            ids = [int(x) for x in re.findall(r"\[n(\d+)\]", sentence)]
            for i in dict.fromkeys(ids):
                n = by_id.get(i)
                said = re.sub(r"\s+([.,;:!?])", r"\1",
                              re.sub(r"\s+", " ", re.sub(r"\[n\d+\]", "", sentence))).strip(" -*#")[:220]
                if n:
                    rows.append(f'- {rel}: "{said}" -> [n{i}] {n["claim"][:160]} | quote: "{n["quote"][:160]}"')
                else:
                    rows.append(f'- {rel}: "{said}" -> [n{i}] (no such note)')
    out, used = [], 0
    for r in rows:
        if used + len(r) > limit:
            out.append(f"- ... {len(rows) - len(out)} more citations not shown; spot-check them with search_notes")
            break
        out.append(r)
        used += len(r) + 1
    return "\n".join(out)


def review_request(job: dict, task: dict, summary: str, new_context: list[dict], citations: str = "") -> str:
    outputs = sorted({c.get("path") for c in task["checks"] if c.get("path")} |
                     set((task.get("params") or {}).get("outputs") or []))
    parts = [f"**Job goal:** {job['goal']}",
             f"**Task [{task['key']}]:** {task['title']}",
             f"**Instructions given to the worker:** {task['instructions'] or '-'}",
             f"**Done when:** {task['done_when'] or '-'}",
             f"**Worker's summary:** {summary}",
             "**Files to inspect:** " + (", ".join(outputs) if outputs else "(none declared; check the summary's claims)")]
    if new_context:
        parts.append("**Context items the worker added during this task:**\n" +
                     "\n".join(f"- [c{i['id']}] {i['text']}" for i in new_context))
    else:
        parts.append("**Context items the worker added:** none")
    if citations:
        parts.append("**Citations to check** (each cited sentence, then the note it cites). Fail any whose note doesn't "
                     "support the sentence:\n" + citations)
    parts.append("Review it, then call report_review.")
    return "\n\n".join(parts)
