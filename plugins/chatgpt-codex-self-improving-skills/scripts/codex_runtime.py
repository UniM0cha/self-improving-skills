"""Resolve the CLI used by Codex desktop background reviews.

An explicit override is authoritative, including when it is broken. On macOS,
prefer the running desktop application's bundle, then standard application
locations, before PATH. Version lookup describes only the selected executable;
it never silently switches executables after a failed version check.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


PROCESS_TIMEOUT_SECONDS = 1.0
VERSION_TIMEOUT_SECONDS = 1.5
MAX_ANCESTORS = 16
DESKTOP_APP_NAMES = ("ChatGPT.app", "Codex.app")
_VERSION_RE = re.compile(
    r"^codex(?:-cli)?\s+(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\s*$", re.MULTILINE
)


def _user_home(values: Dict[str, str]) -> Path:
    for name in ("HOME", "USERPROFILE"):
        value = values.get(name)
        if value and os.path.isabs(value):
            return Path(value)
    return Path.home()


def _expanded_path(value: str, values: Dict[str, str]) -> str:
    if value == "~" or value.startswith(("~/", "~\\")):
        return str(_user_home(values) / value[2:]) if len(value) > 1 else str(_user_home(values))
    return os.path.expanduser(value)


def _executable_path(candidate: str) -> Optional[str]:
    if not candidate:
        return None
    try:
        path = os.path.abspath(candidate)
        if os.path.isfile(path) and (os.access(path, os.X_OK) or path.lower().endswith(".py")):
            return path
    except (OSError, ValueError):
        pass
    return None


def _process_table() -> Dict[int, Tuple[int, str]]:
    """Read executable names once, without process arguments or an unbounded walk."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,comm="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode:
        return {}
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            rows[int(fields[0])] = (int(fields[1]), fields[2])
        except ValueError:
            continue
    return rows


def _desktop_bundle(executable: str) -> Optional[Path]:
    if not os.path.isabs(executable):
        return None
    path = Path(executable)
    for ancestor in path.parents:
        if ancestor.name in DESKTOP_APP_NAMES:
            return ancestor
    return None


def _active_desktop_bundles() -> List[Path]:
    table = _process_table()
    pid = os.getpid()
    seen = set()
    bundles = []
    for _ in range(MAX_ANCESTORS):
        if pid <= 1 or pid in seen or pid not in table:
            break
        seen.add(pid)
        pid, executable = table[pid]
        bundle = _desktop_bundle(executable)
        if bundle is not None and bundle not in bundles:
            bundles.append(bundle)
    return bundles


def _standard_desktop_bundles(values: Dict[str, str]) -> List[Path]:
    return [
        directory / name
        for directory in (Path("/Applications"), _user_home(values) / "Applications")
        for name in DESKTOP_APP_NAMES
    ]


def _bundle_cli(bundle: Path) -> Optional[str]:
    # Codex desktop ships its CLI here; do not recursively search app contents.
    return _executable_path(str(bundle / "Contents" / "Resources" / "codex"))


def _version(path: str, values: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    command = [sys.executable, path] if path.lower().endswith(".py") else [path]
    try:
        result = subprocess.run(
            command + ["--version"],
            env=values,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "codex_version_check_timeout"
    except (OSError, ValueError):
        return None, "codex_version_check_failed"
    match = _VERSION_RE.search(result.stdout) if result.returncode == 0 else None
    if not match:
        return None, "codex_version_check_failed"
    return match.group(1), None


def resolve_codex_runtime(env: Optional[Dict[str, str]] = None) -> Dict[str, Optional[str]]:
    """Return ``path``, ``version``, ``source`` and a sanitized ``error_code``.

    The selected absolute path remains available when only the version query
    fails. Missing/invalid executables have path=None. At most one one-second
    process query and one 1.5-second version query run per call; filesystem
    discovery checks only fixed bundle locations and the supplied PATH.
    """
    values = dict(os.environ if env is None else env)
    configured = str(values.get("CODEX_SELF_IMPROVE_CODEX_BIN") or "").strip()
    path = None
    source = "path"
    if configured:
        source = "override"
        expanded = _expanded_path(configured, values)
        candidate = (
            expanded if os.path.dirname(expanded)
            else shutil.which(expanded, path=values.get("PATH", ""))
        )
        path = _executable_path(candidate or "")
        if path is None:
            return {"path": None, "version": None, "source": source, "error_code": "codex_override_invalid"}
    else:
        if sys.platform == "darwin":
            for bundle in _active_desktop_bundles() + _standard_desktop_bundles(values):
                path = _bundle_cli(bundle)
                if path:
                    source = "desktop_bundle"
                    break
        if path is None:
            path = _executable_path(shutil.which("codex", path=values.get("PATH", "")) or "")
        if path is None:
            return {"path": None, "version": None, "source": source, "error_code": "codex_not_found"}
    version, error_code = _version(path, values)
    return {"path": path, "version": version, "source": source, "error_code": error_code}
