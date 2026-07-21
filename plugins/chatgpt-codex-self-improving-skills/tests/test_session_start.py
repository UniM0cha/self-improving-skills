import json
import os
import sqlite3
import subprocess
import sys
import time


SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS_DIR)

import review_queue
import background_review_worker
import session_start


def _run_session_start(
    tmp_path,
    auto_value=None,
    mode_value=None,
    *,
    extra_env=None,
    launch_disabled=True,
):
    env = dict(os.environ, PLUGIN_DATA=str(tmp_path / "data"))
    env.pop("CODEX_SELF_IMPROVE_AUTO", None)
    env.pop("CODEX_SELF_IMPROVE_MODE", None)
    env.pop("CODEX_SELF_IMPROVE_CODEX_BIN", None)
    env.pop("CODEX_SELF_IMPROVE_TEST_NO_LAUNCH", None)
    if auto_value is not None:
        env["CODEX_SELF_IMPROVE_AUTO"] = auto_value
    if mode_value is not None:
        env["CODEX_SELF_IMPROVE_MODE"] = mode_value
    if launch_disabled:
        env["CODEX_SELF_IMPROVE_TEST_NO_LAUNCH"] = "1"
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "session_start.py")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_session_start_reports_default_background_review_on(tmp_path):
    proc = _run_session_start(tmp_path)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    note = payload["hookSpecificOutput"]["additionalContext"]
    assert "Background review is on." in note


def test_session_start_reports_legacy_explicit_auto_off(tmp_path):
    proc = _run_session_start(tmp_path, auto_value="0")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    note = payload["hookSpecificOutput"]["additionalContext"]
    assert "Automatic review is off." in note


def test_session_start_reports_foreground_compatibility_mode(tmp_path):
    proc = _run_session_start(tmp_path, mode_value="foreground")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    note = payload["hookSpecificOutput"]["additionalContext"]
    assert "Foreground review continuation is on." in note


def test_session_start_fails_closed_on_invalid_mode(tmp_path):
    proc = _run_session_start(tmp_path, auto_value="1", mode_value="surprise")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.strip())
    note = payload["hookSpecificOutput"]["additionalContext"]
    assert "Automatic review is off" in note
    assert "MODE is invalid" in note


def test_session_start_reports_repository_candidate_but_not_successes(tmp_path):
    queue = review_queue.ReviewQueue(tmp_path / "data" / "review-jobs.sqlite3")
    candidate_id = queue.enqueue(
        session_id="candidate-session",
        turn_id="candidate-turn",
        transcript_path=str(tmp_path / "thread.jsonl"),
        transcript_rows=1,
        signal=True,
        signal_source="last_user_message",
        trigger="signal",
        model=None,
    )["job_id"]
    queue.claim_next("test-worker", pid=os.getpid())
    queue.complete(
        candidate_id,
        "test-worker",
        {
            "status": "candidate",
            "skills": [],
            "candidates": [
                {
                    "name": "repo-skill",
                    "reason": "repository root is read-only",
                    "proposed_change": "Add the durable workflow guard.",
                }
            ],
            "summary": "candidate saved",
        },
    )

    success_id = queue.enqueue(
        session_id="success-session",
        turn_id="success-turn",
        transcript_path=str(tmp_path / "thread.jsonl"),
        transcript_rows=1,
        signal=False,
        signal_source="none",
        trigger="interval",
        model=None,
    )["job_id"]
    queue.claim_next("test-worker", pid=os.getpid())
    queue.complete(
        success_id,
        "test-worker",
        {"status": "nothing_to_save", "skills": [], "candidates": [], "summary": "none"},
    )

    proc = _run_session_start(tmp_path, mode_value="off")
    note = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "1 repository-skill candidate(s)" in note
    assert "nothing_to_save" not in note


def test_session_start_warns_when_pending_job_has_no_codex_runner(tmp_path):
    queue = review_queue.ReviewQueue(tmp_path / "data" / "review-jobs.sqlite3")
    queue.enqueue(
        session_id="pending-session",
        turn_id="pending-turn",
        transcript_path=str(tmp_path / "thread.jsonl"),
        transcript_rows=1,
        signal=False,
        signal_source="none",
        trigger="interval",
        model=None,
    )
    proc = _run_session_start(
        tmp_path,
        launch_disabled=False,
        extra_env={"CODEX_SELF_IMPROVE_CODEX_BIN": str(tmp_path / "missing-codex")},
    )
    note = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Codex CLI unavailable" in note


