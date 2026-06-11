#!/usr/bin/env python3
"""Static scanner for skills crossing a machine boundary.

Skills are INSTRUCTIONS to an agent, i.e. a prompt-injection vector. Anything
arriving from (or leaving for) the team repo passes this regex-based scan —
the design (and several patterns) follow Hermes' skills_guard/threat_patterns:
quarantine on receive, detect-only report on send.

Modes:
  scan <dir>    install gate (sync receive side). Prints a JSON report.
                exit 0 = clean or warnings only, exit 1 = blocking findings.
  report <dir>  share-side audit (always exit 0; the LLM rewrite step in
                /share-skill consumes this to know what must be removed).

Severities:
  block  — never install (secrets, private keys, destructive commands,
           prompt-injection markers, symlinks, oversize, name mismatch)
  warn   — surfaced but not fatal on receive (machine-local absolute paths);
           on the SHARE side these must be cleaned before the PR.
"""

import json
import os
import re
import sys

MAX_FILE_BYTES = 1 * 1024 * 1024   # 1MB per file
MAX_TOTAL_BYTES = 5 * 1024 * 1024  # 5MB per skill
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# (compiled regex, finding id, severity). Multi-word bypass `(?:\w+\s+)*`
# between key tokens mirrors Hermes threat_patterns.
_PATTERNS = [
    # --- secrets ---
    (r"ghp_[A-Za-z0-9]{36}", "github-pat", "block"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "github-fine-grained-pat", "block"),
    (r"AKIA[0-9A-Z]{16}", "aws-access-key", "block"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private-key", "block"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "slack-token", "block"),
    (r"sk-[A-Za-z0-9_-]{20,}", "api-secret-key", "block"),
    (r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}", "jwt", "block"),
    # --- destructive commands ---
    (r"curl[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh", "curl-pipe-shell", "block"),
    (r"wget[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh", "wget-pipe-shell", "block"),
    (r"rm\s+-rf\s+(?:~|/(?:\s|$|\*))", "rm-rf-root-or-home", "block"),
    (r"sudo\s+rm\s+-rf", "sudo-rm-rf", "block"),
    (r"chmod\s+777\s+/", "chmod-777-root", "block"),
    # --- prompt injection / behavioural hijack ---
    (r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions",
     "prompt-injection", "block"),
    (r"disregard\s+(?:\w+\s+)*(?:your|all|any)\s+(?:\w+\s+)*(?:instructions|rules|guidelines)",
     "disregard-rules", "block"),
    (r"do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user", "deception-hide", "block"),
    (r"system\s+prompt\s+override", "sys-prompt-override", "block"),
    # --- machine-local leakage (warn on receive, must-fix on share) ---
    (r"/Users/[A-Za-z0-9._-]+/", "macos-home-path", "warn"),
    (r"/home/[A-Za-z0-9._-]+/", "linux-home-path", "warn"),
    (r"[A-Z]:\\Users\\[A-Za-z0-9._-]+", "windows-home-path", "warn"),
]
PATTERNS = [(re.compile(rx, re.IGNORECASE), fid, sev) for rx, fid, sev in _PATTERNS]

# NOTE: every regular file is pattern-scanned regardless of extension — an
# extension allowlist would let `.env`-style or extensionless files carry
# secrets/instructions through the gate unscanned.


def _frontmatter_name(skill_md_path):
    try:
        with open(skill_md_path, encoding="utf-8", errors="ignore") as fh:
            head = fh.read(2048)
        m = re.search(r"^name\s*:\s*(.+)$", head, re.MULTILINE)
        if m:
            v = m.group(1).strip()
            if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                v = v[1:-1]
            return v
    except Exception:
        pass
    return None


def scan_dir(skill_dir):
    """Return a list of findings: {file, id, severity, detail}."""
    findings = []
    skill_dir = os.path.abspath(skill_dir)
    base = os.path.basename(skill_dir.rstrip("/"))

    if base.startswith(".") or not NAME_RE.match(base):
        findings.append({"file": ".", "id": "bad-skill-dirname", "severity": "block",
                         "detail": "디렉토리명이 스킬 이름 규칙 위반: {0!r}".format(base)})

    skill_md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_md):
        findings.append({"file": "SKILL.md", "id": "missing-skill-md",
                         "severity": "block", "detail": "SKILL.md 없음"})
    else:
        fm_name = _frontmatter_name(skill_md)
        if fm_name and fm_name != base:
            findings.append({"file": "SKILL.md", "id": "name-mismatch", "severity": "block",
                             "detail": "frontmatter name({0}) != 디렉토리명({1}) — 로딩·텔레메트리 키가 어긋남"
                             .format(fm_name, base)})

    total = 0
    for root, dirs, files in os.walk(skill_dir):
        for d in list(dirs):
            dp = os.path.join(root, d)
            if os.path.islink(dp):
                findings.append({"file": os.path.relpath(dp, skill_dir),
                                 "id": "symlink", "severity": "block",
                                 "detail": "심볼릭 링크는 허용되지 않음"})
                dirs.remove(d)
            elif d.startswith(".") and d not in (".",):
                findings.append({"file": os.path.relpath(dp, skill_dir),
                                 "id": "hidden-dir", "severity": "block",
                                 "detail": "숨김 디렉토리는 공유/설치 대상이 아님"})
                dirs.remove(d)
        for f in files:
            ap = os.path.join(root, f)
            rel = os.path.relpath(ap, skill_dir)
            if os.path.islink(ap):
                findings.append({"file": rel, "id": "symlink", "severity": "block",
                                 "detail": "심볼릭 링크는 허용되지 않음"})
                continue
            if f.startswith("."):
                # dotfiles (.env 등) — 해시·설치에서 제외되는 파일이 PR에 실리거나
                # 게이트를 우회하지 못하게 명시적으로 차단
                findings.append({"file": rel, "id": "hidden-file", "severity": "block",
                                 "detail": "숨김 파일은 공유/설치 대상이 아님"})
                continue
            try:
                size = os.path.getsize(ap)
            except OSError:
                continue
            total += size
            if size > MAX_FILE_BYTES:
                findings.append({"file": rel, "id": "file-too-large", "severity": "block",
                                 "detail": "{0} bytes > {1}".format(size, MAX_FILE_BYTES)})
                continue
            try:
                with open(ap, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except Exception:
                continue
            for rx, fid, sev in PATTERNS:
                m = rx.search(text)
                if m:
                    findings.append({"file": rel, "id": fid, "severity": sev,
                                     "detail": m.group(0)[:80]})
    if total > MAX_TOTAL_BYTES:
        findings.append({"file": ".", "id": "skill-too-large", "severity": "block",
                         "detail": "{0} bytes > {1}".format(total, MAX_TOTAL_BYTES)})
    return findings


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("scan", "report"):
        print("usage: scan_skill.py [scan|report] <skill-dir>")
        sys.exit(2)
    mode, target = args[0], args[1]
    if not os.path.isdir(target):
        print(json.dumps({"ok": False, "error": "디렉토리가 없습니다: {0}".format(target)},
                         ensure_ascii=False))
        sys.exit(2)
    findings = scan_dir(target)
    blocking = [f for f in findings if f["severity"] == "block"]
    report = {
        "dir": os.path.abspath(target),
        "findings": findings,
        "blocking": len(blocking),
        "warnings": len(findings) - len(blocking),
        "ok": not blocking,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if mode == "scan" and blocking:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
