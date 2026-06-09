#!/usr/bin/env python3
"""Pre-mutation snapshots of the learned-skill library.

Before the curator moves/archives anything, it tars ~/.claude/skills to
~/.claude/self-improve/curator_backups/<utc>.tar.gz (keeping the newest N), so
any autonomous library mutation has an undo handle. Mirrors Hermes
curator_backup. Best-effort: a backup failure must not block the curator, but
the curator should refuse to mutate if a backup was explicitly requested and
failed (caller decides).
"""

import os
import sys
import tarfile
from datetime import datetime, timezone

SKILLS_DIR = os.path.expanduser("~/.claude/skills")
BACKUP_DIR = os.path.expanduser("~/.claude/self-improve/curator_backups")
KEEP = 5
EXCLUDE_DIRS = {".git", "__pycache__", "node_modules"}


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_snapshot():
    """Create a tar.gz of the skills dir. Returns the path, or None on failure."""
    if not os.path.isdir(SKILLS_DIR):
        return None
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        path = os.path.join(BACKUP_DIR, "{0}.tar.gz".format(_stamp()))

        def _filter(ti):
            parts = ti.name.split("/")
            if any(p in EXCLUDE_DIRS for p in parts):
                return None
            return ti

        with tarfile.open(path, "w:gz") as tar:
            tar.add(SKILLS_DIR, arcname="skills", filter=_filter)
        _prune()
        return path
    except Exception:
        return None


def _prune():
    try:
        snaps = sorted(
            (os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
             if f.endswith(".tar.gz")),
            key=os.path.getmtime,
            reverse=True,
        )
        for old in snaps[KEEP:]:
            try:
                os.unlink(old)
            except Exception:
                pass
    except Exception:
        pass


if __name__ == "__main__":
    p = make_snapshot()
    print(p or "(no snapshot)")
    sys.exit(0 if p else 1)
