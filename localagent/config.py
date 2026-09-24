"""Settings: where things live, which model runs, and resource caps.

The data directory is fixed per install (env var LOCALAGENT_DATA_DIR, default D:\\LocalAgent\\data)
because settings.json itself lives in it. Everything else is editable from the UI.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("LOCALAGENT_DATA_DIR", r"D:\LocalAgent\data"))


@dataclass
class ResourceSettings:
    max_vram_gb: float = 14.0            # passed to from_pretrained(max_memory=...)
    offload: str = "auto"                # auto: overflow to system RAM | gpu_only: everything on the GPU or fail
    max_cpu_ram_gb: float = 12.0         # how much system RAM offloaded layers may use
    cpu_threads: int = 8                 # torch threads in the model worker
    idle_unload_minutes: float = 15.0    # 0 disables idle unload
    pause_when_gpu_busy: bool = True
    gpu_busy_util_pct: int = 40          # other processes' GPU utilization that counts as busy
    gpu_busy_mem_gb: float = 3.0         # other processes' VRAM use that counts as busy
    background_hours: str = ""           # e.g. "22-7"; empty = any time (used by long-term tasks)


@dataclass
class Settings:
    models_dir: str = r"D:\LocalAgent\models"
    env_path: str = sys.prefix
    model_id: str = "Qwen/Qwen3.5-9B"          # chosen by benchmark, see experiments.md
    quantization: str = "4bit"           # none | 8bit | 4bit
    tool_call_format: str = "auto"          # auto | hermes | qwen3_coder
    preset: str = "thinking_coding"          # see backend/model_profiles.py; "custom" = hand-tuned
    thinking: bool = True
    thinking_budget: int = 0                 # max reasoning tokens per reply; 0 = no limit
    context_tokens: int = 20000            # 16 GB GPU + Qwen3.5-9B 4-bit: >~22k overflows VRAM (experiments.md)
    # The window is split three ways, and the three shares are bounded by it. Reading is the material a task is
    # given (a chunk of a document); reasoning is what it may spend thinking; output is what it may write. Filling
    # the window with material instead left nothing for the other two, and a task paged through one 33k-character
    # part for nine hours without finishing it.
    reading_share_pct: int = 50
    reasoning_share_pct: int = 10
    # A reading task is asked for all of its notes in one message, so the output share has to hold them. At 20 a
    # 25k-character section truncated mid-call every time (3 batches, all stopping at the same 4,200 tokens), and
    # the task restarted its whole pass after each one, producing 65% duplicates and never finishing.
    output_share_pct: int = 40
    max_new_tokens: int = 4096
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 20
    min_p: float = 0.0
    presence_penalty: float = 0.0
    repetition_penalty: float = 1.0
    # Debugging mode. Off is the system's normal state and carries no bounds of its own, because the bounds a real
    # task needs are not known before it runs. Turning it on applies the limits below, which exist to make a fault
    # show itself during investigation and are not a way to run work.
    debug_mode: bool = False
    debug_stop_after_repeats: int = 3        # only applies while debug_mode is on
    max_steps: int = 0                       # 0 = no limit; a run ends when the work does or the budget does
    max_consecutive_failures: int = 0        # 0 = never abandon a task over repeated tool errors
    tool_timeout_s: int = 0                  # 0 = a tool runs until it finishes
    host: str = "127.0.0.1"
    port: int = 8765
    resources: ResourceSettings = field(default_factory=ResourceSettings)
    data_dir: str = str(DEFAULT_DATA_DIR)

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / "localagent.sqlite3"

    @property
    def general_workspace(self) -> Path:
        return self.data_path / "general_workspace"

    @property
    def tmp_dir(self) -> Path:
        return self.data_path / "tmp"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known and k != "resources"}
        res_known = {f.name for f in fields(ResourceSettings)}
        res = ResourceSettings(**{k: v for k, v in (data.get("resources") or {}).items() if k in res_known})
        return cls(**kwargs, resources=res)

    def updated(self, patch: dict) -> "Settings":
        data = self.to_dict()
        for k, v in patch.items():
            if k == "resources" and isinstance(v, dict):
                data["resources"].update(v)
            elif k != "data_dir":
                data[k] = v
        return Settings.from_dict(data).with_budgets()

    def with_budgets(self) -> "Settings":
        """Keep the three shares inside the window, and derive the generation numbers from them. max_new_tokens is
        a whole reply including its thinking, so it carries the reasoning and output shares together."""
        shares = [max(0, int(self.reading_share_pct)), max(0, int(self.reasoning_share_pct)),
                  max(0, int(self.output_share_pct))]
        total = sum(shares) or 1
        if total > 100:                                   # scale back proportionally rather than refuse a save
            shares = [int(x * 100 / total) for x in shares]
        read_s, think_s, out_s = shares
        window = max(1024, int(self.context_tokens))
        data = self.to_dict()
        data.update(reading_share_pct=read_s, reasoning_share_pct=think_s, output_share_pct=out_s,
                    max_new_tokens=max(256, window * (think_s + out_s) // 100),
                    thinking_budget=window * think_s // 100)
        return Settings.from_dict(data)


def settings_file(data_dir: Path) -> Path:
    return Path(data_dir) / "settings.json"


def load_settings(data_dir: Path | None = None) -> Settings:
    data_dir = Path(data_dir or DEFAULT_DATA_DIR)
    path = settings_file(data_dir)
    if path.exists():
        # Derive on load, not only on save: the shares are the source of truth, and a settings.json written
        # before they changed otherwise keeps its old max_new_tokens for good. Raising output_share_pct had no
        # effect at all until this ran here.
        s = Settings.from_dict(json.loads(path.read_text(encoding="utf-8"))).with_budgets()
    else:
        s = Settings()
    s.data_dir = str(data_dir)
    ensure_dirs(s)
    return s


def save_settings(s: Settings) -> None:
    ensure_dirs(s)
    settings_file(s.data_path).write_text(json.dumps(s.to_dict(), indent=2), encoding="utf-8")


def ensure_dirs(s: Settings) -> None:
    for p in (s.data_path, s.general_workspace, s.tmp_dir):
        p.mkdir(parents=True, exist_ok=True)
