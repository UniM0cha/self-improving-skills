# Self-Improving Skills

[English](README.md) | **한국어**

> **Claude Code · Claude Cowork · Codex · ChatGPT work를 위한 Hermes Agent식 자기개선 루프.**
>
> 어렵게 얻은 작업 노하우를 재사용 가능한 `SKILL.md`로 증류하고, 스킬 편집을 검증하며, 낡은 지식을 정리합니다. (v0.13.0부터) 증류는 **detached 백그라운드 워커**에서 돕니다 — 보이는 턴은 평소처럼 끝나고, 헤드리스 세션이 기법을 캡처합니다.

Claude Code에는 이미 훅·서브에이전트·슬래시 커맨드·스킬이 있습니다. 이 플러그인은 그 프리미티브들을 [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent)에서 영감을 받은 닫힌 학습 루프로 엮습니다:

```text
복잡한 작업 → 통한 것을 증류 → 스킬 저장/패치 → 다음 세션에서 재발견
```

## 변형(Variants)

같은 닫힌 학습 루프의 환경별 이식 4종을 한 리포에 담았습니다:

| 플러그인 | 환경 |
|---|---|
| `claude-code-self-improving-skills` | Claude Code CLI (이 README의 주 대상) |
| `claude-cowork-self-improving-skills` | Claude Cowork(클라우드 컨테이너) — claude.ai '스킬 저장'으로 영속화 |
| `chatgpt-codex-self-improving-skills` | OpenAI Codex CLI (훅 + MCP 스킬 매니저) |
| `chatgpt-work-self-improving-skills` | ChatGPT 데스크톱 Work (공통 `plugins/` 경로의 skills-only 패키지) |

환경당 하나만 설치하세요 — 같은 환경에 두 변형을 함께 설치하면 훅·nudge가 중복됩니다.

## 왜 만들었나

대부분의 코딩 에이전트는 어려운 문제를 한 번은 풉니다. 그 작업에서 재사용 가능한 부분을 기억해 두었다가 나중에 다시 적용하는 에이전트는 드뭅니다.

Hermes Agent는 스킬과 큐레이터 루프로 절차적 기억을 1급 시민으로 다룹니다. 이 프로젝트는 그 아이디어를 Claude Code 플러그인으로 이식했습니다:

- transcript·도구 호출 신호로 복잡한 작업을 **감지**
- 적절한 시점에 재사용 기법의 증류를 **권유(nudge)**
- 전용 서브에이전트가 `~/.claude/skills/<name>/SKILL.md`를 **작성/패치**
- 잘못된 스킬 편집을 자동 **검증·롤백**
- **사용량 추적**으로 안 쓰는 생성 스킬이 무한히 쌓이는 대신 아카이브되게
- 기본은 **백그라운드 증류** — 큐에 잡을 넣고 detached `claude -p` 세션이 처리, 실패 시 자동으로 in-turn nudge 폴백

## 기능

- **백그라운드 증류 (v0.13.0, 기본)**: 도구 호출·파일 편집이 충분히 쌓이면 `Stop` 훅이 조용히 잡을 큐(SQLite)에 넣고, **detached 워커**가 담장 친 헤드리스 `claude -p` 세션을 띄워 transcript를 읽고 스킬을 씁니다 — 보이는 턴에는 출력이 전혀 추가되지 않습니다. CLI가 없거나 구버전이거나 로그아웃 상태면 자동으로 기존 in-turn nudge로 폴백하며, nudge는 **작업 구간당 한 번만** 종료를 막고 transcript 경로를 포함합니다.
- **담장 친 증류 세션**: 백그라운드 자식은 축소된 도구 셋(Bash 없음), `bypassPermissions`에서도 살아있는 deny 규칙, 잡당 예산 상한으로 돌고, 실행 후 **skill guard**가 실행 전 `~/.claude/skills` 스냅샷과 대조해 손댄 SKILL.md를 전부 검증하고 깨졌거나 범위 밖인 것은 롤백합니다. 증류는 새 스킬 생성보다 기존 스킬 패치를 우선합니다(Anthropic skill-creator 가이드).
- **스킬 편집 안전장치**: 편집 직전 백업, 편집 후 검증, provenance 스탬프, 깨진 `SKILL.md` 자동 롤백. 비차단 품질 조언(예: 매 세션 컨텍스트 비용이 되는 과도하게 긴 description)도 제공합니다.
- **정확한 사용량 텔레메트리**: `~/.claude/self-improve/skill_usage.json`에 스킬별 use/view/patch 집계. patch 집계는 `PostToolUse` 훅에서 수행되어 *백그라운드* 세션의 편집까지 포착하고, 큐레이션 중의 일괄 Read는 스킬의 idle 시계를 리셋하지 않습니다.
- **큐레이터 루프**: 안 쓰는 에이전트 생성 스킬은 30일 후 stale, 90일 후 (복구 가능하게) 아카이브됩니다. 반복 사용이 입증된 스킬(`use_count >= 3`)은 절반 속도로 늙습니다. LLM 큐레이션 패스(`/curate-skills`)는 Hermes 큐레이터 프롬프트를 본뜬 umbrella-building 통합으로, 계획을 먼저 제시하고 승인 후에만 적용합니다.
- **수동 커맨드**: `/distill-skill`, `/distill-status`, `/curate-skills`, `/curator-status`, `/curator-rollback`, `/prune-skills`, `/archive-skill`, `/pin-skill`, `/restore-skill`, `/migration`, `/propose-plugin-improvement`.
- **fail-safe 훅**: 훅 에러는 세션을 깨뜨리는 대신 원래 동작을 승인합니다.
- **크로스 플랫폼**: macOS·Linux·Windows(Git Bash)를 3-OS CI 매트릭스로 검증 — 비한국어 Windows 로케일의 UTF-8 출력까지 포함.

