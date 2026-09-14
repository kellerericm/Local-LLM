"""Turn raw model output into (reasoning, content, tool calls).

Models differ in how they emit tool calls:
- "hermes" (Qwen3, many others):
    <tool_call>{"name": "...", "arguments": {...}}</tool_call>
- "qwen3_coder" (Qwen3.5+ and Qwen3-Coder):
    <tool_call><function=name><parameter=path>value</parameter></function></tool_call>
- "auto" detects per message.
Both may be preceded by <think>...</think> reasoning.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ParsedOutput:
    content: str
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)   # [{"name", "arguments"}]
    errors: list[str] = field(default_factory=list)
    format: str = "hermes"


_TOOL_BLOCK = re.compile(r"<tool_call>(.*?)(?:</tool_call>|$)", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")
_FUNCTION = re.compile(r"<function=([^>\s]+)\s*>(.*?)(?:</function>|$)", re.S)
_PARAM = re.compile(r"<parameter=([^>\s]+)\s*>(.*?)(?:</parameter>|(?=<parameter=)|$)", re.S)


def _split_reasoning(text: str) -> tuple[str, str]:
    if "</think>" in text:
        before, after = text.split("</think>", 1)
        return before.replace("<think>", "").strip(), after
    stripped = text.lstrip()
    if stripped.startswith("<think>"):          # never closed (e.g. ran out of tokens)
        return stripped[len("<think>"):].strip(), ""
    return "", text


_CONTROL_TO_ESCAPE = {"\n": "\\n", "\t": "\\t", "\r": "\\r", "\b": "\\b", "\f": "\\f"}
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")


_PATH_KEYS = re.compile(r"path|dir|folder|cwd|file", re.I)


def _fix_windows_paths(args):
    """A path like "D:\\new\\bench" written without escaping parses with \\n and \\b turned into control
    characters. Restore them, but only in path-like arguments so real newlines in content survive."""
    if not isinstance(args, dict):
        return args
    fixed = {}
    for k, v in args.items():
        if isinstance(v, str) and _PATH_KEYS.search(k) and _DRIVE_PATH.match(v) and any(c in v for c in _CONTROL_TO_ESCAPE):
            v = "".join(_CONTROL_TO_ESCAPE.get(c, c) for c in v)
        fixed[k] = v
    return fixed


def _load_json_call(body: str) -> tuple[dict | None, str | None]:
    body = _FENCE.sub("", body.strip())
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        # Common small-model slips: unescaped Windows backslashes, trailing commas, a missing final brace.
        repaired = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", body) if "escape" in e.msg else body
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
        for candidate in (repaired, repaired + "}", repaired + "}}"):
            try:
                obj = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
        else:
            hint = (" Backslashes inside JSON strings must be doubled (C:\\\\dir); simpler: use forward slashes "
                    "(C:/dir) or relative paths.") if "escape" in e.msg else ""
            return None, f"invalid JSON ({e.msg} at char {e.pos}): {body[:200]}.{hint}"
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
        return None, f"expected an object with a string 'name': {body[:200]}"
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return None, f"'arguments' for {obj['name']} is a string that is not valid JSON"
    return {"name": obj["name"], "arguments": _fix_windows_paths(args)}, None


def _schema_types(tools: list[dict] | None, name: str) -> dict[str, object]:
    for t in tools or []:
        fn = t.get("function", t)
        if fn.get("name") == name:
            return {k: v.get("type", "string") for k, v in fn.get("parameters", {}).get("properties", {}).items()}
    return {}


def _coerce(raw: str, type_: object):
    # Values are raw text; strip exactly one leading/trailing newline added by the format.
    value = raw[1:] if raw.startswith("\n") else raw
    value = value[:-1] if value.endswith("\n") else value
    if type_ == "string" or type_ is None:
        return value
    try:
        return json.loads(value.strip())
    except json.JSONDecodeError:
        low = value.strip().lower()
        if type_ == "boolean" and low in ("true", "false"):
            return low == "true"
        return value


def _load_xml_call(body: str, tools: list[dict] | None) -> tuple[dict | None, str | None]:
    m = _FUNCTION.search(body)
    if not m:
        return None, f"expected <function=name>...</function>: {body.strip()[:200]}"
    name = m.group(1).strip()
    types = _schema_types(tools, name)
    args = {p.group(1).strip(): _coerce(p.group(2), types.get(p.group(1).strip(), "string"))
            for p in _PARAM.finditer(m.group(2))}
    return {"name": name, "arguments": args}, None


def parse(text: str, tools: list[dict] | None = None, fmt: str = "auto") -> ParsedOutput:
    reasoning, rest = _split_reasoning(text)
    blocks = list(_TOOL_BLOCK.finditer(rest))
    if fmt == "auto":
        fmt = "qwen3_coder" if ("<function=" in rest) else "hermes"
    calls, errors = [], []
    for m in blocks:
        if not m.group(0).endswith("</tool_call>"):
            errors.append("a <tool_call> block was not closed (output may have been cut off)")
        loader = _load_xml_call if fmt == "qwen3_coder" else (lambda b, _t: _load_json_call(b))
        call, err = loader(m.group(1), tools)
        if call:
            calls.append(call)
        elif err:
            errors.append(err)
    content = _TOOL_BLOCK.sub("", rest).strip()
    if not blocks and fmt == "qwen3_coder" and "<function=" in content:   # tags without <tool_call>
        call, err = _load_xml_call(content, tools)
        if call:
            calls.append(call)
            content = content[:content.find("<function=")].strip()
    if not calls and not errors and content.startswith("{") and content.endswith("}"):
        call, _ = _load_json_call(content)       # bare JSON call without tags
        if call and isinstance(call["arguments"], dict):
            calls.append(call)
            content = ""
    return ParsedOutput(content=content, reasoning=reasoning, tool_calls=calls, errors=errors, format=fmt)


def parse_hermes(text: str, tools: list[dict] | None = None) -> ParsedOutput:
    return parse(text, tools, "hermes")


def parse_qwen3_coder(text: str, tools: list[dict] | None = None) -> ParsedOutput:
    return parse(text, tools, "qwen3_coder")


FORMAT_REMINDER = {
    "hermes": 'Call tools like this: <tool_call>{"name": "tool_name", "arguments": {"arg": "value"}}</tool_call>',
    "qwen3_coder": ("Call tools like this:\n<tool_call>\n<function=tool_name>\n<parameter=arg>\nvalue\n</parameter>\n"
                    "</function>\n</tool_call>"),
}

FORMATS = ("auto", "hermes", "qwen3_coder")


def get_parser(name: str) -> Callable[[str, list[dict] | None], ParsedOutput]:
    fmt = name if name in FORMATS else "auto"
    return lambda text, tools=None: parse(text, tools, fmt)
