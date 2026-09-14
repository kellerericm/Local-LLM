"""Plan structure: validation ("lint") for proposed plans and tree helpers for scheduling.

Probe P1 (docs/design §13) showed the model writes plans that parse fine but are vague ("has been read").
Lint rejects those with specific feedback so the model can fix them.
"""
from __future__ import annotations

import re

MAX_DEPTH = 3
MAX_CHILDREN = 8
MAX_LEAVES = 60
MAX_TASKS = 80
CHECK_TYPES = {
    "file_exists": ["path"],
    "file_contains": ["path", "text"],
    "json_valid": ["path"],
    "command_ok": ["command"],
}

_VAGUE = re.compile(r"\b(has|have|is|are|was|were) (been )?(read|reviewed|considered|understood|identified|analy[sz]ed|"
                    r"explored|investigated|looked at|examined|studied|thought about|completed|done|finished|handled)\b",
                    re.IGNORECASE)
_CONCRETE = re.compile(r"\.\w{1,5}\b|\b\d+\b|\bexists?\b|\bcontains?\b|\bexit\b|\bpass(es|ing)?\b|\bscore\b|"
                       r"\btests?\b|\bwritten to\b|\bsaved (to|in|as)\b|\blists?\b|\btable\b|\bsection\b", re.IGNORECASE)


def normalize_plan(raw_tasks: list[dict]) -> list[dict]:
    """Map the propose_plan tool's fields to stored task fields."""
    out = []
    for t in raw_tasks:
        out.append({
            "key": str(t.get("id", "")).strip(),
            "parent_key": str(t.get("parent_id") or "").strip() or None,
            "title": str(t.get("title", "")).strip(),
            "instructions": str(t.get("instructions", "")).strip(),
            "done_when": str(t.get("done_when", "")).strip(),
            "depends_on": [str(d).strip() for d in (t.get("depends_on") or []) if str(d).strip()],
            "checks": t.get("checks") or [],
        })
    return out


def children_map(tasks: list[dict]) -> dict[str | None, list[dict]]:
    kids: dict[str | None, list[dict]] = {}
    for t in tasks:
        kids.setdefault(t.get("parent_key"), []).append(t)
    return kids


def leaves(tasks: list[dict]) -> list[dict]:
    parents = {t["parent_key"] for t in tasks if t.get("parent_key")}
    return [t for t in tasks if t["key"] not in parents]


def lint_plan(tasks: list[dict], policy=None, guard=None) -> list[str]:
    """Return a list of problems; empty means the plan is acceptable."""
    errors: list[str] = []
    if not tasks:
        return ["The plan has no tasks."]
    if len(tasks) > MAX_TASKS:
        errors.append(f"The plan has {len(tasks)} tasks; the limit is {MAX_TASKS}. Group or simplify.")
    keys = [t["key"] for t in tasks]
    by_key = {t["key"]: t for t in tasks}
    if any(not k for k in keys):
        errors.append("Every task needs a non-empty id.")
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        errors.append(f"Duplicate task ids: {', '.join(dupes)}.")

    for t in tasks:
        if t.get("parent_key") and t["parent_key"] not in by_key:
            errors.append(f"Task {t['key']}: parent_id '{t['parent_key']}' doesn't exist.")
        if not t["title"]:
            errors.append(f"Task {t['key']}: missing title.")

    # depth and cycles in the parent chain
    for t in tasks:
        depth, seen, cur = 1, {t["key"]}, t
        while cur.get("parent_key") and cur["parent_key"] in by_key:
            cur = by_key[cur["parent_key"]]
            if cur["key"] in seen:
                errors.append(f"Task {t['key']}: parent chain loops back on itself.")
                break
            seen.add(cur["key"])
            depth += 1
        if depth > MAX_DEPTH:
            errors.append(f"Task {t['key']} is nested {depth} levels deep; the limit is {MAX_DEPTH}.")

    for parent, kids in children_map(tasks).items():
        if len(kids) > MAX_CHILDREN:
            where = f"Task {parent}" if parent else "The top level"
            errors.append(f"{where} has {len(kids)} direct subtasks; the limit is {MAX_CHILDREN}. Add a grouping level.")

    leaf_list = leaves(tasks)
    if len(leaf_list) > MAX_LEAVES:
        errors.append(f"The plan has {len(leaf_list)} executable tasks; the limit is {MAX_LEAVES}.")

    for t in leaf_list:
        dw = t["done_when"]
        if len(dw) < 15:
            errors.append(f"Task {t['key']} ('{t['title']}'): done_when is missing or too short. Say exactly what will "
                          "exist or be true when it's finished.")
        elif _VAGUE.search(dw) and not _CONCRETE.search(dw) and not t["checks"]:
            errors.append(f"Task {t['key']} ('{t['title']}'): done_when \"{dw}\" can't be checked. Name a file that will "
                          "exist, something it will contain, a number, or a command that will succeed.")
        if not t["instructions"]:
            errors.append(f"Task {t['key']} ('{t['title']}'): missing instructions.")

    # dependencies
    leaf_keys = {t["key"] for t in leaf_list}
    for t in tasks:
        for d in t["depends_on"]:
            if d not in by_key:
                errors.append(f"Task {t['key']}: depends_on '{d}' doesn't exist.")
            elif d == t["key"]:
                errors.append(f"Task {t['key']} depends on itself.")
    if not errors:
        cycle = _dependency_cycle(tasks)
        if cycle:
            errors.append(f"Dependency cycle: {' → '.join(cycle)}.")

    for t in tasks:
        for i, c in enumerate(t["checks"]):
            ctype = c.get("type") if isinstance(c, dict) else None
            if ctype not in CHECK_TYPES:
                errors.append(f"Task {t['key']}: check #{i + 1} has unknown type {ctype!r}. "
                              f"Use one of: {', '.join(CHECK_TYPES)}.")
                continue
            missing = [f for f in CHECK_TYPES[ctype] if not c.get(f)]
            if missing:
                errors.append(f"Task {t['key']}: {ctype} check needs {', '.join(missing)}.")
            if t["key"] not in leaf_keys:
                errors.append(f"Task {t['key']} has subtasks, so it can't have checks; put them on its subtasks.")
            if ctype == "command_ok" and policy is not None and guard is not None and c.get("command"):
                if policy.evaluate(c["command"], guard).action == "deny":
                    errors.append(f"Task {t['key']}: check command is blocked by policy: {c['command']}")
    return errors


