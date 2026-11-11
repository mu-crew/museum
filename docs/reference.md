# museum reference

The details behind [the README](../README.md): what each piece does, and the
edge cases. The decisions behind them are in [design.md](design.md).

## The sync

A pi extension runs `bin/museum-sync` from inside pi: when a session starts, at most every 10 minutes while agents work, and when a session ends. It uses rsync to copy `~/.pi/agent/sessions/*.jsonl` into the store:

```
<store>/<hostname>/<pi cwd folder>/<timestamp>_<session id>.jsonl
```

- **The store** is any ssh host plus folder (`host:/path`), or a local path.
- **Append-only:** the sync never passes `--delete`, so sessions you delete locally stay in the store.
- **No collisions:** each machine writes only its own `<hostname>/` folder, so nothing in the store needs locking or merging.
- **Compression is left to the filesystem:** session files are plain JSONL, and they compress to about a third of their size. Put the store on a filesystem that compresses, such as btrfs mounted with `compress-force=zstd:3`.
- **No daemon:** sessions only change while pi runs, so pi triggers the sync, in the pane's own environment (ssh agent, PATH). Agents search with `skills/museum/scripts/museum-search`, as the `museum` skill describes. A search writes two caches at the store's root, both safe to delete: `.museum-index.json`, the start dates it has read from sessions' first records, and `.museum-turns.sqlite`, an SQLite FTS5 index of user and assistant text, brought up to date by each `find` (see [Search](#search)).
- **Repo metadata:** at session start the extension adds a `museum` custom entry to the session (not sent to the model) with the git repo, remote, branch, commit and mu workstream, so a session in a throwaway mu worktree still names its project.
- **One sync per node:** ten agents finishing at once start one rsync. A kernel lock (`flock(2)`) serialises them; a session that ends mid-sync makes the running sync go round once more; the lock dies with its holder.
- **Failures show up in pi:** the footer says `museum: backup failing: <error>`, and says nothing while backups work. `museum-sync` writes the label into one `state.json` per node and every pi re-reads it every 5 seconds, so when one agent's sync fails or recovers, every agent's footer follows.

## Claude Code, Codex and cursor-agent

The same sync also backs up Claude Code's `~/.claude/projects/` (each session and its `<uuid>/subagents/`), Codex's `~/.codex/sessions/`, and cursor-agent's greppable transcripts under `~/.cursor/projects/*/agent-transcripts/` (not the live SQLite chats in `~/.cursor/chats/`). They land beside pi's folders:

```
<store>/<machine>/claude/<project folder>/<session id>.jsonl
<store>/<machine>/claude/<project folder>/<session id>/subagents/[workflows/<run>/]agent-<id>.jsonl
<store>/<machine>/codex/<project folder>/rollout-<timestamp>-<id>.jsonl
<store>/<machine>/cursor-agent/<project folder>/<session id>.jsonl
<store>/<machine>/<harness>/meta/<session id>.json
```

