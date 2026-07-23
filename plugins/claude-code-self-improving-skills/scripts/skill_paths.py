#!/usr/bin/env python3
"""Shared path rules for learned skills.

The PreToolUse backup hook and the PostToolUse validator have to agree
byte-for-byte on where a skill's rollback copy lives — if they disagree, the
validator silently finds no backup and a broken edit stays on disk. They used
to carry two copies of that logic; this module is the single definition.

Two different notions of "a skill file" live here on purpose:

  * :func:`is_learned_skill` — any ``*/.claude/skills/**/SKILL.md``. This is
    what the hooks match, so a project-local skill still gets backed up and
    validated.
  * :func:`is_personal_skill` — strictly under ``~/.claude/skills``. This is
    the background worker's write root: an unattended distiller may only ever
    install there, never into a checked-out repository.

Every directory is resolved lazily rather than at import time. Module-level
``expanduser()`` constants go stale when a test swaps HOME after import, and
the resulting bug — writing into the developer's real ``~/.claude`` — is
exactly the one this module exists to prevent.
"""

import hashlib
import os


def user_home():
    """The home directory, honouring HOME on every OS.

    ``os.path.expanduser`` consults USERPROFILE on Windows and ignores HOME
    entirely, so a HOME-only test sandbox would leak onto the real home there.
    Checking HOME first keeps hooks, the worker, and the test suite pointing at
    the same place on all three platforms.
    """
    for name in ("HOME", "USERPROFILE"):
        configured = os.environ.get(name)
        if configured and os.path.isabs(configured):
            return configured
    drive = os.environ.get("HOMEDRIVE") or ""
    tail = os.environ.get("HOMEPATH") or ""
    joined = drive + tail
    if joined and os.path.isabs(joined):
        return joined
    return os.path.expanduser("~")


def state_dir():
    """The plugin's data directory — the single definition for every consumer.

    SIS_STATE_DIR relocates ALL of it (telemetry, backups, queue, run files).
    Honouring it in only one consumer would split a single worker's state
    across two directories.
    """
    configured = os.environ.get("SIS_STATE_DIR")
    if configured:
        # Expanded against user_home(), not os.path.expanduser: the latter
        # ignores HOME on Windows, so a redirected home would land part of the
        # state in the real profile while the rest stayed in the sandbox.
        if configured.startswith("~"):
            configured = user_home() + configured[1:]
        return os.path.abspath(configured)
    return os.path.join(user_home(), ".claude", "self-improve")


def backup_dir():
    return os.path.join(state_dir(), "skill_backups")


def personal_skills_root():
    return os.path.join(user_home(), ".claude", "skills")


def _norm(file_path):
    return str(file_path or "").replace("\\", "/")


def is_learned_skill(file_path):
    """True for a SKILL.md inside any .claude/skills tree."""
    norm = _norm(file_path)
    return "/.claude/skills/" in norm and norm.endswith("SKILL.md")


def is_personal_skill(file_path, root=None):
    """True only for a SKILL.md under ~/.claude/skills (the worker's write root).

    Compares real paths so a symlink pointing out of the tree cannot smuggle a
    write past the check.
    """
    if not is_learned_skill(file_path):
        return False
    try:
        base = os.path.realpath(root or personal_skills_root())
        target = os.path.realpath(str(file_path))
        return os.path.commonpath([base, target]) == base
    except (OSError, ValueError):
        # ValueError: different drives on Windows -> definitively outside.
        return False


def skill_name(file_path):
    """The skill's identity: its directory name (what usage telemetry keys on)."""
    return os.path.basename(os.path.dirname(_norm(file_path)))


def backup_path(file_path, backups=None):
    """Where the pre-edit rollback copy of `file_path` lives.

    Keyed by the full path, not just the directory name: ``~/.claude/skills/foo``
    and ``<project>/.claude/skills/foo`` are different skills that used to share
    one ``foo.bak``, so editing both meant one rollback could restore the
    other's contents. The readable prefix is kept so the directory stays
    browsable.
    """
    norm = _norm(file_path)
    digest = hashlib.sha256(
        os.path.normcase(os.path.abspath(norm)).encode("utf-8", "surrogateescape")
    ).hexdigest()[:12]
    name = skill_name(norm) or "skill"
    return os.path.join(backups or backup_dir(), "{0}-{1}.bak".format(name, digest))
