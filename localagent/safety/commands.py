"""Shell command and Python snippet policy.

This is best-effort pattern matching, not a sandbox. It catches the common ways an agent would
make permanent OS changes, install programs, or reach outside the workspace, and routes those to
a hard block (deny) or to the user (ask). Anything it does not recognize is allowed with the
workspace as the working directory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .paths import PathGuard, normalize

_I = re.IGNORECASE

# Permanent OS changes and program installation: never allowed.
DENY_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\breg(\.exe)?\s+(add|delete|import|load|unload|restore|copy|save)\b", _I), "registry edit"),
    (re.compile(r"\b(Set|New|Remove|Rename)-ItemProperty\b", _I), "registry/property edit"),
    (re.compile(r"\b(New|Remove|Set)-Item\b[^|;]*\bHK(LM|CU|CR|U|CC)\b", _I), "registry edit"),
    (re.compile(r"\bsetx\b", _I), "permanent environment variable"),
    (re.compile(r"SetEnvironmentVariable\s*\([^)]*,\s*['\"]?(Machine|User)", _I), "permanent environment variable"),
    (re.compile(r"\b(winget|choco|chocolatey|scoop|msiexec)\b", _I), "program installer"),
    (re.compile(r"\.(msi|msix|appx)\b", _I), "program installer"),
    (re.compile(r"\b(Add-AppxPackage|Install-Module|Install-Package|Install-Script|Install-WindowsFeature)\b", _I), "program installer"),
    (re.compile(r"\b(Enable|Disable)-WindowsOptionalFeature\b|\bdism(\.exe)?\b", _I), "Windows feature change"),
    (re.compile(r"\bsc(\.exe)?\s+(create|delete|config|stop|start)\b", _I), "service change"),
    (re.compile(r"\b(New|Set|Remove|Stop|Start|Restart)-Service\b", _I), "service change"),
    (re.compile(r"\bSet-ExecutionPolicy\b", _I), "execution policy change"),
    (re.compile(r"\bschtasks(\.exe)?\s+/(create|change|delete)\b|\b(Register|Unregister|Set)-ScheduledTask\b", _I), "scheduled task"),
    (re.compile(r"\b(format(\.com)?\s+[a-z]:|diskpart|bcdedit|Format-Volume|Clear-Disk|Initialize-Disk)\b", _I), "disk/boot change"),
    (re.compile(r"\bnetsh\b|\b(New|Set|Remove)-NetFirewall\w*\b|\bSet-DnsClient\w*\b", _I), "network configuration"),
    (re.compile(r"\b(shutdown(\.exe)?|Restart-Computer|Stop-Computer|logoff)\b", _I), "power/session control"),
    (re.compile(r"\b(takeown|icacls|cacls)\b|\bSet-Acl\b", _I), "permission change"),
    (re.compile(r"-Verb\s+RunAs\b|\brunas(\.exe)?\b", _I), "elevation"),
    (re.compile(r"\bnpm\s+(install|i)\s+(-g|--global)\b", _I), "global program install"),
    (re.compile(r"\b(Set-MpPreference|Add-MpPreference)\b", _I), "Defender configuration"),
    (re.compile(r"\bbcdboot\b|\bvssadmin\b|\bwmic\b", _I), "system administration tool"),
]

# Allowed only with the user's approval. (category, pattern, reason)
ASK_RULES: list[tuple[str, re.Pattern, str]] = [
    ("package-install", re.compile(r"\b(pip3?|uv\s+pip)\s+(install|uninstall|download)\b|\bpython(\.exe)?\s+-m\s+pip\s+(install|uninstall)\b", _I), "installs or removes Python packages"),
    ("package-install", re.compile(r"\b(conda|mamba|micromamba)\s+(install|remove|uninstall|update|create|env)\b", _I), "changes a conda environment"),
    ("package-install", re.compile(r"\bnpm\s+(install|i|uninstall|ci)\b|\byarn\s+add\b|\bpnpm\s+(add|install)\b", _I), "installs Node packages"),
    ("network", re.compile(r"\b(Invoke-WebRequest|Invoke-RestMethod|iwr|irm|curl(\.exe)?|wget|Start-BitsTransfer|bitsadmin)\b", _I), "downloads from or sends data to the network"),
    ("network", re.compile(r"\bgit\s+(clone|push|pull|fetch)\b|\bssh\b|\bscp\b", _I), "network access via git/ssh"),
    ("process-control", re.compile(r"\b(Stop-Process|taskkill|kill)\b", _I), "stops processes"),
    ("dynamic-code", re.compile(r"\b(Invoke-Expression|iex)\b|-EncodedCommand\b|\bFromBase64String\b", _I), "runs dynamically built code that cannot be checked"),
    ("user-profile", re.compile(r"\$env:(USERPROFILE|APPDATA|LOCALAPPDATA|ProgramData|ProgramFiles|SystemRoot|windir|HOMEPATH)|%(USERPROFILE|APPDATA|LOCALAPPDATA|PROGRAMDATA|PROGRAMFILES|SYSTEMROOT|WINDIR)%|\$HOME\b|(?<![\w.])~[\\/]", _I), "refers to a location outside the workspace"),
]

PYTHON_ASK_RULES: list[tuple[str, re.Pattern, str]] = [
    ("python-system", re.compile(r"\b(winreg|_winreg|ctypes\.windll|win32api|win32con|win32service)\b"), "touches Windows system APIs"),
    ("python-subprocess", re.compile(r"\b(subprocess|os\.system|os\.popen|os\.exec\w*|os\.spawn\w*|pty)\b"), "starts other programs (their commands can't be checked)"),
    ("network", re.compile(r"\b(urllib\.request|requests\.|httpx\.|socket\.|http\.client|aiohttp|ftplib|smtplib)\b"), "uses the network"),
    ("user-profile", re.compile(r"expanduser\(|Path\.home\(|os\.environ\[?\.?(get\()?['\"](USERPROFILE|APPDATA|LOCALAPPDATA)", _I), "refers to a location outside the workspace"),
]

_ABS_PATH_RE = re.compile(r"""(?<![\w/\\])([A-Za-z]:[\\/][^\s'"`;|&<>(){}\]\[,]*|\\\\[^\s'"`;|&<>(){}\]\[,]+)""")
_PARENT_REF_RE = re.compile(r"""(?:^|[\s'"=(])((?:[^\s'"`;|&<>()]*[\\/])?\.\.(?:[\\/][^\s'"`;|&<>()]*)?)""")


@dataclass
class CommandDecision:
    action: str                        # allow | ask | deny
    reasons: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)   # approval keys; "always" stores these

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons)


