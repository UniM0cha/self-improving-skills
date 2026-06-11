#!/usr/bin/env python3
"""Time-based skill lifecycle state machine — the periodic pruning of unused
skills (Hermes apply_automatic_transitions).

Reads usage telemetry and moves each agent-distilled, unpinned skill through
active -> stale -> archived based on how long since its last activity:

    idle >= SIS_ARCHIVE_AFTER_DAYS (default 90)  -> archived (dir moved to .archive/)
    idle >= SIS_STALE_AFTER_DAYS   (default 30)  -> stale
    was stale but idle < stale cutoff            -> reactivated to active

"idle" = days since the most recent of last_used/viewed/patched, falling back to
created_at (so a brand-new never-used skill ages from first-sight, not epoch).

NEVER touches:
  - skills with created_by != "agent" (user-authored / other plugins)
  - pinned skills (usage record pinned OR `pinned: true` in SKILL.md frontmatter)
  - already-archived skills

Archiving moves the dir to ~/.claude/skills/.archive/<name> (recoverable via
restore()), never deletes. A tar.gz snapshot is taken before the first mutation.
A REPORT.md is written every run. Pure/deterministic — no LLM.
"""

import os
import re
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage_store  # noqa: E402
try:
    import curator_backup
except Exception:
    curator_backup = None

SKILLS_DIR = os.path.expanduser("~/.claude/skills")
ARCHIVE_DIR = os.path.join(SKILLS_DIR, ".archive")
STATE_DIR = os.path.expanduser("~/.claude/self-improve")
LOG_DIR = os.path.join(STATE_DIR, "logs", "curator")


def _int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _now():
    return datetime.now(timezone.utc)


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def _frontmatter_pinned(name):
    try:
        with open(os.path.join(SKILLS_DIR, name, "SKILL.md"), encoding="utf-8", errors="ignore") as fh:
            return bool(re.search(r"^\s*pinned\s*:\s*true", fh.read(2048), re.I | re.M))
    except Exception:
        return False


def _learned_names():
    names = set()
    try:
        for e in os.listdir(SKILLS_DIR):
            if e.startswith("."):
                continue
            if os.path.isfile(os.path.join(SKILLS_DIR, e, "SKILL.md")):
                names.add(e)
    except Exception:
        pass
    return names


def _archive_dir(name):
    src = os.path.join(SKILLS_DIR, name)
    if not os.path.isdir(src):
        return
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    dst = os.path.join(ARCHIVE_DIR, name)
    if os.path.exists(dst):
        dst = dst + "." + _now().strftime("%Y%m%dT%H%M%SZ")
    try:
        shutil.move(src, dst)
    except Exception:
        pass


def restore(name):
    """Move an archived skill back to active. Returns True on success."""
    src = os.path.join(ARCHIVE_DIR, name)
    dst = os.path.join(SKILLS_DIR, name)
    if not os.path.isdir(src) or os.path.exists(dst):
        return False
    try:
        shutil.move(src, dst)
        usage_store.set_fields(name, state="active")
        return True
    except Exception:
        return False


def _idle_days(rec, now):
    """Days since the most recent activity (use/view/patch), else since created_at."""
    latest = None
    for k in ("last_used_at", "last_viewed_at", "last_patched_at"):
        d = _parse(rec.get(k))
        if d and (latest is None or d > latest):
            latest = d
    anchor = latest or _parse(rec.get("created_at")) or now
    return (now - anchor).days


