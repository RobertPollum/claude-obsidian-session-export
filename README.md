# claude-obsidian-session-export

Claude Code plugin that writes each local session, and each subagent, to a Markdown note in an Obsidian vault.

## Install on a machine

Needs Python 3.10+. On Windows, also Git for Windows (the hooks run through `sh`).

```sh
claude plugin marketplace add RobertPollum/claude-obsidian-session-export
claude plugin install obsidian-session-export@robert-tools
```

Start Claude Code and run `/obsidian-session-export:export-chats` to set the vault folder. To reuse your tag rules, copy `tag_rules` from another machine's `~/.claude/obsidian-session-export/config.json`.

## Update

```sh
claude plugin marketplace update robert-tools
claude plugin update obsidian-session-export@robert-tools
```

Bump `version` in `.claude-plugin/plugin.json` with every release.

## Notes

- Only sessions on machines with the plugin are exported. Cloud sessions are not.
- Commands and their output go into the notes unredacted, up to `tool_output_max` characters each.
- Config and state stay on each machine. Session IDs are unique, so machines can share one synced vault.
