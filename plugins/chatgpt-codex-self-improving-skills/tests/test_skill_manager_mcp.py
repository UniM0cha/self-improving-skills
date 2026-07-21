"""MCP contract tests: dynamic serverInfo version + read-before-write guard."""

import json
import os
import shutil
import subprocess
import sys

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
PLUGIN_ROOT = os.path.abspath(os.path.join(SCRIPTS_DIR, ".."))
sys.path.insert(0, SCRIPTS_DIR)

import review_queue
import skill_store
from review_queue import ReviewQueue


def _drive(tmp_path, skills_root, requests, extra_env=None):
    env = dict(os.environ,
               PLUGIN_DATA=str(tmp_path / "data"),
               CODEX_SELF_IMPROVE_SKILL_ROOTS=str(skills_root),
               CODEX_SELF_IMPROVE_CREATE_ROOT=str(skills_root),
               CODEX_SELF_IMPROVE_WRITE_ROOTS=str(skills_root))
    env.pop("CODEX_SELF_IMPROVE_MODE", None)
    env.pop("CODEX_SELF_IMPROVE_AUTO", None)
    if extra_env:
        env.update(extra_env)
    stdin = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "skill_manager_mcp.py")],
        input=stdin, capture_output=True, text=True, env=env, check=False)
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _call(message_id, tool, arguments):
    return {"jsonrpc": "2.0", "id": message_id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}


def _blocked_job(tmp_path, *, error_message="SECRET_TRANSCRIPT_CONTENT"):
    transcript = tmp_path / "private-transcript.jsonl"
    transcript.write_text("private source evidence\n", encoding="utf-8")
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
    assert queue.block(job["id"], owner, code="test_failure", message=error_message)
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


def test_serverinfo_version_matches_plugin_json(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    responses = _drive(tmp_path, skills_root, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    ])
    manifest = json.loads(open(
        os.path.join(PLUGIN_ROOT, ".codex-plugin", "plugin.json"), encoding="utf-8").read())
    assert responses[0]["result"]["serverInfo"]["version"] == manifest["version"]


