"""Backends that need no GPU: a scripted one for tests and an echo one for trying the UI."""
from __future__ import annotations

import threading
import time
from typing import Callable, Iterator


class ScriptedBackend:
    """Returns pre-written responses in order. A response may be a callable(messages) -> str."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.paused = False

    def generate(self, messages, tools, params, adapter=None, cancel=None, on_status=None) -> Iterator[str]:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "params": params, "adapter": adapter})
        if not self.responses:
            yield "All done."
            return
        r = self.responses.pop(0)
        text = r(messages) if callable(r) else r
        for i in range(0, len(text), 16):
            if cancel is not None and cancel.is_set():
                return
            yield text[i:i + 16]

    def is_loaded(self): return True
    def is_busy(self): return False
    def unload(self, force=False): return True
    def set_paused(self, paused): self.paused = paused
    def status(self): return {"backend": "scripted", "loaded": True, "busy": False}


class EchoBackend:
    """Fake model for exercising the app without a GPU (`python -m localagent --fake-model`)."""

    def __init__(self):
        self._busy = False
        self.last_used = time.time()

    def generate(self, messages, tools, params, adapter=None, cancel=None, on_status=None):
        self._busy = True
        try:
            last = next((m for m in reversed(messages) if m["role"] in ("user", "tool")), None)
            if last and last["role"] == "user" and last["content"].strip().lower().startswith("/tasks"):
                text = ('<tool_call>{"name": "update_tasks", "arguments": {"tasks": ['
                        '{"content": "Look around the workspace", "status": "completed"},'
                        '{"content": "Pretend to do the work", "status": "in_progress"}]}}</tool_call>')
            elif last and last["role"] == "user" and last["content"].strip().lower().startswith("/ls"):
                text = '<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>'
            elif last and last["role"] == "user" and last["content"].strip().lower().startswith("/outside"):
                text = '<tool_call>{"name": "read_file", "arguments": {"path": "C:\\\\Windows\\\\win.ini"}}</tool_call>'
            elif last and last["role"] == "tool":
                text = f"(fake model) The tool returned {len(last['content'])} characters. Done."
            else:
                said = last["content"] if last else ""
                text = (f"<think>The user said something; I'm a fake model.</think>(fake model) You said: {said}\n\n"
                        "Try `/ls`, `/tasks`, or `/outside` to exercise tools and approvals.")
            started = time.time()
            for i in range(0, len(text), 6):
                if cancel is not None and cancel.is_set():
                    return
                time.sleep(0.01)
                yield text[i:i + 6]
            # Rough stand-in numbers so the UI's usage display can be exercised without a GPU.
            thinking_tokens = len(text.split("</think>")[0]) // 4 if "</think>" in text else 0
            total = len(text) // 4
            elapsed = max(time.time() - started, 0.01)
            yield {"usage": {"prompt_tokens": sum(len(str(m.get("content") or "")) for m in messages) // 4,
                             "completion_tokens": total, "thinking_tokens": thinking_tokens,
                             "answer_tokens": total - thinking_tokens, "seconds": round(elapsed, 2),
                             "tokens_per_s": round(total / elapsed, 1), "thinking_budget_hit": False}}
        finally:
            self._busy = False
            self.last_used = time.time()

    def is_loaded(self): return True
    def is_busy(self): return self._busy
    def unload(self, force=False): return True
    def set_paused(self, paused): pass
    def status(self): return {"backend": "echo", "model_id": "fake", "loaded": True, "busy": self._busy}
