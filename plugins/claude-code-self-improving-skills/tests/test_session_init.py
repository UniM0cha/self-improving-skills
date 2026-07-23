"""Unit tests for session_init.py counting rules and background reporting."""

import importlib
import json

from conftest import _run_script

PROV = ("---\nname: {0}\ndescription: d\nmetadata:\n"
        "  provenance: self-improving-skills\n---\nbody\n")


def test_count_excludes_support_dir_skillmd(sandbox):
    import session_init
    importlib.reload(session_init)  # rebind SKILLS_DIR to the sandbox HOME
    sandbox.make_skill("learned-one", PROV.format("learned-one"))
    # a SKILL.md copy inside a support dir must NOT inflate the count
    ref = sandbox.skills / "learned-one" / "references"
    ref.mkdir()
    (ref / "SKILL.md").write_text(PROV.format("copy"), encoding="utf-8")
    # nor a non-provenance (user-authored) skill
    sandbox.make_skill("hand-made")
    assert session_init._count_learned_skills() == 1


def _init(sandbox, env=None):
    run_env = {"SIS_REVIEW_MODE": "background", "SIS_TEST_NO_LAUNCH": "1"}
    run_env.update(env or {})
    out = _run_script(sandbox.home, "session_init.py", {"source": "startup"}, run_env)
    return json.loads(out)["hookSpecificOutput"] if out.strip() else {}


def _queue(sandbox):
    import distill_queue
    importlib.reload(distill_queue)
    return distill_queue.DistillQueue(
        sandbox.home / ".claude" / "self-improve" / "distill-jobs.sqlite3")


def _job(queue, session="s1", prompt="p1"):
    return queue.enqueue(
        session_id=session, prompt_id=prompt, transcript_path="/tmp/t.jsonl",
        transcript_rows=5, signal=True, signal_source="stop_hook", trigger="signal")


def test_a_quiet_queue_produces_no_alert(sandbox):
    _queue(sandbox)  # create an empty queue
    context = _init(sandbox)["additionalContext"]
    assert "증류 작업" not in context


def test_a_finished_job_is_not_announced(sandbox):
    queue = _queue(sandbox)
    job_id = _job(queue)["job_id"]
    queue.claim_next("w1")
    queue.complete(job_id, "w1", {"status": "nothing_to_save", "skills": [],
                                  "candidates": [], "summary": "-"})
    # Success is not news; only states a human must act on are surfaced.
    assert "증류 작업" not in _init(sandbox)["additionalContext"]


def test_an_auth_blocked_job_tells_the_user_what_to_do(sandbox):
    queue = _queue(sandbox)
    job_id = _job(queue)["job_id"]
    queue.claim_next("w1")
    queue.block(job_id, "w1", code="authentication_required", message="sign in")
    context = _init(sandbox)["additionalContext"]
    assert "setup-token" in context and "worker.env" in context


def test_an_unprotected_write_is_surfaced(sandbox):
    queue = _queue(sandbox)
    job_id = _job(queue)["job_id"]
    queue.claim_next("w1")
    queue.block(job_id, "w1", code="unprotected_write", message="cannot roll back")
    assert "되돌릴 수 없는 쓰기" in _init(sandbox)["additionalContext"]


def test_an_installed_skill_asks_for_a_skill_reload(sandbox):
    queue = _queue(sandbox)
    job_id = _job(queue)["job_id"]
    queue.claim_next("w1")
    queue.complete(job_id, "w1", {
        "status": "changed",
        "skills": [{"name": "fresh", "action": "installed", "path": "/x/SKILL.md"}],
        "candidates": [], "summary": "kept"})
    # Skill discovery runs before SessionStart finishes, so without this the
    # new skill would only be usable one session later.
    assert _init(sandbox).get("reloadSkills") is True


def test_the_same_finished_job_does_not_ask_twice(sandbox):
    queue = _queue(sandbox)
    job_id = _job(queue)["job_id"]
    queue.claim_next("w1")
    queue.complete(job_id, "w1", {
        "status": "changed",
        "skills": [{"name": "fresh", "action": "installed", "path": "/x/SKILL.md"}],
        "candidates": [], "summary": "kept"})
    assert _init(sandbox).get("reloadSkills") is True
    # SessionStart fires again on resume and fork.
    assert _init(sandbox).get("reloadSkills") is not True


def test_foreground_mode_keeps_the_delegation_guidance(sandbox):
    context = _init(sandbox, {"SIS_REVIEW_MODE": "foreground"})["additionalContext"]
    assert "skill-distiller" in context
    assert "run_in_background=true" in context


def test_off_mode_injects_nothing(sandbox):
    # Claiming the loop is active would be false, and the background child runs
    # in this mode too — any guidance there is noise it must ignore.
    assert _init(sandbox, {"SIS_REVIEW_MODE": "off"}) == {}


def test_background_mode_tells_the_agent_it_has_nothing_to_do(sandbox):
    context = _init(sandbox)["additionalContext"]
    assert "백그라운드 모드" in context
