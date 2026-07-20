#!/usr/bin/env python3
"""propose_pr.py — generic, mechanical git/gh PR plumbing.

Parameterized generalization of the original propose_plugin_pr.py:
  - core-plugin contributions  (propose_plugin_pr.py adapter, SIS_PLUGIN_PR gate)

It NEVER decides WHAT to change — the calling agent edits the clone between
the two phases. Invariants (inherited from the original, all preserved):

  * Fresh clone in a temp dir (`sis-pr-` prefix) — transcripts and local
    secrets never enter the PR.
  * Write access -> branch on upstream; otherwise fork automatically.
  * submit stages ONLY the given whitelist (exact path or true directory
    prefix — a sibling like `<path>-evil/x` is rejected), resets the index
    first, and aborts if anything outside the whitelist ends up staged.
  * No auto-merge. PR creation is the last step; a human merges.
  * Success removes the temp clone, but only when its parent dir carries the
    `sis-pr-` prefix (mis-invocation can never delete an unrelated checkout).

CLI:
  prepare <upstream> <slug> [--branch-prefix improve]
  submit  <dir> <title> <body-file> --paths p1,p2,...
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

META_FILE = ".sis-pr.json"


def die(msg, code=2) -> NoReturn:
    sys.stderr.write(str(msg).rstrip() + "\n")
    sys.exit(code)


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


def whitelisted(path, add_paths):
    """Exact-or-directory-prefix match — a bare startswith would also admit a
    sibling like 'plugins/claude-code-self-improving-skills-evil/x'."""
    for p in add_paths:
        pp = p.rstrip("/")
        if path == pp or path.startswith(pp + "/"):
            return True
    return False


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


def prepare(upstream, slug_arg, branch_prefix="improve"):
    """Set up an isolated clone on a fresh branch; print+return the descriptor."""
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", str(upstream or "")):
        die("upstream slug 형식이 잘못됐습니다: {0!r}".format(upstream))
    run(["gh", "auth", "status"])  # dies clearly if gh is not authenticated
    perm = _gh_json_field(upstream, "viewerPermission", ".viewerPermission")
    base = _gh_json_field(upstream, "defaultBranchRef", ".defaultBranchRef.name") or "main"
    if not base or base == "null":
        die("팀 repo에 기본 브랜치가 없습니다(빈 repo?) — 초기 커밋을 먼저 만드세요.")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-z0-9._-]+", "-", (slug_arg or "change").lower()).strip("-") or "change"
    branch = "{0}/{1}-{2}".format(branch_prefix, safe, ts)

    workdir = tempfile.mkdtemp(prefix="sis-pr-")
    repo_name = upstream.split("/", 1)[1]
    dest = os.path.join(workdir, repo_name)

    if perm in ("WRITE", "ADMIN", "MAINTAIN"):
        mode = "direct"
        clone_with_retry(upstream, dest)
        run(["git", "checkout", "-b", branch], cwd=dest)
        head = branch
    else:
        mode = "fork"
        run(["gh", "repo", "fork", upstream, "--clone=false"])
        login = run(["gh", "api", "user", "-q", ".login"])
        fork = "{0}/{1}".format(login, repo_name)
        clone_with_retry(fork, dest)
        # Base the work branch on UPSTREAM's default ref, NOT the fork's
        # (possibly stale) default — fork drift must not leak into the PR.
        run(["git", "remote", "add", "upstream",
             "https://github.com/{0}.git".format(upstream)], cwd=dest)
        run(["git", "fetch", "--depth", "1", "upstream", base], cwd=dest)
        run(["git", "checkout", "-b", branch, "FETCH_HEAD"], cwd=dest)
        head = "{0}:{1}".format(login, branch)

    # Make `git push` work regardless of the user's global git credential
    # setup: route HTTPS auth through gh itself (per-clone config only). A user
    # who authenticated gh but never ran `gh auth setup-git` would otherwise
    # fail at push with "could not read Username for 'https://github.com'".
    # Harmless for SSH-protocol clones (the helper is simply never consulted).
    run(["git", "config", "credential.https://github.com.helper",
         "!gh auth git-credential"], cwd=dest)

    meta = {
        "dir": dest, "mode": mode, "branch": branch,
        "upstream": upstream, "base": base, "head": head,
    }
    try:
        with open(os.path.join(dest, META_FILE), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass  # printed below regardless; meta file is a convenience for submit
    print(json.dumps(meta, ensure_ascii=False))
    return meta


def submit(dest, title, body_file, add_paths):
    """Stage ONLY the whitelist, commit, push, open the PR; print its URL."""
    if not dest or not os.path.isdir(dest):
        die("작업 디렉토리가 없습니다: {0}".format(dest))
    if not add_paths:
        die("화이트리스트(add_paths)가 비어 있습니다.")
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

    # Clean the index first so nothing pre-staged can slip in, then stage ONLY
    # the whitelist (privacy: never `git add -A`).
    run(["git", "reset", "-q"], cwd=dest)
    staged_any = False
    for p in add_paths:
        if os.path.exists(os.path.join(dest, p)):
            run(["git", "add", "--", p], cwd=dest)
            staged_any = True
    if not staged_any:
        die("스테이징할 화이트리스트 경로가 없습니다: {0}".format(", ".join(add_paths)))

    staged = run(["git", "diff", "--cached", "--name-only"], cwd=dest)
    for line in staged.splitlines():
        path = line.strip()
        if path and not whitelisted(path, add_paths):
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

    # Success — remove the temp clone, but ONLY under the mkdtemp prefix guard.
    parent = os.path.dirname(dest)
    if os.path.basename(parent).startswith("sis-pr-"):
        shutil.rmtree(parent, ignore_errors=True)
    else:
        sys.stderr.write(
            "주의: 예상 밖 작업 디렉토리라 자동 삭제하지 않았습니다: {0}\n".format(parent))
    return url


def main():
    argv = sys.argv[1:]
    if not argv:
        die("usage: propose_pr.py prepare <upstream> <slug> [--branch-prefix X] | "
            "submit <dir> <title> <body-file> --paths p1,p2", 1)
    sub = argv[0]
    if sub == "prepare":
        if len(argv) < 3:
            die("usage: propose_pr.py prepare <upstream> <slug> [--branch-prefix X]", 1)
        prefix = "improve"
        if "--branch-prefix" in argv:
            i = argv.index("--branch-prefix")
            if i + 1 < len(argv):
                prefix = argv[i + 1]
        prepare(argv[1], argv[2], branch_prefix=prefix)
    elif sub == "submit":
        if len(argv) < 4 or "--paths" not in argv:
            die("usage: propose_pr.py submit <dir> <title> <body-file> --paths p1,p2", 1)
        i = argv.index("--paths")
        if i + 1 >= len(argv):
            die("--paths 값이 없습니다.", 1)
        paths = [p.strip() for p in argv[i + 1].split(",") if p.strip()]
        submit(argv[1], argv[2], argv[3], paths)
    else:
        die("알 수 없는 서브커맨드: {0}".format(sub), 1)


if __name__ == "__main__":
    main()
