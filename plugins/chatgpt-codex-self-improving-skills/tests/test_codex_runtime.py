import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import codex_runtime as runtime


def _fake(tmp_path, name="codex.py", body="print('codex-cli 0.134.0-alpha.5')"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + "\n", encoding="utf-8")
    return path


def _env(**values):
    return dict(os.environ, **values)


def test_explicit_nonexecutable_python_override_precedes_desktop_and_path(tmp_path, monkeypatch):
    fake = _fake(tmp_path)
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "_active_desktop_bundles", lambda: pytest.fail("must not inspect apps"))
    result = runtime.resolve_codex_runtime(_env(CODEX_SELF_IMPROVE_CODEX_BIN=str(fake), PATH=""))
    assert result == {"path": str(fake), "version": "0.134.0-alpha.5", "source": "override", "error_code": None}


@pytest.mark.parametrize("kind", ["missing", "directory", "nonexecutable"])
def test_invalid_override_never_uses_valid_path_candidate(tmp_path, monkeypatch, kind):
    override = tmp_path / "override"
    if kind == "directory":
        override.mkdir()
    elif kind == "nonexecutable":
        override.write_text("not executable")
        monkeypatch.setattr(runtime.os, "access", lambda *_args: False)
    fallback = _fake(tmp_path)
    monkeypatch.setattr(runtime.shutil, "which", lambda *_args, **_kwargs: str(fallback))
    result = runtime.resolve_codex_runtime(_env(CODEX_SELF_IMPROVE_CODEX_BIN=str(override)))
    assert result == {"path": None, "version": None, "source": "override", "error_code": "codex_override_invalid"}


def test_override_tilde_uses_supplied_home_without_changing_process_home(tmp_path):
    fake = _fake(tmp_path)
    original = os.environ.get("HOME")
    result = runtime.resolve_codex_runtime(_env(CODEX_SELF_IMPROVE_CODEX_BIN="~/codex.py", HOME=str(tmp_path)))
    assert result["path"] == str(fake)
    assert result["version"] == "0.134.0-alpha.5"
    assert os.environ.get("HOME") == original


def test_named_override_is_resolved_with_supplied_path(tmp_path, monkeypatch):
    fake = _fake(tmp_path)
    seen = []

    def which(name, *, path):
        seen.append((name, path))
        return str(fake)

    monkeypatch.setattr(runtime.shutil, "which", which)
    result = runtime.resolve_codex_runtime(_env(CODEX_SELF_IMPROVE_CODEX_BIN="custom-codex", PATH="custom-bin"))
    assert result["source"] == "override"
    assert seen == [("custom-codex", "custom-bin")]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_platforms_use_path_without_desktop_scan(tmp_path, monkeypatch, platform):
    fake = _fake(tmp_path)
    monkeypatch.setattr(runtime.sys, "platform", platform)
    monkeypatch.setattr(runtime, "_active_desktop_bundles", lambda: pytest.fail("macOS-only scan"))
    monkeypatch.setattr(runtime.shutil, "which", lambda *_args, **_kwargs: str(fake))
    result = runtime.resolve_codex_runtime(_env(CODEX_SELF_IMPROVE_CODEX_BIN=""))
    assert result == {"path": str(fake), "version": "0.134.0-alpha.5", "source": "path", "error_code": None}


def test_macos_running_bundle_wins_over_standard_app_and_path(tmp_path, monkeypatch):
    active = tmp_path / "Custom Apps" / "ChatGPT.app"
    standard = tmp_path / "Applications" / "Codex.app"
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "_process_table", lambda: {
        os.getpid(): (9876, sys.executable),
        9876: (1, str(active / "Contents" / "Resources" / "codex")),
    })
    monkeypatch.setattr(runtime, "_standard_desktop_bundles", lambda _env: [standard])
    checked = []
    fake = _fake(tmp_path)

    def bundle_cli(bundle):
        checked.append(bundle)
        return str(fake)

    monkeypatch.setattr(runtime, "_bundle_cli", bundle_cli)
    monkeypatch.setattr(runtime.shutil, "which", lambda *_args, **_kwargs: pytest.fail("bundle has priority"))
    result = runtime.resolve_codex_runtime(_env(CODEX_SELF_IMPROVE_CODEX_BIN=""))
    assert checked == [active]
    assert result["source"] == "desktop_bundle"
    assert result["version"] == "0.134.0-alpha.5"