def test_session_start_launches_recovery_worker_for_stale_running_job(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
    queue = review_queue.ReviewQueue(tmp_path / "data" / "review-jobs.sqlite3")
    queue.enqueue(
        session_id="stale-session",
        turn_id="stale-turn",
        transcript_path=str(tmp_path / "thread.jsonl"),
        transcript_rows=1,
        signal=False,
        signal_source="none",
        trigger="interval",
        model=None,
    )
    queue.claim_next("stale-worker", pid=999_999_999)
    launched = []
    monkeypatch.setattr(
        background_review_worker,
        "launch_detached",
        lambda path: launched.append(path) or {"launched": True, "reason": "started"},
    )

    session_start._background_review_note(allow_launch=True)

    assert launched == [queue.path]


def test_session_start_does_not_overlap_live_stale_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
    queue = review_queue.ReviewQueue(tmp_path / "data" / "review-jobs.sqlite3")
    queue.enqueue(
        session_id="pending-session",
        turn_id="pending-turn",
        transcript_path=str(tmp_path / "thread.jsonl"),
        transcript_rows=1,
        signal=False,
        signal_source="none",
        trigger="interval",
        model=None,
    )
    queue.acquire_worker_lease("live-stale", pid=os.getpid())
    with sqlite3.connect(queue.path) as conn:
        conn.execute("UPDATE review_worker_lease SET expires_at=0 WHERE singleton=1")
    launched = []
    monkeypatch.setattr(
        background_review_worker,
        "launch_detached",
        lambda path: launched.append(path) or {"launched": True, "reason": "started"},
    )

    note = session_start._background_review_note(allow_launch=True)

    assert launched == []
    assert "stale worker process is still alive" in note


def test_session_start_launches_when_worker_pid_was_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
    identities = {12345: "original"}
    monkeypatch.setattr(review_queue, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(review_queue, "_pid_identity", lambda pid: identities.get(int(pid)))
    queue = review_queue.ReviewQueue(tmp_path / "data" / "review-jobs.sqlite3")
    queue.enqueue(
        session_id="pending-session",
        turn_id="pending-turn",
        transcript_path=str(tmp_path / "thread.jsonl"),
        transcript_rows=1,
        signal=False,
        signal_source="none",
        trigger="interval",
        model=None,
    )
    queue.acquire_worker_lease("old-worker", pid=12345)
    identities[12345] = "reused-by-another-process"
    launched = []
    monkeypatch.setattr(
        background_review_worker,
        "launch_detached",
        lambda path: launched.append(path) or {"launched": True, "reason": "started"},
    )

    session_start._background_review_note(allow_launch=True)

    assert launched == [queue.path]


def test_session_start_reports_authentication_block_with_retry_guidance(tmp_path):
    queue = review_queue.ReviewQueue(tmp_path / "data" / "review-jobs.sqlite3")
    job_id = queue.enqueue(
        session_id="auth-session",
        turn_id="auth-turn",
        transcript_path=str(tmp_path / "thread.jsonl"),
        transcript_rows=1,
        signal=True,
        signal_source="last_user_message",
        trigger="signal",
        model=None,
    )["job_id"]
    queue.claim_next("auth-worker", pid=os.getpid())
    queue.block(
        job_id,
        "auth-worker",
        code="authentication_required",
        message="Codex authentication is required",
    )

    proc = _run_session_start(tmp_path, mode_value="off")
    note = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "waiting for Codex authentication" in note
    assert "sign in, then retry" in note


def test_session_start_cleans_expired_completion_metadata_and_results(tmp_path):
    queue = review_queue.ReviewQueue(tmp_path / "data" / "review-jobs.sqlite3")
    job_id = queue.enqueue(
        session_id="old-session",
        turn_id="old-turn",
        transcript_path=str(tmp_path / "thread.jsonl"),
        transcript_rows=1,
        signal=False,
        signal_source="none",
        trigger="interval",
        model=None,
    )["job_id"]
    queue.claim_next("old-worker", pid=os.getpid())
    queue.complete(
        job_id,
        "old-worker",
        {"status": "nothing_to_save", "skills": [], "candidates": [], "summary": "none"},
    )
    expired = time.time() - 31 * 86400
    with sqlite3.connect(queue.path) as conn:
        conn.execute(
            "UPDATE review_jobs SET completed_at=?, updated_at=? WHERE id=?",
            (expired, expired, job_id),
        )
    run_dir = queue.path.parent / "background-review-runs"
    run_dir.mkdir()
    result = run_dir / f"job-{job_id}-attempt-1.json"
    result.write_text("{}", encoding="utf-8")
    os.utime(result, (expired, expired))

    proc = _run_session_start(tmp_path, mode_value="off")

    assert proc.returncode == 0
    assert queue.get(job_id) is None
    assert not result.exists()
