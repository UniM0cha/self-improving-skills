import json
import os
import subprocess
import sys


SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))


def _run_session_start(tmp_path, auto_value=None):
    env = dict(os.environ, PLUGIN_DATA=str(tmp_path / "data"))
    env.pop("CODEX_SELF_IMPROVE_AUTO", None)
    if auto_value is not None:
        env["CODEX_SELF_IMPROVE_AUTO"] = auto_value
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "session_start.py")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_session_start_reports_default_auto_continue_on(tmp_path):
    proc = _run_session_start(tmp_path)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    note = payload["hookSpecificOutput"]["additionalContext"]
    assert "Auto-continue is on." in note


def test_session_start_reports_explicit_auto_continue_off(tmp_path):
    proc = _run_session_start(tmp_path, auto_value="0")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    note = payload["hookSpecificOutput"]["additionalContext"]
    assert "Auto-continue is off." in note
