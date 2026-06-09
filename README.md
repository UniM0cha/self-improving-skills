# Claude Self-Improving Skills

> **Hermes Agent-style self-improvement for Claude Code.**
>
> It turns hard-won workflow lessons into reusable `SKILL.md` files, validates skill edits, and curates stale knowledge so Claude Code can get better across sessions instead of starting from zero every time.

Claude Code already has hooks, subagents, slash commands, and skills. This plugin wires those primitives into a closed learning loop inspired by [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent):

```text
complex task → distill what worked → save/patch a skill → rediscover next session
```

## Why this exists

Most coding agents can solve a hard problem once. Fewer can reliably remember the reusable part of that work and apply it later.

Hermes Agent has first-class procedural memory through skills and a curator loop. This project ports that idea into Claude Code as a plugin:

- **Detect** complex work using transcript/tool-call signals.
- **Nudge** the agent at the right time to distill the reusable technique.
- **Write or patch** `~/.claude/skills/<name>/SKILL.md` through a dedicated subagent.
- **Validate and roll back** malformed skill edits automatically.
- **Track usage** so stale generated skills can be archived instead of piling up forever.

## Features

- **Automatic distillation nudge**: a `Stop` hook blocks once when enough tool work and file edits happened since the last distillation.
- **Dedicated distiller subagent**: prefers patching existing skills, then umbrella skills, then creating a new class-level skill only when it is truly reusable.
- **Skill edit safety**: pre-edit backups, post-edit validation, provenance stamping, and rollback on malformed `SKILL.md`.
- **Usage telemetry**: tracks skill use/view/patch counts in `~/.claude/self-improve/skill_usage.json`.
- **Curator loop**: marks old unused agent-created skills stale and archives them recoverably under `~/.claude/skills/.archive/`.
- **Manual commands**: `/distill-skill`, `/curate-skills`, `/curator-status`, `/prune-skills`, `/archive-skill`, `/pin-skill`, `/restore-skill`, `/propose-plugin-improvement`.
- **Fail-safe hooks**: hook errors approve the original action instead of breaking your Claude Code session.

## Install

Add this repository as a Claude Code plugin marketplace from inside Claude Code:

```text
/plugin marketplace add UniM0cha/claude-self-improving-skills
/plugin install self-improving-skills@claude-self-improving-skills
```

If your Claude Code version uses a different plugin command shape, add `https://github.com/UniM0cha/claude-self-improving-skills` as a marketplace from Claude Code's plugin UI and install the `self-improving-skills` plugin.

## Configuration

All configuration is optional. Set these in your shell or in `~/.claude/settings.json` under `env`.

| Variable | Default | Meaning |
|---|---:|---|
| `SIS_DISTILL_THRESHOLD` | `12` | Tool-call count since the last distillation before the nudge can trigger |
| `SIS_MIN_FILE_EDITS` | `2` | Minimum file edits since the last distillation; prevents pure research chats from triggering |
| `SIS_CURATE_MIN_SKILLS` | `8` | Minimum learned-skill count before automatic curation runs |
| `SIS_CURATE_INTERVAL_DAYS` | `7` | Automatic curator interval |
| `SIS_STALE_AFTER_DAYS` | `30` | Mark unused agent-created skills as stale after this many inactive days |
| `SIS_ARCHIVE_AFTER_DAYS` | `90` | Move unused agent-created skills to `.archive/` after this many inactive days |
| `SIS_PLUGIN_PR` | unset | Set to `1` to allow the opt-in upstream PR helper for this plugin's own source |

## How it works

```text
Claude Code session ends
  ↓
Stop hook parses the transcript and usage offsets
  ↓
If the work was complex and not yet distilled, it returns a one-time block
  ↓
Claude delegates to self-improving-skills:skill-distiller
  ↓
The distiller patches or creates a reusable SKILL.md under ~/.claude/skills
  ↓
Validation hooks check frontmatter/size/provenance and roll back bad edits
  ↓
Next session: Claude Code discovers the skill normally
```

The learned skills live in your user directory, not inside the plugin. Updating the plugin does not erase your accumulated procedural knowledge.

## Repository layout

```text
.claude-plugin/marketplace.json          # single-plugin Claude Code marketplace manifest
plugins/self-improving-skills/
  .claude-plugin/plugin.json             # plugin metadata
  hooks/                                 # Stop, SessionStart, PreToolUse, PostToolUse wrappers
  scripts/                               # transcript analysis, telemetry, curator, validator, PR helper
  agents/skill-distiller.md              # subagent prompt for skill distillation
  commands/                              # slash commands exposed by the plugin
  README.md                              # detailed design notes
```

## Honest limitations

- Claude Code does not provide Hermes Agent's free background daemon thread. Distillation uses a visible/billable subagent turn.
- This plugin handles procedural memory (`SKILL.md`), not factual memory. Claude Code's native memory features or separate memory plugins should handle facts about you or your projects.
- The curator is intentionally conservative: it archives only agent-created learned skills and keeps recoverable backups.

## Korean summary

Claude Code에 Hermes Agent식 자기개선 루프를 붙이는 플러그인입니다. 복잡한 작업이 끝나면 그 과정에서 재사용 가능한 절차를 `~/.claude/skills/<name>/SKILL.md`로 증류하고, 다음 세션에서 Claude Code가 그 스킬을 다시 발견해 활용하게 만듭니다. 검증·롤백·사용량 추적·오래된 스킬 아카이브까지 포함합니다.

## License

MIT
