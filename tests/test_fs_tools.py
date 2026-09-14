import threading

import pytest

from localagent.config import Settings
from localagent.safety import AutoApprover, CommandPolicy, PathGuard
from localagent.tools.fs import glob_files, glob_match, grep
from localagent.tools.registry import ToolContext


@pytest.mark.parametrize("path,spec,expected", [
    ("main.py", "*.py", True),
    ("app/main.py", "*.py", True),
    ("app/main.py", "app/**/*.py", True),          # ** matches zero directories
    ("app/db/models.py", "app/**/*.py", True),
    ("app/db/models.py", "**/*.py", True),
    ("docs/notes.md", "app/**/*.py,docs/**/*.md", True),
    ("docs/notes.txt", "app/**/*.py,docs/**/*.md", False),
    ("src/a.md", "*.{py,md}", True),
    ("src/a.js", "*.py; *.md", False),
    ("app/main.py", "./app/*.py", True),
])
def test_glob_match(path, spec, expected):
    assert glob_match(path, spec) is expected


@pytest.fixture
def ctx(workspace, settings):
    (workspace / "app" / "db").mkdir(parents=True)
    (workspace / "app" / "main.py").write_text("# TODO: handle missing config\n")
    (workspace / "app" / "db" / "models.py").write_text("x = 1  # TODO: add validation\n")
    (workspace / "docs").mkdir()
    (workspace / "docs" / "notes.md").write_text("nothing\n")
    return ToolContext(chat_id="c", project=None, workspace=workspace, env_path=workspace, guard=PathGuard(workspace),
                       policy=CommandPolicy(), approvals=AutoApprover(), store=None, settings=settings,
                       cancel=threading.Event(), emit=lambda e: None)


def test_grep_with_path_style_glob_list(ctx):
    out = grep(ctx, "TODO", file_glob="app/**/*.py,docs/**/*.md").content
    assert "main.py:1" in out and "models.py:1" in out


def test_grep_no_match_hint(ctx):
    out = grep(ctx, "TODO", file_glob="*.js").content
    assert "without file_glob" in out


def test_glob_tool_path_pattern(ctx):
    out = glob_files(ctx, "app/**/*.py").content
    assert "app/main.py" in out and "app/db/models.py" in out
