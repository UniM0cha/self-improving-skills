import json
import os
import subprocess
import sys

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))


def test_stop_review_detects_recent_user_transcript_signal(tmp_path):
    transcript = tmp_path / "thread.jsonl"
    transcript.write_text(
        json.dumps({"role": "user", "content": "다음부터 이 규칙은 항상 기억해 주세요."})
        + "\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PLUGIN_DATA=str(tmp_path / "data"))
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "stop_review.py")],
        input=json.dumps({"turn_id": "t1", "transcript_path": str(transcript)}),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0

    signal_path = tmp_path / "data" / "review-signals.jsonl"
    row = json.loads(signal_path.read_text(encoding="utf-8").strip())
    assert row["signal"] is True
    assert row["signal_source"] == "transcript_user_messages"


def test_stop_review_loop_guard_writes_nothing(tmp_path):
    env = dict(os.environ, PLUGIN_DATA=str(tmp_path / "data"))
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "stop_review.py")],
        input=json.dumps({"stop_hook_active": True, "turn_id": "t1"}),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    assert not (tmp_path / "data" / "review-signals.jsonl").exists()


def _run_stop(tmp_path, payload, extra_env=None):
    env = dict(os.environ, PLUGIN_DATA=str(tmp_path / "data"))
    env.pop("CODEX_SELF_IMPROVE_AUTO", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "stop_review.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
        check=False,
    )


def _signals(tmp_path):
    path = tmp_path / "data" / "review-signals.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_topic_words_alone_are_not_a_signal(tmp_path):
    transcript = tmp_path / "thread.jsonl"
    transcript.write_text(
        json.dumps({"role": "user", "content": "스킬 훅 개선 관련 코드를 반복해서 보여줘."}) + "\n",
        encoding="utf-8",
    )
    _run_stop(tmp_path, {"turn_id": "t1", "transcript_path": str(transcript)})
    assert _signals(tmp_path)[-1]["signal"] is False


def test_transcript_signal_consumed_once(tmp_path):
    transcript = tmp_path / "thread.jsonl"
    transcript.write_text(
        json.dumps({"role": "user", "content": "다음부터 이 규칙은 항상 기억해 주세요."}) + "\n",
        encoding="utf-8",
    )
    _run_stop(tmp_path, {"turn_id": "t1", "transcript_path": str(transcript)})
    assert _signals(tmp_path)[-1]["signal"] is True
    # same transcript, next Stop: the consumed window must not re-fire
    _run_stop(tmp_path, {"turn_id": "t2", "transcript_path": str(transcript)})
    assert _signals(tmp_path)[-1]["signal"] is False


