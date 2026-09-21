"""Fit a conversation into the model's context window.

Nothing is shortened unless the window requires it, and then only as much as the window requires. Token counts are
estimated from character length (conservative, ~3 chars/token) so no tokenizer round-trip is needed. In order:
1. If it already fits, it is left exactly as it is.
2. Otherwise shorten old tool outputs, oldest first, stopping the moment it fits.
3. Only if that isn't enough, drop the oldest whole turns (an assistant message with its tool results), always
   keeping the system prompt and the first user message, and leave a note saying so.
"""
from __future__ import annotations

import json

CHARS_PER_TOKEN = 3.0
MIN_TOOL_CHARS = 400        # the shortest a tool result is squeezed to before turns are dropped instead


def estimate_tokens(obj) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return int(len(text) / CHARS_PER_TOKEN) + 4


def _shorten(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    half = limit // 2
    # Say plainly that this is the coordinator shortening the history, not the file being cut off. The old wording
    # ("chars elided from an older tool result") read as truncation, and one task re-read the same part twenty-six
    # times trying to get the rest of it.
    return (f"{content[:half]}\n"
            f"... [The middle {len(content) - limit} characters of this earlier result are hidden here to keep the "
            "conversation inside the context window. The file itself is complete and unchanged. Reading it again "
            "returns the same thing and is hidden the same way — work from what you have, or read one part of it "
            "with an offset.] ...\n"
            f"{content[-half:]}")


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
    if sum(estimate_tokens(m) for m in msgs) <= budget:
        return msgs                      # it fits: the model sees every word of it

    # Shorten tool results from the oldest forward, and stop as soon as the conversation fits. The newest results
    # are the ones the model is working from, so they are the last to be touched and only if the window demands it.
    for limit in (8000, 4000, 2000, 1000, MIN_TOOL_CHARS):
        for m in msgs[:-1]:
            if sum(estimate_tokens(x) for x in msgs) <= budget:
                return msgs
            if m["role"] == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > limit:
                m["content"] = _shorten(m["content"], limit)
    if sum(estimate_tokens(m) for m in msgs) <= budget:
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
