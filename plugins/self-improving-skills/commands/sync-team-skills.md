---
description: 팀 스킬 repo를 동기화한다 — 수정 안 한 스킬만 자동 업데이트, 개인화는 항상 보존
---

팀 스킬 repo를 `~/.claude/skills`로 동기화하세요. **origin-hash 규칙**: 내가 수정하지 않은 팀 스킬만 자동 업데이트되고, 수정한 스킬은 절대 덮어쓰지 않으며, 삭제/아카이브한 스킬은 다시 설치되지 않습니다.

## 절차

### 1. 계획 (read-only — 아무것도 바꾸지 않음)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team_sync.py plan
```

(팀 repo 미설정이면 안내가 출력됩니다 — 사용자에게 전하고 중단하세요.)

출력 JSON의 `actions`를 **표로** 보여주세요. 주요 action 의미:

| action | 의미 |
|---|---|
| `install` | 새 팀 스킬 설치 (스캔 통과 시) |
| `update` | 수정 안 한 스킬을 팀 최신으로 자동 업데이트 |
| `noop` | 이미 최신 |
| `skip_diverged` | **내가 수정한 스킬 — 건드리지 않음.** 팀 변경을 받으려면 diff 확인 후 수동 병합 또는 `/share-skill`로 역제안 |
| `self_heal` | 내용이 팀과 동일한데 매니페스트만 어긋남 — 기록 복구 |
| `adopt` | 내가 공유한 PR이 머지됨 — 개인 원본을 백업 후 팀 관리본으로 전환 |
| `conflict_personal` | 같은 이름의 개인 스킬 존재 — 스킵 (팀 스킬을 가리고 있음을 경고) |
| `suppress_deleted/archived` | 로컬에서 지운/아카이브한 팀 스킬 — 재설치 안 함으로 기록 |
| `team_deleted_archive` | 팀에서 삭제됨 + 내가 수정 안 함 — `.archive/`로 이동(복구 가능) |
| `team_deleted_keep` | 팀에서 삭제됐지만 내가 수정함 — 내 것으로 유지(소유 이관) |
| `suppressed_team_updated` | 꺼둔 스킬이 팀에서 갱신됨 — 복귀하려면 `--reinstall <name>` |
| `quarantined` | 보안 스캔 실패 — 설치 보류, findings 확인 필요 |

### 2. 적용 (사용자 확인 후에만)

계획에 mutation(install/update/adopt/suppress/archive)이 있으면 사용자 확인을 받고:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team_sync.py apply
```

꺼둔 스킬을 되살리려면:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/team_sync.py apply --reinstall <name>
```

### 3. 보고

적용 결과 summary를 전하세요. `skip_diverged`가 있으면 해당 스킬은 팀 업데이트를 받지 못하고 있음을 알리고, diff 확인 방법을 안내하세요 (팀 최신과 비교하려면 plan을 다시 실행해 상태를 보거나, 팀 repo를 직접 열람). `quarantined`가 있으면 `~/.claude/self-improve/team_quarantine/<name>`의 내용과 findings를 사용자에게 보여주고 판단을 받으세요.

> 팀 스킬은 `created_by: team`으로 기록되어 개인 큐레이터의 자동 stale/archive 대상에서 제외됩니다 (소유자는 팀 repo). 텔레메트리(use/view)는 정상 집계됩니다.