def _use_count(rec):
    """Defensive int read — one malformed counter must not break a whole run."""
    try:
        return int(rec.get("use_count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _archive_days_for(rec, base_days):
    """Proven skills age slower: lifetime use_count >= 3 doubles the archive
    threshold (stale marking is unchanged). Guards rarely-but-decisively used
    skills from a fixed 90-day guillotine — the failure mode Hermes hit when
    its curator archived the load-bearing 'plan' skill (#41817)."""
    if _use_count(rec) >= 3:
        return base_days * 2
    return base_days


def archive_one(name, absorbed_into=None, dry_run=False):
    """Manually archive a single skill (user-initiated). `absorbed_into` records
    the umbrella it was merged into (vs None = plain prune). Returns a result dict."""
    src = os.path.join(SKILLS_DIR, name)
    if not os.path.isdir(src):
        return {"name": name, "ok": False, "reason": "not found"}
    if dry_run:
        return {"name": name, "ok": True, "dry_run": True, "absorbed_into": absorbed_into}
    if curator_backup is not None:
        curator_backup.make_snapshot()
    _archive_dir(name)
    fields = {"state": "archived"}
    if absorbed_into is not None:
        fields["absorbed_into"] = absorbed_into
    usage_store.set_fields(name, **fields)
    return {"name": name, "ok": True, "absorbed_into": absorbed_into}


def prune_idle(days, dry_run=True):
    """Bulk-archive unpinned, agent-distilled skills idle >= `days`. dry_run=True
    (default) only previews candidates — mutate nothing."""
    now = _now()
    records = usage_store.all_records()
    learned = _learned_names()
    candidates = []
    for name in sorted(learned):
        rec = records.get(name, {})
        if rec.get("created_by", "agent") != "agent":
            continue
        if rec.get("pinned") or _frontmatter_pinned(name):
            continue
        if rec.get("state") == "archived":
            continue
        idle = _idle_days(rec, now)
        if idle >= days:
            candidates.append({"name": name, "idle_days": idle,
                               "use_count": _use_count(rec)})
    result = {"days": days, "dry_run": dry_run, "candidates": candidates}
    if not dry_run and candidates:
        if curator_backup is not None:
            curator_backup.make_snapshot()
        for c in candidates:
            _archive_dir(c["name"])
            usage_store.set_fields(c["name"], state="archived")
    return result


def run(dry_run=False):
    stale_days = _int_env("SIS_STALE_AFTER_DAYS", 30)
    archive_days = _int_env("SIS_ARCHIVE_AFTER_DAYS", 90)
    now = _now()
    records = usage_store.all_records()
    learned = _learned_names()
    summary = {
        "stale": [], "archived": [], "reactivated": [],
        "skipped_pinned": [], "skipped_user": [],
        "stale_days": stale_days, "archive_days": archive_days,
        "dry_run": dry_run, "ran_at": now.replace(microsecond=0).isoformat(),
    }
    backed_up = [False]

    def ensure_backup():
        if dry_run or backed_up[0] or curator_backup is None:
            return
        curator_backup.make_snapshot()
        backed_up[0] = True

    for name in sorted(learned):
        rec = records.get(name, {})
        if rec.get("created_by", "agent") != "agent":
            summary["skipped_user"].append(name)
            continue
        if rec.get("pinned") or _frontmatter_pinned(name):
            summary["skipped_pinned"].append(name)
            continue
        if rec.get("state") == "archived":
            continue
        idle = _idle_days(rec, now)
        if idle >= _archive_days_for(rec, archive_days):
            summary["archived"].append({"name": name, "idle_days": idle})
            if not dry_run:
                ensure_backup()
                _archive_dir(name)
                usage_store.set_fields(name, state="archived")
        elif idle >= stale_days:
            if rec.get("state") != "stale":
                summary["stale"].append({"name": name, "idle_days": idle})
                if not dry_run:
                    usage_store.set_fields(name, state="stale")
        else:
            if rec.get("state") == "stale":
                summary["reactivated"].append(name)
                if not dry_run:
                    usage_store.set_fields(name, state="active")

    _write_report(summary)
    return summary


def _write_report(summary):
    try:
        ts = _now().strftime("%Y%m%dT%H%M%SZ")
        d = os.path.join(LOG_DIR, ts)
        os.makedirs(d, exist_ok=True)
        prefix = "[DRY-RUN] " if summary["dry_run"] else ""
        lines = [
            "# {0}Curator transition report".format(prefix),
            "",
            "- ran_at: {0}".format(summary["ran_at"]),
            "- thresholds: stale>={0}d, archive>={1}d".format(summary["stale_days"], summary["archive_days"]),
            "- archived: {0} | stale: {1} | reactivated: {2}".format(
                len(summary["archived"]), len(summary["stale"]), len(summary["reactivated"])),
            "- skipped (pinned): {0} | skipped (user-authored): {1}".format(
                len(summary["skipped_pinned"]), len(summary["skipped_user"])),
            "",
        ]
        if summary["archived"]:
            lines.append("## Archived (moved to .archive/)")
            lines += ["- {0} (idle {1}d)".format(x["name"], x["idle_days"]) for x in summary["archived"]]
            lines.append("")
        if summary["stale"]:
            lines.append("## Marked stale")
            lines += ["- {0} (idle {1}d)".format(x["name"], x["idle_days"]) for x in summary["stale"]]
            lines.append("")
        if summary["reactivated"]:
            lines.append("## Reactivated")
            lines += ["- {0}".format(n) for n in summary["reactivated"]]
            lines.append("")
        with open(os.path.join(d, "REPORT.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:
        pass


if __name__ == "__main__":
    import json
    args = sys.argv[1:]
    if args and args[0] == "restore" and len(args) >= 2:
        print(json.dumps({"restored": args[1], "ok": restore(args[1])}, ensure_ascii=False))
    elif args and args[0] == "archive" and len(args) >= 2:
        absorbed = args[2] if len(args) >= 3 else None
        print(json.dumps(archive_one(args[1], absorbed_into=absorbed, dry_run=("--dry-run" in args)), ensure_ascii=False))
    elif args and args[0] == "prune" and len(args) >= 2:
        try:
            days = int(args[1])
        except ValueError:
            days = _int_env("SIS_ARCHIVE_AFTER_DAYS", 90)
        # prune is destructive: require --apply to actually mutate, else preview.
        print(json.dumps(prune_idle(days, dry_run=("--apply" not in args)), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(run(dry_run=("--dry-run" in args)), ensure_ascii=False, indent=2))