## 백그라운드 증류 설정

백그라운드 모드에는 헤드리스로 인증 가능한 `claude` CLI(>= 2.1.205)가 필요합니다. 구독을 쓴다면 장기 토큰을 한 번 발급해 워커가 읽는 위치에 두세요:

```bash
claude setup-token
install -m 600 /dev/null ~/.claude/self-improve/worker.env
# 그 파일에 한 줄:  CLAUDE_CODE_OAUTH_TOKEN=<토큰>
```

환경변수의 API 키(`ANTHROPIC_API_KEY`)도 동작합니다. 인증이 안 되어도 플러그인은 계속 동작합니다 — in-turn nudge로 폴백할 뿐입니다. `/distill-status`가 큐·최근 잡·차단 사유별 해결책을 보여줍니다.

자식 세션은 `bypassPermissions`로 `~/.claude/skills`에 직접 쓰므로, 플러그인은 그것을 신뢰하는 대신 방어를 겹칩니다: Bash 제거, 자격증명·persistence 경로에 대한 deny 규칙(bypass 모드에서도 deny는 적용됨), 잡당 예산 상한, 신뢰할 수 없는 transcript를 감싸는 추측 불가능한 증거 경계, 그리고 검증 실패분을 되돌리는 실행 후 skill guard. deny 목록은 블랙리스트이지 증명이 아닙니다 — 전체 보안 모델은 플러그인 README에 정직하게 문서화되어 있습니다.

## 설치

### Claude Code

Claude Code 안에서 이 저장소를 플러그인 마켓플레이스로 추가하세요:

```text
/plugin marketplace add UniM0cha/self-improving-skills
/plugin install claude-code-self-improving-skills@self-improving-skills
```

Claude Code 버전에 따라 명령 형태가 다르면, 플러그인 UI에서 `https://github.com/UniM0cha/self-improving-skills`를 마켓플레이스로 추가하고 `claude-code-self-improving-skills` 플러그인을 설치하면 됩니다.

### Codex · ChatGPT Work

이 저장소를 Codex 플러그인 marketplace로 추가한 뒤 Codex 변형을 설치하세요:

```bash
codex plugin marketplace add UniM0cha/self-improving-skills
codex plugin add chatgpt-codex-self-improving-skills@self-improving-skills
```

ChatGPT 데스크톱 앱의 Plugins Directory에는 현재 두 변형이 함께 표시됩니다. Codex에서는 `chatgpt-codex-self-improving-skills`, Work 모드에서는 `chatgpt-work-self-improving-skills`를 선택하세요. 현행 데스크톱 앱에서는 marketplace의 `policy.products`로 두 화면이 안정적으로 분리되지 않습니다.

기존 설치를 최신 marketplace 구조와 버전으로 갱신하려면 다음을 실행하세요:

```bash
codex plugin marketplace upgrade self-improving-skills
codex plugin add chatgpt-codex-self-improving-skills@self-improving-skills
```