def _dependency_cycle(tasks: list[dict]) -> list[str] | None:
    graph = {t["key"]: list(t["depends_on"]) for t in tasks}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(k: str) -> list[str] | None:
        state[k] = 1
        stack.append(k)
        for d in graph.get(k, []):
            if state.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if state.get(d) is None:
                found = visit(d)
                if found:
                    return found
        stack.pop()
        state[k] = 2
        return None

    for k in graph:
        if state.get(k) is None:
            found = visit(k)
            if found:
                return found
    return None


def dependencies_met(task: dict, by_key: dict[str, dict], finished: set[str]) -> bool:
    """A dependency on a parent task is met when all of that parent's leaves are finished."""
    for d in task["depends_on"]:
        dep = by_key.get(d)
        if dep is None:
            continue
        if d in finished:
            continue
        kids = [t for t in by_key.values() if t.get("parent_key") == d]
        if kids and all(dependencies_subtree_finished(k, by_key, finished) for k in kids):
            continue
        return False
    return True


def dependencies_subtree_finished(task: dict, by_key: dict[str, dict], finished: set[str]) -> bool:
    kids = [t for t in by_key.values() if t.get("parent_key") == task["key"]]
    if not kids:
        return task["key"] in finished
    return all(dependencies_subtree_finished(k, by_key, finished) for k in kids)


def ordered_leaves(tasks: list[dict]) -> list[dict]:
    """Leaves in depth-first plan order."""
    kids = children_map(tasks)
    out: list[dict] = []

    def walk(parent: str | None) -> None:
        for t in sorted(kids.get(parent, []), key=lambda x: x["position"]):
            if t["key"] in kids:
                walk(t["key"])
            else:
                out.append(t)

    walk(None)
    return out


def next_ready_leaf(tasks: list[dict]) -> dict | None:
    from .models import T_FINISHED, T_PENDING

    by_key = {t["key"]: t for t in tasks}
    finished = {t["key"] for t in tasks if t["status"] in T_FINISHED}
    for t in ordered_leaves(tasks):
        if t["status"] == T_PENDING and dependencies_met(t, by_key, finished):
            # A parent's dependencies apply to all of its subtasks.
            parent_ok, cur = True, t
            while cur.get("parent_key") and cur["parent_key"] in by_key:
                cur = by_key[cur["parent_key"]]
                if not dependencies_met(cur, by_key, finished):
                    parent_ok = False
                    break
            if parent_ok:
                return t
    return None


def outline(tasks: list[dict], current_key: str | None = None, max_chars: int = 3500) -> str:
    """Compact plan outline with statuses and short results, for task prompts."""
    marks = {"done": "✓", "skipped": "–", "failed": "✗", "running": "▶", "waiting_user": "?", "pending": "○"}
    kids = children_map(tasks)
    lines: list[str] = []

    def walk(parent: str | None, depth: int) -> None:
        for t in sorted(kids.get(parent, []), key=lambda x: x["position"]):
            mark = "▶" if t["key"] == current_key else marks.get(t["status"], "○")
            line = f"{'  ' * depth}{mark} [{t['key']}] {t['title']}"
            if t["status"] == "done" and t.get("result_summary") and t["key"] != current_key:
                line += f" — {t['result_summary'][:160]}"
            if t["key"] == current_key:
                line += "   ← YOUR TASK"
            lines.append(line)
            walk(t["key"], depth + 1)

    walk(None, 0)
    text = "\n".join(lines)
    if len(text) > max_chars:
        # Keep the neighborhood of the current task.
        idx = next((i for i, l in enumerate(lines) if "← YOUR TASK" in l), 0)
        window = lines[max(0, idx - 12): idx + 12]
        text = "… (plan truncated) …\n" + "\n".join(window) + "\n… (plan truncated) …"
    return text
