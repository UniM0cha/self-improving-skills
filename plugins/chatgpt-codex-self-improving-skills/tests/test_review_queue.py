import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import review_queue


def _enqueue(
    queue,
    *,
    session="session-a",
    turn="turn-1",
    cutoff=3,
    model="gpt-test",
    signal=True,
    signal_source="last_user_message",
    trigger="signal",
    transcript_path=None,
):
    if transcript_path is None:
        transcript_path = queue.path.parent / "thread.jsonl"
    return queue.enqueue(
        session_id=session,
        turn_id=turn,
        transcript_path=str(transcript_path),
        transcript_rows=cutoff,
        signal=signal,
        signal_source=signal_source,
        trigger=trigger,
        model=model,
    )


def _result(status="nothing_to_save"):
    return {"status": status, "skills": [], "candidates": [], "summary": "done"}


def test_fallback_home_skips_relative_home_for_absolute_userprofile(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", "relative-home")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert review_queue._fallback_user_home() == tmp_path.resolve()


def test_enqueue_stores_coordinates_not_transcript_content(tmp_path):
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    created = _enqueue(queue)
    assert created["enqueued"] is True
    assert created["coalesced"] is False

    with sqlite3.connect(queue.path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(review_jobs)")}
        row = conn.execute(
            "SELECT session_id, turn_id, transcript_path, transcript_rows, model FROM review_jobs"
        ).fetchone()
    assert row == ("session-a", "turn-1", str((tmp_path / "thread.jsonl").absolute()), 3, "gpt-test")
    assert "transcript" not in columns
    assert "prompt" not in columns
    assert "source_cwd" not in columns
    assert "permission_mode" not in columns
    assert "trigger" in columns


def test_same_session_pending_jobs_coalesce_and_all_turns_dedupe(tmp_path):
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    first = _enqueue(queue, turn="turn-1", cutoff=3)
    second = _enqueue(queue, turn="turn-2", cutoff=9, model="gpt-new")
    duplicate_old = _enqueue(queue, turn="turn-1", cutoff=99)
    duplicate_new = _enqueue(queue, turn="turn-2", cutoff=99)

    assert second["job_id"] == first["job_id"]
    assert second["coalesced"] is True
    assert queue.get(first["job_id"])["turn_id"] == "turn-2"
    assert queue.get(first["job_id"])["transcript_rows"] == 9
    assert duplicate_old["duplicate"] is True
    assert duplicate_new["duplicate"] is True
    assert len(queue.list_jobs()) == 1


def test_coalescing_preserves_earlier_signal_and_unions_trigger_reason(tmp_path):
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    first = _enqueue(queue, turn="correction", signal=True, trigger="signal")
    second = _enqueue(
        queue,
        turn="interval",
        signal=False,
        signal_source="none",
        trigger="interval",
        model=None,
        transcript_path="",
    )

    assert second["job_id"] == first["job_id"]
    job = queue.get(first["job_id"])
    assert job["signal"] is True
    assert job["signal_source"] == "last_user_message"
    assert job["trigger"] == "signal+interval"
    assert job["transcript_path"] == str((tmp_path / "thread.jsonl").absolute())
    assert job["model"] == "gpt-test"


def test_queue_rejects_symlink_database_and_uses_private_permissions(tmp_path):
    queue = review_queue.ReviewQueue(tmp_path / "private.sqlite3")
    _enqueue(queue)
    if os.name != "nt":
        assert stat.S_IMODE(queue.path.stat().st_mode) == 0o600
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{queue.path}{suffix}")
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600

    target = tmp_path / "target.sqlite3"
    target.write_text("leave me alone", encoding="utf-8")
    link = tmp_path / "linked.sqlite3"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ValueError, match="symbolic link"):
        review_queue.ReviewQueue(link)
    assert target.read_text(encoding="utf-8") == "leave me alone"


def test_running_job_is_not_coalesced_with_a_new_turn(tmp_path):
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    first = _enqueue(queue)
    claimed = queue.claim_next("worker", pid=os.getpid())
    assert claimed["id"] == first["job_id"]
    second = _enqueue(queue, turn="turn-2")
    assert second["coalesced"] is False
    assert second["job_id"] != first["job_id"]


def test_single_worker_lease_and_job_completion(tmp_path):
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    job_id = _enqueue(queue)["job_id"]
    assert queue.acquire_worker_lease("worker-1", pid=os.getpid()) is True
    assert queue.acquire_worker_lease("worker-2", pid=os.getpid()) is False
    claimed = queue.claim_next("worker-1", pid=os.getpid())
    assert claimed["attempts"] == 1
    assert queue.heartbeat_job(job_id, "worker-1") is True
    assert queue.complete(job_id, "worker-1", _result()) is True
    assert queue.get(job_id)["status"] == "done"
    assert queue.get(job_id)["result"]["status"] == "nothing_to_save"
    assert queue.release_worker_lease("worker-1") is True


def test_failure_delays_are_30_then_300_and_third_attempt_is_terminal(tmp_path, monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(review_queue, "_now", lambda: clock[0])
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    job_id = _enqueue(queue)["job_id"]

    first = queue.claim_next("w")
    failed = queue.fail(job_id, "w", code="boom", message="first")
    assert failed == {"updated": True, "status": "pending", "retry_delay_seconds": 30}
    assert queue.claim_next("w") is None

    clock[0] += 30
    assert queue.claim_next("w")["attempts"] == 2
    failed = queue.fail(job_id, "w", code="boom", message="second")
    assert failed["retry_delay_seconds"] == 300

    clock[0] += 300
    assert queue.claim_next("w")["attempts"] == 3
    failed = queue.fail(job_id, "w", code="boom", message="third")
    assert failed == {"updated": True, "status": "failed", "retry_delay_seconds": None}
    assert queue.status()["counts"]["failed"] == 1
    assert queue.retry(job_id) is True
    assert queue.get(job_id)["status"] == "pending"
    assert queue.get(job_id)["attempts"] == 0


def test_coalescing_new_turn_does_not_bypass_retry_delay(tmp_path, monkeypatch):
    clock = [5_000.0]
    monkeypatch.setattr(review_queue, "_now", lambda: clock[0])
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    job_id = _enqueue(queue)["job_id"]
    assert queue.claim_next("w") is not None
    queue.fail(job_id, "w", code="boom", message="fixed")
    ready_at = queue.get(job_id)["available_at"]
    coalesced = _enqueue(queue, turn="turn-2", cutoff=20)
    assert coalesced["coalesced"] is True
    assert queue.get(job_id)["available_at"] == ready_at
    assert queue.claim_next("w") is None


def test_expired_dead_worker_recovers_completed_result(tmp_path, monkeypatch):
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    job_id = _enqueue(queue)["job_id"]
    claimed = queue.claim_next("dead", pid=999_999_999)
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_result("changed")), encoding="utf-8")
    queue.set_result_path(job_id, "dead", str(result_path))
    with sqlite3.connect(queue.path) as conn:
        conn.execute("UPDATE review_jobs SET lease_expires_at=0 WHERE id=?", (job_id,))
    monkeypatch.setattr(review_queue, "_pid_alive", lambda _pid: False)
    assert queue.recover_expired_jobs() == 1
    recovered = queue.get(claimed["id"])
    assert recovered["status"] == "done"
    assert recovered["result"]["status"] == "changed"


