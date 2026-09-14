"""The interface every model backend implements.

Backends are swappable: Transformers today; llama.cpp or others later. Messages use the
OpenAI-style chat format; tools are JSON-schema function definitions.
"""
from __future__ import annotations

import threading
from typing import Callable, Iterator, Protocol


class GenerationCancelled(Exception):
    pass


class ModelBackend(Protocol):
    def generate(self, messages: list[dict], tools: list[dict] | None, params: dict,
                 adapter: dict | None = None, cancel: threading.Event | None = None,
                 on_status: Callable[[str], None] | None = None) -> Iterator[str]:
        """Yield text chunks. `adapter` ({"name", "path"}) selects a LoRA specialist; None = base model."""
        ...

    def is_loaded(self) -> bool: ...
    def is_busy(self) -> bool: ...
    def unload(self) -> bool: ...
    def set_paused(self, paused: bool) -> None: ...
    def status(self) -> dict: ...
