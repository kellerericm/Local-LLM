"""Workspace boundary checks.

Paths are fully resolved (symlinks, junctions, `..`) and case-normalized before comparison,
so `workspace\\..\\secret` or a junction pointing out of the workspace is treated as outside.
"""
from __future__ import annotations

import os
from pathlib import Path


def normalize(path: str | os.PathLike) -> str:
    p = os.path.realpath(os.path.abspath(os.path.expanduser(os.path.expandvars(str(path)))))
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return os.path.normcase(p)


def is_within(path: str | os.PathLike, root: str | os.PathLike) -> bool:
    p, r = normalize(path), normalize(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives
        return False


class PathGuard:
    """Decides whether a path may be touched without asking.

    - workspace: read and write
    - environment: read only (writes into the env go through approval, like installs)
    - anything else: ask
    """

    def __init__(self, workspace: str | os.PathLike, env_path: str | os.PathLike | None = None):
        self.workspace = Path(workspace)
        self.env_path = Path(env_path) if env_path else None

    def resolve(self, path: str | os.PathLike) -> Path:
        p = Path(os.path.expanduser(os.path.expandvars(str(path))))
        if not p.is_absolute():
            p = self.workspace / p
        return Path(normalize(p))

    def access(self, path: str | os.PathLike, mode: str) -> str:
        """Return 'allow' or 'ask' for mode 'read' or 'write'."""
        rp = self.resolve(path)
        if is_within(rp, self.workspace):
            return "allow"
        if mode == "read" and self.env_path and is_within(rp, self.env_path):
            return "allow"
        return "ask"

    def in_bounds(self, path: str | os.PathLike) -> bool:
        """True if the path is in the workspace or environment (used for command paths)."""
        rp = self.resolve(path)
        return is_within(rp, self.workspace) or bool(self.env_path and is_within(rp, self.env_path))
