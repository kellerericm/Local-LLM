"""Approval requests.

- Chats use `request`: the tool thread blocks until the user answers in the UI.
- Jobs use `request_async`: the request is recorded and the task parks, so an unanswered approval never stalls
  the rest of the job (or other jobs). The decision arrives later through a callback.

Decisions: "once" (allow this time), "always" (allow these keys for this project/scope), "deny".
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from ..store import Store

Emit = Callable[[dict], None]
log = logging.getLogger(__name__)


class ApprovalBroker:
    def __init__(self, store: Store, emit: Emit):
        self.store = store
        self.emit = emit
        self._waiters: dict[str, tuple[threading.Event, list[str]]] = {}
        self._async: dict[str, tuple[str, list[str], Callable[[str], None]]] = {}
        self._lock = threading.Lock()

    def request_async(self, chat_id: str | None, scope: str, keys: list[str], summary: str, detail: str, *,
                      job_id: str | None, on_decision: Callable[[str], None]) -> str | None:
        """Returns None if already allowed by a saved rule, else the id of a pending approval."""
        if keys and all(self.store.has_rule(scope, k) for k in keys):
            return None
        approval = self.store.create_approval(chat_id, scope, keys, summary, detail, job_id=job_id)
        with self._lock:
            self._async[approval["id"]] = (scope, keys, on_decision)
        self.emit({"type": "approval_request", "chat_id": chat_id, "job_id": job_id, "approval": approval})
        return approval["id"]

    def request(self, chat_id: str | None, scope: str, keys: list[str], summary: str, detail: str,
                cancel: threading.Event | None = None, job_id: str | None = None) -> bool:
        if keys and all(self.store.has_rule(scope, k) for k in keys):
            return True
        approval = self.store.create_approval(chat_id, scope, keys, summary, detail, job_id=job_id)
        event, box = threading.Event(), []
        with self._lock:
            self._waiters[approval["id"]] = (event, box)
        self.emit({"type": "approval_request", "chat_id": chat_id, "job_id": job_id, "approval": approval})
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
            pending_async = self._async.pop(approval_id, None)
        if pending_async is not None:
            scope, keys, on_decision = pending_async
            approval = self.store.get_approval(approval_id)
            self.store.decide_approval(approval_id, decision)
            if decision == "always":
                self.store.add_rules(scope, keys)
            self.emit({"type": "approval_resolved", "chat_id": approval and approval["chat_id"],
                       "job_id": approval and approval.get("job_id"), "approval_id": approval_id, "decision": decision})
            try:
                on_decision(decision)
            except Exception:
                log.exception("approval callback failed for %s", approval_id)
            return True
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
            live = set(self._waiters) | set(self._async)
        return [a for a in self.store.pending_approvals() if a["id"] in live]


class AutoApprover:
    """Non-interactive stand-in for benchmarks and tests.

    Blocking requests are answered immediately. Async (job) requests are allowed immediately when `allow` is
    true; otherwise they stay pending until the test calls `decide(approval_id, decision)`.
    """

    def __init__(self, allow: bool = False):
        self.allow = allow
        self.requests: list[dict] = []
        self.callbacks: dict[str, Callable[[str], None]] = {}

    def request(self, chat_id, scope, keys, summary, detail, cancel=None, job_id=None) -> bool:
        self.requests.append({"chat_id": chat_id, "scope": scope, "keys": keys, "summary": summary, "detail": detail,
                              "job_id": job_id})
        return self.allow

    def request_async(self, chat_id, scope, keys, summary, detail, *, job_id=None, on_decision) -> str | None:
        self.requests.append({"chat_id": chat_id, "scope": scope, "keys": keys, "summary": summary, "detail": detail,
                              "job_id": job_id})
        if self.allow:
            return None
        aid = f"auto-{len(self.requests)}"
        self.callbacks[aid] = on_decision
        return aid

    def decide(self, approval_id: str, decision: str) -> None:
        self.callbacks.pop(approval_id)(decision)

    def pending(self) -> list[dict]:
        return []
