#!/usr/bin/env python3
"""propose_plugin_pr.py — git/gh plumbing for the self-improving-skills L2 flow.

Mechanical-only helper used by the /propose-plugin-improvement command. It NEVER
decides WHAT to change — the calling agent edits the cloned source between the
two phases. It only handles the git/gh machinery:

  prepare <slug>            Set up an ISOLATED clone in a temp dir on a fresh
                            branch. If the authenticated user lacks write access
                            to upstream, fork first and clone the fork. Prints a
                            JSON descriptor (also written to <dir>/.sis-pr.json).

  submit <dir> <title> <body-file>
                            Stage ONLY the whitelisted paths, commit, push, and
                            open a PR against upstream's default branch. Prints
                            the PR URL, then removes the temp clone.

Design invariants (mirror the approved plan):
  * Fresh clone in a temp dir — the agent's transcript and local secrets never
    enter the PR. Only ADD_PATHS is ever `git add`ed (no `git add -A`).
  * No auto-merge. We open a PR and stop; a human merges.
  * Write access -> branch on the upstream repo. No access -> fork, then PR.
  * Every gh/git failure exits non-zero with a clear message so the command
    wrapper can surface it to the user.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import NoReturn

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_JSON = os.path.normpath(os.path.join(HERE, "..", ".claude-plugin", "plugin.json"))
META_FILE = ".sis-pr.json"

# The ONLY paths that may enter a PR. The plugin source subtree plus the
# marketplace manifest (needed for the synchronized version bump). Anything
# else the agent might have touched in the clone is never staged.
ADD_PATHS = ["plugins/self-improving-skills", ".claude-plugin/marketplace.json"]


def _whitelisted(path):
    """True if a staged path is exactly a whitelist entry or sits under a
    whitelisted directory. Exact-or-directory-prefix match — a bare startswith
    would also admit a sibling like 'plugins/self-improving-skills-evil/x'."""
    for p in ADD_PATHS:
        pp = p.rstrip("/")
        if path == pp or path.startswith(pp + "/"):
            return True
    return False


def die(msg, code=2) -> NoReturn:
    sys.stderr.write(str(msg).rstrip() + "\n")
    sys.exit(code)


def require_optin() -> None:
    """Hard gate: L2 PR creation is opt-in (default OFF), enforced in CODE — not
    only in the command's markdown. Invoking this helper directly (or not
    following the command exactly) must NOT be able to push/PR without the user
    having explicitly set SIS_PLUGIN_PR=1."""
    if os.environ.get("SIS_PLUGIN_PR", "").strip() != "1":
        die("L2 자동 PR 은 opt-in 입니다. 환경변수 SIS_PLUGIN_PR=1 을 설정한 뒤 다시 실행하세요.", 1)


def run(args, cwd=None):
    """Run a command, capturing output; die with stderr on failure."""
    try:
        p = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    except FileNotFoundError:
        die("필요한 실행파일을 찾을 수 없습니다: {0}".format(args[0]))
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        die("명령 실패: {0}\n{1}".format(" ".join(args), err))
    return (p.stdout or "").strip()


def upstream_slug():
    """owner/repo parsed from plugin.json `repository`."""
    try:
        with open(PLUGIN_JSON, encoding="utf-8") as fh:
            repo = json.load(fh).get("repository", "")
    except Exception as e:  # noqa: BLE001
        die("plugin.json 을 읽을 수 없습니다: {0}".format(e))
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", str(repo))
    if not m:
        die("plugin.json repository 에서 owner/repo 를 파싱하지 못했습니다: {0!r}".format(repo))
    return "{0}/{1}".format(m.group(1), m.group(2))


def _gh_json_field(slug, json_field, jq):
    return run(["gh", "repo", "view", slug, "--json", json_field, "-q", jq])


def clone_with_retry(slug, dest, tries=3, delay=2):
    """Clone a repo, retrying — a freshly created fork can lag a few seconds."""
    last = ""
    for _ in range(tries):
        p = subprocess.run(
            ["gh", "repo", "clone", slug, dest, "--", "--depth", "1"],
            text=True, capture_output=True,
        )
        if p.returncode == 0:
            return
        last = (p.stderr or p.stdout or "").strip()
        time.sleep(delay)
    die("clone 실패 ({0}): {1}".format(slug, last))


def cmd_prepare(slug_arg):
    require_optin()
    up = upstream_slug()
    run(["gh", "auth", "status"])  # dies clearly if gh is not authenticated
    perm = _gh_json_field(up, "viewerPermission", ".viewerPermission")
    base = _gh_json_field(up, "defaultBranchRef", ".defaultBranchRef.name") or "main"

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-z0-9._-]+", "-", (slug_arg or "core").lower()).strip("-") or "core"
    branch = "improve/{0}-{1}".format(safe, ts)

    workdir = tempfile.mkdtemp(prefix="sis-pr-")
    repo_name = up.split("/", 1)[1]
    dest = os.path.join(workdir, repo_name)

    if perm in ("WRITE", "ADMIN", "MAINTAIN"):
        mode = "direct"
        clone_with_retry(up, dest)
        # A direct clone's checked-out default branch IS upstream's default == base.
        run(["git", "checkout", "-b", branch], cwd=dest)
        head = branch
    else:
        mode = "fork"
        # Create the fork (idempotent — no-op if it already exists), then clone
        # the fork explicitly so `origin` is unambiguously the pushable repo.
        run(["gh", "repo", "fork", up, "--clone=false"])
        login = run(["gh", "api", "user", "-q", ".login"])
        fork = "{0}/{1}".format(login, repo_name)
        clone_with_retry(fork, dest)
        # Base the work branch on UPSTREAM's default ref, NOT the fork's
        # (possibly stale or diverged) default — otherwise fork drift would leak
        # unrelated commits into the PR. Push still targets `origin` (the fork).
        run(["git", "remote", "add", "upstream",
             "https://github.com/{0}.git".format(up)], cwd=dest)
        run(["git", "fetch", "--depth", "1", "upstream", base], cwd=dest)
        run(["git", "checkout", "-b", branch, "FETCH_HEAD"], cwd=dest)
        head = "{0}:{1}".format(login, branch)

    meta = {
        "dir": dest, "mode": mode, "branch": branch,
        "upstream": up, "base": base, "head": head,
    }
    try:
        with open(os.path.join(dest, META_FILE), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass  # printed below regardless; meta file is a convenience for submit
    print(json.dumps(meta, ensure_ascii=False))


def cmd_submit(dest, title, body_file):
    require_optin()
    if not dest or not os.path.isdir(dest):
        die("작업 디렉토리가 없습니다: {0}".format(dest))
    try:
        with open(os.path.join(dest, META_FILE), encoding="utf-8") as fh:
            meta = json.load(fh)
    except Exception as e:  # noqa: BLE001
        die("{0} 를 읽을 수 없습니다 (prepare 를 먼저 실행했나요?): {1}".format(META_FILE, e))
    try:
        branch, up, base, head = [meta[k] for k in ("branch", "upstream", "base", "head")]
    except (KeyError, TypeError) as e:  # noqa: BLE001
        die("{0} 메타가 손상됐습니다 (prepare 를 다시 실행하세요): {1}".format(META_FILE, e))

    if not title or not title.strip():
        die("PR 제목이 비어 있습니다.")
    try:
        with open(body_file, encoding="utf-8") as fh:
            body = fh.read()
    except Exception as e:  # noqa: BLE001
        die("body 파일을 읽을 수 없습니다: {0}".format(e))
    if not body.strip():
        die("PR 본문이 비어 있습니다.")

    # Clean the index first so nothing the agent may have pre-staged in the clone
    # can slip in, then stage ONLY the whitelist (privacy: never `git add -A`).
    run(["git", "reset", "-q"], cwd=dest)
    staged_any = False
    for p in ADD_PATHS:
        if os.path.exists(os.path.join(dest, p)):
            run(["git", "add", "--", p], cwd=dest)
            staged_any = True
    if not staged_any:
        die("스테이징할 화이트리스트 경로가 없습니다: {0}".format(", ".join(ADD_PATHS)))

    # Defense in depth: refuse to commit if ANYTHING outside the whitelist got
    # staged (e.g. a scratch/transcript file dropped into the subtree). Match
    # exact path OR a true directory-prefix — a bare startswith would also let a
    # sibling like "plugins/self-improving-skills-evil/x" slip through.
    staged = run(["git", "diff", "--cached", "--name-only"], cwd=dest)
    for line in staged.splitlines():
        path = line.strip()
        if path and not _whitelisted(path):
            die("화이트리스트 밖 경로가 스테이징됐습니다 (PR 중단): {0}".format(path))
    if not staged.strip():
        die("커밋할 변경이 없습니다. clone 에서 소스를 수정했는지 확인하세요.")

    run(["git", "commit", "-m", title], cwd=dest)
    run(["git", "push", "-u", "origin", branch], cwd=dest)

    url = run([
        "gh", "pr", "create", "--repo", up,
        "--base", base, "--head", head,
        "--title", title, "--body", body,
    ], cwd=dest)
    print(url)

    # Success — remove the temp clone, but ONLY if its parent is the mkdtemp dir
    # prepare created (prefix guard), so a mis-invocation with some other
    # checkout can never delete unrelated files / sibling repos.
    parent = os.path.dirname(dest)
    if os.path.basename(parent).startswith("sis-pr-"):
        shutil.rmtree(parent, ignore_errors=True)
    else:
        sys.stderr.write(
            "주의: 예상 밖 작업 디렉토리라 자동 삭제하지 않았습니다: {0}\n".format(parent))


def main():
    argv = sys.argv[1:]
    if not argv:
        die("usage: propose_plugin_pr.py prepare <slug> | submit <dir> <title> <body-file>", 1)
    sub = argv[0]
    if sub == "prepare":
        cmd_prepare(argv[1] if len(argv) > 1 else "")
    elif sub == "submit":
        if len(argv) < 4:
            die("usage: propose_plugin_pr.py submit <dir> <title> <body-file>", 1)
        cmd_submit(argv[1], argv[2], argv[3])
    else:
        die("알 수 없는 서브커맨드: {0}".format(sub), 1)


if __name__ == "__main__":
    main()
