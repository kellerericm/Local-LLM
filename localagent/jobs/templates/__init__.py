"""Job templates: how a kind of job builds and grows its plan (design §6)."""
from __future__ import annotations

from .base import HandlerResult, Template

_REGISTRY: dict[str, Template] = {}


def register(template: Template) -> Template:
    _REGISTRY[template.name] = template
    return template


def get_template(name: str | None) -> Template:
    _load()
    return _REGISTRY.get(name or "generic", _REGISTRY["generic"])


def list_templates() -> list[dict]:
    _load()
    return [{"name": t.name, "label": t.label, "description": t.description, "inputs": t.inputs_schema}
            for t in _REGISTRY.values()]


def _load() -> None:
    # Importing is idempotent; modules register themselves. Importing one template directly must not hide the others.
    from . import deep_research, generic, research_report  # noqa: F401


__all__ = ["HandlerResult", "Template", "get_template", "list_templates", "register"]
