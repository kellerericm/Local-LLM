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
    model_id: str = "Qwen/Qwen3-8B"
    quantization: str = "4bit"           # none | 8bit | 4bit
    tool_call_format: str = "auto"          # auto | hermes | qwen3_coder
    thinking: bool = True
    context_tokens: int = 32768
    max_new_tokens: int = 4096
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 20
    max_steps: int = 60
    max_consecutive_failures: int = 3
    tool_timeout_s: int = 300
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
        return Settings.from_dict(data)


def settings_file(data_dir: Path) -> Path:
    return Path(data_dir) / "settings.json"


def load_settings(data_dir: Path | None = None) -> Settings:
    data_dir = Path(data_dir or DEFAULT_DATA_DIR)
    path = settings_file(data_dir)
    if path.exists():
        s = Settings.from_dict(json.loads(path.read_text(encoding="utf-8")))
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