def test_status_without_plugin_data_uses_installed_cache_store(tmp_path):
    for marketplace in ("samton-plugins", "self-improving-skills"):
        codex_home = tmp_path / marketplace / ".codex"
        installed_root = (
            codex_home / "plugins" / "cache" / marketplace /
            "chatgpt-codex-self-improving-skills" / "0.4.0"
        )
        shutil.copytree(PLUGIN_ROOT, installed_root)
        skills_root = tmp_path / marketplace / "skills"
        skills_root.mkdir()
        env = dict(os.environ,
                   CODEX_SELF_IMPROVE_SKILL_ROOTS=str(skills_root),
                   CODEX_SELF_IMPROVE_CREATE_ROOT=str(skills_root),
                   CODEX_SELF_IMPROVE_WRITE_ROOTS=str(skills_root))
        env.pop("PLUGIN_DATA", None)
        env.pop("PLUGIN_ROOT", None)
        env.pop("CODEX_SELF_IMPROVE_MODE", None)
        env.pop("CODEX_SELF_IMPROVE_AUTO", None)
        config = json.loads(
            (installed_root / ".mcp.json").read_text(encoding="utf-8")
        )["mcpServers"]["self-improving-skills"]
        stdin = "\n".join(json.dumps(r) for r in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            _call(2, "codex_self_improvement_status", {}),
        ]) + "\n"
        proc = subprocess.run(
            [config["command"], *config["args"]],
            cwd=installed_root / config["cwd"],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        responses = [
            json.loads(line) for line in proc.stdout.splitlines() if line.strip()
        ]
        by_id = {r["id"]: r for r in responses}
        status = json.loads(by_id[2]["result"]["content"][0]["text"])
        expected = (
            codex_home / "plugins" / "data" /
            f"chatgpt-codex-self-improving-skills-{marketplace}"
        ).resolve()
        assert status["data_dir"] == str(expected)
        assert status["data_dir_source"] == "codex_plugin_cache"
        assert status["review_mode"] == "background"
        assert status["automatic_review"] is True
        assert status["auto_continue"] is False
        assert status["queue"]["available"] is True
        assert status["worker"]["available"] is True


def test_status_exposes_sanitized_queue_counts_and_last_failure(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _job_id, transcript = _blocked_job(tmp_path)
    responses = _drive(tmp_path, skills_root, [
        _call(1, "codex_self_improvement_status", {}),
    ])
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["queue"]["available"] is True
    assert payload["queue"]["blocked"] == 1
    assert payload["worker"] == {
        "active": False,
        "available": True,
        "expires_at": None,
        "heartbeat_at": None,
        "pid": None,
        "state": "idle",
    }
    assert payload["last_failure"]["error_code"] == "test_failure"
    encoded = json.dumps(payload)
    assert str(transcript) not in encoded
    assert "SECRET_TRANSCRIPT_CONTENT" not in encoded


def test_status_remains_available_when_queue_initialization_fails(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CODEX_SELF_IMPROVE_SKILL_ROOTS", str(skills_root))
    monkeypatch.setenv("CODEX_SELF_IMPROVE_CREATE_ROOT", str(skills_root))
    monkeypatch.setenv("CODEX_SELF_IMPROVE_WRITE_ROOTS", str(skills_root))
    monkeypatch.delenv("CODEX_SELF_IMPROVE_MODE", raising=False)
    monkeypatch.delenv("CODEX_SELF_IMPROVE_AUTO", raising=False)

    class BrokenQueue:
        def __init__(self):
            raise PermissionError("queue is unavailable")

    monkeypatch.setattr(review_queue, "ReviewQueue", BrokenQueue)
    payload = skill_store.status()
    assert payload["review_mode"] == "background"
    assert payload["queue"]["available"] is False
    assert payload["queue"]["error"] == "PermissionError"
    assert payload["worker"] == {
        "active": False,
        "available": False,
        "error": "PermissionError",
        "state": "unavailable",
    }
    assert payload["last_failure"] is None


def test_review_job_tools_list_sanitized_metadata_and_retry(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    job_id, transcript = _blocked_job(tmp_path)
    responses = _drive(tmp_path, skills_root, [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        _call(2, "codex_review_jobs", {"status": "blocked"}),
        _call(3, "codex_review_retry", {"job_id": job_id}),
        _call(4, "codex_review_jobs", {"status": "pending"}),
    ])
    by_id = {response["id"]: response for response in responses}
    tool_names = {tool["name"] for tool in by_id[1]["result"]["tools"]}
    assert {
        "codex_review_jobs",
        "codex_review_retry",
        "codex_review_run_worker",
    } <= tool_names

    blocked = json.loads(by_id[2]["result"]["content"][0]["text"])
    assert blocked["count"] == 1
    assert blocked["jobs"][0]["id"] == job_id
    assert blocked["jobs"][0]["status"] == "blocked"
    assert blocked["jobs"][0]["trigger"] == "signal"
    encoded = json.dumps(blocked)
    assert "transcript_path" not in encoded
    assert "result_path" not in encoded
    assert "last_error" not in encoded
    assert str(transcript) not in encoded
    assert "SECRET_TRANSCRIPT_CONTENT" not in encoded

    retried = json.loads(by_id[3]["result"]["content"][0]["text"])
    assert retried == {"job_id": job_id, "retried": True}
    pending = json.loads(by_id[4]["result"]["content"][0]["text"])
    assert pending["jobs"][0]["status"] == "pending"


def test_review_worker_tool_runs_once_without_codex_fallback(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    responses = _drive(
        tmp_path,
        skills_root,
        [_call(1, "codex_review_run_worker", {})],
        extra_env={"CODEX_SELF_IMPROVE_CODEX_BIN": str(tmp_path / "missing-codex")},
    )
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload == {"processed": 0, "reason": "codex_not_found", "started": False}


def test_review_job_tool_exposes_structured_repo_candidate(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    job_id = _candidate_job(tmp_path)
    responses = _drive(
        tmp_path,
        skills_root,
        [_call(1, "codex_review_jobs", {"status": "done"})],
    )
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    job = payload["jobs"][0]
    assert job["id"] == job_id
    assert job["result_status"] == "candidate"
    assert job["candidate_result"]["candidates"][0]["proposed_change"] == (
        "Add the durable verification step."
    )
    assert "private-transcript.jsonl" not in json.dumps(payload)


def test_patch_requires_view_first(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "guarded"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: guarded\ndescription: d\n---\nbody\n", encoding="utf-8")
    responses = _drive(tmp_path, skills_root, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        _call(2, "codex_skill_patch",
              {"name": "guarded", "old_text": "body", "new_text": "body v2"}),
        _call(3, "codex_skill_view", {"name": "guarded"}),
        _call(4, "codex_skill_patch",
              {"name": "guarded", "old_text": "body", "new_text": "body v2"}),
    ])
    by_id = {r["id"]: r for r in responses}
    blind = by_id[2]["result"]
    assert blind["isError"] is True
    assert "Read before write" in blind["content"][0]["text"]
    assert by_id[3]["result"]["isError"] is False
    assert by_id[4]["result"]["isError"] is False  # unlocked by the view
    assert "body v2" in (skill / "SKILL.md").read_text(encoding="utf-8")


def test_new_support_file_exempt_from_guard(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "fresh"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: fresh\ndescription: d\n---\nbody\n", encoding="utf-8")
    responses = _drive(tmp_path, skills_root, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        _call(2, "codex_skill_write_file",
              {"name": "fresh", "file_path": "references/notes.md", "content": "hello"}),
    ])
    by_id = {r["id"]: r for r in responses}
    assert by_id[2]["result"]["isError"] is False  # creating a NEW file is exempt