갱신 후 ChatGPT 데스크톱 앱이 marketplace를 다시 읽도록 재시작하세요. `Self-Improving Skills` 아래에 두 변형이 함께 나타나며, 사용하는 모드에 맞는 플러그인을 설치하면 됩니다. marketplace 형식은 [공식 플러그인 문서](https://learn.chatgpt.com/docs/build-plugins)를 참고할 수 있습니다.

## 설정

모든 설정은 선택입니다. 셸 환경 또는 `~/.claude/settings.json`의 `env`에서 지정하세요.

| 변수 | 기본값 | 의미 |
|---|---:|---|
| `SIS_REVIEW_MODE` | `background` | `background`(detached 워커, 내 턴 출력 0) / `foreground`(기존 nudge) / `off`. background는 CLI를 못 쓰면 자동으로 foreground 폴백 |
| `SIS_CLAUDE_BIN` | 자동탐색 | `claude` 절대경로 — GUI가 띄운 훅은 PATH에 `~/.local/bin`이 없을 수 있음 |
| `SIS_DISTILL_MAX_USD` | `0.50` | 증류 잡 1건의 `--max-budget-usd` 상한 |
| `SIS_DISTILL_MAX_JOBS_PER_DAY` | `12` | 하루에 띄울 백그라운드 증류 세션 수 상한 |
| `SIS_DISTILL_THRESHOLD` | `12` | 증류가 발화할 수 있는, 마지막 증류 이후 누적 도구 호출 수 |
| `SIS_MIN_FILE_EDITS` | `2` | 마지막 증류 이후 최소 파일 편집 수 — 순수 리서치 대화의 발화를 방지 |
| `SIS_DISTILL_READONLY_THRESHOLD` | `24` | 편집 0회 구간도 도구 호출이 이 수를 넘으면 증류 (긴 조사·디버깅의 진단 기법 캡처) |
| `SIS_STATE_DIR` | `~/.claude/self-improve` | 큐·백업·텔레메트리를 전부 함께 옮김 |
| `SIS_CURATE_MIN_SKILLS` | `8` | 자동 큐레이션이 도는 최소 학습 스킬 수 |
| `SIS_CURATE_INTERVAL_DAYS` | `7` | 자동 큐레이터 주기 |
| `SIS_STALE_AFTER_DAYS` | `30` | 이 일수 미사용 시 에이전트 생성 스킬을 stale로 마킹 |
| `SIS_ARCHIVE_AFTER_DAYS` | `90` | 이 일수 미사용 시 `.archive/`로 이동 (`use_count >= 3`인 스킬은 2배) |
| `SIS_PLUGIN_PR` | (없음) | `1`로 설정하면 이 플러그인 자체 소스에 대한 opt-in upstream PR 헬퍼 허용 |

## 동작 방식

```text
Claude Code 세션 종료 시도
  ↓
Stop 훅이 transcript와 usage offset을 파싱
  ↓
작업이 복잡했는데 아직 증류 안 됐으면 잡을 큐(SQLite)에 넣고
approve 반환 — 내 턴은 아무 출력 없이 끝남
  ↓
detached 워커가 잡을 집음 (PID-identity lease, 재시도, backoff)
  ↓
담장 친 헤드리스 `claude -p` 세션 실행: 축소된 도구, deny 규칙,
예산 상한, transcript는 untrusted-evidence 경계로 감쌈
  ↓
세션이 ~/.claude/skills 아래 재사용 가능한 SKILL.md를 패치/생성
  ↓
skill guard가 실행 전 스냅샷과 diff — 손댄 SKILL.md를 전부 검증하고
깨졌거나 범위 밖인 변경은 롤백
  ↓
다음 세션: Claude Code가 스킬을 정상적으로 발견
(폴백: 쓸 수 있는 CLI가 없으면 Stop 훅이 기존 nudge로 1회 block)
```

학습된 스킬은 플러그인 안이 아니라 사용자 디렉토리에 삽니다. 플러그인을 업데이트해도 누적된 절차적 지식은 지워지지 않습니다.

## 저장소 구조

```text
.claude-plugin/marketplace.json          # Claude Code 마켓플레이스 매니페스트 (플러그인 2종)
.agents/plugins/marketplace.json         # Codex + ChatGPT Work 마켓플레이스 매니페스트
plugins/claude-cowork-self-improving-skills/   # Cowork 변형
plugins/chatgpt-codex-self-improving-skills/   # Codex 변형
plugins/chatgpt-work-self-improving-skills/    # ChatGPT Work 변형
plugins/claude-code-self-improving-skills/
  .claude-plugin/plugin.json             # 플러그인 메타데이터
  hooks/                                 # Stop, SessionStart, PreToolUse, PostToolUse 래퍼
  scripts/                               # transcript 분석, 텔레메트리, 큐레이터, 검증기,
                                         #   증류 큐, detached 워커, skill guard
  agents/skill-distiller.md              # foreground 폴백용 서브에이전트 프롬프트
  commands/                              # 플러그인이 노출하는 슬래시 커맨드
  tests/                                 # pytest 스위트 (uv run --with pytest -- pytest tests/)
  README.md                              # 상세 설계 노트 (한국어)
```

## 정직한 한계

- 백그라운드 증류는 보이지 않을 뿐 무료가 아닙니다: detached `claude -p` 세션이 구독 또는 API 사용량을 소비합니다 (잡당 `SIS_DISTILL_MAX_USD`, 하루 `SIS_DISTILL_MAX_JOBS_PER_DAY`로 상한).
- 백그라운드 자식은 `bypassPermissions`로 `~/.claude/skills`에 씁니다. deny 규칙·축소된 도구 셋·실행 후 skill guard가 실질적 방어를 겹치지만, deny 목록은 블랙리스트입니다 — 무엇을 보장하고 무엇을 보장하지 않는지는 플러그인 README에 정확히 문서화되어 있습니다.
- 증거는 세션 transcript이며 이는 신뢰할 수 없는 입력입니다. 추측 불가능한 경계로 감싸고 "지시가 아니라 데이터"로 프레이밍하는 것은 프롬프트 인젝션 위험을 낮출 뿐, 없애지는 못합니다.
- 이 플러그인은 절차적 기억(`SKILL.md`)을 다룹니다. 사실 기억(당신·프로젝트에 대한 정보)은 Claude Code 네이티브 메모리나 별도 메모리 플러그인의 몫입니다.
- 큐레이터는 의도적으로 보수적입니다: 에이전트가 생성한 학습 스킬만 아카이브하고, 복구 가능한 백업을 유지합니다.

## 라이선스

MIT
