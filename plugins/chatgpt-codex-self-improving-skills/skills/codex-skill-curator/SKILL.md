---
name: codex-skill-curator
description: "Curate Codex skills with a Hermes-style maintenance pass: usage telemetry, stale/archive candidates, pins, backups, dry-run reports, and rollback guidance."
---

# Codex Skill Curator

Use this skill when the user asks to clean up, audit, curate, archive, restore, or review Codex skills.

## Curator Rules

1. Start with dry-run unless the user explicitly asks to mutate.
2. Only manage user-created or repo-created skills.
3. Never mutate bundled, system, admin, or plugin-distributed skills.
4. Respect pinned skills. Pinned skills can be patched, but not archived or deleted.
5. Archive instead of deleting. Archive moves must be reversible.
6. Take a backup before any mutating run.
7. Consolidate at the package level. If a skill has `references/`, `templates/`, `scripts/`, or `assets/`, preserve or re-home those files rather than flattening only `SKILL.md`.

## Tooling

Prefer MCP tools from the bundled `self-improving-skills` server:

- `codex_skill_curate` for stale/archive candidates (each run persists a
  report under the data dir's `logs/curator/` — cite it when summarizing)
- `codex_skill_usage` for telemetry
- `codex_skill_archive` for reversible archive moves (a name collision in
  `.archive/` gets a timestamp suffix automatically; restore finds it)
- `codex_skill_restore` for archived skill recovery
- `codex_skill_backups` / `codex_skill_rollback` to list backups and restore
  one by exact backup_id — the rollback backs up the current content first,
  so it is itself undoable (`undo_backup` in the result)
- `codex_skill_prune_backups` to keep only the newest N backups per skill
- `codex_skill_pin` for pin/unpin
- `codex_self_improvement_status` for plugin health (shows `last_curate_at`
  and the last report path)

If MCP is unavailable, run:

```bash
python3 ../../scripts/skill_manager_cli.py curate --dry-run
python3 ../../scripts/skill_manager_cli.py usage
python3 ../../scripts/skill_manager_cli.py status
```

Resolve script paths relative to this `SKILL.md` location.
