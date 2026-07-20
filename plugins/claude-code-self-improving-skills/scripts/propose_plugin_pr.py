#!/usr/bin/env python3
"""propose_plugin_pr.py — thin adapter for the claude-code-self-improving-skills L2 flow.

The actual git/gh machinery lives in propose_pr.py.
This adapter keeps the original CLI contract used by the
/propose-plugin-improvement command:

  prepare <slug>                    isolated clone of THIS plugin's upstream
  submit <dir> <title> <body-file>  whitelist-staged commit + PR

and the original invariants specific to the core-contribution flow:
  * Hard opt-in gate IN CODE: SIS_PLUGIN_PR=1 must be set for BOTH phases.
  * upstream is parsed from plugin.json `repository`.
  * Only the plugin subtree + marketplace manifest may enter a PR.
"""

import json
import os
import re
import sys
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import propose_pr  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_JSON = os.path.normpath(os.path.join(HERE, "..", ".claude-plugin", "plugin.json"))

# The ONLY paths that may enter a core PR. The plugin source subtree plus the
# marketplace manifest (needed for the synchronized version bump).
ADD_PATHS = ["plugins/claude-code-self-improving-skills", ".claude-plugin/marketplace.json"]


def die(msg, code=2) -> NoReturn:
    sys.stderr.write(str(msg).rstrip() + "\n")
    sys.exit(code)


def require_optin() -> None:
    """Hard gate: L2 PR creation is opt-in (default OFF), enforced in CODE — not
    only in the command's markdown."""
    if os.environ.get("SIS_PLUGIN_PR", "").strip() != "1":
        die("L2 자동 PR 은 opt-in 입니다. 환경변수 SIS_PLUGIN_PR=1 을 설정한 뒤 다시 실행하세요.", 1)


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


def main():
    argv = sys.argv[1:]
    if not argv:
        die("usage: propose_plugin_pr.py prepare <slug> | submit <dir> <title> <body-file>", 1)
    sub = argv[0]
    if sub == "prepare":
        require_optin()
        # default slug "core" preserves the original adapter's branch naming
        propose_pr.prepare(upstream_slug(), argv[1] if len(argv) > 1 else "core")
    elif sub == "submit":
        if len(argv) < 4:
            die("usage: propose_plugin_pr.py submit <dir> <title> <body-file>", 1)
        require_optin()
        propose_pr.submit(argv[1], argv[2], argv[3], ADD_PATHS)
    else:
        die("알 수 없는 서브커맨드: {0}".format(sub), 1)


if __name__ == "__main__":
    main()
