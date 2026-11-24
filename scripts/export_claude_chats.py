#!/usr/bin/env python3
"""
Export Claude Code transcripts to Markdown notes in an Obsidian vault. Notes contain prompts,
visible responses, tool calls with their results, and the thinking summaries
Claude Code saves when showThinkingSummaries is on.

Usage:
  python export_claude_chats.py --setup VAULT_DIR  # save this machine's vault folder
  python export_claude_chats.py                    # export everything new or changed
  python export_claude_chats.py --dry-run          # preview
  python export_claude_chats.py --since 2026-11-01 # only newer than date
  python export_claude_chats.py --force            # re-export even if up-to-date
  python export_claude_chats.py --tag home-lab     # force extra tags (repeatable)
  python export_claude_chats.py --quiet            # hide [SKIP] lines
  python export_claude_chats.py --hook             # hook mode: event JSON on stdin
"""
import argparse
import json
import os
import re
import socket
import sys
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
PROJECTS_DIR = CLAUDE_DIR / "projects"
# Outside the plugin, so updates never touch it.
DATA_DIR = CLAUDE_DIR / "obsidian-session-export"
CONFIG_FILE = DATA_DIR / "config.json"


def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


CONFIG = load_config()
_vault = os.environ.get("CLAUDE_CHATS_VAULT_DIR") or CONFIG.get("vault_dir") or ""
VAULT_DIR = Path(os.path.expandvars(os.path.expanduser(_vault))) if _vault else None
# One state file per session, so concurrent Claude sessions never race on an index.
STATE_DIR = Path(os.environ.get("CLAUDE_CHATS_STATE_DIR") or DATA_DIR / "state")
LOG_FILE = STATE_DIR / "hook-errors.log"

TOOL_OUTPUT_MAX = int(CONFIG.get("tool_output_max", 2000))
ARG_PREVIEW_MAX = 300


def vault_problem() -> str:
    """Why exporting cannot run, or ''. Never creates a vault that is not there."""
    if VAULT_DIR is None:
        return "no vault configured. Run /obsidian-session-export:export-chats to set it up."
    if not VAULT_DIR.parent.is_dir():
        return f"vault folder not found: {VAULT_DIR.parent}"
    return ""


def infer_tags(d: dict, forced: list) -> set:
    """Tags from config: {tag: {"paths": [...], "keywords": [...]}}; keywords match user text only."""
    root = d["project_root"].lower()
    text = " ".join([d["title"], *(t["text"] for t in d["user_turns"])]).lower()
    tags = set(forced)
    for tag, rule in (CONFIG.get("tag_rules") or {}).items():
        if any(h.lower() in root for h in rule.get("paths", [])) or \
                any(k.lower() in text for k in rule.get("keywords", [])):
            tags.add(tag)
    return tags


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def block_text(content) -> str:
    """Flatten message or tool-result content to text."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text":
            parts.append(b.get("text") or "")
        elif kind in ("image", "document"):
            parts.append(f"*[{kind} attached]*")
        elif kind == "tool_reference":
            parts.append(f"[tool: {b.get('tool_name', '')}]")
    return "\n".join(parts)


def user_prompt(row: dict) -> str:
    """Text the person actually entered, or '' for harness-injected rows."""
    if row.get("isMeta") or row.get("isCompactSummary"):
        return ""
    kind = (row.get("origin") or {}).get("kind")
    if kind not in (None, "human"):
        return ""
    text = block_text((row.get("message") or {}).get("content")).strip()
    if re.match(r"<command-(?:name|message)>", text):
        name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        line = " ".join(m.group(1).strip() for m in (name, args) if m).strip()
        return f"`{line}`" if line else ""
    shell = re.match(r"<bash-input>(.*?)</bash-input>", text, re.S)
    if shell:
        return f"`! {shell.group(1).strip()}`"
    if text.startswith(("<local-command-", "<task-notification>", "<bash-stdout>", "<agent-message",
                        "[Request interrupted by user")):
        return ""
    return text


def identify(path: Path) -> tuple[str, str]:
    """(session_id, agent_id) for a transcript; agent_id is '' for a main session."""
    if "subagents" in path.parts:
        session = next(p for p in path.parents if p.name == "subagents").parent.name
        return session, path.stem.removeprefix("agent-")
    return path.stem, ""


def start_ts(path: Path) -> str:
    """Timestamp of the first message in a transcript."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("type") in ("user", "assistant") and row.get("timestamp"):
                    return row["timestamp"]
    except OSError:
        pass
    return ""


