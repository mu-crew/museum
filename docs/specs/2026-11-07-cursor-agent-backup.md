# cursor-agent sessions: stage transcripts and search them

Status: implemented (pending release).

Date: 2026-11-07

## Problem

Museum backs up and searches pi, Claude Code, and Codex sessions. cursor-agent
was left out because `~/.cursor/chats/**/store.db` is a live SQLite store
(WAL open, many blobs encrypted) — unsafe to rsync and not greppable. After
the multi-harness sync work, those sessions still never reach bubba.

Cursor already writes a parallel JSONL transcript at
`~/.cursor/projects/<project>/agent-transcripts/<id>/<id>.jsonl`. On
linuxpc that set is 1:1 with `store.db` sessions. Murmur hooks already
receive `transcript_path`. Museum should stage those transcripts (not the
DBs) and make them searchable like claude/codex.

## Approach

Extend the existing stage → rsync → normaliser path. No new sync transport.

1. **Sync/stage.** Treat cursor-agent like claude/codex: filtered copies under
   `~/.local/state/museum/stage/`, byte cursors in `stage.json`, same rsync.
2. **Search.** Walk `cursor-agent/` in the store; normalise transcript lines
   into pi's message shape so `find` / `show` / `stats` / `calls` work.
3. **Hooks.** `museum hook cursor-agent` on Cursor `sessionStart` /
   `sessionEnd` for meta + flush stamp (same contract as the other harnesses).

### Store layout

```
<store>/<machine>/cursor-agent/<project>/<session-id>.jsonl
<store>/<machine>/cursor-agent/meta/<session-id>.json
```

`<project>` is Cursor's folder name under `~/.cursor/projects/` as-is
(e.g. `var-home-martintrojer-dotfiles`). Do not rewrite toward pi/claude
path slugs.

### Source discovery

- Root: `Path(os.environ.get("CURSOR_HOME") or HOME / ".cursor") / "projects"`.
- `harness_sources` adds `Source("cursor-agent", that root)`.
- `stage()` still `rglob("*.jsonl")`.
- `destination()` returns a path only for
  `<project>/agent-transcripts/<id>/<id>.jsonl` →
  `cursor-agent/<project>/<id>.jsonl`. Any other jsonl under `projects/`
  is skipped (`None`).

Forward `CURSOR_HOME` from `museum timer install` when set and non-empty,
same pattern as `CLAUDE_CONFIG_DIR` / `CODEX_HOME`.

### Filter

- Drop lines with `role == "turn_ended"`.
- Keep `user` and `assistant` lines.
- Cap any single tool payload (tool_use `input`, or a tool_result-like
  content part if present) at the existing `CAP` (64 KiB), with the same
  cut marker text used for other harnesses.
- Incomplete trailing lines wait for the next sync; shrink → refilter from
  scratch; one bad file skips, others continue.

No system-prompt stripping: these transcripts have no system role lines in
the observed corpus. Do not read or copy `store.db`.

### Hooks

- Add `cursor-agent` to `HOOKED`.
- Event names are already lowercased in `hook()` (`sessionstart` /
  `sessionend`); Cursor's camelCase payloads match.
- Session id: payload `session_id` (same UUID as the transcript filename).
- Cwd for meta: Cursor's `sessionStart` has `workspace_roots` (array), not
  `cwd`. When writing meta for `cursor-agent`, use `cwd` if present, else the
  first string in `workspace_roots`, else `Path.cwd()` (existing fallback).
  Without that, git/project fields would record the hook process cwd.
- `transcript_path` may be null on `sessionStart`; discovery is by walking
  `projects/`, not by the hook path.
- README documents two entries in `~/.cursor/hooks.json` (`sessionStart`,
  `sessionEnd` → `museum hook cursor-agent`). Do not auto-edit the user's
  hooks file from install (same as claude/codex/muse).

### Search

- Add `cursor-agent` to `HARNESS_FOLDERS` / store walk (depth like claude:
  `<host>/cursor-agent/<project>/<id>.jsonl`; skip `meta/`).
- Normaliser: each kept JSONL line → pi `message` entries. Flatten
  `message.content[]` text parts into message text. Map `tool_use` to
  `toolCall` (tool name lowercased; Cursor's `Shell` counts as `bash`,
  matching the cross-harness shell rule).
- Start date: prefer meta / usable timestamps when cheap; otherwise file
  mtime is enough for v1. Parsing `<timestamp>` inside user_query is
  optional, not required.
- Skill + README: say cursor-agent is backed up and searchable; remove the
  line that says it is not (SQLite / unsafe to copy).

## Scope

**In**

- Stage + sync of agent-transcripts as above
- `museum-search` normaliser and store discovery
- Hook support + docs/skill updates
- Unit tests with fixtures (no live `~/.cursor` in CI)

**Out**

- Unpacking or syncing `~/.cursor/chats/**/store.db`
- Auto-wiring `~/.cursor/hooks.json`
- Muse Code session bodies (meta-only remains)
- Guaranteeing IDE-only chat formats that never write `agent-transcripts`

## Key decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Source | `agent-transcripts` JSONL | Already greppable; 1:1 with chats here; fits byte-cursor stage |
| Not source | `store.db` | Live WAL + encrypted blobs |
| Release cut | Sync and search together | Same as claude/codex 0.3.0; archive without search is half a product |
| Project folder | Cursor name as-is | Stable, searchable substring; matches "keep harness encoding" for claude |
| Harness folder | `cursor-agent` | Already the name in `HARNESSES` / murmur / hooks |
| Hook install | Document only | Matches other harnesses |

## Builder calls (not user-decided)

- Treat Cursor `Shell` tool_use as `bash` in the normaliser.
- Drop `turn_ended` in the filter (search noise; not conversation).
- Skip non-transcript jsonl under `projects/` via `destination()` → `None`.
- Start-date v1: meta or mtime; no hard requirement to parse embedded
  `<timestamp>` tags.
- Meta cwd from `workspace_roots[0]` when `cwd` is missing (Cursor payloads).

## Open questions

None that block implementation.

## Implementation checklist

- [x] `harness_sources` + `CURSOR_HOME`; `destination()` for cursor-agent
- [x] `filter_entry` / markers for cursor-agent (`turn_ended`, tool cap)
- [x] `HOOKED` + README hook snippet; timer forwards `CURSOR_HOME`
- [x] `museum-search`: folder walk + normaliser + start-date
- [x] Skill/README: backed up; delete "not backed up" SQLite sentence
- [x] Tests: destination, filter, hook, search fixture
- [x] Manual: one sync on linuxpc, confirm bubba `…/cursor-agent/…` and a
      `museum-search find` hit
