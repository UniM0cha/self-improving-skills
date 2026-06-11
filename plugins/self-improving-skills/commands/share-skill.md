---
description: 학습 스킬 하나를 sanitize·일반화해 팀 스킬 repo에 PR로 공유한다
---

`$ARGUMENTS` 첫 단어의 학습 스킬을 팀 스킬 repo에 PR로 공유하세요. 스킬 이름이 없으면 `~/.claude/skills/` 목록을 보여주고 무엇을 공유할지 물어보세요.

이것은 자기개선 루프의 **지식 출판 단계**입니다 — 개인 학습 스킬을 팀 표준으로 제안합니다. 머지는 사람이 결정하고, 머지 후 `/sync-team-skills`가 각자에게 배포합니다.

## 절차

### 1. 팀 repo 설정 확인 (먼저 반드시)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team_config.py
```

미설정이면 출력되는 안내(설정 파일 경로 + 예시 JSON)를 사용자에게 그대로 전하고 **중단**하세요. 설정 파일을 만드는 것 자체가 공유 기능 사용 동의입니다 — 별도 opt-in 환경변수는 없습니다.

### 2. sanitize 리포트

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/scan_skill.py report ~/.claude/skills/<name>
```

findings를 확인하세요. **시크릿·위험 명령·인젝션 패턴(block)과 로컬 경로(warn)는 전부 제거 대상**입니다.

### 3. 격리 clone 준비 + 일반화 적용

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/propose_pr.py prepare <owner/team-repo> <skill-name> --branch-prefix share
```

출력 JSON의 `dir` 안에 `<subdir>/<skill-name>/` (보통 `skills/<skill-name>/`)로 스킬 디렉토리를 **복사한 뒤, clone 쪽 사본을** 다음 기준으로 편집하세요 (개인 원본은 건드리지 않습니다):

- scan report의 모든 finding 제거 (로컬 경로는 일반화: `/Users/me/proj` → `<project-root>` 등)
- **개인 취향·개인 환경 의존 내용 제거** — 팀 스킬은 "기법(어떻게 푸는가)"만 담고, "취향(어떤 스타일을 원하는가)"은 개인 스킬에 남깁니다
- description을 3인칭 + 구체적 트리거 문구로 정비 (≤500자)
- frontmatter `metadata`를 팀 provenance로 교체:
  ```yaml
  metadata:
    origin: team
    contributed_by: <gh login>
  ```
  (개인용 `provenance: self-improving-skills` 마커는 제거 — 팀 스킬은 개인 큐레이터 관리 대상이 아닙니다)

편집 후 clone 쪽 사본을 다시 스캔해 깨끗한지 확인하세요:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/scan_skill.py scan <dir>/<subdir>/<skill-name>
```

### 4. 사용자에게 diff 확인

원본과 일반화된 사본의 차이를 보여주고 (`diff -ru ~/.claude/skills/<name> <dir>/<subdir>/<name>`), **사용자 확인을 받은 뒤에만** PR을 만드세요.

### 5. PR 생성 + pending 기록

PR 본문(변경 요약·근거만, transcript 금지)을 clone **밖** 임시 파일에 쓰고:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/propose_pr.py submit "<dir>" "feat(skills): <skill-name> 공유" "<body 파일>" --paths "<subdir>/<skill-name>"
```

PR URL이 나오면 매니페스트에 pending_share를 기록하세요 (머지 후 sync가 개인 원본을 팀 관리본으로 전환하는 데 씁니다):

```bash
python3 - <<'EOF'
import sys, os
sys.path.insert(0, os.path.expanduser("<CLAUDE_PLUGIN_ROOT>/scripts"))
import team_manifest
def f(m):
    m["pending_share"]["<skill-name>"] = {
        "pr_url": "<PR URL>",
        "sanitized_hash": team_manifest.dir_hash("<clone의 스킬 경로 — submit 전 기록해둔 값이면 생략 가능>") ,
        "local_hash_at_share": team_manifest.dir_hash(os.path.expanduser("~/.claude/skills/<skill-name>")),
        "at": team_manifest.now_iso(),
    }
team_manifest.mutate(f)
EOF
```

(submit이 성공하면 clone은 자동 삭제되므로 `sanitized_hash`는 submit **전에** 계산해 두세요. 놓쳤다면 null로 두어도 됩니다 — adopt는 `local_hash_at_share`만 사용합니다.)

### 6. 보고

PR URL을 사용자에게 전하고, 머지되면 다음 `/sync-team-skills`에서 개인 원본이 팀 관리본으로 전환(adopt)됨을 안내하세요. 전환 시 개인 원본은 자동 백업됩니다.
