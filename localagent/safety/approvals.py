"""Approval requests: a tool thread blocks until the user answers in the UI.

Decisions: "once" (allow this time), "always" (allow these keys for this project/scope), "deny".
"""
from __future__ import annotations

import threading
from typing import Callable

from ..store import Store

Emit = Callable[[dict], None]


class ApprovalBroker:
    def __init__(self, store: Store, emit: Emit):
        self.store = store
        self.emit = emit
        self._waiters: dict[str, tuple[threading.Event, list[str]]] = {}
        self._lock = threading.Lock()

    def request(self, chat_id: str | None, scope: str, keys: list[str], summary: str, detail: str,
                cancel: threading.Event | None = None) -> bool:
        if keys and all(self.store.has_rule(scope, k) for k in keys):
            return True
        approval = self.store.create_approval(chat_id, scope, keys, summary, detail)
        event, box = threading.Event(), []
        with self._lock:
            self._waiters[approval["id"]] = (event, box)
        self.emit({"type": "approval_request", "chat_id": chat_id, "approval": approval})
        try:
            while not event.wait(0.25):
                if cancel is not None and cancel.is_set():
                    self.store.decide_approval(approval["id"], "cancelled")
                    self.emit({"type": "approval_resolved", "chat_id": chat_id,
                               "approval_id": approval["id"], "decision": "cancelled"})
                    return False
        finally:
            with self._lock:
                self._waiters.pop(approval["id"], None)
        decision = box[0]
        if decision == "always":
            self.store.add_rules(scope, keys)
        return decision in ("once", "always")

    def resolve(self, approval_id: str, decision: str) -> bool:
        if decision not in ("once", "always", "deny"):
            raise ValueError(f"bad decision {decision!r}")
        with self._lock:
            waiter = self._waiters.get(approval_id)
        if waiter is None:
            return False
        approval = self.store.get_approval(approval_id)
        self.store.decide_approval(approval_id, decision)
        event, box = waiter
        box.append(decision)
        event.set()
        self.emit({"type": "approval_resolved", "chat_id": approval and approval["chat_id"],
                   "approval_id": approval_id, "decision": decision})
        return True

    def pending(self) -> list[dict]:
        with self._lock:
            live = set(self._waiters)
        return [a for a in self.store.pending_approvals() if a["id"] in live]


class AutoApprover:
    """Non-interactive stand-in for benchmarks and tests: always answers the same way."""

    def __init__(self, allow: bool = False):
        self.allow = allow
        self.requests: list[dict] = []

    def request(self, chat_id, scope, keys, summary, detail, cancel=None) -> bool:
        self.requests.append({"chat_id": chat_id, "scope": scope, "keys": keys, "summary": summary, "detail": detail})
        return self.allow

    def pending(self) -> list[dict]:
        return []
