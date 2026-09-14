"""File tools: read, write, edit, list, glob, grep."""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from .registry import Tool, ToolContext, ToolError, ToolResult

MAX_READ_CHARS = 60_000
MAX_LIST_ENTRIES = 500
MAX_MATCHES = 200
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".idea"}


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return False


def _rel(ctx: ToolContext, p: Path) -> str:
    try:
        return str(p.relative_to(ctx.guard.resolve(ctx.workspace)))
    except ValueError:
        return str(p)


def read_file(ctx: ToolContext, path: str, offset: int = 1, limit: int = 400) -> ToolResult:
    p = ctx.check_path(path, "read")
    if not p.exists():
        raise ToolError(f"File not found: {p}")
    if p.is_dir():
        raise ToolError(f"{p} is a directory; use list_dir.")
    if _is_binary(p):
        raise ToolError(f"{p} looks like a binary file ({p.stat().st_size} bytes); not reading it as text.")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(offset, 1)
    chunk = lines[start - 1:start - 1 + limit]
    out, size = [], 0
    for i, line in enumerate(chunk, start):
        row = f"{i:>6}\t{line}"
        size += len(row) + 1
        if size > MAX_READ_CHARS:
            out.append(f"... [output truncated at {MAX_READ_CHARS} chars; use offset={i}]")
            break
        out.append(row)
    end = start - 1 + len(chunk)
    header = f"{p} (lines {start}-{end} of {len(lines)})"
    if end < len(lines):
        header += f"; more remains, use offset={end + 1}"
    return ToolResult(header + "\n" + "\n".join(out))


def write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    p = ctx.check_path(path, "write")
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content, encoding="utf-8", newline="")
    return ToolResult(f"{'Overwrote' if existed else 'Created'} {p} ({len(content)} chars).")


def edit_file(ctx: ToolContext, path: str, old_text: str, new_text: str, replace_all: bool = False) -> ToolResult:
    p = ctx.check_path(path, "write")
    if not p.exists():
        raise ToolError(f"File not found: {p}. Use write_file to create it.")
    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_text)
    if count == 0:
        raise ToolError("old_text was not found in the file. Read the file again and copy the exact text, "
                        "including whitespace and indentation.")
    if count > 1 and not replace_all:
        raise ToolError(f"old_text appears {count} times. Include more surrounding context to make it unique, "
                        "or set replace_all=true.")
    text = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
    p.write_text(text, encoding="utf-8", newline="")
    return ToolResult(f"Edited {p}: replaced {count if replace_all else 1} occurrence(s).")


def list_dir(ctx: ToolContext, path: str = ".") -> ToolResult:
    p = ctx.check_path(path, "read")
    if not p.is_dir():
        raise ToolError(f"Not a directory: {p}")
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    rows = []
    for e in entries[:MAX_LIST_ENTRIES]:
        if e.is_dir():
            rows.append(f"{e.name}/")
        else:
            try:
                rows.append(f"{e.name}  ({e.stat().st_size} bytes)")
            except OSError:
                rows.append(e.name)
    more = f"\n... and {len(entries) - MAX_LIST_ENTRIES} more" if len(entries) > MAX_LIST_ENTRIES else ""
    return ToolResult(f"{p} ({len(entries)} entries)\n" + ("\n".join(rows) or "(empty)") + more)


def glob_match(rel_posix: str, spec: str) -> bool:
    """Match a path relative to the search root against a glob spec.

    Accepts what models commonly write: bare names ('*.py'), path globs ('src/**/*.py', where
    '**/' may match zero directories), comma/semicolon/space-separated lists, and '{py,md}' braces.
    """
    name = rel_posix.rsplit("/", 1)[-1]
    patterns: list[str] = []
    for part in re.split(r"[,;\s]+(?![^{]*\})", spec.strip()):   # don't split inside {a,b}
        if not part:
            continue
        m = re.search(r"\{([^}]*)\}", part)
        alts = [part[:m.start()] + a + part[m.end():] for a in m.group(1).split(",")] if m else [part]
        patterns.extend(p.replace("\\", "/").removeprefix("./") for p in alts)
    for pat in patterns:
        variants = {pat, pat.replace("**/", ""), pat.replace("/**/", "/")}
        for v in variants:
            if fnmatch.fnmatch(rel_posix, v) or ("/" not in v and fnmatch.fnmatch(name, v)):
                return True
    return False