def _outside_paths(text: str, guard: PathGuard) -> list[str]:
    found: list[str] = []
    for m in _ABS_PATH_RE.finditer(text):
        p = m.group(1).rstrip(".:")
        if p and not guard.in_bounds(p):
            found.append(p)
    for m in _PARENT_REF_RE.finditer(text):
        p = m.group(1)
        if p and not guard.in_bounds(p):
            found.append(p)
    return found


def _path_key(path: str, guard: PathGuard) -> str:
    rp = guard.resolve(path)
    return f"path:{normalize(rp.parent if rp.suffix else rp)}"


class CommandPolicy:
    def evaluate(self, command: str, guard: PathGuard) -> CommandDecision:
        denied = [reason for pat, reason in DENY_RULES if pat.search(command)]
        if denied:
            return CommandDecision("deny", sorted(set(denied)))
        return self._ask_rules(command, guard, ASK_RULES)

    def evaluate_python(self, code: str, guard: PathGuard) -> CommandDecision:
        decision = self._ask_rules(code, guard, PYTHON_ASK_RULES + ASK_RULES[:1])
        # Python can shell out to the same OS tools. Deny-list words are too common in ordinary
        # Python (e.g. executor.shutdown) to hard-block, so surface them in the approval instead.
        if "cmd:python-subprocess" in decision.keys:
            for pat, reason in DENY_RULES:
                if pat.search(code):
                    decision.reasons.append(f"may run: {reason}")
        return decision

    def _ask_rules(self, text: str, guard: PathGuard, rules) -> CommandDecision:
        reasons: list[str] = []
        keys: list[str] = []
        for category, pat, reason in rules:
            if pat.search(text) and f"cmd:{category}" not in keys:
                reasons.append(reason)
                keys.append(f"cmd:{category}")
        for p in _outside_paths(text, guard):
            key = _path_key(p, guard)
            if key not in keys:
                reasons.append(f"uses a path outside the workspace: {p}")
                keys.append(key)
        return CommandDecision("ask" if keys else "allow", reasons, keys)
