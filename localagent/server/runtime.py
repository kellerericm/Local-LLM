"""Long-lived application objects shared by the HTTP routes."""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from ..backend.worker import WorkerBackend
from ..config import Settings, save_settings
from ..coordinator import Coordinator
from ..jobs.models import JobStore
from ..jobs.runner import JobRunner
from ..resources import ResourceManager
from ..safety import ApprovalBroker
from ..store import Store
from ..tools import default_registry

log = logging.getLogger(__name__)


class EventBus:
    """Fan out events from worker threads to every connected WebSocket."""

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, event: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            loop.call_soon_threadsafe(q.put_nowait, event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)


class RunManager:
    """One agent run (user turn) per chat, each on its own thread."""

    def __init__(self, coordinator: Coordinator):
        self.coordinator = coordinator
        self._runs: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._lock = threading.Lock()

    def start(self, chat_id: str, text: str) -> bool:
        with self._lock:
            if chat_id in self._runs and self._runs[chat_id][0].is_alive():
                return False
            cancel = threading.Event()
            thread = threading.Thread(target=self._run, args=(chat_id, text, cancel), daemon=True,
                                      name=f"run-{chat_id}")
            self._runs[chat_id] = (thread, cancel)
            thread.start()
            return True

    def _run(self, chat_id: str, text: str, cancel: threading.Event) -> None:
        try:
            self.coordinator.run(chat_id, text, cancel)
        finally:
            with self._lock:
                if self._runs.get(chat_id, (None,))[0] is threading.current_thread():
                    del self._runs[chat_id]

    def cancel(self, chat_id: str) -> bool:
        with self._lock:
            run = self._runs.get(chat_id)
        if run:
            run[1].set()
            return True
        return False

    def cancel_all(self) -> None:
        with self._lock:
            runs = list(self._runs.values())
        for _, cancel in runs:
            cancel.set()

    def running(self) -> list[str]:
        with self._lock:
            return [cid for cid, (t, _) in self._runs.items() if t.is_alive()]


class Runtime:
    def __init__(self, settings: Settings, backend=None):
        self.settings = settings
        self._settings_lock = threading.Lock()
        self.bus = EventBus()
        self.store = Store(settings.db_path)
        self.approvals = ApprovalBroker(self.store, self.bus.publish)
        self.registry = default_registry()
        self.backend = backend if backend is not None else WorkerBackend(self.model_spec)
        self.resources = ResourceManager(self.get_settings, self.backend, self.bus.publish)
        self.coordinator = Coordinator(self.backend, self.store, self.registry, self.approvals, self.get_settings,
                                       self.bus.publish, self.resources)
        self.runs = RunManager(self.coordinator)
        self.jobs = JobStore(self.store)
        self.job_runner = JobRunner(self.jobs, self.store, self.coordinator, self.get_settings, self.bus.publish,
                                    self.approvals, chat_runs=self.runs)

    def get_settings(self) -> Settings:
        return self.settings

    def model_spec(self) -> dict:
        s = self.settings
        return {"model_id": s.model_id, "quantization": s.quantization, "max_vram_gb": s.resources.max_vram_gb,
                "cpu_threads": s.resources.cpu_threads, "cache_dir": str(Path(s.models_dir)),
                "offload": s.resources.offload, "max_cpu_ram_gb": s.resources.max_cpu_ram_gb}

    def update_settings(self, patch: dict) -> Settings:
        with self._settings_lock:
            new = self.settings.updated(patch)
            save_settings(new)
            self.settings = new
        self.bus.publish({"type": "settings", "settings": new.to_dict()})
        return new

    def start(self, run_jobs: bool = True) -> None:
        self.resources.start()
        if run_jobs:
            self.job_runner.start()

    def shutdown(self) -> None:
        self.runs.cancel_all()
        self.job_runner.stop()
        self.resources.stop()
        try:
            self.backend.unload(force=True)
        except Exception:
            log.exception("backend unload failed")
        self.store.close()
