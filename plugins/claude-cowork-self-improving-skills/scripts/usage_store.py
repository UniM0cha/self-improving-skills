#!/usr/bin/env python3
"""Skill usage telemetry store for the claude-cowork-self-improving-skills plugin.

This is the DATA substrate the Hermes loop has and the plugin was missing: a
per-skill sidecar recording how often / when each learned skill is used, viewed,
and patched. The curator (v0.3+) reads it to drive the time-based
active->stale->archived state machine and to rank what's actually unused.

Schema mirrors Hermes tools/skill_usage.py `_empty_record`. One JSON file:

    ~/.claude/self-improve/skill_usage.json
    {
      "_meta": {
        "offsets": { "<session_id>": {"o": <rows processed>, "t": "<iso>"} },
        "nudges":  { "<session_id>": {"r": <rows at last nudge>, "t": "<iso>"} }
      },
      "<skill-name>": {
        "use_count": int, "view_count": int, "patch_count": int,
        "last_used_at": iso|null, "last_viewed_at": iso|null, "last_patched_at": iso|null,
        "created_at": iso, "state": "active|stale|archived",
        "pinned": bool, "created_by": "agent|user", "absorbed_into": str|null
      }, ...
    }

All writes are atomic (tempfile + os.replace in the same dir) under an advisory
flock, so concurrent Stop hooks from parallel sessions don't clobber each other.
Every public function is best-effort and must never raise into a hook.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

try:
    import fcntl  # POSIX (macOS/Linux). Absent on Windows -> we degrade to no-lock.
except Exception:  # pragma: no cover
    fcntl = None

STATE_DIR = os.path.expanduser("~/.claude/self-improve")
STORE_PATH = os.path.join(STATE_DIR, "skill_usage.json")
LOCK_PATH = os.path.join(STATE_DIR, "skill_usage.lock")

KINDS = {
    "use": ("use_count", "last_used_at"),
    "view": ("view_count", "last_viewed_at"),
    "patch": ("patch_count", "last_patched_at"),
}

# _meta session maps (offsets, nudges) would otherwise grow one entry per
# session forever (observed: 106 entries in 8 days). Prune on every write.
META_MAX_ENTRIES = 100
META_MAX_AGE_DAYS = 30


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def empty_record(created_at=None, created_by="agent"):
    return {
        "use_count": 0,
        "view_count": 0,
        "patch_count": 0,
        "last_used_at": None,
        "last_viewed_at": None,
        "last_patched_at": None,
        "created_at": created_at or now_iso(),
        "state": "active",
        "pinned": False,
        "created_by": created_by,
        "absorbed_into": None,
    }


def _ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


class _Lock:
    """Advisory file lock; no-op if fcntl is unavailable."""

    def __init__(self):
        self._fh = None

    def __enter__(self):
        if fcntl is None:
            return self
        try:
            _ensure_dir()
            self._fh = open(LOCK_PATH, "w")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh is not None and fcntl is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
            except Exception:
                pass
        return False


def load():
    try:
        with open(STORE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save(data):
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".skill_usage.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STORE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _records(data):
    """Iterate (name, record) skipping the _meta key."""
    for k, v in data.items():
        if k == "_meta" or not isinstance(v, dict):
            continue
        yield k, v


def _parse_ts(value, fallback):
    """Parse an ISO timestamp defensively; tz-naive becomes UTC."""
    try:
        ts = datetime.fromisoformat(str(value))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return fallback


def _prune_session_map(m, legacy_key="o"):
    """Normalize+prune a _meta per-session map. Values are {"t": iso, ...};
    legacy bare-int values are grandfathered as {legacy_key: v, "t": now}
    (no rescan/double count). Entries older than META_MAX_AGE_DAYS or beyond
    the newest META_MAX_ENTRIES are dropped. Returns the pruned dict."""
    now = datetime.now(timezone.utc)
    norm = {}
    for sid, v in m.items():
        if isinstance(v, dict):
            norm[sid] = v
        elif isinstance(v, int):
            norm[sid] = {legacy_key: v, "t": now_iso()}
    kept = {}
    for sid, v in norm.items():
        # Unparseable "t" -> treat as fresh for the age filter (keep), but as
        # oldest for the size cap below (drop first) — never above real entries.
        if (now - _parse_ts(v.get("t"), now)).days < META_MAX_AGE_DAYS:
            kept[sid] = v
    if len(kept) > META_MAX_ENTRIES:
        oldest_sentinel = datetime.min.replace(tzinfo=timezone.utc)
        newest = sorted(kept.items(),
                        key=lambda kv: _parse_ts(kv[1].get("t"), oldest_sentinel),
                        reverse=True)
        kept = dict(newest[:META_MAX_ENTRIES])
    return kept


def get_offset(session_id):
    v = load().get("_meta", {}).get("offsets", {}).get(session_id, 0)
    if isinstance(v, dict):
        v = v.get("o", 0)
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def get_nudge_row(session_id):
    """Transcript row count at the moment this session was last nudged (0 if
    never). The Stop analyzer counts work only past max(anchor, this) so one
    block per segment of work — a declined nudge is not re-raised every turn."""
    v = load().get("_meta", {}).get("nudges", {}).get(session_id)
    if isinstance(v, dict):
        try:
            return int(v.get("r", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def record_nudge(session_id, row_count):
    """Remember that the Stop hook just nudged this session at `row_count`."""
    if not session_id:
        return
    with _Lock():
        data = load()
        meta = data.setdefault("_meta", {})
        # prune BEFORE inserting so the entry just written always survives
        nudges = _prune_session_map(meta.get("nudges") or {}, legacy_key="r")
        nudges[session_id] = {"r": int(row_count), "t": now_iso()}
        meta["nudges"] = nudges
        _save(data)


def apply_events(events, session_id=None, new_offset=None):
    """Apply a batch of (skill_name, kind, created_by) events atomically.

    `kind` in {use, view, patch}. Seeds a record (with created_at=now) the first
    time a skill is seen. Optionally records the session's processed line offset
    so the next Stop pass only scans new transcript lines (dedupe).
    """
    if not events and new_offset is None:
        return
    with _Lock():
        data = load()
        ts = now_iso()
        for name, kind, created_by in events:
            if not name or kind not in KINDS:
                continue
            rec = data.get(name)
            if not isinstance(rec, dict):
                rec = empty_record(created_at=ts, created_by=created_by or "agent")
                data[name] = rec
            count_key, ts_key = KINDS[kind]
            rec[count_key] = int(rec.get(count_key, 0)) + 1
            rec[ts_key] = ts
            # A stale skill that gets used again reactivates.
            if rec.get("state") == "stale":
                rec["state"] = "active"
        if session_id is not None and new_offset is not None:
            meta = data.setdefault("_meta", {})
            # prune BEFORE inserting so the entry just written always survives
            offsets = _prune_session_map(meta.get("offsets") or {})
            offsets[session_id] = {"o": int(new_offset), "t": now_iso()}
            meta["offsets"] = offsets
        _save(data)


def seed_if_missing(name, created_by="agent"):
    """Ensure a record exists for a freshly-created skill (sets created_at)."""
    if not name:
        return
    with _Lock():
        data = load()
        if name not in data or not isinstance(data.get(name), dict):
            data[name] = empty_record(created_by=created_by)
            _save(data)


def set_fields(name, **fields):
    """Set arbitrary record fields (state, pinned, absorbed_into, created_by...)."""
    if not name:
        return
    with _Lock():
        data = load()
        rec = data.get(name)
        if not isinstance(rec, dict):
            rec = empty_record()
            data[name] = rec
        rec.update(fields)
        _save(data)


def forget_missing(existing_names, grace_hours=24):
    """Drop records for skills whose dir no longer exists — EXCEPT archived ones
    (those live under .archive/ and must keep their record so /restore works).

    A record is not dropped on first sight: it is marked `missing_since` and
    only deleted if STILL missing `grace_hours` later. A skill that is merely
    mid-move (e.g. a parallel session's archive between dir-move and
    state-write) would otherwise lose its accumulated counters."""
    existing = set(existing_names)
    with _Lock():
        data = load()
        changed = False
        now = datetime.now(timezone.utc)
        for name, rec in list(_records(data)):
            if name in existing:
                if rec.pop("missing_since", None) is not None:
                    changed = True  # reappeared — clear the marker
                continue
            if rec.get("state") == "archived":
                continue
            since = _parse_ts(rec.get("missing_since"), None) if rec.get("missing_since") else None
            if since is None:
                rec["missing_since"] = now_iso()
                changed = True
            elif (now - since).total_seconds() >= grace_hours * 3600:
                del data[name]
                changed = True
        if changed:
            _save(data)


def all_records():
    return {n: r for n, r in _records(load())}


if __name__ == "__main__":
    # Tiny CLI: dump | pin <name> | unpin <name>
    args = sys.argv[1:]
    if args and args[0] == "dump":
        print(json.dumps(load(), ensure_ascii=False, indent=2))
    elif len(args) >= 2 and args[0] in ("pin", "unpin"):
        set_fields(args[1], pinned=(args[0] == "pin"))
        print("{0}: pinned={1}".format(args[1], args[0] == "pin"))
    else:
        print("usage: usage_store.py [dump | pin <name> | unpin <name>]")
