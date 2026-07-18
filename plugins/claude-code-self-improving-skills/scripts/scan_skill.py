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
import unicodedata

MAX_FILE_BYTES = 1 * 1024 * 1024   # 1MB per file
MAX_TOTAL_BYTES = 5 * 1024 * 1024  # 5MB per skill
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Bump when detection meaningfully strengthens: the sync manifest persists
# this per install (scan_provenance), so /sync-team-skills can spot skills
# installed under an older scanner and rescan them.
SCANNER_VERSION = "sis-scan-v2"

# (compiled regex, finding id, severity). The bounded multi-word filler
# `(?:\w+\s+){0,8}` between key tokens mirrors Hermes threat_patterns after
# 060779bb — an UNbounded `*` filler is quadratic on adversarial near-miss
# inputs (ReDoS), and 8 filler words is beyond any natural phrasing.
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
    (r"ignore\s+(?:\w+\s+){0,8}(?:previous|all|above|prior)\s+(?:\w+\s+){0,8}instructions",
     "prompt-injection", "block"),
    (r"disregard\s+(?:\w+\s+){0,8}(?:your|all|any)\s+(?:\w+\s+){0,8}(?:instructions|rules|guidelines)",
     "disregard-rules", "block"),
    (r"do\s+not\s+(?:\w+\s+){0,8}tell\s+(?:\w+\s+){0,8}the\s+user", "deception-hide", "block"),
    (r"system\s+prompt\s+override", "sys-prompt-override", "block"),
    # --- machine-local leakage (warn on receive, must-fix on share) ---
    (r"/Users/[A-Za-z0-9._-]+/", "macos-home-path", "warn"),
    (r"/home/[A-Za-z0-9._-]+/", "linux-home-path", "warn"),
    (r"[A-Z]:\\Users\\[A-Za-z0-9._-]+", "windows-home-path", "warn"),
]
PATTERNS = [(re.compile(rx, re.IGNORECASE), fid, sev) for rx, fid, sev in _PATTERNS]

# Invisible / bidirectional unicode used to hide injected instructions from a
# human reviewer (they render as nothing, the model still reads them). Ported
# 1:1 from Hermes threat_patterns.INVISIBLE_CHARS — directional isolates
# (U+2066-U+2069) and invisible math operators (U+2062-U+2064) included.
INVISIBLE_CHARS = frozenset({
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\u2060",  # word joiner
    "\u2062",  # invisible times
    "\u2063",  # invisible separator
    "\u2064",  # invisible plus
    "\ufeff",  # zero-width no-break space (BOM)
    "\u202a",  # left-to-right embedding
    "\u202b",  # right-to-left embedding
    "\u202c",  # pop directional formatting
    "\u202d",  # left-to-right override
    "\u202e",  # right-to-left override
    "\u2066",  # left-to-right isolate
    "\u2067",  # right-to-left isolate
    "\u2068",  # first strong isolate
    "\u2069",  # pop directional isolate
})

# NOTE: every regular file is pattern-scanned regardless of extension — an
# extension allowlist would let `.env`-style or extensionless files carry
# secrets/instructions through the gate unscanned. No scan-size truncation
# either (a MAX_SCAN_CHARS cutoff would let padding push a payload past the
# scanned window): the 1MB/5MB hard size caps already bound the work.


# Findings for these ids contain live credentials — the matched string must
# never be replayed into reports/logs/MCP transcripts (masking beats leaking
# the very secret the scan exists to catch).
SECRET_IDS = frozenset({
    "github-pat", "github-fine-grained-pat", "aws-access-key", "private-key",
    "slack-token", "api-secret-key", "jwt",
})

# Bounded-filler regexes stop matching past 8 filler words (the ReDoS
# tradeoff). This linear-time proximity sweep has no such horizon: it flags
# an injection keyword followed by a target keyword within a char window.
# BLOCK severity: at the team boundary a false positive costs a recoverable
# quarantine a human reviews, while a miss installs an instruction hijack —
# padding past the exact regexes must not buy a pass.
_PROXIMITY_WINDOW = 240
_PROXIMITY_PAIRS = (
    (("ignore", "disregard"), ("instruction", "guideline", " rules"),
     "prompt-injection-proximity"),
    (("do not tell", "don't tell", "never tell"), ("the user", "the human"),
     "deception-hide-proximity"),
)


def _proximity_findings(normalized):
    hits = []
    lowered = normalized.lower()
    for triggers, targets, fid in _PROXIMITY_PAIRS:
        for trigger in triggers:
            start = 0
            while True:
                i = lowered.find(trigger, start)
                if i == -1:
                    break
                window = lowered[i:i + _PROXIMITY_WINDOW]
                if any(t in window for t in targets):
                    # NO content snippet: the window can contain the very
                    # secret another finding just masked — describe, don't quote
                    hits.append((fid,
                                 f"'{trigger}' near a target keyword within "
                                 f"{_PROXIMITY_WINDOW} chars (content not quoted)"))
                    break  # one finding per (pair, file) is enough
                start = i + len(trigger)
    return hits


def _finding_detail(fid, matched):
    if fid in SECRET_IDS:
        return matched[:6] + "…[masked secret]"
    return matched[:80]


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
            # Invisible unicode on the RAW text FIRST — NFKC strips some of
            # these codepoints, so normalizing before this check loses them
            # (same ordering as Hermes scan_for_threats).
            for ch in sorted(set(text) & INVISIBLE_CHARS):
                findings.append({"file": rel,
                                 "id": "invisible-unicode-U+{0:04X}".format(ord(ch)),
                                 "severity": "block",
                                 "detail": "보이지 않는 유니코드 문자 — 사람 리뷰어에게 숨겨진 "
                                           "지시문 은닉에 쓰이는 문자"})
            # NFKC-fold before pattern matching so full-width/compat variants
            # (ｉｇｎｏｒｅ → ignore) can't slip keyword checks. (Cross-script
            # confusables like Cyrillic 'а' are NOT covered — that needs a
            # TR#39 confusable DB, out of scope here as in Hermes.)
            normalized = unicodedata.normalize("NFKC", text)
            for rx, fid, sev in PATTERNS:
                m = rx.search(normalized)
                if m:
                    findings.append({"file": rel, "id": fid, "severity": sev,
                                     "detail": _finding_detail(fid, m.group(0))})
            for fid, snippet in _proximity_findings(normalized):
                findings.append({"file": rel, "id": fid, "severity": "block",
                                 "detail": snippet})
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
        "scanner_version": SCANNER_VERSION,
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
