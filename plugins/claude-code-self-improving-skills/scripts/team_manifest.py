#!/usr/bin/env python3
"""Team-skill sync manifest + the ONE deterministic directory-hash implementation.

The manifest (~/.claude/self-improve/team_sync.json) is this machine's single
source of truth for which team skills are installed and at which origin hash.
Mirrors the Hermes skills_sync origin-hash design: a skill whose local hash
still equals its recorded origin hash has NOT been customized and is safe to
auto-update; any difference means the user's copy wins and sync must skip it.

Schema (v1):
{
  "version": 1,
  "repo": "owner/name",
  "last_sync_at": iso|null,
  "last_synced_commit": sha|null,
  "last_reminded_at": iso|null,
  "skills":       { name: {origin_hash, team_commit, installed_at, updated_at} },
  "suppressed":   { name: {reason: "deleted"|"archived"|"conflict", at,
                           last_seen_team_hash, origin_hash} },
  "pending_share":{ name: {pr_url, sanitized_hash, local_hash_at_share, at} },
  "quarantined":  { name: {at, reasons, team_hash} }
}

"diverged" is intentionally NOT stored — it is recomputed every sync as
hash(local) != origin_hash, so it can never go stale against user edits.

All writes are atomic (tempfile + os.replace) under an advisory flock, the
same pattern as usage_store. Every function is defensive — a broken manifest
degrades to an empty one (after a .corrupt-<ts> backup) rather than crashing.
"""

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None

STATE_DIR = os.path.expanduser("~/.claude/self-improve")
MANIFEST_PATH = os.path.join(STATE_DIR, "team_sync.json")
LOCK_PATH = os.path.join(STATE_DIR, "team_sync.lock")

# Content noise that must never affect the hash (or get installed).
EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules"}
HASH_EXCLUDE_SUFFIXES = (".pyc",)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


class _Lock:
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

    def __exit__(self, *_exc):
        if self._fh is not None and fcntl is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
            except Exception:
                pass
        return False


def empty_manifest():
    return {
        "version": 1,
        "repo": None,
        "last_sync_at": None,
        "last_synced_commit": None,
        "last_reminded_at": None,
        "skills": {},
        "suppressed": {},
        "pending_share": {},
        "quarantined": {},
    }


def load():
    """Load the manifest. A corrupt file is backed up aside and replaced by an
    empty manifest — the safe direction: every team skill then degrades to the
    'conflict skip' branch (never overwritten), and unmodified ones self-heal
    back to managed on the next sync (local hash == team hash)."""
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("manifest root is not an object")
        base = empty_manifest()
        base.update(data)
        for key in ("skills", "suppressed", "pending_share", "quarantined"):
            if not isinstance(base.get(key), dict):
                base[key] = {}
        return base
    except FileNotFoundError:
        return empty_manifest()
    except Exception:
        try:
            backup = MANIFEST_PATH + ".corrupt-" + \
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            os.replace(MANIFEST_PATH, backup)
            sys.stderr.write("team_sync.json 손상 — {0} 로 백업 후 초기화\n".format(backup))
        except Exception:
            pass
        return empty_manifest()


def save(data):
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".team_sync.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, MANIFEST_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def mutate(fn):
    """Run fn(manifest) under the lock and persist the result."""
    with _Lock():
        data = load()
        fn(data)
        save(data)
        return data


def _hashable_files(skill_dir):
    """Yield (relpath, abspath) of regular files that participate in the hash.
    Symlinks are skipped here (and rejected outright by the scanner)."""
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs
                   if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for f in files:
            if f.startswith(".") or f.endswith(HASH_EXCLUDE_SUFFIXES):
                continue
            ap = os.path.join(root, f)
            if os.path.islink(ap) or not os.path.isfile(ap):
                continue
            rel = os.path.relpath(ap, skill_dir).replace(os.sep, "/")
            yield rel, ap


def dir_hash(skill_dir):
    """Deterministic content-only hash of a skill directory.

    sha256 over sorted (sha256(relpath) + sha256(file_bytes)) — mtime, mode,
    and owner never participate, so the same content hashes identically on
    every machine and across checkouts. Returns None if the directory is
    missing or holds no hashable files."""
    if not os.path.isdir(skill_dir):
        return None
    entries = sorted(_hashable_files(skill_dir), key=lambda t: t[0])
    if not entries:
        return None
    outer = hashlib.sha256()
    for rel, ap in entries:
        outer.update(hashlib.sha256(rel.encode("utf-8")).digest())
        h = hashlib.sha256()
        try:
            with open(ap, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
        except Exception:
            return None
        outer.update(h.digest())
    return outer.hexdigest()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "hash" and len(args) >= 2:
        print(dir_hash(args[1]) or "(no hashable content)")
    elif args and args[0] == "dump":
        print(json.dumps(load(), ensure_ascii=False, indent=2))
    else:
        print("usage: team_manifest.py [hash <dir> | dump]")
