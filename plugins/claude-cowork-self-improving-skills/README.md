# claude-cowork-self-improving-skills

[claude-code-self-improving-skills](../claude-code-self-improving-skills)의 **Cowork(클라우드 컨테이너) 전용 변형**. 원본과 같은 자기개선 루프(경험 → 증류 → 재사용)를 돌리되, Cowork 환경에서 실측으로 확인된 두 가지 구조적 제약을 우회하도록 재설계했습니다.

> 로컬 Claude Code CLI에는 원본 `claude-code-self-improving-skills`를, Cowork에는 이 플러그인을 설치하세요. 같은 환경에 둘을 동시에 설치하는 것은 권장하지 않습니다(훅·nudge 중복).

## 왜 별도 플러그인인가 (Cowork 실측 진단, 2026-07-16)

**문제 1 — SessionStart 훅 race.** 콜드 Cowork 컨테이너에서는 훅 등록(`load_plugin_hooks`, 부팅 후 ~2.6초)이 플러그인 다운로드 완료(`plugins_sync_complete`, ~5초)보다 먼저 실행됩니다. SessionStart 이벤트는 그 사이에 지나가므로 **플러그인의 SessionStart 훅만 매번 유실**됩니다 (PreToolUse/PostToolUse/Stop은 이벤트가 나중에 발생해 정상 동작 — 진단 로그 타임라인과 훅 부산물 파일로 검증).

**문제 2 — 영속성.** 컨테이너의 `~/.claude/skills`와 usage telemetry(`~/.claude/self-improve`)는 세션 종료와 함께 사라집니다. 스킬 동기화는 claude.ai → 컨테이너 **단방향 다운로드**뿐이라, 증류한 스킬을 그대로 두면 다음 세션에 남지 않습니다. 더 나쁘게는, **주기적 스킬 동기화(약 10분 간격)가 registry에 없는 학습 스킬 디렉터리를 세션 도중에도 삭제**하는 것이 관찰됐습니다 — 증류 즉시 저장이 필수인 이유입니다. 시간 기반 큐레이터(30일 stale/90일 archive)도 상태가 매 세션 리셋되어 무의미해집니다.

**claude.ai '스킬 저장' 거부 규칙 (실측).** ① `name` 에 예약어 `claude`/`anthropic` 포함 시 거부, ② `description` 에 꺾쇠 태그 형태(`<...>`, placeholder 포함) 포함 시 거부. 본문(body)의 꺾쇠는 무방합니다. PostToolUse 검증기가 둘 다 작성 시점에 경고합니다.

## 해법: 루프를 claude.ai 저장으로 닫는다

```
복잡한 작업 종료
   │  Stop 훅: 도구호출 ≥ SIS_DISTILL_THRESHOLD & 파일편집 ≥ SIS_MIN_FILE_EDITS?
   ▼ (yes)
종료를 1회 block → "skill-distiller에 위임 + 저장까지 안내하라"
   ▼
skill-distiller 서브에이전트: patch > umbrella > 참조추가 > 신규 (예약어 없는 이름)
   ▼
~/.claude/skills/<name>/SKILL.md 작성·수정
   │  PreToolUse: 편집 전 백업 / PostToolUse: 검증 + provenance + 예약어 경고
   ▼
SendUserFile 로 SKILL.md 를 사용자에게 전송
   ▼
사용자가 파일 카드의 [스킬 저장] 클릭 → claude.ai 계정에 등록
   ▼
다음 세션: 컨테이너 부팅 시 claude.ai 스킬 자동 동기화 → 루프 닫힘
```

`metadata.provenance: self-improving-skills` 값은 원본과 **동일하게 유지**됩니다 — Cowork에서 저장한 스킬이 로컬 CLI로 동기화되면 원본 플러그인의 카운터·큐레이터가 그대로 인식합니다(교차 호환).

## 구성

