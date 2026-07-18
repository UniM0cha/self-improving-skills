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
        note = (
            "Codex Self Improvement is active. Use $self-improving-skills-review "
            "for durable skill updates and $codex-skill-curator for dry-run skill maintenance. "
            f"Auto-continue is {'on' if st.get('auto_continue') else 'off'}."
        )
        try:
            curator = _curator_note()
        except Exception:
            curator = None
        if curator:
            note = note + " " + curator
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
