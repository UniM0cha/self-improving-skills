---
description: 백그라운드 스킬 증류가 실제로 돌고 있는지 확인하고, 막힌 작업을 재시도한다
---

백그라운드 증류 큐의 상태를 확인하세요.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/distill_cli.py" status
```

출력의 `blocked` 항목마다 `fix` 필드에 사람이 취할 조치가 들어 있습니다. 사용자에게
**무엇이 막혔고 무엇을 하면 되는지**를 한국어로 요약해 전달하세요 — JSON 을 그대로
붙여넣지 마세요.

자주 나오는 상태:

- `authentication_required` — 워커가 CLI 인증을 못 씁니다. 사용자가 `claude setup-token`
  을 실행한 뒤 발급된 토큰을 `~/.claude/self-improve/worker.env` 에
  `CLAUDE_CODE_OAUTH_TOKEN=<토큰>` 한 줄로 넣고 `chmod 600` 해야 합니다.
  **토큰을 대화에 붙여넣지 말라고 안내하세요** — 대화 로그에 평문으로 남습니다.
- `out_of_scope_write` / `unprotected_write` — 증류 자식이 스킬 트리 밖을 건드렸거나,
  가드가 롤백을 보장할 수 없는 변경이 있었습니다. **재시도 전에 경로를 사용자와 함께
  확인하세요.** 이건 안전장치가 실제로 발동한 경우입니다.
- `symlinked_skills` — `~/.claude/skills` 안에 심볼릭 링크된 스킬이 있어 백그라운드
  증류를 보류했습니다. 링크를 통한 쓰기는 스냅샷 밖이라 되돌릴 수 없습니다.
- `cli_too_old` — `claude update` 후 재시도.

인자로 `retry` 가 주어졌거나 사용자가 재시도를 요청하면:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/distill_cli.py" retry --all-blocked
```

단 `out_of_scope_write` / `unprotected_write` 가 섞여 있으면 **먼저 사용자에게 확인**한
뒤 재시도하세요.

개별 작업 내용을 보려면:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/distill_cli.py" jobs --status blocked
```