def test_crash_recovery_retries_reported_failed_result(tmp_path, monkeypatch):
    clock = [1_500.0]
    monkeypatch.setattr(review_queue, "_now", lambda: clock[0])
    monkeypatch.setattr(review_queue, "_pid_alive", lambda _pid: False)
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    job_id = _enqueue(queue)["job_id"]
    queue.claim_next("dead", pid=999_999_999)
    result_path = tmp_path / "failed-result.json"
    result_path.write_text(json.dumps(_result("failed")), encoding="utf-8")
    queue.set_result_path(job_id, "dead", str(result_path))

    assert queue.recover_expired_jobs() == 1
    recovered = queue.get(job_id)
    assert recovered["status"] == "pending"
    assert recovered["error_code"] == "review_reported_failure"
    assert recovered["retry_delay_seconds"] == 30


def test_interrupted_worker_recovery_observes_fixed_backoff(tmp_path, monkeypatch):
    clock = [2_000.0]
    monkeypatch.setattr(review_queue, "_now", lambda: clock[0])
    monkeypatch.setattr(review_queue, "_pid_alive", lambda _pid: False)
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    job_id = _enqueue(queue)["job_id"]
    queue.claim_next("dead", pid=999_999_999)
    assert queue.recover_expired_jobs() == 1
    recovered = queue.get(job_id)
    assert recovered["status"] == "pending"
    assert recovered["retry_delay_seconds"] == 30
    assert recovered["available_at"] == 2_030.0