def note_stem(d: dict) -> str:
    """claude-{YYYY-MM-DD}-{session-id}[-sub-{sub-agent-id}], dated by the session's start."""
    stem = f"claude-{local_time(d['session_ts']):%Y-%m-%d}-{d['session_id']}"
    return f"{stem}-sub-{d['agent_id']}" if d["agent_id"] else stem


def parse_transcript(path: Path, last_message: str = "") -> dict:
    session_id, agent_id = identify(path)
    turns: list[dict] = []
    calls: dict[str, dict] = {}
    usage: dict[str, dict] = {}
    seen: set[str] = set()
    by_message: dict[str, dict] = {}
    meta = {"model": "", "cwd": "", "branch": "", "ai_title": "", "entrypoint": ""}
    steps = 0
    first_ts = last_ts = ""

    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue  # partial last line while Claude is still writing
            if not isinstance(row, dict) or (row.get("isSidechain") and not agent_id):
                continue
            uid = row.get("uuid")
            if uid:
                if uid in seen:
                    continue
                seen.add(uid)

            rtype, ts = row.get("type"), row.get("timestamp") or ""
            msg = row.get("message") or {}
            if rtype == "ai-title":
                meta["ai_title"] = row.get("aiTitle") or meta["ai_title"]
                continue
            if rtype == "attachment":
                att = row.get("attachment") or {}
                # A prompt typed while Claude was still working
                if (att.get("type") == "queued_command" and att.get("commandMode") == "prompt"
                        and not att.get("isMeta")):
                    text = user_prompt({"origin": att.get("origin"),
                                        "message": {"content": att.get("prompt")}})
                    if text:
                        turns.append({"role": "user", "ts": ts, "text": text})
                continue
            if rtype not in ("user", "assistant"):
                continue

            steps += 1
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            meta["cwd"] = meta["cwd"] or row.get("cwd") or ""
            meta["branch"] = row.get("gitBranch") or meta["branch"]
            meta["entrypoint"] = meta["entrypoint"] or row.get("entrypoint") or ""

            if rtype == "user":
                content = msg.get("content")
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tc = calls.get(b.get("tool_use_id"))
                        if tc:
                            tc["output"] = block_text(b.get("content"))
                            tc["success"] = not b.get("is_error")
                            tc["end"] = ts
                text = user_prompt(row)
                if text:
                    turns.append({"role": "user", "ts": ts, "text": text})
                continue

            mid = msg.get("id") or uid
            model = msg.get("model") or ""
            if model and model != "<synthetic>":
                meta["model"] = model
                if msg.get("usage"):
                    usage[mid] = msg["usage"]  # repeated on every block of one message
            # One API message is stored as one row per content block.
            turn = by_message.get(mid)
            if turn is None:
                turn = by_message[mid] = {"role": "agent", "ts": ts, "text": "", "summary": "", "tool_calls": []}
                turns.append(turn)
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    turn["text"] = (turn["text"] + "\n\n" + b["text"].strip()).strip()
                elif b.get("type") == "thinking" and not agent_id and (b.get("thinking") or "").strip():
                    turn["summary"] = (turn["summary"] + "\n\n" + b["thinking"].strip()).strip()
                elif b.get("type") == "tool_use":
                    tc = {"name": b.get("name") or "", "args": b.get("input") or {},
                          "output": "", "success": None, "start": ts, "end": ""}
                    turn["tool_calls"].append(tc)
                    calls[b.get("id")] = tc

    turns = [t for t in turns if t["text"] or t.get("tool_calls") or t.get("summary")]
    # Stop can fire before the final message reaches the transcript file.
    last_message = (last_message or "").strip()
    if last_message and not any(last_message in t["text"] for t in turns if t["role"] == "agent"):
        turns.append({"role": "agent", "ts": "", "text": last_message, "tool_calls": []})

    prompts = [t for t in turns if t["role"] == "user"]
    # Prefer a real prompt over a slash command for the title.
    title_src = next((t["text"] for t in prompts if not t["text"].startswith("`")),
                     prompts[0]["text"] if prompts else "")
    title = next((l.strip()[:100] for l in title_src.splitlines() if l.strip()), "Untitled Conversation")

    info: dict = {}
    session_ts = first_ts
    if agent_id:
        try:
            info = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        title = info.get("description") or title
        # Date subagent notes by the parent session so both share a name prefix.
        root = next(p for p in path.parents if p.name == session_id)
        session_ts = start_ts(root.with_name(f"{session_id}.jsonl")) or first_ts

    return {
        "session_id": session_id, "title": title,
        "agent_id": agent_id, "agent_type": info.get("agentType") or "", "session_ts": session_ts,
        "project_root": meta["cwd"], "first_ts": first_ts, "last_ts": last_ts,
        "turns": turns, "user_turns": prompts, "step_count": steps,
        "total_tool_calls": len(calls),
        "input_tokens": sum((u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
                            for u in usage.values()),
        "cache_read_tokens": sum(u.get("cache_read_input_tokens") or 0 for u in usage.values()),
        "output_tokens": sum(u.get("output_tokens") or 0 for u in usage.values()),
        **{k: meta[k] for k in ("model", "branch", "ai_title", "entrypoint")},
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def local_time(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return datetime.now().astimezone()


def yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # valid YAML, including Windows backslashes


def fence(text: str, lang: str = "") -> list[str]:
    """Fence that stays closed even when the content itself contains backticks."""
    longest = max((len(m) for m in re.findall(r"`{3,}", text)), default=2)
    ticks = "`" * (longest + 1)
    return [ticks + lang, text, ticks]


def clean(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def clip(text: str, limit: int = ARG_PREVIEW_MAX) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def render_tool_call(tc: dict) -> list[str]:
    name, args = tc["name"], tc["args"] if isinstance(tc["args"], dict) else {}
    duration = ""
    if tc["start"] and tc["end"]:
        ms = (local_time(tc["end"]) - local_time(tc["start"])).total_seconds() * 1000
        duration = f" *({max(0, int(ms))}ms)*"
    status = {None: "", True: " ✓", False: " ✗"}[tc["success"]]
    lines = [f"#### `{name}`{duration}{status}", ""]

    if name in ("Bash", "PowerShell"):
        if args.get("description"):
            lines += [f"*{args['description']}*", ""]
        lines += fence(str(args.get("command", "")), "bash" if name == "Bash" else "powershell")
    elif name in ("Read", "Write", "Edit", "NotebookEdit"):
        lines.append(f"**File:** `{args.get('file_path') or args.get('notebook_path', '')}`")
        if name == "Edit":
            diff = [f"- {l}" for l in str(args.get("old_string", "")).splitlines()[:10]]
            diff += [f"+ {l}" for l in str(args.get("new_string", "")).splitlines()[:10]]
            if diff:
                lines += ["**Change:**", *fence("\n".join(diff), "diff")]
        elif name == "Write" and args.get("content"):
            lines += fence(clip(str(args["content"])))
    elif name in ("Grep", "Glob"):
        lines.append(f"**Pattern:** `{args.get('pattern', '')}` in `{args.get('path', '.')}`")
    elif name in ("WebSearch", "WebFetch"):
        lines.append(f"**Query/URL:** `{args.get('query') or args.get('url', '')}`")
    elif name == "Skill":
        lines.append(f"**Skill:** `{args.get('skill', '')}` {args.get('args', '')}".rstrip())
    elif args:
        lines += ["**Args:**", *fence(clip(json.dumps(args, ensure_ascii=False)), "json")]

    output = clean(tc["output"])
    if output:
        shown = output[:TOOL_OUTPUT_MAX]
        if len(output) > TOOL_OUTPUT_MAX:
            shown += f"\n... (truncated — {len(output)} chars total)"
        lines += ["", "**Output:**", *fence(shown)]
    elif tc["success"] is False:
        lines += ["", "**Output:** *(no output / failed)*"]
    return lines


def build_note(d: dict, tags: list[str], status: str) -> str:
    created = local_time(d["first_ts"])
    processed = datetime.now().strftime("%Y-%m-%d %H:%M")
    parent = note_stem(d | {"agent_id": ""})
    lines = [
        "---",
        f"title: {yaml_str(d['title'])}",
        "tags:",
        *[f"  - {t}" for t in tags],
        "source: claude",
        f"machine: {yaml_str(socket.gethostname())}",
        "origin: claude-code",
        f"created: {created:%Y-%m-%d}",
        f"updated: {local_time(d['last_ts']):%Y-%m-%d %H:%M}",
        f"processed: {processed}",
        f"status: {status}",
        "type: chat",
        f"session_id: {d['session_id']}",
        *([f"agent_id: {d['agent_id']}", f"agent_type: {yaml_str(d['agent_type'])}",
           f"parent: {yaml_str('[[' + parent + ']]')}"] if d["agent_id"] else []),
        f"ai_title: {yaml_str(d['ai_title'])}",
        f"model: {yaml_str(d['model'])}",
        "backend: Anthropic",
        f"entrypoint: {yaml_str(d['entrypoint'])}",
        f"project_root: {yaml_str(d['project_root'])}",
        f"git_branch: {yaml_str(d['branch'])}",
        f"steps: {d['step_count']}",
        f"user_prompts: {len(d['user_turns'])}",
        f"tool_calls: {d['total_tool_calls']}",
        f"input_tokens: {d['input_tokens']}",
        f"cache_read_tokens: {d['cache_read_tokens']}",
        f"output_tokens: {d['output_tokens']}",
        "---", "",
        f"# {d['title']}", "",
        f"**Session ID:** {d['session_id']}",
        f"**Date:** {created:%Y-%m-%d %H:%M}",
        f"**Project:** {d['project_root']}",
        f"**Parent:** [[{parent}]]" if d["agent_id"] else f"**Resume:** `claude --resume {d['session_id']}`",
        "", "---", "",
    ]
    user_label = "🧭 Parent agent" if d["agent_id"] else "👤 User"
    for t in d["turns"]:
        stamp = f" — {local_time(t['ts']):%Y-%m-%d %H:%M}" if t["ts"] else ""
        lines += [f"## {user_label if t['role'] == 'user' else '🤖 Claude'}{stamp}", ""]
        if t.get("summary"):
            lines += ["> [!note]- Reasoning summary", *(f"> {l}" for l in t["summary"].splitlines()), ""]
        if t["text"]:
            lines += [t["text"], ""]
        if t.get("tool_calls"):
            lines += ["### 🔧 Tool Calls", ""]
            for tc in t["tool_calls"]:
                lines += [*render_tool_call(tc), ""]
        lines += ["---", ""]
    lines.append(f"*Exported from Claude Code transcript on {processed}*")
    # Control bytes from raw tool output would make Obsidian treat the note as binary.
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", "\n".join(lines))


# ---------------------------------------------------------------------------
# Existing notes and state
# ---------------------------------------------------------------------------

def frontmatter(path: Path) -> dict:
    """Read the simple frontmatter this script writes (only the top of the file)."""
    out: dict = {"tags": []}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            if f.readline().strip() != "---":
                return out
            key = ""
            for _ in range(80):
                line = f.readline()
                if not line or line.strip() == "---":
                    break
                item = re.match(r"\s*-\s+(.+)", line)
                if item:
                    if key == "tags":
                        out["tags"].append(item.group(1).strip().strip("\"'"))
                elif ":" in line and not line[0].isspace():
                    key, val = (s.strip() for s in line.split(":", 1))
                    if key != "tags":
                        out[key] = val.strip("\"'")
    except OSError:
        pass
    return out


def load_state(session_id: str) -> dict:
    try:
        return json.loads((STATE_DIR / f"{session_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", errors="replace", newline="\n")
        os.replace(tmp, path)
    except PermissionError:  # target held open by OneDrive/Obsidian
        path.write_text(text, encoding="utf-8", errors="replace", newline="\n")
    finally:
        tmp.unlink(missing_ok=True)


def find_note(d: dict, state: dict) -> Path | None:
    def matches(p: Path) -> bool:
        fm = frontmatter(p)
        return fm.get("session_id") == d["session_id"] and fm.get("agent_id", "") == d["agent_id"]

    known = Path(state["file_path"]) if state.get("file_path") else None
    for p in (known, VAULT_DIR / f"{note_stem(d)}.md"):
        if p and p.is_relative_to(VAULT_DIR) and p.exists() and matches(p):
            return p
    if known and VAULT_DIR.exists():  # moved or renamed inside the vault folder
        for p in VAULT_DIR.rglob("*.md"):
            if "(conflict" not in p.name and matches(p):
                return p
    return None


def export_one(path: Path, dry_run=False, force=False, forced_tags=(), last_message="") -> str:
    session_id, agent_id = identify(path)
    key = f"{session_id}-sub-{agent_id}" if agent_id else session_id
    stat = path.stat()
    state = load_state(key)
    mark = [stat.st_mtime, stat.st_size]
    if (not force and not last_message and state.get("source") == mark
            and Path(state["file_path"]).is_relative_to(VAULT_DIR)):
        return f"[SKIP] Up-to-date: {Path(state['file_path']).name}"

    d = parse_transcript(path, last_message)
    if not d["user_turns"]:
        return f"[SKIP] No user messages: {path.name}"

    note = find_note(d, state)
    old = frontmatter(note) if note else {"tags": []}
    auto = infer_tags(d, list(forced_tags))
    # Keep tags and status edited by hand in Obsidian.
    base = {"claude-conversation", "ai-assistant"} | ({"claude-subagent"} if agent_id else set())
    tags = sorted(auto | base | set(old["tags"]))
    out = note or VAULT_DIR / f"{note_stem(d)}.md"
    # Migrate notes still using the original {topic-slug}_{id} name; leave hand-renamed ones.
    legacy = note is not None and not agent_id and note.name.endswith(f"_{session_id[:8]}.md")
    if legacy:
        out = note.with_name(f"{note_stem(d)}.md")
    info = f"{out.name} -- prompts={len(d['user_turns'])} tools={d['total_tool_calls']} tags={','.join(tags)}"
    if dry_run:
        return f"[DRY-RUN] Would {'update' if note else 'create'}: {info}"

    if legacy and not out.exists():
        os.replace(note, out)
    atomic_write(out, build_note(d, tags, old.get("status") or "imported"))
    atomic_write(STATE_DIR / f"{key}.json", json.dumps(
        {"session_id": session_id, "agent_id": agent_id, "title": d["title"], "file_path": str(out), "source": mark,
         "exported_at": datetime.now().astimezone().isoformat()}, indent=2))
    return f"[{'Updated' if note else 'Created'}] {info}"


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def subagent_transcripts(main: Path) -> list[Path]:
    return sorted(main.with_suffix("").glob("subagents/**/agent-*.jsonl"))


def all_transcripts() -> list[Path]:
    mains = sorted(PROJECTS_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p for m in mains for p in (m, *subagent_transcripts(m))]


def log_error(where: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {where}\n{traceback.format_exc()}\n")
    except OSError:
        pass


def run_hook() -> None:
    """Never fails, and prints only a setup warning: hook output could reach Claude."""
    event: dict = {}
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        name = event.get("hook_event_name")
        problem = vault_problem()
        if problem:
            if name == "SessionStart":  # shown to the user, not added to context
                print(json.dumps({"systemMessage": f"obsidian-session-export: {problem}"}))
            return
        if name == "SessionStart":
            paths = all_transcripts()  # catch up on crashed or interrupted sessions
        else:
            p = Path(event.get("transcript_path") or "")
            paths = [p, *subagent_transcripts(p)] if p.suffix == ".jsonl" and p.exists() else []
    except Exception:
        log_error("reading hook event")
        return
    last = (event.get("last_assistant_message") or "") if name == "Stop" else ""
    for p in paths:
        try:
            export_one(p, last_message="" if identify(p)[1] else last)
        except Exception:
            log_error(str(p))


def main() -> None:
    ap = argparse.ArgumentParser(description="Export Claude Code transcripts to Obsidian")
    ap.add_argument("--hook", action="store_true", help="Hook mode (event JSON on stdin)")
    ap.add_argument("--setup", metavar="VAULT_DIR", help="Save this machine's vault folder and exit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", default="", help="Only transcripts newer than YYYY-MM-DD")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()
    if args.hook:
        run_hook()
        return

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.setup:
        target = Path(os.path.expandvars(os.path.expanduser(args.setup)))
        if not target.parent.is_dir():
            sys.exit(f"Parent folder does not exist: {target.parent}")
        atomic_write(CONFIG_FILE, json.dumps(CONFIG | {"vault_dir": args.setup}, indent=2) + "\n")
        print(f"Saved {CONFIG_FILE}")
        return
    if vault_problem():
        sys.exit(f"Cannot export: {vault_problem()}")
    since = datetime.strptime(args.since, "%Y-%m-%d").timestamp() if args.since else 0
    counts: dict[str, int] = {}
    for p in all_transcripts():
        if p.stat().st_mtime < since:
            continue
        try:
            result = export_one(p, args.dry_run, args.force, args.tag)
        except Exception as e:
            result = f"[FAILED] {p.name}: {e!r}"
        kind = result[1:result.index("]")]
        counts[kind] = counts.get(kind, 0) + 1
        if not (args.quiet and kind == "SKIP"):
            print(result)
    print("  ".join(f"{k}: {v}" for k, v in counts.items()) or "No transcripts found")


if __name__ == "__main__":
    main()
