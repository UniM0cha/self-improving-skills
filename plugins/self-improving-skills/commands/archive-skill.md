---
description: 학습 스킬 하나를 수동으로 아카이브한다 (선택적으로 umbrella 병합 기록)
---

지정한 학습 스킬 하나를 아카이브하세요 (`~/.claude/skills/.archive/` 로 이동, 삭제 아님).

`$ARGUMENTS` 첫 단어가 스킬 이름입니다. 주어지지 않았으면 `~/.claude/skills/` 목록을 보여주고 어떤 것을 아카이브할지 물어보세요.

**미리보기:**
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/curator_transitions.py archive "<name>" --dry-run
```

**적용:**
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/curator_transitions.py archive "<name>"
```

이 스킬의 내용이 다른 umbrella 스킬로 **병합되어** 아카이브하는 경우, 그 umbrella 이름을 함께 기록하면 (폐기가 아니라 통합이었음이 리포트에 남습니다):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/curator_transitions.py archive "<name>" "<umbrella-skill-name>"
```

변경 전 tar.gz 스냅샷이 자동으로 생성되며, `/restore-skill "<name>"` 로 복구할 수 있습니다.
