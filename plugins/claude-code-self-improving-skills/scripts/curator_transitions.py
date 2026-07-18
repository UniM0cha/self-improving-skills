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
from datetime import datetime, timedelta, timezone

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
CURATOR_STATE = os.path.join(STATE_DIR, "curator_state.json")

# The exact collision suffix _archive_dir() appends: ".%Y%m%dT%H%M%SZ".
_ARCHIVE_SUFFIX_RE = re.compile(r"^\d{8}T\d{6}Z$")


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


def _frontmatter_provenance(name):
    """True when the CLOSED FRONTMATTER of SKILL.md carries this plugin's
    distilled/provenance marker. Scoped to the frontmatter block — a user
    skill whose body merely MENTIONS the marker (e.g. documentation about
    this plugin) must never lose user protection through it. Unreadable or
    unclosed frontmatter fails closed (returns False → treated as user)."""
    try:
        with open(os.path.join(SKILLS_DIR, name, "SKILL.md"), encoding="utf-8", errors="ignore") as fh:
            head = fh.read(4096)
        m = re.match(r"^---\s*\n(.*?)\n---", head, re.DOTALL)
        if not m:
            return False
        fm = m.group(1)
        # origin: distilled ONLY — the plain provenance stamp is applied by
        # the PostToolUse hook to ANY learned-skill write (including
        # foreground user-authored ones recorded created_by=user), so it must
        # not flip unknown ownership to agent after a usage.json loss
        return bool(re.search(r"^\s*origin\s*:\s*distilled\s*$", fm, re.M))
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
        # bump seconds until free — moving INTO an existing dir would nest
        # instead of replace, and the suffix must keep the exact shape
        # _split_archive_suffix() matches
        t = _now()
        while True:
            cand = dst + "." + t.strftime("%Y%m%dT%H%M%SZ")
            if not os.path.exists(cand):
                dst = cand
                break
            t += timedelta(seconds=1)
    try:
        shutil.move(src, dst)
    except Exception:
        pass


def _split_archive_suffix(name):
    """(bare_name, suffix) if `name` ends with _archive_dir()'s exact collision
    suffix shape (`<name>.<%Y%m%dT%H%M%SZ>`), else (name, None)."""
    base, dot, suffix = str(name).rpartition(".")
    if dot and base and _ARCHIVE_SUFFIX_RE.match(suffix):
        return base, suffix
    return name, None


def restore(name):
    """Move an archived skill back to active. Returns True on success.

    Accepts either the bare skill name or a timestamp-suffixed archive name
    (both restore to the bare name — usage records are always keyed bare).
    When the bare-name archive dir is missing, falls back to the newest
    exact-shape timestamp-suffixed archive of that skill. NEVER loose
    startswith prefix matching — Hermes 992b9223: restoring 'git' must not
    swallow an unrelated 'git-helpers'."""
    raw = str(name)
    name = _split_archive_suffix(raw)[0]  # destination is always the bare name
    dst = os.path.join(SKILLS_DIR, name)
    if not name or os.path.exists(dst):
        return False
    # An explicitly suffixed request restores EXACTLY that archive — with
    # both .archive/<name> and .archive/<name>.<ts> present, normalizing
    # first would silently pick the bare one instead of the asked-for copy.
    # And if the asked-for copy is GONE, fail — never substitute a different
    # version for an explicit ID.
    if raw != name:
        src = os.path.join(ARCHIVE_DIR, raw)
        if not os.path.isdir(src):
            return False
    else:
        src = os.path.join(ARCHIVE_DIR, name)
    if not os.path.isdir(src):
        candidates = []
        try:
            for entry in os.listdir(ARCHIVE_DIR):
                base, sfx = _split_archive_suffix(entry)
                if base == name and sfx and os.path.isdir(os.path.join(ARCHIVE_DIR, entry)):
                    candidates.append(entry)
        except Exception:
            return False
        if not candidates:
            return False
        # suffix is zero-padded UTC — lexicographic max == newest snapshot
        src = os.path.join(ARCHIVE_DIR, sorted(candidates)[-1])
    try:
        shutil.move(src, dst)
        usage_store.set_fields(name, state="active")
        return True
    except Exception:
        return False