def _walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def glob_files(ctx: ToolContext, pattern: str, path: str = ".") -> ToolResult:
    root = ctx.check_path(path, "read")
    matches = []
    for f in _walk(root):
        rel = f.relative_to(root).as_posix()
        if glob_match(rel, pattern):
            matches.append(rel)
            if len(matches) >= MAX_MATCHES:
                break
    if not matches:
        return ToolResult(f"No files under {root} match {pattern!r}.")
    note = f"\n(stopped at {MAX_MATCHES} matches)" if len(matches) >= MAX_MATCHES else ""
    return ToolResult(f"{len(matches)} match(es) under {root}:\n" + "\n".join(matches) + note)


def grep(ctx: ToolContext, pattern: str, path: str = ".", file_glob: str | None = None,
         ignore_case: bool = False) -> ToolResult:
    root = ctx.check_path(path, "read")
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"Invalid regex: {e}")
    files = [root] if root.is_file() else _walk(root)
    hits = []
    for f in files:
        if file_glob and root.is_dir() and not glob_match(f.relative_to(root).as_posix(), file_glob):
            continue
        if _is_binary(f):
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{_rel(ctx, f)}:{n}: {line.rstrip()[:300]}")
                        if len(hits) >= MAX_MATCHES:
                            break
        except OSError:
            continue
        if len(hits) >= MAX_MATCHES:
            break
    if not hits:
        scope = f" in files matching {file_glob!r}" if file_glob else ""
        return ToolResult(f"No matches for {pattern!r} under {root}{scope}. "
                          + ("Try again without file_glob to search all files." if file_glob else ""))
    note = f"\n(stopped at {MAX_MATCHES} matches; narrow the search)" if len(hits) >= MAX_MATCHES else ""
    return ToolResult("\n".join(hits) + note)


_PATH = {"type": "string", "description": "File path, absolute or relative to the workspace."}

TOOLS = [
    Tool("read_file", "Read a text file with line numbers. Use offset/limit for large files.",
         {"type": "object", "properties": {
             "path": _PATH,
             "offset": {"type": "integer", "minimum": 1, "description": "First line to read (1-based)."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "Max lines (default 400)."}},
          "required": ["path"]}, read_file, "files"),
    Tool("write_file", "Create or overwrite a text file with the given content.",
         {"type": "object", "properties": {"path": _PATH, "content": {"type": "string"}},
          "required": ["path", "content"]}, write_file, "files"),
    Tool("edit_file", "Replace exact text in a file. old_text must match exactly and be unique unless replace_all is true. Read the file first.",
         {"type": "object", "properties": {
             "path": _PATH, "old_text": {"type": "string"}, "new_text": {"type": "string"},
             "replace_all": {"type": "boolean"}},
          "required": ["path", "old_text", "new_text"]}, edit_file, "files"),
    Tool("list_dir", "List the entries of a directory.",
         {"type": "object", "properties": {"path": {"type": "string", "description": "Directory (default: workspace)."}}},
         list_dir, "files"),
    Tool("glob", "Find files by name pattern, e.g. '*.py' or 'src/**/*.md'.",
         {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
          "required": ["pattern"]}, glob_files, "files"),
    Tool("grep", "Search file contents with a regular expression. Returns file:line: text.",
         {"type": "object", "properties": {
             "pattern": {"type": "string"}, "path": {"type": "string"},
             "file_glob": {"type": "string", "description": "Only search files whose name matches, e.g. '*.py'."},
             "ignore_case": {"type": "boolean"}},
          "required": ["pattern"]}, grep, "files"),
]
