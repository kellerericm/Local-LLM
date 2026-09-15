"""read_document: readable text from PDF, DOCX, and plain-text files (chats and jobs)."""
from __future__ import annotations

from pathlib import Path

from ..jobs.documents import DOC_TYPES, extract
from .registry import Tool, ToolContext, ToolError, ToolResult

MAX_CHARS = 40_000


def doc_cache(ctx: ToolContext) -> Path:
    return Path(ctx.settings.data_dir) / "doc_cache"


def read_document(ctx: ToolContext, path: str, offset: int = 1, limit: int = 300) -> ToolResult:
    p = ctx.check_path(path, "read")
    if not p.is_file():
        raise ToolError(f"File not found: {p}")
    if p.suffix.lower() not in DOC_TYPES:
        raise ToolError(f"{p.suffix} isn't a supported document type ({', '.join(sorted(DOC_TYPES))}). "
                        "Use read_file for code and other text.")
    try:
        doc = extract(p, doc_cache(ctx))
    except Exception as e:
        raise ToolError(f"Couldn't extract text from {p.name}: {type(e).__name__}: {e}")
    lines = doc.text.splitlines()
    start = max(1, offset)
    chunk, size = [], 0
    for i, line in enumerate(lines[start - 1:start - 1 + limit], start):
        row = f"{i:>6}\t{line}"
        size += len(row) + 1
        if size > MAX_CHARS:
            break
        chunk.append(row)
    end = start - 1 + len(chunk)
    header = f"{p.name}: lines {start}-{end} of {len(lines)}" + (f", {doc.pages} pages" if doc.pages else "")
    if end < len(lines):
        header += f"; continue with offset={end + 1}"
    if doc.warning:
        header += f"\nWarning: {doc.warning}"
    return ToolResult(header + "\n" + "\n".join(chunk))


TOOLS = [
    Tool("read_document",
         "Read the text of a document (PDF, DOCX, TXT, MD) in pieces, with line numbers. PDFs include [page N] "
         "markers. Use offset/limit to page through long documents.",
         {"type": "object", "properties": {
             "path": {"type": "string"},
             "offset": {"type": "integer", "minimum": 1, "description": "first line (1-based)"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "lines to return (default 300)"}},
          "required": ["path"]},
         read_document, "files"),
]
