# Self-Improving Skills

[English](README.md) | **한국어**

> **Claude Code · Claude Cowork · Codex · ChatGPT work를 위한 Hermes Agent식 자기개선 루프.**
>
> 어렵게 얻은 작업 노하우를 재사용 가능한 `SKILL.md`로 증류하고, 스킬 편집을 검증하며, 낡은 지식을 정리하고, (v0.9.0부터) 팀이 학습 스킬을 git repo로 공유합니다 — 누구의 개인 커스터마이즈도 절대 덮어쓰지 않으면서.

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
| `chatgpt-work-self-improving-skills` | ChatGPT 데스크톱 Work (skills-only 패키지, `chatgpt-work/` 아래) |

환경당 하나만 설치하세요 — 같은 환경에 두 변형을 함께 설치하면 훅·nudge가 중복됩니다.

## 왜 만들었나

대부분의 코딩 에이전트는 어려운 문제를 한 번은 풉니다. 그 작업에서 재사용 가능한 부분을 기억해 두었다가 나중에 다시 적용하는 에이전트는 드뭅니다.

Hermes Agent는 스킬과 큐레이터 루프로 절차적 기억을 1급 시민으로 다룹니다. 이 프로젝트는 그 아이디어를 Claude Code 플러그인으로 이식했습니다:

- transcript·도구 호출 신호로 복잡한 작업을 **감지**
- 적절한 시점에 재사용 기법의 증류를 **권유(nudge)**
- 전용 서브에이전트가 `~/.claude/skills/<name>/SKILL.md`를 **작성/패치**
- 잘못된 스킬 편집을 자동 **검증·롤백**
- **사용량 추적**으로 안 쓰는 생성 스킬이 무한히 쌓이는 대신 아카이브되게
- 검증된 스킬을 팀과 **공유** — opt-in, 리뷰 게이트, 개인화 항상 우선

## 기능

- **자동 증류 nudge**: 마지막 증류 이후 도구 호출·파일 편집이 충분히 쌓이면 `Stop` 훅이 **작업 구간당 한 번만** 종료를 막고 증류를 권유합니다. 거절한 nudge는 거절로 남고, *새* 작업이 임계만큼 다시 쌓여야 재발화합니다. nudge에는 transcript 경로가 포함되어 백그라운드 distiller가 실제 작업 내용을 직접 읽을 수 있습니다.
- **전용 distiller 서브에이전트**: 기존 스킬 패치 > umbrella 스킬 확장 > 참조 파일 추가 순으로 우선하고, 새 class-level 스킬 생성은 최후의 수단입니다. description은 Anthropic skill-creator 가이드(3인칭 상황 매칭 + 구체적 트리거 문구)를 따릅니다.
- **스킬 편집 안전장치**: 편집 직전 백업, 편집 후 검증, provenance 스탬프, 깨진 `SKILL.md` 자동 롤백. 비차단 품질 조언(예: 매 세션 컨텍스트 비용이 되는 과도하게 긴 description)도 제공합니다.
- **정확한 사용량 텔레메트리**: `~/.claude/self-improve/skill_usage.json`에 스킬별 use/view/patch 집계. patch 집계는 `PostToolUse` 훅에서 수행되어 *백그라운드* 서브에이전트의 편집까지 포착하고, 큐레이션 중의 일괄 Read는 스킬의 idle 시계를 리셋하지 않습니다.
- **큐레이터 루프**: 안 쓰는 에이전트 생성 스킬은 30일 후 stale, 90일 후 (복구 가능하게) 아카이브됩니다. 반복 사용이 입증된 스킬(`use_count >= 3`)은 절반 속도로 늙습니다. LLM 큐레이션 패스(`/curate-skills`)는 Hermes 큐레이터 프롬프트를 본뜬 umbrella-building 통합으로, 계획을 먼저 제시하고 승인 후에만 적용합니다.
- **팀 스킬 공유 (v0.9.0)**: **origin-hash 동기화**로 팀 git repo를 통해 학습 스킬을 공유 — 아래 참조.
- **수동 커맨드**: `/distill-skill`, `/curate-skills`, `/curator-status`, `/prune-skills`, `/archive-skill`, `/pin-skill`, `/restore-skill`, `/share-skill`, `/sync-team-skills`, `/propose-plugin-improvement`.
- **fail-safe 훅**: 훅 에러는 세션을 깨뜨리는 대신 원래 동작을 승인합니다.

## 팀 스킬 공유

플러그인을 팀의 (대개 private) 스킬 repo로 향하게 하세요 — 하드코딩된 repo는 없습니다:

```jsonc
// ~/.claude/self-improve/team_config.json
{
  "repo": "your-org/your-team-skills",
  "subdir": "skills"
}
```

- **보내기** `/share-skill <name>`: 스킬을 스캔(시크릿·로컬 경로·인젝션 패턴)하고, 일반화(기법은 남기고 개인 스타일은 제거)한 뒤, diff를 보여주고 확인을 받아 팀 repo에 PR을 엽니다. 머지는 사람이 합니다.
- **받기** `/sync-team-skills`: 매번 fresh shallow clone → read-only 계획 확인 → 스킬 단위 트랜잭션 적용.

**origin-hash 규칙**이 공유를 구조적으로 안전하게 만듭니다. 설치되는 팀 스킬마다 설치 시점의 결정적 내용 해시를 기록해 두고:

