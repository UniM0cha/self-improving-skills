---
description: 아카이브된 학습 스킬을 ~/.claude/skills 로 복구한다
---

큐레이터가 아카이브한 스킬을 다시 활성 상태로 복구하세요.

먼저 아카이브된 스킬 목록을 보여주세요:

```bash
ls -1 ~/.claude/skills/.archive/ 2>/dev/null
```

`$ARGUMENTS` 로 복구할 스킬 이름이 주어졌으면 그대로, 아니면 위 목록에서 어떤 것을 복구할지 물어보세요. 그 다음:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/curator_transitions.py restore "<skill-name>"
```

이 명령은 `.archive/<name>` 을 `~/.claude/skills/<name>` 으로 되돌리고 usage 상태를 active로 바꿉니다. 복구 후, 다시 아카이브되지 않게 하려면 `/pin-skill` 로 pin하는 것을 권하세요.
