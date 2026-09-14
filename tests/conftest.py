import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localagent.config import Settings, ensure_dirs  # noqa: E402
from localagent.store import Store  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    s = Settings(data_dir=str(tmp_path / "data"), models_dir=str(tmp_path / "models"))
    s.max_steps = 10
    s.tool_timeout_s = 30
    ensure_dirs(s)
    return s


@pytest.fixture
def store(settings):
    st = Store(settings.db_path)
    yield st
    st.close()


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws
