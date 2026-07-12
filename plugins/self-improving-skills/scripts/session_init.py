#!/usr/bin/env python3
"""SessionStart-hook logic for the self-improving-skills plugin.

Injects a small amount of additionalContext at session start so the agent:
  1. knows the self-improvement loop is active and how to feed it (the advisory
     nudge — Hermes' SKILLS_GUIDANCE analogue),
  2. is aware of how many learned skills already exist under ~/.claude/skills, and
  3. is reminded to run /curate-skills when the learned-skill library has grown
     and hasn't been consolidated in a while (the Hermes 7-day curator analogue,
     here event-gated rather than wall-clock).

Output contract: a SessionStart hook adds context by printing
  {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
to stdout. Fails safe to silent (no context) on any error.

Config:
  SIS_CURATE_MIN_SKILLS  learned-skill count above which curation is suggested (default 8)
  SIS_CURATE_INTERVAL_DAYS  days since last curation before re-suggesting (default 7)
"""

import json
import os
import sys
import time
from typing import NoReturn

SKILLS_DIR = os.path.expanduser("~/.claude/skills")
STATE_DIR = os.path.expanduser("~/.claude/self-improve")
CURATOR_STATE = os.path.join(STATE_DIR, "curator_state.json")
PROVENANCE_KEY = "self-improving-skills"  # marker we write into learned SKILL.md frontmatter


def emit_context(text) -> NoReturn:
    if text:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }
        }, ensure_ascii=False))
    sys.exit(0)


def _int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _count_learned_skills():
    """Count learned skills this plugin distilled: DIRECT children of
    ~/.claude/skills with a SKILL.md carrying our provenance marker — the same
    counting rule as curator_transitions._learned_names. (A recursive walk
    would also count SKILL.md copies under support dirs like references/ or
    templates/, inflating the curation trigger — Hermes 9137b86a excludes
    support-dir SKILL.md from discovery for the same reason.)"""
    learned = 0
    if not os.path.isdir(SKILLS_DIR):
        return learned
    try:
        entries = os.listdir(SKILLS_DIR)
    except Exception:
        return learned
    for entry in entries:
        if entry.startswith("."):
            continue
        path = os.path.join(SKILLS_DIR, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                head = fh.read(2048)
            if PROVENANCE_KEY in head:
                learned += 1
        except Exception:
            pass
    return learned


def _read_curator_state():
    try:
        with open(CURATOR_STATE, encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_curator_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = CURATOR_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CURATOR_STATE)
    except Exception:
        pass


def _curation_status(learned_count):
    """'seed' (first ever — defer), 'due' (run now), or 'idle'."""
    state = _read_curator_state()
    if "last_run" not in state:
        return "seed"
    if learned_count < _int_env("SIS_CURATE_MIN_SKILLS", 8):
        return "idle"
    interval = _int_env("SIS_CURATE_INTERVAL_DAYS", 7) * 86400
    try:
        last = float(state.get("last_run", 0))
    except (TypeError, ValueError):
        last = 0.0
    return "due" if (time.time() - last) >= interval else "idle"


def _run_curator(state, lines):
    """Run the deterministic time-based transition pass inline and report it."""
    summary = None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import curator_transitions
        summary = curator_transitions.run(dry_run=False)
    except Exception:
        summary = None
    state["last_run"] = time.time()
    state["run_count"] = int(state.get("run_count", 0)) + 1
    if summary:
        na, ns, nr = len(summary["archived"]), len(summary["stale"]), len(summary["reactivated"])
        state["last_summary"] = {"archived": na, "stale": ns, "reactivated": nr}
        if na or ns or nr:
            lines.append(
                "[큐레이터] 미사용 스킬 자동 정리 실행: stale {0}개, 아카이브 {1}개, 재활성화 {2}개. "
                "아카이브된 스킬은 ~/.claude/skills/.archive/ 로 이동(삭제 아님, /restore-skill 로 복구). "
                "세부 리포트는 ~/.claude/self-improve/logs/curator/. "
                "중복 스킬의 의미 기반 통합이 필요하면 /curate-skills 를 실행하세요.".format(ns, na, nr)
            )
        else:
            lines.append("[큐레이터] 정기 점검 완료 — 정리할 미사용 스킬이 없습니다.")
    _write_curator_state(state)


def main():
    try:
        sys.stdin.read()  # drain payload; we don't need its fields
    except Exception:
        pass

    try:
        learned = _count_learned_skills()
    except Exception:
        emit_context(None)

    lines = [
        "[self-improving-skills] 자기개선 루프 활성. 복잡한 작업·까다로운 디버깅·새 기법 발견을 "
        "끝냈고 재사용 가능하다면, /distill-skill 또는 Task(또는 Agent) 도구로 "
        "subagent_type=\"self-improving-skills:skill-distiller\"(네임스페이스 접두사 생략 시 "
        "호출 실패)를 run_in_background=true 로 호출해 ~/.claude/skills 에 남기세요 — "
        "그냥 종료하면 Stop 훅이 한 번 상기시킵니다.",
        "학습 스킬에서 낡거나 틀린 내용을 발견하면 그 자리에서 해당 SKILL.md 를 patch 하세요. "
        "백그라운드 증류 호출이 \"권한 분류기에 의해 차단됨\"으로 막히면 플러그인 README 의 "
        "'auto mode' 섹션(permissions.allow 5줄)을 참조해 사용자에게 1회 추가를 안내하세요.",
    ]
    if learned:
        lines.append("현재 학습된 스킬 {0}개가 ~/.claude/skills 에 누적되어 있습니다.".format(learned))

    try:
        status = _curation_status(learned)
        if status == "seed":
            # First ever tick: seed the clock and DEFER (never curate on install).
            _write_curator_state({"last_run": time.time(), "run_count": 0})
        elif status == "due":
            _run_curator(_read_curator_state(), lines)
    except Exception:
        pass

    _team_sync_reminder(lines)

    emit_context("\n".join(lines))


def _team_sync_reminder(lines):
    """If team skills are synced on this machine and the last sync is old,
    append a one-line reminder. Reads ONLY the local manifest — a SessionStart
    hook must never touch the network. Throttled via last_reminded_at."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import team_manifest
        m = team_manifest.load()
        if not m.get("skills"):
            return

        def _age_days(iso):
            try:
                from datetime import datetime
                return (time.time() - datetime.fromisoformat(str(iso)).timestamp()) / 86400.0
            except Exception:
                return None

        interval = _int_env("SIS_TEAM_SYNC_REMIND_DAYS", 7)
        # age-check and throttle-stamp under ONE manifest lock, so two
        # concurrent SessionStarts can't both win the throttle race
        fired = []

        def _check_and_mark(mm):
            last_age = _age_days(mm.get("last_sync_at")) if mm.get("last_sync_at") else None
            rem_age = _age_days(mm.get("last_reminded_at")) if mm.get("last_reminded_at") else None
            if (last_age is None or last_age >= interval) and (rem_age is None or rem_age >= 1):
                mm["last_reminded_at"] = team_manifest.now_iso()
                fired.append(last_age)
        team_manifest.mutate(_check_and_mark)
        if fired:
            lines.append(
                "[팀 스킬] 마지막 동기화 후 {0}일 경과 — /sync-team-skills 로 "
                "팀 업데이트를 확인하세요.".format(
                    "{0}+".format(interval) if fired[0] is None else int(fired[0])))
    except Exception:
        pass


# NOTE(v0.10.0): the always-on ~470-char permission-recovery hint that used to
# be injected here every session was replaced by one-clause pointers — in the
# intro lines above and in the Stop-hook nudge (where blocks actually happen).
# The full 5-rule recovery recipe lives in README "auto mode" 섹션. The design
# rationale stands: never predict a block from settings (skip flags, runtime
# modes, enterprise settings make that unreliable) — react to a REAL block.


if __name__ == "__main__":
    main()