claude's project folder is its own (`-home-u-proj`). A codex rollout's date folders say nothing about the project, so its folder is the cwd from its first record, `session_meta`, spelled the same way. cursor-agent keeps Cursor's project folder name under `~/.cursor/projects/` as-is (`var-home-u-proj`). pi's layout does not move. `museum-search` reads these folders too (see [Search](#search)). Muse Code's session store is not known yet, so only its meta records are kept.

- **A filtered copy.** claude transcripts carry `api-request`, `api-request-blob` and `api-request-shape` records (the last holds the whole system prompt), `file-history-snapshot` records and full tool output; codex carries `encrypted_content` and a full `replacement_history` and `guardian_history` at every compaction, enough for one session to reach hundreds of MB; cursor-agent transcripts drop `turn_ended` lines and can still carry large tool inputs. museum keeps a filtered copy of each session under `~/.local/state/museum/stage/`, extended from a byte cursor (`stage.json`), and syncs that. The filter drops those records (every claude record type starting `api-request`) and fields, and cuts any tool result (or cursor-agent tool input) to 64 KB. A line still being written waits for the next sync. A file the filter fails on is skipped and named on stderr; the other files, and pi's sessions, still sync. A source that shrank is filtered again from scratch. Every source file is stat'ed on each sync, because a codex rollout can grow long after its date folder.
- **Append-only here too:** claude deletes old transcripts after `cleanupPeriodDays`; the store keeps them.
- **The meta record.** claude and codex record the cwd and branch in their transcripts, but the mu agent, role and workstream exist only in mu's anchor file or the agent's environment. `museum hook <harness>` on `SessionStart` writes them, with the session id and the cwd's git repo, remote, branch and commit, to `~/.local/state/museum/meta/<harness>/<session id>.json`, once per session. That path, and the flush stamp beside it, ignore `XDG_STATE_HOME`, because hooks and timers run with different environments. It reads the anchor (`~/.local/state/mu-next/anchors/<pid>.json`) of the nearest ancestor process whose start time matches, taking the role from its `role`, else its `orchestrator` flag. Without an anchor it falls back to `MU_AGENT_NAME` and `MU_WORKSTREAM`; the role is orchestrator for `MU_ORCHESTRATOR=1`, else worker for `MU_MANAGED_AGENT=1`. An anchor reached only past the session's own harness process, such as a lead's for a `claude -p` it started, is not the session's: that session gets no identity. museum never writes into another harness's transcript. Cursor's hook payload has `workspace_roots` instead of `cwd`; the meta record uses the first root when `cwd` is missing.
- **Triggered from outside every sandbox.** Harness hooks may run in a sandbox that refuses rsync and ssh, so they only leave stamps:
  - `museum tick` is a shell script that does nothing unless a sync is due: the last one started over 10 minutes ago, or a session ended. Then it starts `museum-sync`, detached. It is cheap enough for a tmux status line, `#(museum tick)`, which the tmux server runs outside any agent's sandbox.
  - `museum hook <harness>` on `SessionEnd` only leaves a flush stamp, so the next tick syncs the final turn without waiting out the 10 minutes.
  - A launchd agent (macOS) or systemd user timer (Linux) runs `museum tick` every 5 minutes, for a host with no tmux client attached. So a final turn reaches the store within 5 minutes of `SessionEnd`.
  - The sync runs the `rsync_path` pinned in the config, not whatever rsync the caller's `PATH` finds.

`museum init` writes each harness's entries (see [museum init](#museum-init)). Codex asks you to trust them once; its hidden title-generation session is ignored. cursor-agent takes `museum hook cursor-agent` in `~/.cursor/hooks.json` on `sessionStart` and `sessionEnd` (camelCase event names). Muse Code takes `museum hook muse`. Muse clears its hooks' environment, so they see neither `MU_*` nor `MUSEUM_STATE_DIR`, and the anchor is the only source of mu identity there. A hook never fails a turn: problems go to stderr, and it prints nothing on stdout. On a Linux host that should sync while you are logged out, run `loginctl enable-linger` so the user timer keeps running.

## museum init

The store uses rsync's syntax: `host:/path` goes over ssh, anything else is a local path. On the store host itself, use the local path rather than `localhost:`, which would need sshd and a key for your own account.

`museum init [STORE [MACHINE]] [--hooks LIST|all] [--no-hooks]` (`./install.sh` in a checkout runs the same):

1. writes `~/.config/museum/config.toml` on the first run (see [Configuration](#configuration)), and keeps it after. The machine name defaults to `hostname -s` and is fixed then, so a later hostname change keeps using the same folder. If `<store>/<machine>` already exists, or an ssh store host cannot be asked, it stops unless you pass the name explicitly. The config is checked by the sync's own rules before it is written. It creates a local store's folder; the sync never does
2. adds `museum hook <harness>` to the session start and end hooks in `~/.claude/settings.json` (`$CLAUDE_CONFIG_DIR`), `~/.codex/hooks.json` (`$CODEX_HOME`), `~/.cursor/hooks.json` (`sessionStart`/`sessionEnd`, beside `version: 1`) and `~/.config/muse/settings.json`, for each harness whose folder exists, or those `--hooks` names. It owns the handlers whose command runs museum (`museum …`, or a path to `museum` or `museum-sync`, which older setups wrote) and nothing else: one per event, the user's extra fields kept, every other museum handler removed. A file it cannot parse is named and left alone; a symlinked file is written through to its target; a file another program changed meanwhile is planned again
3. links `skills/museum` into `$CLAUDE_CONFIG_DIR/skills/museum` (else `~/.claude/skills/museum`) and `$CODEX_HOME/skills/museum` (else `~/.codex/skills/museum`) when those harnesses are set up, and into `~/.cursor/skills/museum` only when neither literal `~/.claude/skills/museum` nor `~/.codex/skills/museum` holds the skill (cursor-agent reads those too). A real directory already there is left alone. Muse Code takes it through `muse skills`
4. puts `museum` on PATH: npm's bin link does for a global install; a checkout links `~/.local/bin/museum`. A `~/.local/bin/museum` link to another museum folder (0.4.0's into pi's package copy, a hand-made one to a checkout) is removed, and it says when the `museum` on PATH is not this one
5. installs the pi package of this install's kind, `npm:@mu-crew/museum` for an npm install or the checkout itself, and removes every other museum package pi loads, which would load the extension twice
6. removes older installs' `~/.agents/skills/museum` and `~/.pi/agent/extensions/museum.ts` links, and the pre-0.2 `~/.cache/museum` folder
7. runs the first sync, and shows its error if it fails
8. installs the timer that runs `museum tick` every 5 minutes (`museum timer install`), afterwards, so the timer's first sync cannot hide the first sync's result

Run it again after an upgrade: each step leaves what is already right alone. The extension, and so the backup from inside pi, runs only in pi; the other harnesses use `museum hook` and the timer. pi's extension runs the `museum-sync` beside the `museum` on PATH (or, when that is a version manager's shim, the one `museum sync-path` names), so the global copy does every sync even when pi's package copy is another version.

## Search

```sh
S=skills/museum/scripts/museum-search
$S find stalwart hail     # sessions mentioning TERM, in store folders matching *hail*
$S show FILE --users      # a session's arc: its user turns, one line each
$S show FILE stalwart     # that session's turns about TERM, with neighbours
```

For an ssh store it runs on the store host in one ssh call. The `museum` skill tells agents how to use it.

`find` answers the conversation tier from `.museum-turns.sqlite`, a trigram FTS5 index of every user and assistant turn (about 7% of the store's size), and reads the session files only for sessions that mention the term in tool output alone, when the conversation matches leave room in the listing; otherwise the header says `tool output not searched`. After each sync to a local store, `museum-sync` runs `museum-search index`, under its lock, to add what the sync copied; on a store host that runs museum itself, that sync (at most every 10 minutes) also covers what the other machines copied in. Each `find` also adds whatever arrived since, so the index is never stale, and the first search or sync builds it (about a minute per 4 GB). An index failure goes to the sync's stderr, not the footer: it costs only a slower search. A term under three characters, SQLite older than 3.34, a store the search cannot write, or `MUSEUM_NO_INDEX=1` reads the files as before.

`find`, `show`, `stats` and `calls` read pi, Claude Code, Codex and cursor-agent sessions alike. The script walks the whole store and tells the formats apart by depth: `<machine>/<folder>/<file>` is pi's, and so is `<machine>/<file>`, from before pi kept a folder per cwd; `<machine>/claude/...`, `<machine>/codex/...` and `<machine>/cursor-agent/...` are the others'. A `PROJECT` matches their project folders too. Muse Code's folder is skipped until its format is known; `rg` on the store still sees it.

One normaliser per format maps each session to one stream of user and assistant messages, tool calls and results, timestamps and the model, and every command reads that stream:

- **Counted after merging.** claude writes each content block of a reply as its own line, under one `message.id`, and a parallel call's result can land between them: those lines are one message. codex writes a reply as several items (reasoning, text, calls) and a call's outcome as records sharing its `call_id`: the items between two user messages or tool outputs are one message, and each `call_id` is one call. cursor-agent's transcript lines are already one turn each. So the same conversation gives the same message and call counts across formats.
- **Tool names** are lowercased (`Bash` is `bash`), and every harness's shell tool is `bash` (codex's `exec_command`/`shell`, cursor-agent's `Shell`), so `--tool bash --match` works across harnesses. codex records how long a command took (`Wall time`) and its exit code, which give `durationMs` and the error flag; claude's and cursor-agent's calls are estimated from the gap.
- **`find` matches normalised text.** A user or assistant turn's text is conversation; claude's `isMeta` additions, codex's developer messages, the instructions codex adds as user messages (`<environment_context>`, `# AGENTS.md instructions`) and its `session_meta`, `turn_context` and `world_state` records count as the system prompt; codex's `event_msg` records and claude's queued prompts only repeat other lines, so they are skipped.
- **Metadata** for claude, codex and cursor-agent comes from the meta record `museum hook` wrote (`<machine>/<harness>/meta/<session id>.json`; a claude subagent uses its parent's), so `--role`, `--agent` and `--workstream` and the project shown by `find` work as for pi.

## Session timing and tool calls

```sh
S=skills/museum/scripts/museum-search
$S stats swayward --since 2026-10-19 --by 6h --slow 10
$S calls swayward --tool bash --match 'contrib/gate' --min-secs 30
$S calls swayward --match 'gate.*\.log' --group-by role
$S calls swayward --result 'gate: ok' --group-by session --json
```

`stats` reports model, tool and waiting time by role and time window;
`calls` lists the underlying tool calls or groups them by session, agent,
workstream or role. Both run on the store host through the same ssh path
as search. See `stats --help` and `calls --help` for shared filters.

Model time is the gap before an assistant message, not provider latency
alone. Tool wall time counts each gap before a tool result once. Model/tool
wall spans are split across UTC window boundaries. `--since`/`--until` select
session-start date cohorts, not event-time slices; selected sessions can
contribute to later windows. A session starts at the time in a pi file's name,
else at its first record's timestamp (a codex rollout's name is in local time).
`stats` also counts each role's user and assistant messages.

Call rows include `duration_source`: `durationMs` for recorded measurements,
`estimated` for call-to-result gaps (also marked in text output). Calls in one
assistant message share a request timestamp, not an execution start; fallback
gaps may include earlier calls. Their sum is not tool wall time. No concurrency
or sequential/parallel execution semantics are reported. Medians average the
two middle values for even counts; p90 uses sorted index floor(n × 0.9), capped
at the last value. Empty `stats --json` keeps `roles`, `trend` and `slow`.

Shell matching retains simple bash/sh heredoc bodies (including `env bash`),
but suppresses Python/data bodies. Background is a launch heuristic: unquoted
lone `&` with a redirect, unless followed by explicit `wait`; quoted operators
do not count. This is not a shell parser: wrappers, nested/multiple heredocs,
multiline quotes and conditional waits can be misclassified. Inspect later
log polls for background jobs. Matching text does not prove execution; use
`--tool bash`, `--exclude` and duration filters, then inspect examples.

## Configuration

`~/.config/museum/config.toml`:

```toml
machine = "macmini"          # this machine's folder in the store: <path>/macmini/
rsync_path = "/opt/homebrew/bin/rsync"  # pinned at install; left out, the sync looks on PATH

[store]
host = "devbox"              # set: an ssh store on devbox. Left out: a local store
path = "/data/museum"        # absolute, on whichever host holds the store
ssh_mux_only = false         # ssh only, see below
```

`host` alone decides between an ssh and a local store. A local store's folder must already exist: `museum init` creates it, and the sync only ever creates `<path>/<machine>/` inside it. So a forgotten `host` line fails with `store.path ... does not exist here (is store.host missing?)` instead of backing up to this machine. The file `museum init` writes explains all of this in comments.

Both scripts check the file strictly: an unknown key, a relative `path` or `rsync_path`, or `ssh_mux_only` without a `host` is an error, which the pi footer shows. `museum init` pins the first rsync 3 it finds as `rsync_path`, because a timer, the tmux server and harness hooks start the sync with a `PATH` of their own. A machine still on the old `KEY=value` `config` file is converted on its first sync, and the old file is kept as `config.old`.

`ssh_mux_only = true` is for an ssh store whose login needs a human, such as a security-key touch. The sync then runs only over an ssh ControlMaster that is already open, and never opens a new connection. If no master is running, it records `no ssh master for <host> (run: ssh -MNf <host>)` and skips, so the footer tells you what to run and nothing asks for your key. In this mode a failed sync retries once after 30 seconds, because a retry over the master costs no touch. Without the setting, a failed sync waits for the next one: a new connection might need a touch.

These environment variables are for tests and unusual layouts:

| Variable | Default | Effect |
| --- | --- | --- |
| `MUSEUM_INTERVAL` | `600` | Seconds between routine syncs |
| `MUSEUM_RETRY_DELAY` | `30` | Seconds before the one retry (`ssh_mux_only` only) |
| `MUSEUM_CONFIG` | `~/.config/museum/config.toml` | Config file |
| `MUSEUM_STATE_DIR` | `$XDG_STATE_HOME/museum`, else `~/.local/state/museum` | Lock, `pending`, `last-start` and `state.json`; also the hooks' meta records and flush stamp, which default to `~/.local/state/museum` |
| `PI_SESSIONS_DIR` | `~/.pi/agent/sessions` | pi's sessions |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | claude's sessions, in `projects/` |
| `CODEX_HOME` | `~/.codex` | codex's sessions, in `sessions/` |
| `CURSOR_HOME` | `~/.cursor` | cursor-agent transcripts, in `projects/*/agent-transcripts/` |
| `MUSEUM_TIMER_KIND` | `launchd` on macOS, else `systemd` | Which timer `museum timer` installs |
| `MUSEUM_SYNC` | `bin/museum-sync` beside the extension | Script the extension runs |

The extension and the script read the environment of the pi process, so set these where pi starts. The timer and `#(museum tick)` read the environment of launchd, systemd or the tmux server; `museum timer install` copies `XDG_STATE_HOME` and `MUSEUM_STATE_DIR` into the timer when they are set, so its syncs share pi's state folder, and `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `CURSOR_HOME` when they are set and not empty, so they back up the sessions those harnesses write. A status line's `#(museum tick)` gets none of these unless they are in tmux's global environment: if you move claude's, codex's or cursor's folder, also run `tmux set-environment -g CLAUDE_CONFIG_DIR "$CLAUDE_CONFIG_DIR"` (likewise `CODEX_HOME`, `CURSOR_HOME`), or set them before the tmux server starts.

## Versions

Each sync leaves a marker in the store, `<store>/<machine>/.museum/<version>.json` (`<version>+<commit>` from a checkout), with the version, commit, machine and the folder museum runs from. A marker is written once per version and never changes, because the copy appends; rsync keeps its mtime, so it says when the machine first synced with that version. `museum status` (and `museum-search machines`, which reaches an ssh store like a search) lists each machine folder's newest marker and newest session, and marks a machine `behind` when it reports an older release than the newest any machine reports. A machine that never reported runs museum before 0.4.0. A newest session that has stopped moving means the machine's syncs have stopped.

Releases: [RELEASING.md](../RELEASING.md). A `v*` tag publishes the package from CI.
