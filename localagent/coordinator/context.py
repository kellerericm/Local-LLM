"""Fit a conversation into the model's context window.

Token counts are estimated from character length (conservative, ~3 chars/token) so no tokenizer
round-trip is needed. Strategy, in order:
1. Shorten old tool outputs (everything but the most recent few messages).
2. Drop the oldest whole turns (an assistant message together with its tool results),
   always keeping the system prompt and the first user message, and leave a note.
"""
from __future__ import annotations

import json

CHARS_PER_TOKEN = 3.0
OLD_TOOL_CHARS = 1200
KEEP_RECENT = 8


def estimate_tokens(obj) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return int(len(text) / CHARS_PER_TOKEN) + 4


def _shorten(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    half = limit // 2
    return f"{content[:half]}\n... [{len(content) - limit} chars elided from an older tool result] ...\n{content[-half:]}"


def _groups(messages: list[dict]) -> list[list[dict]]:
    """Group so tool results stay with the assistant message that requested them."""
    groups: list[list[dict]] = []
    for m in messages:
        if m["role"] == "tool" and groups:
            groups[-1].append(m)
        else:
            groups.append([m])
    return groups


def fit_messages(messages: list[dict], budget_tokens: int, tools: list[dict] | None = None) -> list[dict]:
    if not messages:
        return messages
    budget = budget_tokens - (estimate_tokens(tools) if tools else 0)
    msgs = [dict(m) for m in messages]
    cutoff = len(msgs) - KEEP_RECENT
    for i, m in enumerate(msgs):
        if i < cutoff and m["role"] == "tool" and isinstance(m.get("content"), str):
            m["content"] = _shorten(m["content"], OLD_TOOL_CHARS)

    total = sum(estimate_tokens(m) for m in msgs)
    if total <= budget:
        return msgs

    head = [msgs[0]] if msgs[0]["role"] == "system" else []
    body = msgs[len(head):]
    groups = _groups(body)
    first = groups.pop(0) if groups and groups[0][0]["role"] == "user" else []
    dropped = 0
    fixed = sum(estimate_tokens(m) for m in head + first) + 60
    while len(groups) > 1 and fixed + sum(estimate_tokens(m) for g in groups for m in g) > budget:
        dropped += len(groups.pop(0))
    note = [{"role": "user", "content": f"[coordinator] {dropped} earlier messages were removed to fit the context "
                                        "window. Re-read files if you need details from that part of the work."}] if dropped else []
    result = head + first + note + [m for g in groups for m in g]
    # A lone oversized message: shorten it rather than fail.
    if fixed + sum(estimate_tokens(m) for g in groups for m in g) > budget and groups:
        last = result[-1]
        if isinstance(last.get("content"), str):
            allowed = max(2000, int((budget - fixed) * CHARS_PER_TOKEN) - 500)
            last["content"] = _shorten(last["content"], allowed)
    return result
