#!/usr/bin/env python3
"""Pre-mutation snapshots of the learned-skill library — with an undo handle.

Before the curator moves/archives anything, it tars ~/.claude/skills to
~/.claude/self-improve/curator_backups/<utc>.tar.gz (keeping the newest N), so
any autonomous library mutation can be rolled back. Mirrors Hermes
curator_backup. Best-effort: a backup failure must not block the curator, but
the curator should refuse to mutate if a backup was explicitly requested and
failed (caller decides).

Snapshots also carry the usage/state sidecars under meta/ in the tar
(skill_usage.json, curator_state.json), so a rollback restores the counters
and lifecycle states as they were — a bare skills-tree restore would leave
records stuck at state=archived and the restored skills excluded from the
lifecycle (curator_transitions skips archived records).

`rollback [<stamp>]` restores a snapshot and is itself undoable: the current
tree is snapshotted first, and both that snapshot and the restore source are
protected from the keep-N prune (Hermes fc1119ca — the prune used to delete
the very snapshot being restored).
"""

import os
import shutil
import sys
import tarfile
from datetime import datetime, timezone

SKILLS_DIR = os.path.expanduser("~/.claude/skills")
STATE_DIR = os.path.expanduser("~/.claude/self-improve")
BACKUP_DIR = os.path.join(STATE_DIR, "curator_backups")
KEEP = 5
EXCLUDE_DIRS = {".git", "__pycache__", "node_modules"}
# team_sync.json rides along because the tree includes team-managed skills:
# restoring old content while keeping a NEWER origin_hash would make the next
# sync misread the rollback as a personal edit (skip_diverged) and stop
# auto-updating. Restoring both keeps content and manifest consistent.
META_FILES = ("skill_usage.json", "curator_state.json", "team_sync.json")
META_RESTORE = ("skill_usage.json", "team_sync.json")  # curator_state: never


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_snapshot(protect=()):
    """Create a tar.gz of the skills dir (+ meta sidecars). Returns the path,
    or None on failure. `protect` paths are never deleted by the keep-N prune."""
    if not os.path.isdir(SKILLS_DIR):
        return None
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        # The stamp has second resolution — two snapshots in the same second
        # (e.g. rollback's undo snapshot right after the one being restored)
        # must NEVER share a path, or the later write silently destroys the
        # earlier snapshot's content.
        base = _stamp()
        path = os.path.join(BACKUP_DIR, "{0}.tar.gz".format(base))
        n = 1
        while os.path.exists(path):
            path = os.path.join(BACKUP_DIR, "{0}-{1}.tar.gz".format(base, n))
            n += 1

        def _filter(ti):
            parts = ti.name.split("/")
            if any(p in EXCLUDE_DIRS for p in parts):
                return None
            # Symlinks are excluded at CREATION time: rollback (rightly)
            # refuses link members, so a snapshot containing one would be
            # permanently unrestorable — and the team scanner already treats
            # symlinks in skill packages as a blocking finding anyway.
            if ti.issym() or ti.islnk():
                return None
            return ti

        with tarfile.open(path, "w:gz") as tar:
            # realpath the ROOT: if ~/.claude/skills is itself a symlink,
            # tar.add would emit a single link member for "skills" which the
            # filter then drops — yielding a meta-only tar that "succeeds"
            # but can never be restored. Resolving the root keeps the tree;
            # only links INSIDE packages are excluded.
            tar.add(os.path.realpath(SKILLS_DIR), arcname="skills", filter=_filter)
            captured = []
            for fn in META_FILES:
                fp = os.path.join(STATE_DIR, fn)
                if os.path.isfile(fp):
                    tar.add(fp, arcname="meta/" + fn)
                    captured.append(fn)
            # meta/CONTENTS.json separates "file absent at snapshot time"
            # (restore should REMOVE the live one) from a legacy snapshot
            # that simply never captured meta (restore must not guess).
            import io
            import json as _json
            contents = _json.dumps({"meta_files": captured}).encode("utf-8")
            info = tarfile.TarInfo("meta/CONTENTS.json")
            info.size = len(contents)
            tar.addfile(info, io.BytesIO(contents))
        _prune(protect=tuple(protect) + (path,))
        return path
    except Exception:
        return None