| 구성 요소 | 파일 | 역할 |
|---|---|---|
| **UserPromptSubmit 훅** | `hooks/session-advisory.sh` → `scripts/session_advisory.py` | 세션 첫 프롬프트에서 루프 안내 1회 주입 (SessionStart 대체 — race 회피). 플래그: `~/.claude/self-improve/advisory_shown` |
| **Stop 훅** | `hooks/distill-nudge.sh` → `scripts/analyze_turn.py` | 임계 초과 미증류 구간에서 종료 1회 block + 증류·**저장 유도**. 안내 유실 시 fallback advisory 를 여기서 대신 주입 |
| **PreToolUse 훅** | `hooks/backup-skill.sh` → `scripts/backup_skill.py` | 학습 SKILL.md 편집 직전 백업 (구조 깨짐 시 롤백용) |
| **PostToolUse 훅** | `hooks/validate-skill.sh` → `scripts/validate_skill.py` | frontmatter·크기 검증, 깨지면 자동 롤백, provenance 스탬프, **claude.ai 예약어(`claude`/`anthropic`) 이름 경고** |
| **skill-distiller 에이전트** | `agents/skill-distiller.md` | 증류 판단·작성. Cowork판: 예약어 금지 네이밍 + 보고에 "SendUserFile 로 보내 저장 안내" 지시 포함 |
| **/distill-skill** | `skills/distill-skill/` | 수동 증류 트리거 + 저장 안내까지 |
| **/save-skill** (신규) | `skills/save-skill/` | 미저장 학습 스킬 자동 감지(manifest 대조) → SendUserFile → '스킬 저장' 안내. 예약어 사전 점검·rename 포함 |
| **/loop-status** (신규) | `skills/loop-status/` | 학습 스킬 × 저장 여부 × 이번 세션 telemetry 요약 |

## 원본과의 차이 요약

- **교체**: SessionStart 훅 → UserPromptSubmit 훅(첫 프롬프트 1회) + Stop 훅 fallback.
- **추가**: `/save-skill`, `/loop-status`, 예약어 이름 경고(PostToolUse), nudge·distiller의 "SendUserFile → 스킬 저장" 단계.
- **제외**: 큐레이터 계열 7종(`/curate-skills`, `/curator-status`, `/prune-skills`, `/pin-skill`, `/archive-skill`, `/restore-skill`, `/curator-rollback`) — 세션별 상태 리셋으로 시간 기반 정리가 무의미하며, 라이브러리 관리는 claude.ai 설정 > 스킬에서 합니다. upstream PR(`/propose-plugin-improvement`)도 제외(필요 시 원본 참조).
- **커맨드 형식**: 레거시 `commands/*.md` 대신 `skills/*/SKILL.md`(Cowork 권장 형식).

## 설치

Cowork(claude.ai)의 플러그인 설치 흐름 또는 이 마켓플레이스를 통해 `claude-cowork-self-improving-skills`를 설치합니다. 별도 설정 없이 바로 동작합니다.

## 환경변수

| 변수 | 기본 | 의미 |
|---|---|---|
| `SIS_DISTILL_THRESHOLD` | 12 | nudge에 필요한 미증류 도구 호출 수 |
| `SIS_MIN_FILE_EDITS` | 2 | nudge에 필요한 실제 파일 편집 수 |
| `SIS_DISTILL_READONLY_THRESHOLD` | 24 | 편집 0회(조사·디버깅) 구간의 nudge 임계 |
| `SIS_DISTILLER_MODEL` | (없음) | distiller 서브에이전트 모델 라우팅 opt-in (`haiku` 금지) |

## 테스트

```bash
cd plugins/claude-cowork-self-improving-skills
python3 -m pytest tests/ -q
```

훅 계약 테스트(서브프로세스, HOME 샌드박스) + usage store 단위 테스트. Cowork 실환경 검증 기법(진단 로그 타임라인, 훅 부산물, 합성 stdin payload)은 이 플러그인을 낳은 진단 세션에서 증류된 `cloud-hook-diagnostics` 학습 스킬을 참조하세요.
