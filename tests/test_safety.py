import os
import subprocess

import pytest

from localagent.safety import CommandPolicy, PathGuard, is_within


@pytest.fixture
def guard(workspace, tmp_path):
    env = tmp_path / "env"
    env.mkdir()
    return PathGuard(workspace, env)


def test_workspace_paths_allowed(guard, workspace):
    assert guard.access("notes.txt", "write") == "allow"
    assert guard.access(workspace / "sub" / "x.py", "write") == "allow"
    assert guard.access(str(workspace).upper() + "\\a.txt", "read") == "allow"   # Windows is case-insensitive


def test_parent_escape_needs_approval(guard):
    assert guard.access("..\\secret.txt", "read") == "ask"
    assert guard.access("sub\\..\\..\\secret.txt", "write") == "ask"


def test_other_drive_and_system_paths(guard):
    assert guard.access("C:\\Windows\\win.ini", "read") == "ask"
    assert not is_within("Z:\\foo", "C:\\bar")


def test_env_is_read_only(guard, tmp_path):
    assert guard.access(tmp_path / "env" / "Lib" / "x.py", "read") == "allow"
    assert guard.access(tmp_path / "env" / "Lib" / "x.py", "write") == "ask"


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_junction_escape_detected(guard, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "link"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    assert guard.access(link / "file.txt", "write") == "ask"


@pytest.mark.parametrize("cmd", [
    "reg add HKCU\\Software\\X /v Y /d 1",
    "Set-ItemProperty -Path HKCU:\\Software\\X -Name Y -Value 1",
    "setx PATH C:\\foo",
    "winget install Git.Git",
    "msiexec /i thing.msi",
    "Set-ExecutionPolicy Unrestricted",
    "sc.exe create evil binPath= C:\\x.exe",
    "schtasks /create /tn x /tr y",
    "Start-Process powershell -Verb RunAs",
    "npm install -g typescript",
    "Install-Module PSReadLine",
])
def test_denied_commands(guard, cmd):
    assert CommandPolicy().evaluate(cmd, guard).action == "deny"


@pytest.mark.parametrize("cmd,key", [
    ("pip install requests", "cmd:package-install"),
    ("python -m pip install numpy", "cmd:package-install"),
    ("conda install scipy", "cmd:package-install"),
    ("Invoke-WebRequest https://example.com -OutFile a.html", "cmd:network"),
    ("git clone https://github.com/x/y", "cmd:network"),
    ("Get-Content $env:USERPROFILE\\secrets.txt", "cmd:user-profile"),
    ("iex (Get-Content script.txt -Raw)", "cmd:dynamic-code"),
])
def test_commands_needing_approval(guard, cmd, key):
    d = CommandPolicy().evaluate(cmd, guard)
    assert d.action == "ask"
    assert key in d.keys


def test_outside_absolute_path_needs_approval(guard):
    d = CommandPolicy().evaluate("Get-ChildItem C:\\Users", guard)
    assert d.action == "ask"
    assert any(k.startswith("path:") for k in d.keys)


def test_cd_parent_needs_approval(guard):
    assert CommandPolicy().evaluate("cd ..; dir", guard).action == "ask"


@pytest.mark.parametrize("cmd", [
    "Get-ChildItem",
    "python script.py",
    "python -m pytest -q",
    "Get-Content notes.txt | Select-String todo",
    "1..10 | ForEach-Object { $_ * 2 }",
    "git status",
    "git log main..feature",
])
def test_ordinary_commands_allowed(guard, cmd):
    d = CommandPolicy().evaluate(cmd, guard)
    assert d.action == "allow", d.reasons


def test_workspace_absolute_path_allowed(guard, workspace):
    assert CommandPolicy().evaluate(f"Get-Content {workspace}\\a.txt", guard).action == "allow"


def test_python_policy(guard):
    p = CommandPolicy()
    assert p.evaluate_python("print(sum(range(10)))", guard).action == "allow"
    assert p.evaluate_python("from concurrent.futures import ThreadPoolExecutor\nex=ThreadPoolExecutor()\nex.shutdown()", guard).action == "allow"
    d = p.evaluate_python("import subprocess\nsubprocess.run(['winget','install','x'])", guard)
    assert d.action == "ask" and any("program installer" in r for r in d.reasons)
    assert p.evaluate_python("import winreg", guard).action == "ask"
    assert p.evaluate_python("open(r'C:\\Windows\\win.ini').read()", guard).action == "ask"
