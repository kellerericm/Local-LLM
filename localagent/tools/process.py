"""Run a child process politely: below-normal priority, output cap, timeout, cancel, kill whole tree."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import psutil

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000
OUTPUT_HEAD = 12_000
OUTPUT_TAIL = 8_000


def env_for(env_path: Path) -> dict:
    """Environment where `python`/`pip` resolve to the project's Python environment."""
    env = dict(os.environ)
    extra = [env_path, env_path / "Scripts", env_path / "Library" / "bin"]
    env["PATH"] = os.pathsep.join([str(p) for p in extra if p.exists()] + [env.get("PATH", "")])
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def kill_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)


def _truncate(text: str) -> str:
    if len(text) <= OUTPUT_HEAD + OUTPUT_TAIL:
        return text
    skipped = len(text) - OUTPUT_HEAD - OUTPUT_TAIL
    return f"{text[:OUTPUT_HEAD]}\n\n... [{skipped} chars of output omitted] ...\n\n{text[-OUTPUT_TAIL:]}"


def run_process(argv: list[str], cwd: Path, env: dict, timeout_s: float,
                cancel: threading.Event | None = None) -> tuple[int | None, str, str]:
    """Returns (exit_code, output, status) where status is ok | timeout | cancelled."""
    flags = BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
    chunks: list[bytes] = []
    size = [0]

    def pump():
        for chunk in iter(lambda: proc.stdout.read(4096), b""):
            if size[0] < 4_000_000:          # keep memory bounded for runaway output
                chunks.append(chunk)
                size[0] += len(chunk)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_s
    status = "ok"
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            status = "cancelled"
        elif time.monotonic() > deadline:
            status = "timeout"
        if status != "ok":
            kill_tree(proc.pid)
            break
        time.sleep(0.1)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
    reader.join(timeout=5)
    output = b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n")
    return proc.returncode, _truncate(output), status


def format_result(exit_code: int | None, output: str, status: str, timeout_s: float) -> tuple[str, bool]:
    body = output.strip() or "(no output)"
    if status == "timeout":
        return f"Timed out after {timeout_s:.0f}s and was stopped.\nOutput so far:\n{body}", False
    if status == "cancelled":
        return f"Cancelled by the user.\nOutput so far:\n{body}", False
    return f"Exit code: {exit_code}\n{body}", exit_code == 0