def test_expired_job_lease_never_recovers_over_a_live_worker(tmp_path, monkeypatch):
    clock = [3_000.0]
    monkeypatch.setattr(review_queue, "_now", lambda: clock[0])
    monkeypatch.setattr(review_queue, "_pid_alive", lambda _pid: True)
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    job_id = _enqueue(queue)["job_id"]
    queue.claim_next("hung", pid=os.getpid())
    with sqlite3.connect(queue.path) as conn:
        conn.execute(
            "UPDATE review_jobs SET lease_expires_at=? WHERE id=?",
            (clock[0] - 1, job_id),
        )

    assert queue.recover_expired_jobs() == 0
    assert queue.get(job_id)["status"] == "running"


def test_expired_singleton_lease_is_not_stolen_from_live_worker(tmp_path, monkeypatch):
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    assert queue.acquire_worker_lease("old", pid=os.getpid()) is True
    with sqlite3.connect(queue.path) as conn:
        conn.execute("UPDATE review_worker_lease SET expires_at=0 WHERE singleton=1")
    monkeypatch.setattr(review_queue, "_pid_alive", lambda _pid: True)
    assert queue.acquire_worker_lease("new", pid=os.getpid()) is False


def test_reused_pid_does_not_hold_singleton_worker_lease(tmp_path, monkeypatch):
    identities = {12345: "original", 67890: "new-worker"}
    monkeypatch.setattr(review_queue, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(review_queue, "_pid_identity", lambda pid: identities.get(int(pid)))
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    assert queue.acquire_worker_lease("old", pid=12345) is True

    identities[12345] = "reused-by-another-process"

    assert queue.acquire_worker_lease("new", pid=67890) is True
    assert queue.worker_lease()["owner"] == "new"


def test_reused_pid_allows_interrupted_job_recovery(tmp_path, monkeypatch):
    identities = {12345: "original"}
    monkeypatch.setattr(review_queue, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(review_queue, "_pid_identity", lambda pid: identities.get(int(pid)))
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    job_id = _enqueue(queue)["job_id"]
    queue.claim_next("old", pid=12345)

    identities[12345] = "reused-by-another-process"

    assert queue.recover_expired_jobs() == 1
    assert queue.get(job_id)["status"] == "pending"
    assert queue.get(job_id)["error_code"] == "worker_interrupted"


def test_model_fallback_marker_persists_and_manual_retry_resets_it(tmp_path):
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    job_id = _enqueue(queue)["job_id"]
    queue.claim_next("worker", pid=os.getpid())
    assert queue.mark_model_fallback_used(job_id, "worker") is True
    queue.block(job_id, "worker", code="test", message="blocked")
    assert queue.get(job_id)["model_fallback_used"] is True
    assert queue.retry(job_id) is True
    assert queue.get(job_id)["model_fallback_used"] is False


def test_pid_liveness_probe_never_terminates_a_live_child():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert review_queue._pid_alive(child.pid) is True
        time.sleep(0.05)
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_result_contract_rejects_malformed_entries():
    with pytest.raises(ValueError):
        review_queue.validate_result({"status": "changed", "skills": [], "candidates": []})
    with pytest.raises(ValueError):
        review_queue.validate_result(
            {"status": "changed", "skills": [{"name": "x"}], "candidates": [], "summary": "x"}
        )


def test_cleanup_keeps_pending_and_blocked_jobs(tmp_path, monkeypatch):
    clock = [10_000_000.0]
    monkeypatch.setattr(review_queue, "_now", lambda: clock[0])
    queue = review_queue.ReviewQueue(tmp_path / "jobs.sqlite3")
    ids = []
    for index, status_name in enumerate(("done", "failed", "blocked", "pending"), start=1):
        job_id = _enqueue(queue, session=f"s-{index}", turn=f"t-{index}")["job_id"]
        ids.append(job_id)
        with sqlite3.connect(queue.path) as conn:
            conn.execute(
                "UPDATE review_jobs SET status=?, completed_at=?, updated_at=? WHERE id=?",
                (status_name, clock[0] - 31 * 86400, clock[0] - 31 * 86400, job_id),
            )

    assert queue.cleanup() == 2
    assert queue.get(ids[0]) is None
    assert queue.get(ids[1]) is None
    assert queue.get(ids[2])["status"] == "blocked"
    assert queue.get(ids[3])["status"] == "pending"
