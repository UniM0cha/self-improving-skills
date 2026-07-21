#!/usr/bin/env python3
"""Codex SessionStart hook: status note + interval-gated curator nudge.

The curator nudge (Hermes maybe_run_curator port): when the last curate pass
is older than the interval AND enough skills are tracked, run a dry-run
curate in-process and inject a one-line candidate summary. First sight seeds
the clock and stays silent — never curate right after install (Hermes defers
the first pass by one full interval). curate() itself stamps last_curate_at,
so the nudge naturally appears at most once per interval.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import skill_store
from skill_store import status


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _curator_note() -> str | None:
    state = skill_store.load_state()
    last = state.get("last_curate_at")
    if not last:
        # locked seed — a bare load→save could clobber a concurrent hook's
        # state writes (transcript offsets, auto-review marker)
        def _seed(st):
            st.setdefault("last_curate_at", skill_store.now_iso())

        skill_store.mutate_state(_seed)
        return None
    parsed = skill_store._parse_time(last)
    if parsed is None:
        return None
    interval_days = _int_env("CODEX_SELF_IMPROVE_CURATE_INTERVAL_DAYS", 7)
    if (datetime.now(timezone.utc) - parsed).days < interval_days:
        return None
    tracked = len(skill_store.load_usage().get("skills", {}))
    if tracked < _int_env("CODEX_SELF_IMPROVE_CURATE_MIN_SKILLS", 8):
        return None
    result = skill_store.curate(dry_run=True)
    candidates = result.get("candidates") or []
    n_archive = sum(1 for r in candidates if r.get("candidate_action") == "archive")
    n_stale = sum(1 for r in candidates if r.get("candidate_action") == "mark_stale")
    if not (n_archive or n_stale):
        return None
    return (
        f"curator: {n_archive} archive / {n_stale} stale candidates — "
        "review with $codex-skill-curator (dry-run report saved to "
        f"{result.get('report_path') or 'logs/curator'})."
    )


def _background_review_note(*, allow_launch: bool = True) -> str | None:
    """Start recoverable queued work and report only actionable states.

    Completed ``changed``/``nothing_to_save`` jobs are deliberately silent.
    This runs at SessionStart so a Stop-hook launch failure, a missing runner,
    or a machine restart never forces a foreground review continuation.
    """
    try:
        from background_review_worker import (
            RUN_DIR_NAME,
            _cleanup_run_files,
            launch_detached,
        )
        from review_queue import ReviewQueue, _pid_matches_identity

        queue = ReviewQueue()
        queue.cleanup()
        run_dir = queue.path.parent / RUN_DIR_NAME
        if run_dir.exists() and not run_dir.is_symlink():
            _cleanup_run_files(run_dir)
        queue_state = queue.status()
        counts = dict(queue_state.get("counts") or {})
        now = time.time()
        worker = queue_state.get("worker")
        worker_live = bool(
            isinstance(worker, dict)
            and _pid_matches_identity(
                worker.get("pid"), worker.get("pid_identity")
            )
        )
        worker_active = bool(
            worker_live
            and float(worker.get("expires_at") or 0) > now
        )
        worker_stale_live = worker_live and not worker_active
        launch = None
        recoverable = int(counts.get("pending") or 0) + int(counts.get("running") or 0)
        if allow_launch and recoverable > 0 and not worker_live:
            launch = launch_detached(queue.path)

        candidate_count = sum(
            1
            for job in queue.list_jobs(status="done", limit=1000)
            if isinstance(job.get("result"), dict)
            and job["result"].get("status") == "candidate"
        )
        long_pending = sum(
            1
            for job in queue.list_jobs(status="pending", limit=1000)
            if now - float(job.get("created_at") or now) >= 24 * 60 * 60
        )
        blocked_jobs = queue.list_jobs(status="blocked", limit=1000)
        authentication_blocked = sum(
            1
            for job in blocked_jobs
            if job.get("error_code") == "authentication_required"
        )
        other_blocked = max(
            0, int(counts.get("blocked") or 0) - authentication_blocked
        )

        alerts = []
        if candidate_count:
            alerts.append(f"{candidate_count} repository-skill candidate(s)")
        if int(counts.get("failed") or 0):
            alerts.append(f"{int(counts['failed'])} failed job(s)")
        if authentication_blocked:
            alerts.append(
                f"{authentication_blocked} job(s) waiting for Codex authentication; "
                "sign in, then retry"
            )
        if other_blocked:
            alerts.append(f"{other_blocked} other blocked job(s)")
        if long_pending:
            alerts.append(f"{long_pending} job(s) queued over 24 hours")
        if worker_stale_live:
            alerts.append("stale worker process is still alive")
        if isinstance(launch, dict) and launch.get("reason") == "codex_not_found":
            alerts.append("Codex CLI unavailable")
        elif isinstance(launch, dict) and launch.get("reason") == "launch_failed":
            alerts.append("worker launch failed")
        if not alerts:
            return None
        return "Background review needs attention: " + ", ".join(alerts) + "."
    except Exception:
        # SessionStart must stay available even if the optional queue store is
        # temporarily inaccessible. The status tool exposes detailed failures.
        return None


def main() -> int:
    try:
        # Seed the bypass-watch baseline BEFORE any tool runs: without it the
        # first mutating tool of a session both makes and blesses its own
        # change (post_tool_use only diffs against an existing baseline).
        import post_tool_use
        post_tool_use.seed_baseline()
    except Exception:
        pass
    try:
        st = status()
        mode = str(st.get("review_mode") or "off")
        if st.get("mode_invalid"):
            mode_note = (
                "Automatic review is off because CODEX_SELF_IMPROVE_MODE is invalid."
            )
        elif mode == "background":
            mode_note = "Background review is on."
        elif mode == "foreground":
            mode_note = "Foreground review continuation is on."
        else:
            mode_note = "Automatic review is off."
        note = (
            "Codex Self Improvement is active. Use $self-improving-skills-review "
            "for durable skill updates and $codex-skill-curator for dry-run skill maintenance. "
            + mode_note
        )
        try:
            curator = _curator_note()
        except Exception:
            curator = None
        if curator:
            note = note + " " + curator
        background = _background_review_note(
            allow_launch=mode == "background" and not st.get("mode_invalid")
        )
        if background:
            note = note + " " + background
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": note,
                    }
                },
                ensure_ascii=False,
            )
        )
    except Exception as exc:
        print(json.dumps({"systemMessage": f"Self-improvement status unavailable: {exc}"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
