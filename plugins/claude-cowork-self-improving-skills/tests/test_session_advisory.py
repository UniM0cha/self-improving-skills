"""UserPromptSubmit hook-contract tests for session_advisory.py (subprocess)."""

import json

PROV = ("---\nname: {0}\ndescription: d\nmetadata:\n"
        "  provenance: self-improving-skills\n---\nbody\n")


def _ctx(out):
    """Parse hook stdout -> additionalContext string (or None for silent)."""
    if not out.strip():
        return None
    d = json.loads(out)
    h = d["hookSpecificOutput"]
    assert h["hookEventName"] == "UserPromptSubmit"
    return h["additionalContext"]


def test_first_prompt_emits_advisory_and_marks_flag(run_advisory, sandbox):
    ctx = _ctx(run_advisory())
    assert ctx and "자기개선 루프 활성" in ctx
    # the Cowork-critical rules must be in the advisory
    assert "스킬 저장" in ctx and "SendUserFile" in ctx
    assert "claude" in ctx  # reserved-word naming rule mentioned
    assert (sandbox.home / ".claude" / "self-improve" / "advisory_shown").is_file()


def test_second_prompt_is_silent(run_advisory):
    assert _ctx(run_advisory()) is not None
    assert _ctx(run_advisory()) is None  # once per session


def test_learned_count_line_appears(run_advisory, sandbox):
    sandbox.make_skill("learned-one", PROV.format("learned-one"))
    # a SKILL.md copy inside a support dir must NOT inflate the count
    ref = sandbox.skills / "learned-one" / "references"
    ref.mkdir()
    (ref / "SKILL.md").write_text(PROV.format("copy"), encoding="utf-8")
    # nor a non-provenance (user-authored) skill
    sandbox.make_skill("hand-made")
    ctx = _ctx(run_advisory())
    assert "학습 스킬 1개" in ctx


def test_no_count_line_when_no_learned_skills(run_advisory):
    ctx = _ctx(run_advisory())
    assert "개가 동기화되어" not in ctx


def test_origin_marker_alone_counts_as_learned(run_advisory, sandbox):
    # a distiller may write its own metadata with only origin: distilled —
    # the validator leaves existing metadata untouched, so the counter must
    # accept either marker
    sandbox.make_skill("origin-only",
                       "---\nname: origin-only\ndescription: d\nmetadata:\n"
                       "  origin: distilled\n---\nbody\n")
    ctx = _ctx(run_advisory())
    assert "학습 스킬 1개" in ctx


def test_stop_fallback_flag_respected(run_advisory, sandbox):
    # if the Stop-hook fallback already showed the advisory, stay silent
    d = sandbox.home / ".claude" / "self-improve"
    d.mkdir(parents=True, exist_ok=True)
    (d / "advisory_shown").write_text("shown-by-stop-fallback\n", encoding="utf-8")
    assert _ctx(run_advisory()) is None
