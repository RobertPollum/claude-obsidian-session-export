---
name: export-chats
description: Set up, run, backfill, or troubleshoot exporting Claude Code chats to Obsidian. Use when the user wants to configure their vault folder, re-export sessions, or find out why a session has no note.
---

# Export Claude chats to Obsidian

Hooks export every session automatically. Use the script for setup, backfill and troubleshooting:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" --setup "<vault folder for chat notes>"
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" --dry-run
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh"                 # new or changed sessions
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" --force         # rebuild every note
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" --since 2026-11-01 --tag home-lab
```

## Setup

Config is per machine, in `~/.claude/obsidian-session-export/config.json`:

```json
{
  "vault_dir": "~/Documents/MyVault/Chats",
  "tool_output_max": 2000,
  "tag_rules": { "docker": { "paths": ["docker"], "keywords": ["docker-compose"] } }
}
```

1. If there is no config, ask the user for the vault folder. Do not guess it.
2. Run `--setup`, then `--dry-run`, show the result, then run the export.

## Troubleshooting

- Errors: `~/.claude/obsidian-session-export/state/hook-errors.log`
- Python 3.10+ is found automatically; set `OBSIDIAN_EXPORT_PYTHON` to force one.
- A missing vault parent folder stops the export instead of creating a new vault.
- Note names: `claude-{date}-{session-id}.md`, plus `-sub-{agent-id}` for subagents.
- Note bodies are rewritten on export. Tags, `status` and renames made in Obsidian are kept.
