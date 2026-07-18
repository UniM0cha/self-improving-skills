#!/usr/bin/env python3
"""Per-user team-skills configuration.

The plugin ships with NO default team repo — it is distributed through a
public marketplace, so each user points it at their own (usually private)
team repo. Source of truth: ~/.claude/self-improve/team_config.json

    {
      "repo": "owner/name",        // required — GitHub slug of the team repo
      "subdir": "skills",          // optional — where skills/<name>/ live
      "branch": null               // optional — default branch if null
    }

The SIS_TEAM_SKILLS_REPO environment variable overrides "repo" (test/CI use).
Config (user intent) and manifest (sync state) are deliberately separate files
so recovering from a corrupt manifest never destroys the configuration.
"""

import json
import os
import re
import sys
from typing import NoReturn

CONFIG_PATH = os.path.expanduser("~/.claude/self-improve/team_config.json")
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

GUIDANCE = """팀 스킬 repo가 설정되어 있지 않습니다.

{path} 파일을 만들어 사용할 팀 repo를 지정하세요 (대부분 private repo):

  {{
    "repo": "your-org/your-team-skills",
    "subdir": "skills"
  }}

repo 레이아웃은 <subdir>/<skill-name>/SKILL.md 입니다. private repo는 gh CLI
인증(gh auth login)이 되어 있어야 합니다. 임시로는 환경변수
SIS_TEAM_SKILLS_REPO=owner/name 으로도 지정할 수 있습니다."""


def die(msg, code=1) -> NoReturn:
    sys.stderr.write(str(msg).rstrip() + "\n")
    sys.exit(code)


def load_config():
    """Return {"repo", "subdir", "branch"} or die with setup guidance."""
    cfg = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            cfg = raw
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        die("team_config.json 을 파싱할 수 없습니다 ({0}): {1}".format(CONFIG_PATH, e))

    repo = os.environ.get("SIS_TEAM_SKILLS_REPO", "").strip() or str(cfg.get("repo") or "").strip()
    if not repo:
        die(GUIDANCE.format(path=CONFIG_PATH))
    if not SLUG_RE.match(repo):
        die("팀 repo 형식이 잘못됐습니다: {0!r} — \"owner/name\" 형태여야 합니다.".format(repo))

    subdir = str(cfg.get("subdir") or "skills").strip().strip("/")
    if not subdir or ".." in subdir.split("/") or subdir.startswith("/"):
        die("subdir 가 잘못됐습니다: {0!r}".format(subdir))
    branch = cfg.get("branch")
    branch = str(branch).strip() if branch else None

    return {"repo": repo, "subdir": subdir, "branch": branch}


if __name__ == "__main__":
    print(json.dumps(load_config(), ensure_ascii=False))
