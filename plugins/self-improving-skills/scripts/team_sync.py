#!/usr/bin/env python3
"""Team-skill sync engine — origin-hash manifest semantics (Hermes skills_sync).

Receives team skills from the configured repo into ~/.claude/skills while
guaranteeing personal customization always wins:

  local == origin (untouched)   -> auto-update to team latest
  local != origin (customized)  -> NEVER overwritten (skip + diverged notice)
  locally deleted/archived      -> suppressed; never re-installed (until --reinstall)
  name clash with personal skill-> conflict skip + warning

CLI:
  plan  [--from-clone DIR] [--json]      read-only decision table (mutates nothing)
  apply [--from-clone DIR] [--reinstall NAME]...   execute the plan

`--from-clone DIR` points at a local skills root (DIR/<name>/SKILL.md) and
bypasses gh/network — used by tests. Without it, the team repo is freshly
shallow-cloned via `gh` (private repos need gh auth) on EVERY run, so there is
no persistent clone to be poisoned by force-pushes.

Atomicity: per-skill transaction through a staging dir OUTSIDE ~/.claude/skills
(stage new -> rename old away -> rename new in -> update manifest -> drop old).
A crash between rename-in and manifest write self-heals on the next run:
local == team but != origin  =>  origin_hash is simply refreshed (the same rule
also reconciles a second machine that already has identical content).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import team_config  # noqa: E402
import team_manifest  # noqa: E402
from scan_skill import NAME_RE, scan_dir  # noqa: E402

try:
    import usage_store
except Exception:  # pragma: no cover
    usage_store = None

SKILLS_DIR = os.path.expanduser("~/.claude/skills")
ARCHIVE_DIR = os.path.join(SKILLS_DIR, ".archive")
STATE_DIR = os.path.expanduser("~/.claude/self-improve")
STAGING_DIR = os.path.join(STATE_DIR, "team_staging")
QUARANTINE_DIR = os.path.join(STATE_DIR, "team_quarantine")


def die(msg, code=2) -> NoReturn:
    sys.stderr.write(str(msg).rstrip() + "\n")
    sys.exit(code)


def _run(args, cwd=None):
    try:
        p = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    except FileNotFoundError:
        die("필요한 실행파일을 찾을 수 없습니다: {0}".format(args[0]))
    if p.returncode != 0:
        die("명령 실패: {0}\n{1}".format(" ".join(args), (p.stderr or p.stdout or "").strip()))
    return (p.stdout or "").strip()


def clone_team(cfg):
    """Fresh shallow clone via gh. Returns (skills_root, commit, workdir)."""
    _run(["gh", "auth", "status"])
    # distinguish "repo doesn't exist / typo" from "no access" early
    _run(["gh", "repo", "view", cfg["repo"], "--json", "name"])
    workdir = tempfile.mkdtemp(prefix="sis-sync-")
    dest = os.path.join(workdir, "repo")
    git_args = ["--depth", "1"]
    if cfg["branch"]:
        git_args += ["--branch", cfg["branch"]]
    _run(["gh", "repo", "clone", cfg["repo"], dest, "--"] + git_args)
    commit = _run(["git", "rev-parse", "HEAD"], cwd=dest)
    skills_root = os.path.join(dest, cfg["subdir"])
    return skills_root, commit, workdir


def list_team_skills(skills_root):
    """{name: abs_dir} of valid team skills + a list of skipped-entry notes."""
    skills, notes = {}, []
    if not os.path.isdir(skills_root):
        return skills, notes
    entries = sorted(os.listdir(skills_root))
    folded = {}
    for e in entries:
        folded.setdefault(e.lower(), []).append(e)
    for e in entries:
        d = os.path.join(skills_root, e)
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        if e.startswith(".") or not NAME_RE.match(e):
            notes.append({"name": e, "action": "skipped_invalid_name"})
            continue
        if len(folded[e.lower()]) > 1:
            notes.append({"name": e, "action": "skipped_casefold_duplicate"})
            continue
        if not os.path.isfile(os.path.join(d, "SKILL.md")):
            notes.append({"name": e, "action": "skipped_no_skill_md"})
            continue
        skills[e] = d
    return skills, notes


def _local_dir(name):
    return os.path.join(SKILLS_DIR, name)


def _is_archived_locally(name):
    return os.path.isdir(os.path.join(ARCHIVE_DIR, name))


def compute_plan(team_skills, manifest, reinstall=()):
    """Pure decision pass — the state machine. Returns a list of action dicts.
    Mutates nothing; `apply` executes and records the outcomes."""
    actions = []
    reinstall = set(reinstall)
    skills = manifest.get("skills", {})
    suppressed = manifest.get("suppressed", {})
    pending = manifest.get("pending_share", {})

    names = sorted(set(team_skills) | set(skills) | set(suppressed) | set(pending))
    for name in names:
        team_dir = team_skills.get(name)
        t_hash = team_manifest.dir_hash(team_dir) if team_dir else None
        entry = skills.get(name)
        sup = suppressed.get(name)
        pend = pending.get(name)
        local = _local_dir(name)
        l_hash = team_manifest.dir_hash(local)
        exists = os.path.isdir(local)

        def act(action, **kw):
            a = {"name": name, "action": action}
            a.update(kw)
            actions.append(a)

        # entry+suppressed coexistence is an inconsistent state (e.g. a crash
        # between two mutations) — drop the stale suppression first; the entry
        # is authoritative and normal handling resumes next sync.
        if entry and sup:
            act("gc_stale_suppression")
            continue

        # --- suppressed: stays off unless restored or explicitly reinstalled ---
        if sup and not entry:
            if exists:
                # user restored the dir (e.g. /restore-skill) -> back to managed
                o_hash = sup.get("origin_hash")
                if t_hash is None:
                    act("unsuppress_keep_local")  # team gone meanwhile
                elif l_hash == t_hash:
                    act("unsuppress_heal", origin_hash=t_hash)
                elif o_hash and l_hash == o_hash:
                    act("unsuppress_update", origin_hash=o_hash)
                else:
                    act("unsuppress_diverged", origin_hash=o_hash or "")
            elif name in reinstall:
                if t_hash is None:
                    act("reinstall_unavailable")
                else:
                    act("install", reinstalled=True)
            elif t_hash is None:
                act("gc_suppressed")
            elif sup.get("last_seen_team_hash") != t_hash:
                act("suppressed_team_updated")
            else:
                act("suppressed_noop")
            continue

        # --- managed ---
        if entry:
            o_hash = entry.get("origin_hash")
            if exists:
                # a managed skill the user customized, shared back, and got
                # merged: adopt the team version (otherwise the entry branch
                # would sit in skip_diverged forever despite the merge)
                if pend and t_hash is not None and t_hash != o_hash \
                        and l_hash == pend.get("local_hash_at_share"):
                    act("adopt")
                    continue
                if l_hash == o_hash:
                    if t_hash is None:
                        act("team_deleted_archive")
                    elif t_hash == o_hash:
                        act("noop")
                    else:
                        act("update")
                else:
                    if t_hash is None:
                        act("team_deleted_keep")
                    elif l_hash == t_hash:
                        act("self_heal", origin_hash=t_hash)
                    else:
                        act("skip_diverged")
            else:
                if _is_archived_locally(name):
                    act("suppress_archived")
                elif t_hash is None:
                    act("gc_entry")
                else:
                    act("suppress_deleted")
            continue

        # --- team has it; no manifest entry ---
        if t_hash is not None:
            if exists:
                if l_hash == t_hash:
                    # identical content: corrupt-manifest recovery AND the
                    # adopt-crash window (content swapped, manifest not yet
                    # written) — both converge here; self_heal also clears
                    # any leftover pending_share entry.
                    act("self_heal", origin_hash=t_hash)
                elif pend:
                    if l_hash == pend.get("local_hash_at_share"):
                        act("adopt")
                    else:
                        act("adopt_conflict")
                else:
                    act("conflict_personal")
            else:
                act("install")
            continue

        # --- pending share, team doesn't have it (yet) ---
        if pend:
            act("pending_share_open", pr_url=pend.get("pr_url"))

    return actions


# ---------------------------------------------------------------------------
# apply — executes a plan with per-skill transactions
# ---------------------------------------------------------------------------

def _recover_staging():
    """Crash recovery: restore any '<name>.old' whose live dir went missing,
    then clear the staging area."""
    if not os.path.isdir(STAGING_DIR):
        return
    for e in os.listdir(STAGING_DIR):
        p = os.path.join(STAGING_DIR, e)
        if e.endswith(".old"):
            name = e[:-4]
            live = _local_dir(name)
            if not os.path.exists(live):
                try:
                    shutil.move(p, live)
                    continue
                except Exception:
                    pass
        shutil.rmtree(p, ignore_errors=True)


def _move(src, dst):
    """rename with EXDEV fallback (skills dir may be a cross-device symlink)."""
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(src, dst)


def _install_content(name, team_dir):
    """Stage team content and atomically swap it into ~/.claude/skills/<name>.
    The ignore set mirrors team_manifest's hash exclusions exactly, so the
    post-install local hash equals the team-side hash of the same content.
    If the final swap fails, the previous content is restored in place."""
    os.makedirs(STAGING_DIR, exist_ok=True)
    os.makedirs(SKILLS_DIR, exist_ok=True)
    stage = os.path.join(STAGING_DIR, name)
    old = os.path.join(STAGING_DIR, name + ".old")
    shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    shutil.copytree(team_dir, stage,
                    ignore=shutil.ignore_patterns(".*", "__pycache__", "*.pyc"))
    live = _local_dir(name)
    if os.path.isdir(live):
        _move(live, old)
    try:
        _move(stage, live)
    except BaseException:
        # swap failed mid-flight: put the old content back so a non-crash
        # failure never leaves the skill missing for the rest of the run
        if os.path.isdir(old) and not os.path.exists(live):
            try:
                _move(old, live)
            except Exception:
                pass
        raise
    shutil.rmtree(old, ignore_errors=True)
    return team_manifest.dir_hash(live)


def _blocking_findings(team_dir):
    """Blocking scan findings for incoming team content (empty list = clean)."""
    try:
        return [f for f in scan_dir(team_dir) if f["severity"] == "block"]
    except Exception:
        # scanner failure must fail CLOSED for incoming content
        return [{"file": ".", "id": "scan-error", "severity": "block",
                 "detail": "scanner failed"}]


def _quarantine(name, team_dir, blocking):
    """Park blocked team content in quarantine + record it in the manifest."""
    qdir = os.path.join(QUARANTINE_DIR, name)
    shutil.rmtree(qdir, ignore_errors=True)
    os.makedirs(QUARANTINE_DIR, exist_ok=True)
    shutil.copytree(team_dir, qdir)
    t_hash = team_manifest.dir_hash(team_dir)

    def _q(m):
        m["quarantined"][name] = {
            "at": team_manifest.now_iso(),
            "reasons": [f["id"] for f in blocking],
            "team_hash": t_hash,
        }
    team_manifest.mutate(_q)


def _archive_local(name):
    src = _local_dir(name)
    if not os.path.isdir(src):
        return
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    dst = os.path.join(ARCHIVE_DIR, name)
    if os.path.exists(dst):
        dst = dst + "." + team_manifest.now_iso().replace(":", "")
    shutil.move(src, dst)


def _backup_before_adopt(name):
    """Keep the personal original before replacing it with the team version."""
    bdir = os.path.join(STATE_DIR, "skill_backups",
                        "{0}.pre-adopt.{1}".format(name, team_manifest.now_iso().replace(":", "")))
    try:
        shutil.copytree(_local_dir(name), bdir)
        return bdir
    except Exception:
        return None


def _set_usage(name, **fields):
    if usage_store is None:
        return
    try:
        usage_store.set_fields(name, **fields)
    except Exception:
        pass


def _seed_usage(name):
    if usage_store is None:
        return
    try:
        usage_store.seed_if_missing(name, "team")
    except Exception:
        pass


def apply_plan(actions, team_skills, commit, repo):
    """Execute actions; every state change lands in the manifest immediately
    after the corresponding filesystem change (per-skill transaction)."""
    results = []
    _recover_staging()
    for a in actions:
        name, action = a["name"], a["action"]
        team_dir = team_skills.get(name)
        out = dict(a)
        try:
            # EVERY action that writes team content to disk passes the scan
            # gate — not just first install. A skill that was clean at install
            # time can turn malicious in a later team update.
            if action in ("install", "update", "unsuppress_update", "adopt"):
                blocking = _blocking_findings(team_dir)
                if blocking:
                    _quarantine(name, team_dir, blocking)
                    out["action"] = "quarantined"
                    out["findings"] = blocking
                    results.append(out)
                    continue

            if action in ("install",):
                new_hash = _install_content(name, team_dir)

                def _i(m):
                    m["skills"][name] = {
                        "origin_hash": new_hash,
                        "team_commit": commit,
                        "installed_at": team_manifest.now_iso(),
                        "updated_at": team_manifest.now_iso(),
                    }
                    m["suppressed"].pop(name, None)
                    m["quarantined"].pop(name, None)
                team_manifest.mutate(_i)
                _seed_usage(name)

            elif action in ("update", "unsuppress_update"):
                new_hash = _install_content(name, team_dir)

                def _u(m):
                    e = m["skills"].setdefault(name, {})
                    e["origin_hash"] = new_hash
                    e["team_commit"] = commit
                    e.setdefault("installed_at", team_manifest.now_iso())
                    e["updated_at"] = team_manifest.now_iso()
                    e.pop("diverged_notified", None)
                    m["suppressed"].pop(name, None)
                team_manifest.mutate(_u)
                _seed_usage(name)

            elif action in ("self_heal", "unsuppress_heal"):
                o = a.get("origin_hash")

                def _h(m):
                    e = m["skills"].setdefault(name, {})
                    e["origin_hash"] = o
                    e["team_commit"] = commit
                    e.setdefault("installed_at", team_manifest.now_iso())
                    e["updated_at"] = team_manifest.now_iso()
                    e.pop("diverged_notified", None)
                    m["suppressed"].pop(name, None)
                    m["pending_share"].pop(name, None)
                team_manifest.mutate(_h)
                _seed_usage(name)

            elif action == "adopt":
                backup = _backup_before_adopt(name)
                new_hash = _install_content(name, team_dir)

                def _a(m):
                    m["skills"][name] = {
                        "origin_hash": new_hash,
                        "team_commit": commit,
                        "installed_at": team_manifest.now_iso(),
                        "updated_at": team_manifest.now_iso(),
                    }
                    m["pending_share"].pop(name, None)
                team_manifest.mutate(_a)
                _set_usage(name, created_by="team")
                out["backup"] = backup

            elif action == "team_deleted_archive":
                _archive_local(name)

                def _da(m):
                    m["skills"].pop(name, None)
                team_manifest.mutate(_da)
                _set_usage(name, state="archived")

            elif action == "team_deleted_keep":
                def _dk(m):
                    m["skills"].pop(name, None)
                team_manifest.mutate(_dk)
                _set_usage(name, created_by="user")

            elif action in ("suppress_deleted", "suppress_archived"):
                reason = "deleted" if action == "suppress_deleted" else "archived"
                t_hash = team_manifest.dir_hash(team_dir) if team_dir else None
                entry_hash = None

                def _s(m):
                    e = m["skills"].pop(name, None) or {}
                    m["suppressed"][name] = {
                        "reason": reason,
                        "at": team_manifest.now_iso(),
                        "last_seen_team_hash": t_hash,
                        "origin_hash": e.get("origin_hash"),
                    }
                team_manifest.mutate(_s)
                out["entry_hash"] = entry_hash

            elif action in ("gc_suppressed", "gc_entry"):
                def _g(m):
                    m["suppressed"].pop(name, None)
                    m["skills"].pop(name, None)
                team_manifest.mutate(_g)

            elif action == "gc_stale_suppression":
                def _gs(m):
                    m["suppressed"].pop(name, None)
                team_manifest.mutate(_gs)

            elif action == "suppressed_team_updated":
                t_hash = team_manifest.dir_hash(team_dir) if team_dir else None

                def _su(m):
                    if name in m["suppressed"]:
                        m["suppressed"][name]["last_seen_team_hash"] = t_hash
                team_manifest.mutate(_su)

            elif action == "unsuppress_keep_local":
                def _uk(m):
                    m["suppressed"].pop(name, None)
                team_manifest.mutate(_uk)
                _set_usage(name, state="active")

            elif action == "unsuppress_diverged":
                o = a.get("origin_hash") or ""

                def _ud(m):
                    sup = m["suppressed"].pop(name, None) or {}
                    m["skills"][name] = {
                        "origin_hash": o or sup.get("origin_hash") or "",
                        "team_commit": commit,
                        "installed_at": team_manifest.now_iso(),
                        "updated_at": team_manifest.now_iso(),
                    }
                team_manifest.mutate(_ud)
                _set_usage(name, state="active")
                out["note"] = "복구된 사본이 팀 버전과 다릅니다 — diverged로 관리"

            # noop / skip_diverged / conflict_personal / adopt_conflict /
            # suppressed_noop / pending_share_open / reinstall_unavailable:
            # informational — no mutation.
        except Exception as e:  # per-skill isolation: one failure never cascades
            out["action"] = "failed"
            out["error"] = "{0}: {1}".format(action, e)
        results.append(out)

    def _stamp(m):
        m["repo"] = repo
        m["last_sync_at"] = team_manifest.now_iso()
        m["last_synced_commit"] = commit
    team_manifest.mutate(_stamp)
    return results


def _summarize(actions):
    counts = {}
    for a in actions:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    return counts


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("plan", "apply"):
        die("usage: team_sync.py plan|apply [--from-clone DIR] [--reinstall NAME]...", 1)
    mode = args[0]
    from_clone = None
    reinstall = []
    rest = args[1:]
    i = 0
    while i < len(rest):
        if rest[i] == "--from-clone" and i + 1 < len(rest):
            from_clone = rest[i + 1]
            i += 2
        elif rest[i] == "--reinstall" and i + 1 < len(rest):
            reinstall.append(rest[i + 1])
            i += 2
        else:
            i += 1

    cfg = team_config.load_config()
    workdir = None
    try:
        if from_clone:
            skills_root, commit = from_clone, "(local)"
        else:
            skills_root, commit, workdir = clone_team(cfg)
        team_skills, notes = list_team_skills(skills_root)
        manifest = team_manifest.load()
        actions = compute_plan(team_skills, manifest, reinstall=reinstall)
        if mode == "apply":
            results = apply_plan(actions, team_skills, commit, cfg["repo"])
        else:
            results = actions
        print(json.dumps({
            "mode": mode,
            "repo": cfg["repo"],
            "commit": commit,
            "actions": results + notes,
            "summary": _summarize(results + notes),
        }, ensure_ascii=False, indent=2))
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
