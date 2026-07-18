# Codex Self Improvement

This plugin implements a Hermes-inspired learning loop for Codex:

- `PostToolUse` records tool telemetry, counts tool iterations toward the
  review trigger, and watches for skill edits that bypassed the skill manager
  (snapshot diff — a direct shell edit gets validated, its patch telemetry
  repaired, and a post-hoc checkpoint backup).
- `Stop` can optionally request a self-improvement continuation. The interval
  trigger counts accumulated tool iterations (default 10,
  `CODEX_SELF_IMPROVE_INTERVAL`) — not Stop turns — and real skill work
  (create/patch/write through the manager) resets the counter. The signal
  regex matches only explicit corrective phrasings ("next time", "다음부터",
  "틀렸" …), and a transcript signal is consumed once seen so the same old
  message never re-fires the review. `$skill-name` mentions in new transcript
  rows are attributed to that skill's use count.
- `SessionStart` injects a tiny status note, plus an interval-gated curator
  nudge: when the last curate pass is older than
  `CODEX_SELF_IMPROVE_CURATE_INTERVAL_DAYS` (default 7) and at least
  `CODEX_SELF_IMPROVE_CURATE_MIN_SKILLS` (default 8) skills are tracked, a
  dry-run curate runs in-process and a one-line candidate summary is injected.
  First sight only seeds the clock (never curates right after install).
- Skills guide review and curator workflows. The review skill follows the
  Hermes ladder: patch the skill that was in play > extend an existing skill >
  embed the corrected preference > create new (last resort). "Nothing to
  save." is a real option, but not the default.
- A local MCP/CLI skill manager creates backups, tracks usage (viewing a
  skill counts as a use — loading is behavioural intent), pins important
  skills, archives stale skills (timestamp-suffixed on collision), restores
  archived skills, lists/restores/prunes backups (rollback is itself
  undoable), and runs an advisory security scan (secrets / prompt injection /
  invisible unicode / local paths — findings never block a local write).
- New skills are provenance-stamped in frontmatter
  (`metadata.provenance: self-improving-skills`), so agent-created skills
  stay curation-eligible even if the usage sidecar is lost or the skill moves
  machines. Every curate run persists a report under the data dir's
  `logs/curator/<utc>/`.

The default Stop hook is conservative. It records review signals but only
auto-continues when enabled with:

```bash
export CODEX_SELF_IMPROVE_AUTO=1
```

State is stored in `PLUGIN_DATA` when Codex provides it, otherwise under
`~/.self-improving-skills`.

By default, new skills created through the manager are written to
`~/.codex/skills`. The manager can also read existing user skills from
`~/.agents/skills` for compatibility, and `CODEX_SELF_IMPROVE_CREATE_ROOT`
can override the create location.

## Read-before-write guard

Patching or overwriting an EXISTING skill file requires reading it first
(Hermes rule: edit what you actually saw, not what you remember):

- **MCP**: `codex_skill_view` registers the resolved file path; `codex_skill_patch`
  and `codex_skill_write_file` reject unviewed existing targets. Creating a
  new file is exempt. Unlike Hermes there is no background-review origin to
  scope the guard to (the Stop hook continues the same session), so the guard
  applies to the whole MCP session unconditionally.
- **CLI**: the process dies between calls, so the CLI approximates with the
  skill's `last_viewed_at` (within 30 minutes, skill-level). Humans can pass
  `--force-unviewed`.

## Environment variables

| variable | default | meaning |
|---|---|---|
| `CODEX_SELF_IMPROVE_AUTO` | off | enable Stop-hook auto-continue into the review pass |
| `CODEX_SELF_IMPROVE_INTERVAL` | `10` | tool iterations since the last review/skill-work that trigger the interval review (0 disables) |
| `CODEX_SELF_IMPROVE_CURATE_INTERVAL_DAYS` | `7` | days between SessionStart curator nudges |
| `CODEX_SELF_IMPROVE_CURATE_MIN_SKILLS` | `8` | tracked-skill count below which the curator nudge stays silent |
| `CODEX_SELF_IMPROVE_SKILL_ROOTS` | (auto) | override the skill root search list |
| `CODEX_SELF_IMPROVE_CREATE_ROOT` | `~/.codex/skills` | where new skills are created |

## MCP server startup contract

`.mcp.json` starts the skill manager with `cwd: "."` (the plugin root) and a
relative script path — the server resolves the plugin root from `PLUGIN_ROOT`
when Codex provides it, else from its own `__file__` location, so path
resolution survives either launch shape. `serverInfo.version` is read from
`.codex-plugin/plugin.json` at initialize time (a hardcoded version literal
drifted from the manifest once; never reintroduce one).

## Recommended reasoning effort

The review and curator passes inherit the Codex session's
`model_reasoning_effort` (`~/.codex/config.toml`; codex CLI 0.144+ accepts
`minimal`–`xhigh`, `max`, `ultra`):

- Day-to-day post-turn reviews: the default (`medium`) is enough.
- Large consolidation passes (`$codex-skill-curator` over many skills):
  `high`–`xhigh` is worth it.
- `ultra` is not just "more reasoning": it enables proactive multi-agent
  behaviour and can hit transient "Selected model is at capacity" errors —
  one retry usually clears them. Don't leave it on for routine passes.

Per-skill effort pinning in `agents/openai.yaml` is NOT supported by the
current schema (interface/dependencies/policy only) — don't add model fields
there.
