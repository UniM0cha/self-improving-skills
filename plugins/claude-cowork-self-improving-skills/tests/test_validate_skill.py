"""PostToolUse hook-contract tests for validate_skill.py (real subprocess)."""

import json
import shutil
import subprocess
import sys
import os

from conftest import SCRIPTS_DIR


def _payload(path, agent_type=None):
    p = {"tool_input": {"file_path": str(path)}}
    if agent_type:
        p["agent_type"] = agent_type
    return p


def _backup(sandbox, name):
    """Simulate the PreToolUse backup the real hook pair would have made."""
    bdir = sandbox.home / ".claude" / "self-improve" / "skill_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(sandbox.skills / name / "SKILL.md"), str(bdir / (name + ".bak")))


def test_valid_skill_is_silent_and_counts_patch(run_validator, sandbox, store_data):
    d = sandbox.make_skill("good-skill")
    out = run_validator(_payload(d / "SKILL.md"))
    assert out == ""
    rec = store_data()["good-skill"]
    assert rec["patch_count"] == 1 and rec["created_by"] == "user"


def test_distiller_agent_type_seeds_agent(run_validator, sandbox, store_data):
    d = sandbox.make_skill("distilled-one")
    run_validator(_payload(d / "SKILL.md",
                           agent_type="claude-cowork-self-improving-skills:skill-distiller"))
    assert store_data()["distilled-one"]["created_by"] == "agent"


def test_reserved_word_name_gets_advisory(run_validator, sandbox):
    # claude.ai '스킬 저장' rejects names containing 'claude'/'anthropic' —
    # the validator must surface that while the writer can still rename.
    body = "---\nname: claude-code-tricks\ndescription: d\n---\nbody\n"
    d = sandbox.make_skill("claude-code-tricks", body)
    out = run_validator(_payload(d / "SKILL.md"))
    assert "예약어" in out and "롤백" not in out


def test_clean_name_no_reserved_word_advisory(run_validator, sandbox):
    d = sandbox.make_skill("cloud-tricks")
    out = run_validator(_payload(d / "SKILL.md"))
    assert "예약어" not in out


def test_angle_bracket_in_description_gets_advisory(run_validator, sandbox):
    # claude.ai '스킬 저장' rejects XML-tag-like text in the description
    # (observed: a mnt/<folder> placeholder → "cannot contain XML tags").
    body = "---\nname: mount-tricks\ndescription: mnt/<folder> 경로에서 사용\n---\nbody\n"
    d = sandbox.make_skill("mount-tricks", body)
    out = run_validator(_payload(d / "SKILL.md"))
    assert "꺾쇠" in out and "롤백" not in out


def test_angle_bracket_in_body_is_fine(run_validator, sandbox):
    body = "---\nname: body-brackets\ndescription: plain description\n---\nuse <placeholder> here\n"
    d = sandbox.make_skill("body-brackets", body)
    out = run_validator(_payload(d / "SKILL.md"))
    assert "꺾쇠" not in out


def test_broken_edit_rolls_back_from_backup(run_validator, sandbox, store_data):
    d = sandbox.make_skill("frag-skill")
    good = (d / "SKILL.md").read_text(encoding="utf-8")
    _backup(sandbox, "frag-skill")
    (d / "SKILL.md").write_text("no frontmatter at all", encoding="utf-8")
    out = run_validator(_payload(d / "SKILL.md"))
    assert "롤백" in out
    assert (d / "SKILL.md").read_text(encoding="utf-8") == good
    # the rolled-back edit must NOT have been counted as a patch (the store
    # file may not even exist yet — that also proves nothing was recorded)
    store_file = sandbox.home / ".claude" / "self-improve" / "skill_usage.json"
    if store_file.exists():
        assert store_data().get("frag-skill", {}).get("patch_count", 0) == 0


def test_new_broken_skill_warns_without_rollback(run_validator, sandbox):
    d = sandbox.skills / "newborn"
    d.mkdir()
    (d / "SKILL.md").write_text("---\ndescription: no name\n---\nbody\n", encoding="utf-8")
    out = run_validator(_payload(d / "SKILL.md"))
    assert "name" in out and "롤백" not in out


def test_long_description_gets_advisory_not_rollback(run_validator, sandbox):
    body = "---\nname: chatty\ndescription: {0}\n---\nbody\n".format("x" * 600)
    d = sandbox.make_skill("chatty", body)
    out = run_validator(_payload(d / "SKILL.md"))
    assert "압축" in out  # advisory present
    assert (d / "SKILL.md").read_text(encoding="utf-8").count("x" * 600) == 1  # untouched


def test_hyphen_position_violation_is_advisory_only(run_validator, sandbox):
    body = "---\nname: legacy-name-\ndescription: d\n---\nbody\n"
    d = sandbox.make_skill("legacy-name-", body)
    _backup(sandbox, "legacy-name-")
    out = run_validator(_payload(d / "SKILL.md"))
    assert "하이픈" in out and "롤백" not in out
    assert (d / "SKILL.md").read_text(encoding="utf-8") == body or "provenance" in (
        d / "SKILL.md").read_text(encoding="utf-8")  # stamped, not rolled back


def test_provenance_stamped_once(run_validator, sandbox):
    d = sandbox.make_skill("plain-skill")
    run_validator(_payload(d / "SKILL.md"))
    text = (d / "SKILL.md").read_text(encoding="utf-8")
    assert "provenance: self-improving-skills" in text
    run_validator(_payload(d / "SKILL.md"))
    assert (d / "SKILL.md").read_text(encoding="utf-8").count("provenance") == 1


def test_pinned_skill_distiller_edit_rolls_back(run_validator, sandbox):
    d = sandbox.make_skill("pinned-skill")
    good = (d / "SKILL.md").read_text(encoding="utf-8")
    sandbox.usage_store.set_fields("pinned-skill", pinned=True)
    _backup(sandbox, "pinned-skill")
    (d / "SKILL.md").write_text(good + "\nDISTILLER EDIT\n", encoding="utf-8")
    out = run_validator(_payload(d / "SKILL.md",
                                 agent_type="claude-cowork-self-improving-skills:skill-distiller"))
    assert "pinned" in out
    assert (d / "SKILL.md").read_text(encoding="utf-8") == good  # rolled back


def test_pinned_skill_foreground_edit_allowed(run_validator, sandbox):
    d = sandbox.make_skill("pinned-skill")
    sandbox.usage_store.set_fields("pinned-skill", pinned=True)
    _backup(sandbox, "pinned-skill")
    new = "---\nname: pinned-skill\ndescription: d2\n---\nbody2\n"
    (d / "SKILL.md").write_text(new, encoding="utf-8")
    out = run_validator(_payload(d / "SKILL.md"))  # no agent_type → human-driven
    assert "pinned" not in out
    assert "body2" in (d / "SKILL.md").read_text(encoding="utf-8")  # kept


def test_name_dir_mismatch_is_advisory_only(run_validator, sandbox):
    body = "---\nname: other-name\ndescription: d\n---\nbody\n"
    d = sandbox.make_skill("mismatch-skill", body)
    out = run_validator(_payload(d / "SKILL.md"))
    assert "디렉토리명" in out and "롤백" not in out
    assert "body" in (d / "SKILL.md").read_text(encoding="utf-8")  # untouched


def test_non_skill_paths_ignored(sandbox):
    env = dict(os.environ, HOME=str(sandbox.home))
    p = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "validate_skill.py")],
        input=json.dumps({"tool_input": {"file_path": "/tmp/whatever.md"}}),
        capture_output=True, text=True, env=env)
    assert p.stdout == ""