def test_interval_trigger_uses_tool_counter_and_resets(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    # seeded in the LEGACY single-int shape on purpose — migration must pick
    # it up as the "global" session
    (data / "usage.json").write_text(json.dumps({
        "version": 1, "skills": {}, "tools": {},
        "counters": {"iters_since_review": 10},
    }), encoding="utf-8")
    transcript = tmp_path / "thread.jsonl"
    transcript.write_text(json.dumps({"role": "user", "content": "ok"}) + "\n",
                          encoding="utf-8")
    proc = _run_stop(tmp_path, {"turn_id": "t1", "transcript_path": str(transcript)},
                     extra_env={"CODEX_SELF_IMPROVE_AUTO": "1"})
    out = json.loads(proc.stdout.strip())
    assert out["decision"] == "block"
    usage = json.loads((data / "usage.json").read_text(encoding="utf-8"))
    counters = usage["counters"]["iters_since_review_by_session"]
    assert counters["global"]["v"] == 0  # consumed after firing
    assert "iters_since_review" not in usage["counters"]  # legacy key migrated


def test_interval_trigger_auto_continues_when_env_is_unset(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "usage.json").write_text(json.dumps({
        "version": 1, "skills": {}, "tools": {},
        "counters": {"iters_since_review": 10},
    }), encoding="utf-8")

    proc = _run_stop(tmp_path, {"turn_id": "default-on"})
    out = json.loads(proc.stdout.strip())
    assert out["decision"] == "block"


def test_explicit_non_truthy_value_disables_auto_continue(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "usage.json").write_text(json.dumps({
        "version": 1, "skills": {}, "tools": {},
        "counters": {"iters_since_review": 10},
    }), encoding="utf-8")

    proc = _run_stop(
        tmp_path,
        {"turn_id": "explicit-off"},
        extra_env={"CODEX_SELF_IMPROVE_AUTO": "off"},
    )
    assert proc.stdout.strip() == ""


def test_below_counter_threshold_does_not_continue(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "usage.json").write_text(json.dumps({
        "version": 1, "skills": {}, "tools": {},
        "counters": {"iters_since_review": 3},
    }), encoding="utf-8")
    proc = _run_stop(tmp_path, {"turn_id": "t1"},
                     extra_env={"CODEX_SELF_IMPROVE_AUTO": "1"})
    assert proc.stdout.strip() == ""  # no block emitted


def test_payload_signal_also_consumes_transcript_window(tmp_path):
    """codex review R1: a last_user_message signal must consume the transcript
    rows too, or the SAME message re-fires as a transcript signal next Stop."""
    message = "다음부터 이 규칙은 항상 기억해 주세요."
    transcript = tmp_path / "thread.jsonl"
    transcript.write_text(json.dumps({"role": "user", "content": message}) + "\n",
                          encoding="utf-8")
    _run_stop(tmp_path, {"turn_id": "t1", "transcript_path": str(transcript),
                         "last_user_message": message})
    assert _signals(tmp_path)[-1]["signal_source"] == "last_user_message"
    _run_stop(tmp_path, {"turn_id": "t2", "transcript_path": str(transcript)})
    assert _signals(tmp_path)[-1]["signal"] is False


def test_codex_response_item_payload_rows_are_parsed(tmp_path):
    """codex review R4: real Codex rollout rows wrap text under
    response_item.payload.content — both the signal scan and the $skill
    attribution must see through it."""
    skills_root = tmp_path / "skills"
    skill = skills_root / "wrapped-trick"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: wrapped-trick\ndescription: d\n---\nbody\n", encoding="utf-8")
    transcript = tmp_path / "thread.jsonl"
    row = {"type": "response_item",
           "payload": {"role": "user",
                       "content": [{"type": "input_text",
                                    "text": "다음부터 $wrapped-trick 을 항상 기억해."}]}}
    transcript.write_text(json.dumps(row) + "\n", encoding="utf-8")
    _run_stop(tmp_path, {"turn_id": "t1", "transcript_path": str(transcript)},
              extra_env={"CODEX_SELF_IMPROVE_SKILL_ROOTS": str(skills_root)})
    assert _signals(tmp_path)[-1]["signal"] is True  # role seen through payload
    usage = json.loads((tmp_path / "data" / "usage.json").read_text(encoding="utf-8"))
    assert usage["skills"]["wrapped-trick"]["use_count"] == 1


def test_skill_use_attributed_from_transcript(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "my-trick"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: my-trick\ndescription: d\n---\nbody\n", encoding="utf-8")
    transcript = tmp_path / "thread.jsonl"
    transcript.write_text(
        json.dumps({"role": "user", "content": "run $my-trick on this file"}) + "\n",
        encoding="utf-8",
    )
    _run_stop(tmp_path, {"turn_id": "t1", "transcript_path": str(transcript)},
              extra_env={"CODEX_SELF_IMPROVE_SKILL_ROOTS": str(skills_root)})
    usage = json.loads((tmp_path / "data" / "usage.json").read_text(encoding="utf-8"))
    assert usage["skills"]["my-trick"]["use_count"] == 1
    # second Stop on the same transcript: no new rows → no double count
    _run_stop(tmp_path, {"turn_id": "t2", "transcript_path": str(transcript)},
              extra_env={"CODEX_SELF_IMPROVE_SKILL_ROOTS": str(skills_root)})
    usage = json.loads((tmp_path / "data" / "usage.json").read_text(encoding="utf-8"))
    assert usage["skills"]["my-trick"]["use_count"] == 1
