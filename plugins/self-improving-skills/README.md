# self-improving-skills

복잡한 작업을 끝낼 때마다, 거기서 얻은 **재사용 가능한 기법을 자동으로 `SKILL.md`로 증류**하고 기존 스킬을 스스로 개선하는 자기개선 루프. Nous Research의 [Hermes Agent](https://github.com/NousResearch/hermes-agent)가 가진 "closed learning loop"를 Claude Code 플러그인 프리미티브(훅·서브에이전트·커맨드)로 이식한 것입니다.

> 한 세션에서 배운 것이 다음 세션의 자신에게 남습니다. 경험 → 증류 → 재사용.

## 동작 방식 (4층 루프)

| 층 | 구현 | 무엇을 하나 |
|---|---|---|
| **STORE** | `~/.claude/skills/<name>/SKILL.md` | 학습된 스킬을 전역 사용자 디렉토리에 누적. 플러그인 업데이트와 무관하게 지식이 쌓임 |
| **TRIGGER** | `Stop` 훅 (`distill-nudge.sh` → `analyze_turn.py`) | 마지막 증류 이후 도구 호출이 임계 이상 누적됐는데 아직 스킬화되지 않았으면, 종료를 한 번 막고 증류를 유도 |
| **REVIEW** | `skill-distiller` 서브에이전트 | 격리된 컨텍스트에서 `patch > umbrella수정 > 참조추가 > 신규생성` 우선순위로 판단해 SKILL.md를 쓰거나 고침 |
| **DISCOVER** | Claude Code 기본 동작 | 세션 시작 시 `skills/`를 자동 재스캔 → 새 스킬이 다음 세션 프롬프트에 자동 등장 |

부가:

- **TELEMETRY** (v0.2.0) — `Stop` 훅이 transcript에서 학습 스킬의 **사용 빈도를 추적**: `Skill` 호출→use, SKILL.md `Read`→view, `Write/Edit`→patch. `~/.claude/self-improve/skill_usage.json`에 use/view/patch 카운트 + 마지막 사용 시각 + `created_at` + `created_by`(agent/user)를 기록(atomic+flock, 세션별 offset으로 중복 방지). 이게 큐레이터가 "실제 안 쓰는 스킬"을 식별하는 데이터 기반입니다.
- **VALIDATE + 트랜잭션 편집** (v0.5.0) — `PreToolUse` 훅이 학습 SKILL.md를 편집 **직전에 백업**하고, `PostToolUse` 훅이 편집 후 frontmatter·크기를 검증. 편집이 구조를 깨뜨리면 **백업에서 자동 롤백**(Hermes `_patch_skill`의 backup→re-validate→rollback 이식)하고 모델에 다시 시도하도록 알림. 정상 편집은 무간섭. 처음 만들어진 학습 스킬엔 `metadata.provenance` 자동 부착 + usage 레코드 시딩(티어링: distiller=agent, 사용자 직접=user).
- **CURATE** (v0.3.0) — **시간기반 미사용 스킬 자동 정리**. `SessionStart` 훅이 큐레이션 주기(기본 7일)가 됐는지 확인하고, 됐으면 `curator_transitions.py`를 **인라인 자동 실행**: 마지막 활동(use/view/patch) 기준 **30일 미사용→stale, 90일→archive**(`.archive/` 로 이동, 삭제 아님). 변경 전 tar.gz 스냅샷을 뜨고, 다시 쓰이면 stale→active로 재활성화. **pin된 스킬과 사용자 작성(`created_by:user`) 스킬은 절대 건드리지 않음.** 의미 기반 중복 통합은 `/curate-skills`(LLM, 병합 시 `absorbed_into` 기록)가 담당. 수동 제어 커맨드: `/curator-status`(상태·통계), `/prune-skills`(N일 미사용 일괄, dry-run), `/archive-skill`(단일), `/pin-skill`(보호), `/restore-skill`(복구), `/curator-rollback`(스냅샷 전체 롤백 — usage 메타 포함, 롤백도 언두 가능).
- **수동 트리거** — `/distill-skill` 로 언제든 증류를 직접 실행.
- **TEAM SHARE** (v0.9.0) — **팀 스킬 공유(origin-hash 동기화)**. 팀 repo는 하드코딩되지 않으며 각 사용자가 `~/.claude/self-improve/team_config.json` 에 지정합니다(대부분 private repo, gh 인증 경유). **보내기** `/share-skill <name>`: 정적 스캔(시크릿·로컬경로·인젝션) → LLM 일반화(취향 제거, 기법만) → diff 확인 → 격리 clone 에서 팀 repo `skills/<name>/` 로 PR(머지는 사람). **받기** `/sync-team-skills`: 매 실행 fresh shallow clone → plan(read-only) 표 확인 → apply. **origin-hash 규칙이 개인화를 구조적으로 보호합니다**: 설치 시점의 디렉토리 내용 해시를 매니페스트(`team_sync.json`)에 기록하고, ▸ 내 사본이 그대로면 자동 업데이트 ▸ 내가 수정했으면 절대 덮지 않음(diverged 안내 1회) ▸ 내가 삭제/아카이브하면 재설치 안 함(suppression, `--reinstall` 로 복귀) ▸ 동명 개인 스킬은 충돌 스킵. 설치 전 정적 스캔 실패 시 격리(`team_quarantine/`). 내가 공유한 PR이 머지되면 다음 sync 가 개인 원본을 백업 후 팀 관리본으로 전환(adopt). 팀 스킬은 `created_by: team` 으로 기록되어 **개인 큐레이터가 절대 정리하지 않습니다**(소유자는 팀 repo — Hermes hub 원칙). per-skill 스테이징 트랜잭션 + 크래시 자가치유(local==team 이면 origin 재기록) 포함.
- **HERMES SYNC** (v0.10.0) — Hermes 최신(v2026.7.1, 2026-07) 대조 재이식 14건. **distiller 프롬프트**: 이번 세션에 로드된(in-play) 스킬 최우선 패치 + 사용자 교정·좌절 표현을 1급 시그널로(태스크 결부 교정만 스킬에, 일반 선호는 네이티브 메모리 몫) + 세션 내 해소된 일시 오류 캡처 금지 + description 저장 전 자기검증 + "실행/관찰한 것만 기록" 환각 방지. **트리거**: 파일 편집 0회 조사·디버깅 구간도 증류 대상(`SIS_DISTILL_READONLY_THRESHOLD`). **큐레이터 안전장치**: `archive_one` fail-closed 가드(absorbed_into 실존·자기참조 검증, pinned/team/user 거부 — Hermes #29912 이식), pinned 스킬 자율 편집 자동 롤백, 타임스탬프 접미 아카이브 정확 복구(prefix 삼킴 금지 — Hermes 992b9223), 스냅샷에 usage 메타 수록 + `/curator-rollback`(롤백 자체도 언두 가능 — fc1119ca), `/curate-skills` 시각 기록을 검토 시작 시점으로 이동(`mark-curated`, nag 루프 차단) + 구조화 결과 기록(consolidations.yaml). **팀 공유**: 스캐너에 invisible unicode 17종 검출·NFKC 동형문자 폴딩·정규식 ReDoS 바운딩 + 설치 스캔 attestation(`scan_provenance` — 스캐너 업그레이드 시 자동 rescan, 기설치본은 자동 제거 없이 사람 판단). **현대화**: SessionStart 상시 주입 축소(~10줄→~4줄, 권한 복구는 차단 발생 지점으로 이동), `SIS_DISTILLER_MODEL` 비용 라우팅 opt-in.
- **UPSTREAM 기여** (v0.6.0) — 플러그인 *자기 자신*의 개선을 닫는 두 단계. **L1(감지·알림)**: `Stop` 훅이 이번 구간에서 self-improving-skills 코어 소스(`plugins/self-improving-skills/**`)를 편집했음을 감지하면, skills 증류와 별개로 "코어를 건드렸으니 upstream에 PR로 제안 가능"을 알립니다(정보 제공만, 자동 행동 없음). **L2(opt-in 자동 PR)**: `/propose-plugin-improvement` 가 격리된 fresh clone에서 변경을 재현해 upstream(`samton-inc/samton-claude`)에 PR을 엽니다. **설계 불변식**: 자동 push·머지 없음(PR까지만, 머지는 사람) / `SIS_PLUGIN_PR=1` opt-in 기본 OFF / fresh clone + commit 전 index 초기화 + 화이트리스트 서브트리만 스테이징(그 밖 경로가 staged되면 PR 중단)으로 transcript·로컬 비밀 유입 차단 / write 권한 없으면 fork 경유. distiller(skills 전담)와 책임이 분리되어 있습니다 — distiller는 PR을 만들지 않습니다.

## 흐름

```
복잡한 작업 종료
   │  Stop 훅: 마지막 증류 이후 도구호출 ≥ SIS_DISTILL_THRESHOLD 이고 파일편집 ≥ SIS_MIN_FILE_EDITS?
   ▼ (yes)
종료를 1회 block → "skill-distiller에 위임하라"
   ▼
skill-distiller 서브에이전트 (격리 컨텍스트)
   │  기존 스킬과 매칭 → patch  /  없으면 class-level 신규  /  일회성이면 skip
   ▼
~/.claude/skills/<name>/SKILL.md 작성·수정
   │  PostToolUse 훅: frontmatter 검증 + provenance 스탬프
   ▼
다음 세션 시작 → Claude Code가 자동 발견 → 스킬 재사용
```

## 설정 (환경변수)

| 변수 | 기본값 | 의미 |
|---|---|---|
| `SIS_DISTILL_THRESHOLD` | `12` | 증류 nudge를 띄울, 마지막 증류 이후 누적 도구 호출 수 |
| `SIS_MIN_FILE_EDITS` | `2` | nudge 조건: 마지막 증류 이후 실제 파일 편집(Edit/Write/MultiEdit) 최소 횟수. 순수 탐색·질의 턴은 트리거하지 않게 함 |
| `SIS_DISTILL_READONLY_THRESHOLD` | `24` | 파일 편집이 **0회**인 구간도 도구 호출이 이 수를 넘으면 nudge — 긴 조사·디버깅 세션의 진단 기법(커맨드 사다리·원인 규명 패턴)이 영원히 증류되지 않는 갭을 막음 (Hermes 는 툴 iteration 만으로 트리거) |
| `SIS_DISTILLER_MODEL` | (없음) | 설정 시(예: `sonnet`) nudge·/distill-skill 이 distiller 호출에 `model="<값>"` 파라미터를 포함하라고 안내 — per-invocation model 이 frontmatter 를 이기므로 **증류만** 저가 모델로 라우팅하는 opt-in (기본은 메인 모델 상속, `haiku` 값은 무시 — 서브에이전트 Haiku 금지 정책) |
| `SIS_CURATE_MIN_SKILLS` | `8` | 자동 큐레이션을 시작하는 학습 스킬 수 |
| `SIS_CURATE_INTERVAL_DAYS` | `7` | 큐레이터 자동 실행 간격(일) |
| `SIS_STALE_AFTER_DAYS` | `30` | 마지막 활동 후 이 일수 미사용 시 stale 마킹 |
| `SIS_ARCHIVE_AFTER_DAYS` | `90` | 마지막 활동 후 이 일수 미사용 시 `.archive/` 로 이동 |
| `SIS_PLUGIN_PR` | (없음) | `1` 로 설정하면 `/propose-plugin-improvement` 의 L2 자동 PR을 활성화. 미설정이면 코어 변경 L1 알림만 동작하고 PR은 만들지 않음 |
| `SIS_TEAM_SKILLS_REPO` | (없음) | 팀 스킬 repo override (`owner/name`). 기본은 `~/.claude/self-improve/team_config.json` 의 `repo` — 둘 다 없으면 공유 커맨드는 안내 후 중단 |
| `SIS_TEAM_SYNC_REMIND_DAYS` | `7` | 마지막 팀 동기화 후 이 일수가 지나면 SessionStart 가 /sync-team-skills 를 권유(네트워크 0, 1일 1회) |

`~/.claude/settings.json` 의 `env` 블록이나 셸 환경에서 조정합니다.

## auto mode 사용자 — 권한 허용 (자동 증류가 막힐 때)

`~/.claude/settings.json` 의 `permissions.defaultMode` 가 `"auto"` 면, **백그라운드 도구 호출은 권한 프롬프트를 띄울 수 없어 자동 거부(auto-deny)** 됩니다. 그래서 증류 nudge 가 `skill-distiller` 를 `run_in_background:true` 로 호출해도 그 자리에서 차단되어, 자동 증류 루프가 돌지 않습니다.

플러그인은 **설치만으로 사용자 권한을 열 수 없습니다** — plugin.json/marketplace 에 권한 선언 필드가 없고, 플러그인이 ship 할 수 있는 default settings 도 `permissions` 를 제외합니다. 이는 의도된 보안 경계입니다(설치=권한 자동부여가 되면 악성 플러그인이 위험). 대신 **사용자가 1회** 다음 규칙을 `permissions.allow` 에 추가하면 영구 해결됩니다:

```jsonc
"Agent(self-improving-skills:skill-distiller)",  // 서브에이전트 기동
"Read(~/.claude/skills/**)",                      // 기존 스킬 읽기/검색
"Edit(~/.claude/skills/**)",                      // SKILL.md patch
"Write(~/.claude/skills/**)",                     // 새 SKILL.md 작성
"Read(~/.claude/projects/**)"                     // transcript(증류 근거) 읽기
```

- 서브에이전트는 **네임스페이스 포함 전체 이름**(`self-improving-skills:skill-distiller`)으로 매칭됩니다. 짧은 이름(`skill-distiller`)은 매칭되지 않습니다.
- `Agent(...)` 는 서브에이전트 "기동"만 허용합니다. 그 안에서 일어나는 Read/Edit/Write 는 **각각 별도로** 권한 평가되므로(백그라운드라 프롬프트 불가 → 명시 없으면 또 auto-deny), 위 경로 규칙이 함께 필요합니다.
- 홈 경로는 `~` 를 그대로 씁니다. 절대경로로 쓰려면 `Write(//Users/me/.claude/skills/**)` 처럼 **앞에 슬래시 2개**가 필요합니다(`/Users/...` 는 프로젝트 루트 상대로 해석됨).
- distiller 가 보조 `Bash`(검증·grep)를 쓰다 막히면, 백그라운드 에이전트 로그의 deny 를 보고 좁은 `Bash(...)` 규칙을 점진 추가하세요.
- `defaultMode` 가 `"default"`(대화형 승인) 면 포그라운드 호출은 프롬프트로 승인할 수 있으나, **백그라운드 호출은 mode 와 무관하게 사전 allow 가 필요**합니다.

SessionStart 는 이 절차의 전문을 매 세션 주입하지 않습니다(상시 컨텍스트 비용) — 짧은 참조 포인터만 남기고, 실제 차단이 가장 자주 발생하는 지점인 Stop 훅 nudge 문구가 차단 시 이 섹션을 참조하라고 안내합니다. 원칙은 그대로입니다: settings 를 읽어 차단을 예측하지 말 것(skip 플래그·런타임 모드 등 변수가 많음), 실제로 막혔을 때만 위 5줄 추가를 안내할 것.

## 설계 원칙 — dev-log의 실패에서 배운 가드레일

이 플러그인은 같은 마켓플레이스의 `dev-log` 훅이 **396개 실제 세션에서 단 한 번도 발동하지 않은** 원인 분석 위에 만들어졌습니다. 그 7대 실수를 모두 막았습니다:

1. **transcript를 실측 구조로 파싱.** 도구 호출은 `"name":"Edit"` (assistant 행의 `message.content[]` 안)으로 기록됩니다 — `"tool":"Edit"`가 아닙니다. `analyze_turn.py`는 실제 구조를 파싱합니다.
2. **"이미 했나"를 실제 행위로 판정.** 플러그인 이름 문자열(`grep -q`)이 아니라, skill-distiller로의 Task 위임 또는 `~/.claude/skills`의 SKILL.md 쓰기라는 실제 tool_use를 anchor로 봅니다. (substring 매칭은 플러그인 자기 이름이 transcript에 박혀 자기트립합니다.)
3. **임계값을 실데이터로.** 키워드 5회 같은 비현실적 게이트 대신, 누적 도구 호출·파일 편집 수라는 작업량 신호를 씁니다.
4. **Stop-hook 계약 준수.** block은 STDOUT + exit 0 의 `{"decision":"block","reason":...}` 로 emit합니다 (stderr+exit2 아님). `stop_hook_active` 루프 가드 포함.
5. **작업 유형 무관 트리거.** "빌드 에러 수정"처럼 좁은 조건(사용자 396세션 중 빌드 도구 호출 12회뿐)이 아니라, 모든 작업에 걸리는 기계적 복잡도 신호를 씁니다.
6. **situation-first 스킬 description.** distiller가 쓰는 SKILL.md는 방어적 "MUST ALWAYS"가 아니라 "이런 상황에 사용한다"는 상황 매칭으로 작성합니다.
7. **모델 자발성에만 의존하지 않음.** 훅이 강제하되, 그 훅이 정확하게 동작합니다. 동시에 어떤 에러에도 fail-safe로 approve(세션을 막지 않음).

## 한계 (정직하게)

- Hermes의 **무음·무료 데몬 스레드**(메인 대화 비용 0으로 백그라운드에서 스킬을 쓰는 fork)는 Claude Code에 대응물이 없습니다. 여기 격리 리뷰어는 서브에이전트라 **메인 턴에 빌링되고 사용자에게 보입니다.** 진짜 백그라운드를 원하면 launchd/cron `claude -p` 로 별도 구성해야 합니다.
- 프리픽스 캐시 상속, 런타임 ContextVar 기반 provenance도 이식 불가 → frontmatter 스탬프로 근사.
- 크로스세션 FTS5 검색(Hermes의 RECALL)은 이 플러그인 범위 밖입니다. 필요하면 `remember` 플러그인(메모리 자율 캡처)과 함께 쓰는 것을 권장.
- **메모리 루프(MEMORY.md/USER.md)는 의도적으로 만들지 않습니다.** Hermes는 메모리(서술적 지식)와 스킬(절차적 지식)을 한 루프에 묶었지만, Claude Code는 **네이티브 `MEMORY.md` auto memory**(v2.1.59+ GA, 기본 ON)가 이미 에이전트 자율 메모리를 담당합니다. 이 플러그인은 **절차적 능력(스킬)** 축만 맡고, 사실 메모리는 네이티브에 위임 — 중복·이중 주입을 피합니다. (역할 분담: `CLAUDE.md`=정적 정책, 네이티브 `MEMORY.md`=자율 사실 메모리, `remember`=세션 요약, 이 플러그인=재사용 스킬.)

## 구성 파일

```
self-improving-skills/
├── hooks/
│   ├── hooks.json            # Stop + SessionStart + PreToolUse + PostToolUse 등록
│   ├── distill-nudge.sh      # Stop 래퍼 (fail-safe)
│   ├── session-init.sh       # SessionStart 래퍼
│   ├── backup-skill.sh       # PreToolUse 래퍼 (편집 직전 백업)
│   └── validate-skill.sh     # PostToolUse 래퍼 (검증 + 롤백)
├── scripts/
│   ├── analyze_turn.py        # 복잡도 측정 + block/approve 결정 + usage 캡처 + 코어 변경 감지(L1)
│   ├── usage_store.py         # 스킬 사용 telemetry 저장소 (atomic+flock, _meta prune)
│   ├── curator_transitions.py # 시간기반 stale→archive 상태머신 (+restore/prune, use_count 보호)
│   ├── curator_backup.py      # 변경 전 tar.gz 스냅샷
│   ├── backup_skill.py        # PreToolUse: SKILL.md 편집 직전 백업
│   ├── session_init.py        # 자기개선 안내 + 큐레이터 자동 실행 + 팀 동기화 리마인더
│   ├── validate_skill.py      # SKILL.md 검증 + 롤백 + provenance + patch 집계 + diverged 안내
│   ├── propose_pr.py          # 범용 PR plumbing (fresh clone, 화이트리스트, fork 경유)
│   ├── propose_plugin_pr.py   # 코어 기여 어댑터 (SIS_PLUGIN_PR 게이트)
│   ├── team_config.py         # 팀 repo 사용자 설정 로드 (하드코딩 없음)
│   ├── team_manifest.py       # 동기화 매니페스트 + 결정적 디렉토리 해시
│   ├── team_sync.py           # 동기화 엔진 (plan/apply/--reinstall, 상태머신)
│   └── scan_skill.py          # 정적 스캐너 (시크릿·인젝션·위험명령, 설치 게이트/공유 리포트)
├── agents/
│   └── skill-distiller.md     # 격리 리뷰어 (patch>create 우선순위)
├── tests/                     # pytest 스위트 (uv run --with pytest -- pytest tests/)
└── commands/
    ├── distill-skill.md       # 수동 증류 트리거
    ├── curate-skills.md       # umbrella 통합 패스 (LLM, absorbed_into 기록)
    ├── curator-status.md      # 루프 상태·사용 통계 조회
    ├── prune-skills.md        # N일 미사용 일괄 아카이브 (dry-run)
    ├── archive-skill.md       # 단일 스킬 수동 아카이브
    ├── pin-skill.md           # 스킬 pin (자동 정리 보호)
    ├── restore-skill.md       # 아카이브 복구
    ├── share-skill.md         # 학습 스킬을 팀 repo에 PR로 공유 (sanitize→일반화→PR)
    ├── sync-team-skills.md    # 팀 스킬 동기화 (origin-hash, plan→apply)
    └── propose-plugin-improvement.md  # 코어 개선을 upstream에 PR 제안 (L2, opt-in)
```
