#!/usr/bin/env python3
"""Skill usage telemetry store for the self-improving-skills plugin.

This is the DATA substrate the Hermes loop has and the plugin was missing: a
per-skill sidecar recording how often / when each learned skill is used, viewed,
and patched. The curator (v0.3+) reads it to drive the time-based
active->stale->archived state machine and to rank what's actually unused.

Schema mirrors Hermes tools/skill_usage.py `_empty_record`. One JSON file:

    ~/.claude/self-improve/skill_usage.json
    {
      "_meta": { "offsets": { "<session_id>": <int lines already processed> } },
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


def get_offset(session_id):
    return int(load().get("_meta", {}).get("offsets", {}).get(session_id, 0))


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
            offsets = meta.setdefault("offsets", {})
            offsets[session_id] = int(new_offset)
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


def forget_missing(existing_names):
    """Drop records for skills whose dir no longer exists — EXCEPT archived ones
    (those live under .archive/ and must keep their record so /restore works)."""
    existing = set(existing_names)
    with _Lock():
        data = load()
        changed = False
        for name, rec in list(_records(data)):
            if name not in existing and rec.get("state") != "archived":
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