def _prune(protect=()):
    protected = {os.path.abspath(p) for p in protect if p}
    # The pass-start snapshot recorded by curator_transitions.mark_curated()
    # is protected for as long as it is referenced: a multi-skill curation
    # pass snapshots per archive, and keep-N would otherwise delete the only
    # snapshot predating the whole pass.
    try:
        import json
        with open(os.path.join(STATE_DIR, "curator_state.json"), encoding="utf-8") as fh:
            lps = json.load(fh).get("last_pass_snapshot")
        if lps:
            protected.add(os.path.abspath(lps))
    except Exception:
        pass
    try:
        snaps = sorted(
            (os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
             if f.endswith(".tar.gz")),
            key=os.path.getmtime,
            reverse=True,
        )
        for old in snaps[KEEP:]:
            if os.path.abspath(old) in protected:
                continue
            try:
                os.unlink(old)
            except Exception:
                pass
    except Exception:
        pass


def list_snapshots():
    """Snapshot filenames, oldest first. Ordered by mtime (sub-second), not
    name — same-second collision suffixes ("-1") would sort wrong by name."""
    try:
        files = [f for f in os.listdir(BACKUP_DIR) if f.endswith(".tar.gz")]
        return sorted(files,
                      key=lambda f: (os.path.getmtime(os.path.join(BACKUP_DIR, f)), f))
    except Exception:
        return []


def _atomic_replace_file(src_file, dest_file):
    """Copy src over dest via tmp + os.replace so concurrent READERS of the
    sidecar JSONs never see a half-written file. (Concurrent WRITERS remain
    last-writer-wins — fully serializing a whole-library rollback against
    every hook's lock would be deadlock-prone for marginal benefit; the
    rollback command already tells the human to run it quiesced.)"""
    dest_dir = os.path.dirname(dest_file)
    os.makedirs(dest_dir, exist_ok=True)
    tmp = os.path.join(dest_dir, ".{0}.rollback-tmp".format(os.path.basename(dest_file)))
    shutil.copy2(src_file, tmp)
    os.replace(tmp, dest_file)


