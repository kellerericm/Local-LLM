"""Model worker process and the client the app uses to talk to it.

The model lives in its own process so that:
- unloading frees VRAM completely (the process exits),
- its CPU priority can be lowered without slowing the UI,
- a crash in CUDA code doesn't take down the app.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import queue
import threading
import time
import traceback
from typing import Callable, Iterator

from .base import GenerationCancelled


def worker_main(spec: dict, requests, responses, pause_ev, cancel_ev) -> None:
    if spec.get("cache_dir"):
        os.environ.setdefault("HF_HOME", spec["cache_dir"])
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:
        pass
    try:
        from .transformers_backend import Control, TransformersBackend
        backend = TransformersBackend(**spec)
        backend.load()
    except BaseException:
        responses.put({"type": "load_error", "error": traceback.format_exc()})
        return
    responses.put({"type": "loaded", "placement": backend.placement})
    control = Control(pause=pause_ev, cancel=cancel_ev)
    while True:
        req = requests.get()
        op = req.get("op")
        if op == "shutdown":
            break
        if op == "generate":
            cancel_ev.clear()
            try:
                for chunk in backend.generate(req["messages"], req["tools"], req["params"], req.get("adapter"), control):
                    if isinstance(chunk, dict):
                        responses.put({"type": "usage", "id": req["id"], "usage": chunk["usage"]})
                    else:
                        responses.put({"type": "token", "id": req["id"], "text": chunk})
                responses.put({"type": "done", "id": req["id"], "cancelled": cancel_ev.is_set()})
            except BaseException:
                responses.put({"type": "error", "id": req["id"], "error": traceback.format_exc()})
        elif op == "count_tokens":
            responses.put({"type": "count", "id": req["id"], "value": backend.count_tokens(req["text"])})


class WorkerBackend:
    """ModelBackend implementation that forwards to the worker process, loading it on demand."""

    def __init__(self, spec_getter: Callable[[], dict]):
        self._spec_getter = spec_getter
        self._ctx = mp.get_context("spawn")
        self._proc = None
        self._requests = self._responses = None
        self._pause = self._ctx.Event()
        self._cancel = self._ctx.Event()
        self._gen_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._busy = False
        self._loading = False
        self._spec: dict | None = None
        self.last_used = time.time()
        self.last_error: str | None = None
        self.placement: dict = {}

    # -- lifecycle ---------------------------------------------------------
    @property
    def worker_pid(self) -> int | None:
        return self._proc.pid if self._proc is not None and self._proc.is_alive() else None

    def is_loaded(self) -> bool:
        return self._proc is not None and self._proc.is_alive() and not self._loading

    def is_busy(self) -> bool:
        return self._busy or self._loading

    def _ensure_loaded(self, on_status: Callable[[str], None] | None, cancel: threading.Event | None) -> None:
        spec = self._spec_getter()
        with self._state_lock:
            if self._proc is not None and self._proc.is_alive() and spec == self._spec:
                return
            self._stop_process()
            if on_status:
                on_status("loading_model")
            self._loading = True
            self._spec = spec
            self._requests, self._responses = self._ctx.Queue(), self._ctx.Queue()
            self._pause.clear()
            self._cancel.clear()
            self._proc = self._ctx.Process(target=worker_main, name="localagent-model",
                                           args=(spec, self._requests, self._responses, self._pause, self._cancel),
                                           daemon=True)
            self._proc.start()
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    self._stop_process()
                    raise GenerationCancelled()
                try:
                    msg = self._responses.get(timeout=0.5)
                except queue.Empty:
                    if not self._proc.is_alive():
                        raise RuntimeError(f"Model worker exited while loading (exit code {self._proc.exitcode}).")
                    continue
                if msg["type"] == "loaded":
                    self.last_error = None
                    self.placement = msg.get("placement") or {}
                    return
                if msg["type"] == "load_error":
                    self.last_error = msg["error"]
                    self._stop_process()
                    raise RuntimeError("Failed to load model:\n" + msg["error"])
        finally:
            self._loading = False

    def _stop_process(self) -> None:
        with self._state_lock:
            proc = self._proc
            if proc is None:
                return
            if proc.is_alive():
                try:
                    self._requests.put({"op": "shutdown"})
                except Exception:
                    pass
                proc.join(timeout=10)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=5)
            self._proc = None
            self._spec = None

    def unload(self, force: bool = False) -> bool:
        if self._busy and not force:
            return False
        if force:
            self._cancel.set()
        self._stop_process()
        return True

    def set_paused(self, paused: bool) -> None:
        (self._pause.set if paused else self._pause.clear)()

    def status(self) -> dict:
        spec = self._spec or self._spec_getter()
        return {"backend": "transformers", "model_id": spec.get("model_id"), "loaded": self.is_loaded(),
                "loading": self._loading, "busy": self._busy, "paused": self._pause.is_set(),
                "worker_pid": self.worker_pid, "last_error": self.last_error,
                "placement": self.placement if self.is_loaded() else {}}

    # -- generation --------------------------------------------------------
    def generate(self, messages, tools, params, adapter=None, cancel=None, on_status=None) -> Iterator[str]:
        while not self._gen_lock.acquire(timeout=0.5):
            if on_status:
                on_status("queued")
            if cancel is not None and cancel.is_set():
                raise GenerationCancelled()
        finished = True
        try:
            self._ensure_loaded(on_status, cancel)
            if on_status:
                on_status("generating")
            self._busy = True
            req_id = f"g{time.time_ns()}"
            self._cancel.clear()
            self._requests.put({"op": "generate", "id": req_id, "messages": messages, "tools": tools,
                                "params": params, "adapter": adapter})
            finished = False
            while True:
                if cancel is not None and cancel.is_set():
                    self._cancel.set()
                try:
                    msg = self._responses.get(timeout=0.5)
                except queue.Empty:
                    if not self._proc or not self._proc.is_alive():
                        raise RuntimeError("Model worker stopped unexpectedly during generation.")
                    continue
                if msg.get("id") != req_id:
                    continue
                if msg["type"] == "token":
                    yield msg["text"]
                elif msg["type"] == "usage":
                    yield {"usage": msg["usage"]}
                elif msg["type"] == "done":
                    finished = True
                    return
                elif msg["type"] == "error":
                    finished = True
                    raise RuntimeError("Generation failed:\n" + msg["error"])
        finally:
            if not finished:
                self._cancel.set()          # consumer stopped early; stop the worker too
            self._busy = False
            self.last_used = time.time()
            self._gen_lock.release()
