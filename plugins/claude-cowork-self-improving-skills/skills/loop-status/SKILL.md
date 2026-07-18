---
name: loop-status
description: Cowork 자기개선 루프 상태를 요약한다 — 이 컨테이너의 학습 스킬 목록, claude.ai 저장(동기화) 여부, 이번 세션 usage telemetry. "루프 상태 보여줘", "학습 스킬 뭐 있어", "스킬 저장 됐나", "미저장 스킬 있나" 등에 사용.
---

자기개선 루프의 현재 상태를 사람이 읽기 좋게 보여주세요.

## 1단계 — 데이터 수집

```bash
echo "=== 학습 스킬 × 저장 여부 ==="
python3 - <<'EOF'
import json, os
skills_dir = os.path.expanduser("~/.claude/skills")
synced = set()
try:
    with open(os.path.join(skills_dir, "manifest.json"), encoding="utf-8") as fh:
        for s in json.load(fh).get("skills", []):
            synced.add(str(s.get("skillId") or ""))
            synced.add(str(s.get("name") or ""))
except Exception:
    pass
if not os.path.isdir(skills_dir):
    print("(학습 스킬 디렉토리 없음)")
else:
    for entry in sorted(os.listdir(skills_dir)):
        if entry.startswith("."):
            continue
        p = os.path.join(skills_dir, entry, "SKILL.md")
        if not os.path.isfile(p):
            continue
        head = open(p, encoding="utf-8", errors="ignore").read(2048)
        learned = "self-improving-skills" in head or "origin: distilled" in head
        status = "저장됨(동기화)" if entry in synced else "미저장(세션 종료 시 소실)"
        print("{0}\t{1}\t{2}".format(entry, "학습" if learned else "일반/동기화", status))
EOF
echo; echo "=== 이번 세션 usage telemetry ==="
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/usage_store.py" dump 2>/dev/null || echo "(telemetry 없음)"
```

## 2단계 — 정리해서 보여주기

위 출력을 파싱해 **표로** 정리하세요:

- 각 학습 스킬: 이름 · 저장 여부(저장됨/미저장) · 이번 세션 use/view/patch 횟수 · created_by(agent/user)
- **미저장 학습 스킬이 있으면 강조**하고, `/save-skill` 로 지금 저장할 수 있음을 안내하세요 — 이 컨테이너의 스킬은 세션 종료 시 사라집니다.
- 모든 학습 스킬이 저장돼 있으면 그렇게 한 줄로 보고하세요.

## 참고 (Cowork 특성)

- usage telemetry 는 **세션(컨테이너) 단위로 리셋**됩니다 — 누적 사용 통계가 아니라 "이번 세션에서 무엇이 쓰였나"입니다.
- 저장 여부는 `~/.claude/skills/manifest.json`(부팅 시 claude.ai 에서 동기화된 목록) 기준입니다. manifest 에 없는 학습 스킬 = 이번 세션에서 만들어진 것 = 아직 claude.ai 에 없음.
- 스킬 라이브러리의 정리(삭제·이름 변경)는 claude.ai 설정 > 스킬에서 하세요 — 컨테이너 안에서 지워도 다음 세션에 다시 동기화됩니다.