def _touch_curator_state_last_run():
    """Stamp curator_state.json last_run=now, preserving every other field.

    curator_state.json is captured inside snapshots but deliberately NOT
    restored on rollback: unlike Hermes, the state file lives OUTSIDE the
    skills tree, and restoring a snapshot-era last_run would make the
    SessionStart curator re-fire sooner — the opposite of what a user who
    just rolled back wants."""
    import json
    import time
    path = os.path.join(STATE_DIR, "curator_state.json")
    state = {}
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
            if isinstance(d, dict):
                state = d
    except Exception:
        pass
    state["last_run"] = time.time()
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def rollback(stamp=None):
    """Replace the skills tree (and usage sidecar) with a snapshot's content.

    stamp: a snapshot name like 20260713T090000Z (with or without .tar.gz);
    None = newest. Returns a result dict; on success it names the undo
    snapshot taken from the pre-rollback tree."""
    snaps = list_snapshots()
    if not snaps:
        return {"ok": False, "reason": "no snapshots"}
    if stamp:
        fname = stamp if stamp.endswith(".tar.gz") else stamp + ".tar.gz"
        if fname not in snaps:
            return {"ok": False, "reason": "snapshot not found: {0}".format(fname),
                    "available": snaps}
    else:
        fname = snaps[-1]
    src = os.path.join(BACKUP_DIR, fname)

    # Undo handle FIRST: snapshot the current tree; protect both tarballs
    # from the prune. Refuse to proceed if the undo snapshot failed — UNLESS
    # there is no tree at all: full-tree loss is a core recovery scenario,
    # and with nothing to lose the missing undo snapshot is not a blocker.
    pre = make_snapshot(protect=(src,))
    if pre is None and os.path.isdir(SKILLS_DIR):
        return {"ok": False,
                "reason": "could not snapshot current tree — refusing to roll back"}

    staging = os.path.join(BACKUP_DIR, ".restore-staging")
    try:
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        with tarfile.open(src, "r:gz") as tar:
            members = []
            for m in tar.getmembers():
                name = m.name.replace("\\", "/")
                parts = name.split("/")
                if os.path.isabs(m.name) or ".." in parts:
                    raise ValueError("unsafe member path: " + m.name)
                if m.issym() or m.islnk():
                    raise ValueError("link member refused: " + m.name)
                if parts[0] not in ("skills", "meta"):
                    continue
                members.append(m)
            try:
                tar.extractall(staging, members=members, filter="data")
            except TypeError:  # Python < 3.12: no filter= (members pre-vetted above)
                tar.extractall(staging, members=members)
        staged_skills = os.path.join(staging, "skills")
        if not os.path.isdir(staged_skills):
            raise ValueError("snapshot has no skills/ tree")

        # Preserve the CURRENT sidecars FIRST — before anything live moves —
        # so a partial failure can revert everything (tree AND meta), never
        # leaving them at different points in time.
        meta_prev = os.path.join(staging, ".meta-prev")
        os.makedirs(meta_prev, exist_ok=True)
        prev_meta_existed = {}
        for meta_name in META_RESTORE:
            live_meta = os.path.join(STATE_DIR, meta_name)
            prev_meta_existed[meta_name] = os.path.isfile(live_meta)
            if prev_meta_existed[meta_name]:
                shutil.copy2(live_meta, os.path.join(meta_prev, meta_name))
        # Swap the REAL directory, never the symlink: if ~/.claude/skills is
        # a symlink elsewhere, moving SKILLS_DIR would relocate the link and
        # then plant a real dir at the link's path — two diverging trees.
        # Operating on realpath keeps the link intact and pointing at the
        # restored content.
        real_skills = os.path.realpath(SKILLS_DIR)
        aside = real_skills + ".rollback-aside"
        shutil.rmtree(aside, ignore_errors=True)
        # From the FIRST live mutation (the aside move) onward, everything is
        # inside one recovery guard — a failure at any point moves the
        # original tree back instead of stranding it in .rollback-aside.
        moved_aside = False
        swapped = False
        meta_touched = []
        try:
            if os.path.isdir(real_skills):
                shutil.move(real_skills, aside)
                moved_aside = True
            shutil.move(staged_skills, real_skills)
            swapped = True
            # meta restore happens INSIDE the aside-protected window: a
            # failure here reverts the tree swap below, keeping the "failure
            # leaves the library unchanged" contract.
            usage_meta_restored = False
            contents_path = os.path.join(staging, "meta", "CONTENTS.json")
            new_format = os.path.isfile(contents_path)
            for meta_name in META_RESTORE:
                staged_meta = os.path.join(staging, "meta", meta_name)
                live_meta = os.path.join(STATE_DIR, meta_name)
                if os.path.isfile(staged_meta):
                    os.makedirs(STATE_DIR, exist_ok=True)
                    meta_touched.append(meta_name)
                    _atomic_replace_file(staged_meta, live_meta)
                    if meta_name == "skill_usage.json":
                        usage_meta_restored = True
                elif new_format:
                    # new-format snapshot records absence explicitly: the file
                    # did not exist at snapshot time, so a faithful restore
                    # removes the newer one (else e.g. team_sync.json would
                    # point at hashes the restored tree no longer matches).
                    # Absent-then AND absent-now is also a faithful restore.
                    if os.path.isfile(live_meta):
                        meta_touched.append(meta_name)
                        os.unlink(live_meta)
                    if meta_name == "skill_usage.json":
                        usage_meta_restored = True
        except Exception:
            for meta_name in meta_touched:  # sidecars back to pre-rollback
                live_meta = os.path.join(STATE_DIR, meta_name)
                try:
                    if prev_meta_existed.get(meta_name):
                        shutil.copy2(os.path.join(meta_prev, meta_name), live_meta)
                    elif os.path.isfile(live_meta):
                        os.unlink(live_meta)
                except Exception:
                    pass
            if swapped:
                shutil.rmtree(real_skills, ignore_errors=True)
            elif moved_aside and os.path.isdir(real_skills):
                # a cross-device move (skills root on another volume) is
                # copy+delete — a mid-copy failure leaves a PARTIAL tree at
                # the live path with swapped still False; clear it so the
                # aside restore below can put the original back
                shutil.rmtree(real_skills, ignore_errors=True)
            if moved_aside and not os.path.isdir(real_skills) and os.path.isdir(aside):
                shutil.move(aside, real_skills)  # put the original back
            raise
        shutil.rmtree(aside, ignore_errors=True)

        _touch_curator_state_last_run()
        result = {"ok": True, "restored_from": fname,
                  "undo_snapshot": os.path.basename(pre) if pre else None,
                  "usage_meta_restored": usage_meta_restored}
        if not usage_meta_restored:
            # pre-v0.10.0 snapshot without meta/ — the tree is rolled back but
            # usage records may disagree with it (e.g. state stuck archived)
            result["warning"] = (
                "스냅샷에 meta/skill_usage.json 이 없어(구버전 스냅샷) usage 레코드는 "
                "복원되지 않았습니다 — /curator-status 로 state 불일치를 점검하세요.")
        return result
    except Exception as e:
        return {"ok": False, "reason": str(e),
                "undo_snapshot": os.path.basename(pre) if pre else None}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "rollback":
        import json
        res = rollback(args[1] if len(args) >= 2 else None)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(0 if res.get("ok") else 1)
    if args and args[0] == "list":
        for s in list_snapshots():
            print(s)
        sys.exit(0)
    p = make_snapshot()
    print(p or "(no snapshot)")
    sys.exit(0 if p else 1)