def test_macos_missing_active_cli_uses_standard_app_then_path(tmp_path, monkeypatch):
    active = tmp_path / "running.app"
    standard = tmp_path / "ChatGPT.app"
    fake = _fake(tmp_path)
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "_active_desktop_bundles", lambda: [active])
    monkeypatch.setattr(runtime, "_standard_desktop_bundles", lambda _env: [standard])
    monkeypatch.setattr(runtime, "_bundle_cli", lambda bundle: str(fake) if bundle == standard else None)
    monkeypatch.setattr(runtime.shutil, "which", lambda *_args, **_kwargs: pytest.fail("standard app has priority"))
    assert runtime.resolve_codex_runtime(_env(CODEX_SELF_IMPROVE_CODEX_BIN=""))["source"] == "desktop_bundle"
    monkeypatch.setattr(runtime, "_bundle_cli", lambda _bundle: None)
    monkeypatch.setattr(runtime.shutil, "which", lambda *_args, **_kwargs: str(fake))
    assert runtime.resolve_codex_runtime(_env(CODEX_SELF_IMPROVE_CODEX_BIN=""))["source"] == "path"


def test_bundle_cli_checks_only_known_executable_location(tmp_path, monkeypatch):
    bundle = tmp_path / "ChatGPT.app"
    seen = []
    monkeypatch.setattr(runtime, "_executable_path", lambda path: seen.append(path))
    runtime._bundle_cli(bundle)
    assert seen == [str(bundle / "Contents" / "Resources" / "codex")]


def test_standard_app_roots_include_supplied_user_home(tmp_path):
    bundles = runtime._standard_desktop_bundles({"HOME": str(tmp_path)})
    assert bundles == [
        Path("/Applications/ChatGPT.app"), Path("/Applications/Codex.app"),
        tmp_path / "Applications/ChatGPT.app", tmp_path / "Applications/Codex.app",
    ]


def test_process_scan_uses_names_not_args_and_has_timeout(monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="12 1 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT\nbad row\n")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert runtime._process_table() == {12: (1, "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT")}
    assert seen[0][0] == ["/bin/ps", "-axo", "pid=,ppid=,comm="]
    assert seen[0][1]["timeout"] == runtime.PROCESS_TIMEOUT_SECONDS
    assert seen[0][1]["stderr"] == subprocess.DEVNULL


def test_process_scan_timeout_is_nonfatal(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ps", 1)

    monkeypatch.setattr(runtime.subprocess, "run", timeout)
    assert runtime._process_table() == {}


def test_parent_cycle_terminates_and_nested_helper_finds_outer_app(monkeypatch):
    monkeypatch.setattr(runtime.os, "getpid", lambda: 12)
    monkeypatch.setattr(runtime, "_process_table", lambda: {
        12: (13, "/Applications/ChatGPT.app/Contents/Frameworks/Helper.app/Contents/MacOS/Helper"),
        13: (12, "/usr/bin/python3"),
    })
    assert runtime._active_desktop_bundles() == [Path("/Applications/ChatGPT.app")]


@pytest.mark.parametrize("body", [
    "print('unrecognized version')",
    "import sys; print('private error contents', file=sys.stderr); raise SystemExit(3)",
    "pass",
])
def test_version_failure_keeps_selected_path_with_sanitized_error(tmp_path, body):
    fake = _fake(tmp_path, body=body)
    result = runtime.resolve_codex_runtime(_env(CODEX_SELF_IMPROVE_CODEX_BIN=str(fake)))
    assert result == {"path": str(fake), "version": None, "source": "override", "error_code": "codex_version_check_failed"}


def test_desktop_version_failure_does_not_switch_to_path(tmp_path, monkeypatch):
    fake = _fake(tmp_path, body="pass")
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime, "_active_desktop_bundles", lambda: [tmp_path / "ChatGPT.app"])
    monkeypatch.setattr(runtime, "_standard_desktop_bundles", lambda _env: [])
    monkeypatch.setattr(runtime, "_bundle_cli", lambda _bundle: str(fake))
    monkeypatch.setattr(runtime.shutil, "which", lambda *_args, **_kwargs: pytest.fail("must not switch CLI"))
    assert runtime.resolve_codex_runtime(_env(CODEX_SELF_IMPROVE_CODEX_BIN="")) == {
        "path": str(fake), "version": None, "source": "desktop_bundle", "error_code": "codex_version_check_failed",
    }


def test_version_timeout_is_bounded_and_does_not_fallback(tmp_path, monkeypatch):
    fake = _fake(tmp_path, body="import time; time.sleep(30)")
    monkeypatch.setattr(runtime, "VERSION_TIMEOUT_SECONDS", 0.05)
    result = runtime.resolve_codex_runtime(_env(CODEX_SELF_IMPROVE_CODEX_BIN=str(fake)))
    assert result["path"] == str(fake)
    assert result["version"] is None
    assert result["error_code"] == "codex_version_check_timeout"


def test_no_cli_returns_explicit_not_found(monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.shutil, "which", lambda *_args, **_kwargs: None)
    assert runtime.resolve_codex_runtime({"PATH": ""}) == {
        "path": None, "version": None, "source": "path", "error_code": "codex_not_found",
    }
