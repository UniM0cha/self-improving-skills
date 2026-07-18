"""PostToolUse hook tests: counter bump + bypass-edit snapshot detection."""

import json
import os
import subprocess
import sys

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))


def _run(tmp_path, payload, skills_root):
    env = dict(os.environ,
               PLUGIN_DATA=str(tmp_path / "data"),
               CODEX_SELF_IMPROVE_SKILL_ROOTS=str(skills_root))
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "post_tool_use.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
        check=False,
    )


def _usage(tmp_path):
    return json.loads((tmp_path / "data" / "usage.json").read_text(encoding="utf-8"))


def test_counter_bumps_every_tool_event(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    for _ in range(2):
        proc = _run(tmp_path, {"tool_name": "web_search"}, skills_root)
        assert proc.returncode == 0
    counters = _usage(tmp_path)["counters"]["iters_since_review_by_session"]
    assert counters["global"]["v"] == 2


def test_counter_is_per_session(tmp_path):
    """codex review R4: concurrent sessions sharing PLUGIN_DATA must not pool
    their iteration counts."""
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _run(tmp_path, {"tool_name": "web_search", "session_id": "sess-a"}, skills_root)
    _run(tmp_path, {"tool_name": "web_search", "session_id": "sess-a"}, skills_root)
    _run(tmp_path, {"tool_name": "web_search", "session_id": "sess-b"}, skills_root)
    counters = _usage(tmp_path)["counters"]["iters_since_review_by_session"]
    assert counters["sess-a"]["v"] == 2
    assert counters["sess-b"]["v"] == 1


def test_bypass_edit_detected_and_repaired(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "sneaky"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: sneaky\ndescription: d\n---\nbody\n", encoding="utf-8")
    # first mutating event seeds the snapshot baseline (no detection)
    _run(tmp_path, {"tool_name": "shell"}, skills_root)
    # direct shell-style edit that BREAKS the frontmatter
    (skill / "SKILL.md").write_text("no frontmatter anymore, oops", encoding="utf-8")
    proc = _run(tmp_path, {"tool_name": "shell"}, skills_root)
    out = json.loads(proc.stdout.strip())
    assert "edited outside the skill manager" in out["systemMessage"]
    usage = _usage(tmp_path)
    assert usage["skills"]["sneaky"]["patch_count"] == 1  # telemetry repaired
    events = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")
    assert "bypass_edit" in events
    backups = list((tmp_path / "data" / "backups").iterdir())
    assert backups  # post-hoc checkpoint taken


def test_bypass_delete_detected(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "doomed"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: doomed\ndescription: d\n---\nbody\n", encoding="utf-8")
    _run(tmp_path, {"tool_name": "shell"}, skills_root)  # seed baseline
    import shutil
    shutil.rmtree(skill)
    proc = _run(tmp_path, {"tool_name": "shell"}, skills_root)
    out = json.loads(proc.stdout.strip())
    assert "DELETED outside the skill manager" in out["systemMessage"]
    events = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")
    assert "bypass_delete" in events


def test_edit_family_tools_trigger_diff(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "edited"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: edited\ndescription: d\n---\nbody\n", encoding="utf-8")
    _run(tmp_path, {"tool_name": "Edit"}, skills_root)  # seed via edit-family tool
    (skill / "SKILL.md").write_text("broken frontmatter", encoding="utf-8")
    proc = _run(tmp_path, {"tool_name": "MultiEdit"}, skills_root)
    out = json.loads(proc.stdout.strip())
    assert "edited outside the skill manager" in out["systemMessage"]


def test_direct_edit_right_after_manager_patch_is_flagged(tmp_path):
    """codex review R3: a shell edit within the manager's time window must
    still be flagged — the managed SIGNATURE, not the clock, decides."""
    import importlib
    import sys as _sys
    _sys.path.insert(0, SCRIPTS_DIR)
    skills_root = tmp_path / "skills"
    skill = skills_root / "twice"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: twice\ndescription: d\n---\nbody\n", encoding="utf-8")
    _run(tmp_path, {"tool_name": "shell"}, skills_root)  # seed baseline
    # manager-mediated patch (records last_managed_at + managed_sig)
    env_backup = dict(os.environ)
    os.environ["PLUGIN_DATA"] = str(tmp_path / "data")
    os.environ["CODEX_SELF_IMPROVE_SKILL_ROOTS"] = str(skills_root)
    try:
        import skill_store
        store = importlib.reload(skill_store)
        store.patch_skill("twice", "body", "body v2")
    finally:
        os.environ.clear()
        os.environ.update(env_backup)
    # the manager's own write is skipped...
    proc = _run(tmp_path, {"tool_name": "codex_skill_patch"}, skills_root)
    assert proc.stdout.strip() == ""
    # ...but a DIRECT edit right after (same time window) is flagged
    (skill / "SKILL.md").write_text("broken by shell right after", encoding="utf-8")
    proc = _run(tmp_path, {"tool_name": "shell"}, skills_root)
    out = json.loads(proc.stdout.strip())
    assert "edited outside the skill manager" in out["systemMessage"]
    assert out["hookSpecificOutput"]["additionalContext"]  # model feedback too


def test_seed_baseline_via_session_start(tmp_path):
    """codex review R3: the very first mutating tool of a session must
    already have a baseline (SessionStart seeds it)."""
    skills_root = tmp_path / "skills"
    skill = skills_root / "early"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: early\ndescription: d\n---\nbody\n", encoding="utf-8")
    env = dict(os.environ,
               PLUGIN_DATA=str(tmp_path / "data"),
               CODEX_SELF_IMPROVE_SKILL_ROOTS=str(skills_root))
    subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, "session_start.py")],
                   input="{}", capture_output=True, text=True, env=env, check=False)
    # first mutating tool AFTER SessionStart: the direct edit is caught
    (skill / "SKILL.md").write_text("broken as very first action", encoding="utf-8")
    proc = _run(tmp_path, {"tool_name": "shell"}, skills_root)
    out = json.loads(proc.stdout.strip())
    assert "edited outside the skill manager" in out["systemMessage"]


def test_non_mutating_tool_skips_diff(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "quiet"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: quiet\ndescription: d\n---\nbody\n", encoding="utf-8")
    _run(tmp_path, {"tool_name": "shell"}, skills_root)  # seed baseline
    (skill / "SKILL.md").write_text(
        "---\nname: quiet\ndescription: d\n---\nbody v2\n", encoding="utf-8")
    proc = _run(tmp_path, {"tool_name": "web_search"}, skills_root)
    assert proc.stdout.strip() == ""  # cost gate: read-only tools don't diff
