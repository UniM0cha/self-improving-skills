#!/usr/bin/env python3
"""Inspect and drive the background distillation queue.

    python3 scripts/distill_cli.py status
    python3 scripts/distill_cli.py jobs [--status blocked] [--limit 20]
    python3 scripts/distill_cli.py retry <job-id>|--all-blocked
    python3 scripts/distill_cli.py run --once

`status` is the one to reach for first: it answers "is background distillation
actually working, and if not, what do I have to do about it?" rather than
dumping queue internals.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List

import sis_io
from distill_queue import DistillQueue, default_queue_path
import distill_worker

# Pin UTF-8 before this CLI's Korean status text is printed; see sis_io.
sis_io.pin_utf8_stdio()

# What a human can actually do about each blocked state.
REMEDIES = {
    "authentication_required": (
        "run `claude setup-token`, then put CLAUDE_CODE_OAUTH_TOKEN=<token> in "
        "{env} (chmod 600), then `retry --all-blocked`"
    ),
    "cli_too_old": "upgrade Claude Code (`claude update`), then `retry --all-blocked`",
    "cli_not_found": "set SIS_CLAUDE_BIN to the absolute path of the claude binary",
    "root_refused": "run the worker as your own user, not root",
    "bypass_disabled": (
        "your organization disables bypassPermissions; background distillation "
        "cannot write skills. Set SIS_REVIEW_MODE=foreground to keep the nudge."
    ),
    "symlinked_skills": (
        "a skill under ~/.claude/skills is a symlink; writes through it land "
        "outside the snapshotted tree and could not be rolled back. Replace the "
        "link with a real directory, or keep SIS_REVIEW_MODE=foreground."
    ),
    "unprotected_write": (
        "the guard saw a change it could not guarantee a rollback for — inspect "
        "the paths below before retrying"
    ),
    "budget_exhausted": "raise SIS_DISTILL_MAX_USD, then `retry --all-blocked`",
    "invalid_schema": "plugin bug: the result schema was rejected by the CLI",
}


def _queue(args: argparse.Namespace) -> DistillQueue:
    return DistillQueue(args.queue)


def _print(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_status(args: argparse.Namespace) -> int:
    queue = _queue(args)
    status = queue.status()
    counts = status["counts"]
    env = dict(os.environ)
    claude_bin = distill_worker.discover_claude(env)

    problems: List[Dict[str, str]] = []
    for job in queue.list_jobs(status="blocked", limit=100):
        code = str(job.get("error_code") or "unknown")
        entry = {"code": code, "job": job["id"]}
        remedy = REMEDIES.get(code)
        if remedy:
            entry["fix"] = remedy.format(env=distill_worker.worker_env_file())
        result = job.get("result") or {}
        if result.get("unprotected"):
            entry["unprotected"] = result["unprotected"]
        problems.append(entry)

    _print(
        {
            "mode": (os.environ.get("SIS_REVIEW_MODE") or "background"),
            "queue": str(queue.path),
            "counts": counts,
            "claude_binary": claude_bin or "NOT FOUND",
            "worker_running": queue.worker_alive(),
            "blocked": problems,
            "last_failure": status["last_failure"],
        }
    )
    # Non-zero when something needs a human, so this is usable in a check.
    return 1 if problems or counts.get("failed") else 0


def cmd_jobs(args: argparse.Namespace) -> int:
    jobs = _queue(args).list_jobs(status=args.status, limit=args.limit)
    _print(
        {
            "jobs": [
                {
                    "id": job["id"],
                    "status": job["status"],
                    "trigger": job["trigger"],
                    "attempts": job["attempts"],
                    "error_code": job["error_code"],
                    "cli_version": job.get("cli_version"),
                    "result": job.get("result"),
                }
                for job in jobs
            ]
        }
    )
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    queue = _queue(args)
    if args.all_blocked:
        targets = [job["id"] for job in queue.list_jobs(status="blocked", limit=1000)]
    else:
        targets = [args.job_id]
    retried = [job_id for job_id in targets if queue.retry(int(job_id))]
    if retried:
        distill_worker.launch_detached(queue.path)
    _print({"retried": retried})
    return 0 if retried else 1


def cmd_run(args: argparse.Namespace) -> int:
    result = distill_worker.run_worker(_queue(args), once=args.once)
    _print(result)
    return 0 if result.get("started") else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Background distillation queue")
    parser.add_argument("--queue", default=str(default_queue_path()))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="is background distillation working?").set_defaults(
        func=cmd_status
    )

    jobs = sub.add_parser("jobs", help="list queued jobs")
    jobs.add_argument("--status", choices=["pending", "running", "done", "failed", "blocked"])
    jobs.add_argument("--limit", type=int, default=20)
    jobs.set_defaults(func=cmd_jobs)

    retry = sub.add_parser("retry", help="re-queue a failed or blocked job")
    retry.add_argument("job_id", nargs="?")
    retry.add_argument("--all-blocked", action="store_true")
    retry.set_defaults(func=cmd_retry)

    run = sub.add_parser("run", help="run the worker in the foreground")
    run.add_argument("--once", action="store_true", help="process at most one job")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    if args.command == "retry" and not args.job_id and not args.all_blocked:
        parser.error("retry needs a job id or --all-blocked")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
