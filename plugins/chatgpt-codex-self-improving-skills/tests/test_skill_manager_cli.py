"""CLI contracts for sanitized background-review queue controls."""

import json
import os
import subprocess
import sys


SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
CLI = os.path.join(SCRIPTS_DIR, "skill_manager_cli.py")
sys.path.insert(0, SCRIPTS_DIR)

from review_queue import ReviewQueue


def _env(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    env = dict(
        os.environ,
        PLUGIN_DATA=str(tmp_path / "data"),
        CODEX_SELF_IMPROVE_SKILL_ROOTS=str(skills_root),
        CODEX_SELF_IMPROVE_CREATE_ROOT=str(skills_root),
        CODEX_SELF_IMPROVE_WRITE_ROOTS=str(skills_root),
        CODEX_SELF_IMPROVE_CODEX_BIN=str(tmp_path / "missing-codex"),
    )
    env.pop("CODEX_SELF_IMPROVE_MODE", None)
    env.pop("CODEX_SELF_IMPROVE_AUTO", None)
    return env


def _run(tmp_path, *args):
    return subprocess.run(
        [sys.executable, CLI, *args],
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        check=False,
    )


def _blocked_job(tmp_path):
    transcript = tmp_path / "private-transcript.jsonl"
    transcript.write_text("SECRET_TRANSCRIPT_CONTENT\n", encoding="utf-8")
    queue = ReviewQueue(tmp_path / "data" / "review-jobs.sqlite3")
    enqueued = queue.enqueue(
        session_id="session-1",
        turn_id="turn-1",
        transcript_path=str(transcript),
        transcript_rows=1,
        signal=True,
        signal_source="test",
        trigger="signal",
        model="test-model",
    )
    owner = "test-owner"
    assert queue.acquire_worker_lease(owner, pid=os.getpid())
    job = queue.claim_next(owner, pid=os.getpid())
    assert job is not None
    assert queue.block(
        job["id"],
        owner,
        code="test_failure",
        message="SECRET_TRANSCRIPT_CONTENT",
    )
    assert queue.release_worker_lease(owner)
    return int(enqueued["job_id"]), transcript


def _candidate_job(tmp_path):
    queue = ReviewQueue(tmp_path / "data" / "review-jobs.sqlite3")
    job_id = queue.enqueue(
        session_id="candidate-session",
        turn_id="candidate-turn",
        transcript_path=str(tmp_path / "private-transcript.jsonl"),
        transcript_rows=1,
        signal=True,
        signal_source="test",
        trigger="signal",
        model="test-model",
    )["job_id"]
    queue.claim_next("candidate-owner", pid=os.getpid())
    queue.complete(
        job_id,
        "candidate-owner",
        {
            "status": "candidate",
            "skills": [],
            "candidates": [
                {
                    "name": "repo-skill",
                    "reason": "repository skill is read-only",
                    "proposed_change": "Add the durable verification step.",
                }
            ],
            "summary": "Review this repo-skill patch.",
        },
    )
    return job_id


def test_review_jobs_and_retry_hide_transcript_data(tmp_path):
    job_id, transcript = _blocked_job(tmp_path)
    listed = _run(tmp_path, "review-jobs", "--status", "blocked", "--limit", "5")
    assert listed.returncode == 0, listed.stderr
    payload = json.loads(listed.stdout)
    assert payload["count"] == 1
    assert payload["jobs"][0]["id"] == job_id
    assert payload["jobs"][0]["status"] == "blocked"
    assert payload["jobs"][0]["trigger"] == "signal"
    assert str(transcript) not in listed.stdout
    assert "transcript_path" not in listed.stdout
    assert "result_path" not in listed.stdout
    assert "last_error" not in listed.stdout
    assert "SECRET_TRANSCRIPT_CONTENT" not in listed.stdout

    retried = _run(tmp_path, "review-retry", str(job_id))
    assert retried.returncode == 0, retried.stderr
    assert json.loads(retried.stdout) == {"job_id": job_id, "retried": True}

    pending = _run(tmp_path, "review-jobs", "--status", "pending")
    assert pending.returncode == 0, pending.stderr
    assert json.loads(pending.stdout)["jobs"][0]["status"] == "pending"


def test_review_retry_rejects_non_failed_job(tmp_path):
    job_id, _transcript = _blocked_job(tmp_path)
    first = _run(tmp_path, "review-retry", str(job_id))
    assert first.returncode == 0
    second = _run(tmp_path, "review-retry", str(job_id))
    assert second.returncode == 2
    assert "not failed/blocked" in json.loads(second.stderr)["error"]


def test_review_worker_once_does_not_fallback_when_codex_is_missing(tmp_path):
    result = _run(tmp_path, "review-worker", "--once")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "processed": 0,
        "reason": "codex_not_found",
        "started": False,
    }


def test_review_jobs_exposes_structured_repo_candidate_without_transcript(tmp_path):
    job_id = _candidate_job(tmp_path)
    listed = _run(tmp_path, "review-jobs", "--status", "done")
    assert listed.returncode == 0, listed.stderr
    job = json.loads(listed.stdout)["jobs"][0]
    assert job["id"] == job_id
    assert job["result_status"] == "candidate"
    assert job["candidate_result"]["candidates"][0] == {
        "name": "repo-skill",
        "reason": "repository skill is read-only",
        "proposed_change": "Add the durable verification step.",
    }
    assert "private-transcript.jsonl" not in listed.stdout