def mark_curated():
    """Stamp the curator clock: last_run=now, run_count+=1, EVERY other state
    field preserved (read-modify-write — never a blind overwrite that would
    drop last_summary etc.). Called by /curate-skills at the START of its
    review pass (mirrors Hermes curator.py, which persists state before the
    LLM pass) so an aborted or no-change pass still resets the SessionStart
    re-trigger clock instead of nagging every session. Returns True on write.

    Also takes a pass-start snapshot and records it as last_pass_snapshot:
    a multi-skill curation pass makes one snapshot per archive_one, and the
    keep-N prune would otherwise delete the only snapshot that predates the
    WHOLE pass — /curator-rollback then couldn't undo it. _prune() protects
    the recorded path."""
    import json
    import time
    state = {}
    try:
        with open(CURATOR_STATE, encoding="utf-8") as fh:
            d = json.load(fh)
            if isinstance(d, dict):
                state = d
    except Exception:
        pass
    state["last_run"] = time.time()
    try:
        rc = int(state.get("run_count", 0))
    except (TypeError, ValueError):
        rc = 0
    state["run_count"] = rc + 1
    if curator_backup is not None:
        try:
            snap = curator_backup.make_snapshot()
            if snap:
                state["last_pass_snapshot"] = snap
        except Exception:
            pass
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = CURATOR_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CURATOR_STATE)
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


def archive_one(name, absorbed_into=None, dry_run=False, force=False):
    """Manually archive a single skill (user-initiated). `absorbed_into` records
    the umbrella it was merged into (vs None = plain prune). Returns a result dict.

    Fail-closed guards (Hermes #29912 — a consolidation pass that archived a
    whole cluster while merging nothing; script-level checks, never trust the
    prompt alone):
      - a declared umbrella must exist on disk and must not be the skill itself
      - pinned / user-authored / team-synced skills are refused unless a human
        passes --force (mirrors run()/prune_idle()'s skip rules — this was the
        one entry point without them)
    """
    src = os.path.join(SKILLS_DIR, name)
    if not os.path.isdir(src):
        return {"name": name, "ok": False, "reason": "not found"}
    if absorbed_into is not None:
        if absorbed_into == name:
            return {"name": name, "ok": False,
                    "reason": "absorbed_into is self-referential"}
        if not os.path.isfile(os.path.join(SKILLS_DIR, absorbed_into, "SKILL.md")):
            return {"name": name, "ok": False,
                    "reason": "umbrella not found — create/patch the umbrella "
                              "skill first, then retry the archive"}
    if not force:
        rec = usage_store.all_records().get(name)
        if rec is None:
            # no telemetry record at all: ownership is UNKNOWN — fail closed
            # unless the file itself carries our distilled marker (the same
            # explicit-marker rule the telemetry seeder uses; never infer
            # "agent" from absence of evidence)
            rec = {}
            created_by = "agent" if _frontmatter_provenance(name) else "user"
        else:
            created_by = rec.get("created_by", "agent")
        if rec.get("pinned") or _frontmatter_pinned(name):
            return {"name": name, "ok": False,
                    "reason": "pinned — unpin first (/pin-skill 해제) or pass --force"}
        if created_by in ("team", "user"):
            return {"name": name, "ok": False,
                    "reason": "created_by={0} — not curator-eligible; "
                              "pass --force to override".format(created_by)}
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
        "skipped_pinned": [], "skipped_user": [], "skipped_team": [],
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
            # team-synced skills have an external upstream owner (the team
            # repo) and are NEVER curation-eligible — Hermes hub rule.
            key = "skipped_team" if rec.get("created_by") == "team" else "skipped_user"
            summary[key].append(name)
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
            "- skipped (pinned): {0} | skipped (user-authored): {1} | skipped (team): {2}".format(
                len(summary["skipped_pinned"]), len(summary["skipped_user"]),
                len(summary.get("skipped_team", []))),
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
    elif args and args[0] == "mark-curated":
        print(json.dumps({"marked": mark_curated()}, ensure_ascii=False))
    elif args and args[0] == "archive" and len(args) >= 2:
        positional = [a for a in args[2:] if not a.startswith("--")]
        absorbed = positional[0] if positional else None
        print(json.dumps(archive_one(args[1], absorbed_into=absorbed,
                                     dry_run=("--dry-run" in args),
                                     force=("--force" in args)), ensure_ascii=False))
    elif args and args[0] == "prune" and len(args) >= 2:
        try:
            days = int(args[1])
        except ValueError:
            days = _int_env("SIS_ARCHIVE_AFTER_DAYS", 90)
        # prune is destructive: require --apply to actually mutate, else preview.
        print(json.dumps(prune_idle(days, dry_run=("--apply" not in args)), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(run(dry_run=("--dry-run" in args)), ensure_ascii=False, indent=2))