| 내 로컬 사본 상태 | 동기화 동작 |
|---|---|
| 손대지 않음 (해시 == origin) | 팀 최신으로 자동 업데이트 |
| **내가 수정함** | **절대 덮어쓰지 않음** — "diverged" 1회 안내; 원하면 내 버전을 역으로 공유 |
| 내가 삭제/아카이브함 | 재설치하지 않음 (`--reinstall <name>` 전까지) |
| 동명의 개인 스킬과 충돌 | 경고와 함께 스킵 |

스킬은 *에이전트에 대한 지시문* — 즉 프롬프트 인젝션 벡터입니다. 그래서 팀 콘텐츠의 모든 쓰기(최초 설치 **그리고** 이후 업데이트)가 정적 스캐너(시크릿·파괴적 명령·인젝션 마커·심볼릭 링크·숨김 파일·크기 상한)를 통과해야 하며, 차단된 콘텐츠는 `~/.claude/skills`가 아니라 격리 디렉토리에 들어갑니다. 팀 스킬은 `created_by: team`으로 표시되어 개인 큐레이터가 절대 건드리지 않습니다 — 소유자는 팀 repo입니다.

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
| `SIS_DISTILL_THRESHOLD` | `12` | nudge가 발화할 수 있는, 마지막 증류 이후 누적 도구 호출 수 |
| `SIS_MIN_FILE_EDITS` | `2` | 마지막 증류 이후 최소 파일 편집 수 — 순수 리서치 대화의 발화를 방지 |
| `SIS_CURATE_MIN_SKILLS` | `8` | 자동 큐레이션이 도는 최소 학습 스킬 수 |
| `SIS_CURATE_INTERVAL_DAYS` | `7` | 자동 큐레이터 주기 |
| `SIS_STALE_AFTER_DAYS` | `30` | 이 일수 미사용 시 에이전트 생성 스킬을 stale로 마킹 |
| `SIS_ARCHIVE_AFTER_DAYS` | `90` | 이 일수 미사용 시 `.archive/`로 이동 (`use_count >= 3`인 스킬은 2배) |
| `SIS_TEAM_SKILLS_REPO` | (없음) | 팀 repo override (`owner/name`); 기본 소스는 `~/.claude/self-improve/team_config.json` |
| `SIS_TEAM_SYNC_REMIND_DAYS` | `7` | 마지막 팀 동기화 후 이 일수가 지나면 SessionStart가 `/sync-team-skills`를 권유 (네트워크 0, 1일 1회) |
| `SIS_PLUGIN_PR` | (없음) | `1`로 설정하면 이 플러그인 자체 소스에 대한 opt-in upstream PR 헬퍼 허용 |

## 동작 방식

```text
Claude Code 세션 종료 시도
  ↓
Stop 훅이 transcript와 usage offset을 파싱
  ↓
작업이 복잡했는데 아직 증류 안 됐으면 1회성 block 반환
  ↓
Claude가 claude-code-self-improving-skills:skill-distiller에 위임 (백그라운드)
  ↓
distiller가 ~/.claude/skills 아래 재사용 가능한 SKILL.md를 패치/생성
  ↓
검증 훅이 frontmatter/크기/provenance를 확인하고 잘못된 편집은 롤백
  ↓
다음 세션: Claude Code가 스킬을 정상적으로 발견
```

학습된 스킬은 플러그인 안이 아니라 사용자 디렉토리에 삽니다. 플러그인을 업데이트해도 누적된 절차적 지식은 지워지지 않습니다.

## 저장소 구조

```text
.claude-plugin/marketplace.json          # Claude Code 마켓플레이스 매니페스트 (플러그인 2종)
.agents/plugins/marketplace.json         # Codex + ChatGPT Work 마켓플레이스 매니페스트
chatgpt-work/                            # 루트 마켓플레이스에 등록된 Work 플러그인 본체
plugins/claude-cowork-self-improving-skills/   # Cowork 변형
plugins/chatgpt-codex-self-improving-skills/   # Codex 변형
plugins/claude-code-self-improving-skills/
  .claude-plugin/plugin.json             # 플러그인 메타데이터
  hooks/                                 # Stop, SessionStart, PreToolUse, PostToolUse 래퍼
  scripts/                               # transcript 분석, 텔레메트리, 큐레이터, 검증기,
                                         #   팀 동기화 엔진, 보안 스캐너, PR plumbing
  agents/skill-distiller.md              # 스킬 증류 서브에이전트 프롬프트
  commands/                              # 플러그인이 노출하는 슬래시 커맨드
  tests/                                 # pytest 스위트 (uv run --with pytest -- pytest tests/)
  README.md                              # 상세 설계 노트 (한국어)
```

## 정직한 한계

- Claude Code에는 Hermes Agent의 무료 백그라운드 데몬 스레드가 없습니다. 증류는 보이는/과금되는 서브에이전트 턴을 사용합니다.
- 이 플러그인은 절차적 기억(`SKILL.md`)을 다룹니다. 사실 기억(당신·프로젝트에 대한 정보)은 Claude Code 네이티브 메모리나 별도 메모리 플러그인의 몫입니다.
- 큐레이터는 의도적으로 보수적입니다: 에이전트가 생성한 학습 스킬만 아카이브하고, 복구 가능한 백업을 유지합니다.
- 팀 동기화는 의도적으로 실시간이 아니라 PR 게이트입니다. 실시간 공유 저장소는 한 명의 오염된 세션이 전 팀원의 에이전트에 지시를 주입하는 통로가 됩니다 — 사람의 리뷰 게이트가 **곧** 보안 경계입니다.

## 라이선스

MIT
