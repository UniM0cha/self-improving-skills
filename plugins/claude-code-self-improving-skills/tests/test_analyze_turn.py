"""Stop-hook contract tests for analyze_turn.py (run as a real subprocess)."""

import json

from conftest import tool_use


def _work_rows(calls=12, edits=2):
    rows = [tool_use("Bash", {"command": "x"}) for _ in range(calls)]
    rows += [tool_use("Edit", {"file_path": "/tmp/f{0}.py".format(i)}) for i in range(edits)]
    return rows


def test_nudge_fires_at_threshold(run_analyzer):
    r = run_analyzer(_work_rows(), "s")
    assert r["decision"] == "block"
    assert "transcript" in r["reason"] and "s.jsonl" in r["reason"]


def test_below_call_threshold_approves(run_analyzer):
    assert run_analyzer(_work_rows(calls=9), "s")["decision"] == "approve"


def test_below_edit_threshold_approves(run_analyzer):
    assert run_analyzer(_work_rows(edits=1), "s")["decision"] == "approve"


def test_nudge_fires_only_once_per_segment(run_analyzer):
    rows = _work_rows()
    assert run_analyzer(rows, "s")["decision"] == "block"
    # Same accumulated work, next Stop: the recorded nudge suppresses a repeat.
    assert run_analyzer(rows, "s")["decision"] == "approve"


def test_renudges_after_another_threshold_of_work(run_analyzer):
    rows = _work_rows()
    assert run_analyzer(rows, "s")["decision"] == "block"
    rows2 = rows + _work_rows()
    assert run_analyzer(rows2, "s")["decision"] == "block"


def test_distillation_anchor_resets_counting(run_analyzer, sandbox):
    rows = _work_rows()
    rows.append(tool_use("Task", {"subagent_type": "claude-code-self-improving-skills:skill-distiller",
                                  "prompt": "distill"}))
    assert run_analyzer(rows, "s")["decision"] == "approve"


def test_stop_hook_active_loop_guard(run_analyzer):
    r = run_analyzer(_work_rows(), "s", {"stop_hook_active": True})
    assert r["decision"] == "approve"


def test_empty_payload_fails_safe(sandbox):
    from conftest import _run_script
    out = _run_script(sandbox.home, "analyze_turn.py", {})
    assert json.loads(out)["decision"] == "approve"


def test_core_advisory_fires_once(run_analyzer):
    core = [tool_use("Edit", {"file_path": "/r/plugins/claude-code-self-improving-skills/x.py"})]
    r = run_analyzer(core, "s")
    assert r["decision"] == "block" and "코어 소스" in r["reason"]
    assert run_analyzer(core, "s")["decision"] == "approve"


def test_readonly_segment_nudges_at_higher_threshold(run_analyzer):
    rows = [tool_use("Bash", {"command": "x"}) for _ in range(24)]
    r = run_analyzer(rows, "ro")
    assert r["decision"] == "block"
    assert "조사·디버깅" in r["reason"]  # read-only guidance branch


def test_readonly_below_threshold_approves(run_analyzer):
    rows = [tool_use("Bash", {"command": "x"}) for _ in range(23)]
    assert run_analyzer(rows, "ro2")["decision"] == "approve"


def test_distiller_model_env_included_in_nudge(run_analyzer, monkeypatch):
    monkeypatch.setenv("SIS_DISTILLER_MODEL", "sonnet")
    r = run_analyzer(_work_rows(), "m1")
    assert r["decision"] == "block" and 'model="sonnet"' in r["reason"]


def test_distiller_model_haiku_ignored(run_analyzer, monkeypatch):
    monkeypatch.setenv("SIS_DISTILLER_MODEL", "haiku")  # 정책: 서브에이전트 Haiku 금지
    r = run_analyzer(_work_rows(), "m2")
    assert r["decision"] == "block" and "model=" not in r["reason"]


def test_seed_labels_user_vs_distilled(run_analyzer, sandbox, store_data):
    sandbox.make_skill("hand-made")
    sandbox.make_skill("auto-made",
                       "---\nname: auto-made\ndescription: d\nmetadata:\n"
                       "  provenance: self-improving-skills\n  origin: distilled\n---\nbody\n")
    rows = [tool_use("Read", {"file_path": str(sandbox.skills / "hand-made" / "SKILL.md")}),
            tool_use("Read", {"file_path": str(sandbox.skills / "auto-made" / "SKILL.md")})]
    run_analyzer(rows, "s")
    data = store_data()
    assert data["hand-made"]["created_by"] == "user"
    assert data["auto-made"]["created_by"] == "agent"


def test_maintenance_segment_views_not_counted(run_analyzer, sandbox, store_data):
    sandbox.make_skill("some-skill")
    sp = str(sandbox.skills / "some-skill" / "SKILL.md")
    run_analyzer([tool_use("Skill", {"skill": "claude-code-self-improving-skills:curate-skills"}),
                  tool_use("Read", {"file_path": sp})], "maint")
    data = store_data()
    assert data.get("some-skill", {}).get("view_count", 0) == 0
    # a normal segment still counts the view
    run_analyzer([tool_use("Read", {"file_path": sp})], "normal")
    assert store_data()["some-skill"]["view_count"] == 1


def test_patch_not_counted_from_transcript_scan(run_analyzer, sandbox, store_data):
    sandbox.make_skill("some-skill")
    sp = str(sandbox.skills / "some-skill" / "SKILL.md")
    run_analyzer([tool_use("Edit", {"file_path": sp})], "s")
    assert store_data().get("some-skill", {}).get("patch_count", 0) == 0


def test_skill_use_counted(run_analyzer, sandbox, store_data):
    sandbox.make_skill("some-skill")
    run_analyzer([tool_use("Skill", {"skill": "some-skill"})], "s")
    assert store_data()["some-skill"]["use_count"] == 1
