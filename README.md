# museum

> "We talked about this before. What did we decide?"

That's easy to answer when one machine holds all your sessions. Across a fleet
of laptops, devservers and throwaway worktrees, the session you need is on a
host you've forgotten, or on one that no longer exists.

museum is a central, append-only backup of agent sessions (pi, Claude Code and Codex) from every machine, searchable by agents with the `museum` skill and its `museum-search` script.

## How it works

A pi extension runs `bin/museum-sync` from inside pi: when a session starts, at most every 10 minutes while agents work, and when a session ends. It uses rsync to copy `~/.pi/agent/sessions/*.jsonl` into the store:

```
<store>/<hostname>/<pi cwd folder>/<timestamp>_<session id>.jsonl
```

- **The store** is any ssh host plus folder (`host:/path`), or a local path.
- **Append-only:** the sync never passes `--delete`, so sessions you delete locally stay in the store.
- **No collisions:** each machine writes only its own `<hostname>/` folder, so nothing in the store needs locking or merging.
- **Compression is left to the filesystem:** session files are plain JSONL, and they compress to about a third of their size. Put the store on a filesystem that compresses, such as btrfs mounted with `compress-force=zstd:3`.
- **No daemon, no search index:** sessions only change while pi runs, so pi triggers the sync, in the pane's own environment (ssh agent, PATH). Agents search the files directly with `skills/museum/scripts/museum-search`, as the `museum` skill describes. The one file a search writes is `.museum-index.json` at the store's root: the start dates it has read from sessions' first records, which never change.
- **Repo metadata:** at session start the extension adds a `museum` custom entry to the session (not sent to the model) with the git repo, remote, branch, commit and mu workstream, so a session in a throwaway mu worktree still names its project.
- **One sync per node:** ten agents finishing at once start one rsync. A kernel lock (`flock(2)`) serialises them; a session that ends mid-sync makes the running sync go round once more; the lock dies with its holder.
- **Failures show up in pi:** the footer says `museum: backup failing: <error>`, and says nothing while backups work. `museum-sync` writes the label into one `state.json` per node and every pi re-reads it every 5 seconds, so when one agent's sync fails or recovers, every agent's footer follows.

## Claude Code and Codex

The same sync also backs up Claude Code's `~/.claude/projects/` (each session and its `<uuid>/subagents/`) and Codex's `~/.codex/sessions/`, walked recursively. They land beside pi's folders:

```
<store>/<machine>/claude/<project folder>/<session id>.jsonl
<store>/<machine>/claude/<project folder>/<session id>/subagents/[workflows/<run>/]agent-<id>.jsonl
<store>/<machine>/codex/<project folder>/rollout-<timestamp>-<id>.jsonl
<store>/<machine>/<harness>/meta/<session id>.json
```

claude's project folder is its own (`-home-u-proj`). A codex rollout's date folders say nothing about the project, so its folder is the cwd from its first record, `session_meta`, spelled the same way. pi's layout does not move. `museum-search` reads these folders too (see [Search](#search)). cursor-agent is not backed up: its chats are live SQLite databases, unsafe to copy and not greppable. Muse Code's session store is not known yet, so only its meta records are kept.

- **A filtered copy.** claude transcripts carry `api-request`, `api-request-blob` and `api-request-shape` records (the last holds the whole system prompt), `file-history-snapshot` records and full tool output; codex carries `encrypted_content` and a full `replacement_history` and `guardian_history` at every compaction, enough for one session to reach hundreds of MB. museum keeps a filtered copy of each session under `~/.local/state/museum/stage/`, extended from a byte cursor (`stage.json`), and syncs that. The filter drops those records (every claude record type starting `api-request`) and fields, and cuts any tool result to 64 KB. A line still being written waits for the next sync. A file the filter fails on is skipped and named on stderr; the other files, and pi's sessions, still sync. A source that shrank is filtered again from scratch. Every source file is stat'ed on each sync, because a codex rollout can grow long after its date folder.
- **Append-only here too:** claude deletes old transcripts after `cleanupPeriodDays`; the store keeps them.
- **The meta record.** claude and codex record the cwd and branch in their transcripts, but the mu agent, role and workstream exist only in mu's anchor file or the agent's environment. `museum hook <harness>` on `SessionStart` writes them, with the session id and the cwd's git repo, remote, branch and commit, to `~/.local/state/museum/meta/<harness>/<session id>.json`, once per session. That path, and the flush stamp beside it, ignore `XDG_STATE_HOME`, because hooks and timers run with different environments. It reads the anchor (`~/.local/state/mu-next/anchors/<pid>.json`) of the nearest ancestor process whose start time matches, and falls back to `MU_AGENT_NAME`, `MU_WORKSTREAM` and `MU_ORCHESTRATOR`. An anchor reached only past the session's own harness process, such as a lead's for a `claude -p` it started, is not the session's: that session gets no identity. museum never writes into another harness's transcript.
- **Triggered from outside every sandbox.** Harness hooks may run in a sandbox that refuses rsync and ssh, so they only leave stamps:
  - `museum tick` is a shell script that does nothing unless a sync is due: the last one started over 10 minutes ago, or a session ended. Then it starts `museum-sync`, detached. It is cheap enough for a tmux status line, `#(museum tick)`, which the tmux server runs outside any agent's sandbox.
  - `museum hook <harness>` on `SessionEnd` only leaves a flush stamp, so the next tick syncs the final turn without waiting out the 10 minutes.
  - A launchd agent (macOS) or systemd user timer (Linux) runs `museum tick` every 5 minutes, for a host with no tmux client attached. So a final turn reaches the store within 5 minutes of `SessionEnd`.
  - The sync runs the `rsync_path` pinned in the config, not whatever rsync the caller's `PATH` finds.

To set it up, put `bin/museum` on your `PATH` (`ln -s "$PWD/bin/museum" ~/.local/bin/`), then:

```sh
museum timer install                          # install.sh does this too
tmux set -ag status-right '#(museum tick)'    # optional: sync from the status line too
```

and add `museum hook <harness>` to each harness's `SessionStart` and `SessionEnd` hooks. For Claude Code, in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "museum hook claude" }] }],
    "SessionEnd": [{ "hooks": [{ "type": "command", "command": "museum hook claude" }] }]
  }
}
```

Codex takes the same entries in `~/.codex/hooks.json`, as `museum hook codex`, and asks you to trust them once; its hidden title-generation session is ignored. Muse Code takes `museum hook muse`. Muse clears its hooks' environment, so they see neither `MU_*` nor `MUSEUM_STATE_DIR`, and the anchor is the only source of mu identity there. A hook never fails a turn: problems go to stderr, and it prints nothing on stdout. On a Linux host that should sync while you are logged out, run `loginctl enable-linger` so the user timer keeps running.

## Install (per machine)

```sh
./install.sh devbox:/data/museum          # first time, ssh store: writes ~/.config/museum/config.toml
./install.sh /data/museum                 # first time, local store (this machine is the store host)
./install.sh /Volumes/backup/museum       # local path on a mounted disk or network share
./install.sh devbox:/data/museum work-mbp # first time, with an explicit machine folder name
./install.sh                              # later runs: re-register the pi package, the timer, sync
```

The store uses rsync's syntax: `host:/path` goes over ssh, anything else is a local path. On the store host itself, use the local path rather than `localhost:`, which would need sshd and a key for your own account.

The install script:
1. writes `~/.config/museum/config.toml` (see [Configuration](#configuration)). The machine name defaults to `hostname -s` and is fixed at install time, so a later hostname change keeps using the same folder. If `<store>/<machine>` already exists, the install stops unless you pass the name explicitly
2. removes the symlinks older installs made, `~/.agents/skills/museum` and `~/.pi/agent/extensions/museum.ts`, so pi does not load museum twice
3. runs `pi install <this folder>` when pi is on `PATH`, which adds the repo to `packages` in `~/.pi/agent/settings.json`. pi loads the extension (`pi/museum.ts`) and the skill (`skills/museum`) from the repo, so edits are live. Running pi agents pick it up on restart or `/reload`
4. runs the first sync, and shows its error if it fails
5. installs the timer that runs `museum tick` every 5 minutes (`museum timer install`), afterwards, so the timer's first sync cannot hide the first sync's result

`pi list` shows the package, and `pi remove <this folder>` takes it out. The repo must stay where it was installed from: the package entry is its path. To use the skill with another agent CLI, link or copy `skills/museum` into that CLI's skills folder; the extension, and so the backup, runs only in pi.

## Check and sync by hand

```sh
cat ~/.local/state/museum/state.json  # status ok|error, time (epoch), message (last error), warning (footer label)
bin/museum-sync                  # sync now, and print rsync or ssh errors
bin/museum sync                  # the same
```

The pi footer shows the `warning` field of `state.json`, which the sync writes: nothing while backups work, `museum: backup failing: <error>` after a failed sync. pi adds `museum: no backup yet` before the first sync finishes, and `museum: cannot run museum-sync` when it cannot start the script.

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

## Search

```sh
S=skills/museum/scripts/museum-search
$S find stalwart hail     # sessions mentioning TERM, in store folders matching *hail*
$S show FILE --users      # a session's arc: its user turns, one line each
$S show FILE stalwart     # that session's turns about TERM, with neighbours
```

For an ssh store it runs on the store host in one ssh call. The `museum` skill tells agents how to use it.

`find`, `show`, `stats` and `calls` read pi, Claude Code and Codex sessions alike. The script walks the whole store and tells the formats apart by depth: `<machine>/<folder>/<file>` is pi's, and so is `<machine>/<file>`, from before pi kept a folder per cwd; `<machine>/claude/...` and `<machine>/codex/...` are the others'. A `PROJECT` matches their project folders too. Muse Code's folder is skipped until its format is known; `rg` on the store still sees it.

One normaliser per format maps each session to one stream of user and assistant messages, tool calls and results, timestamps and the model, and every command reads that stream:

- **Counted after merging.** claude writes each content block of a reply as its own line, under one `message.id`, and a parallel call's result can land between them: those lines are one message. codex writes a reply as several items (reasoning, text, calls) and a call's outcome as records sharing its `call_id`: the items between two user messages or tool outputs are one message, and each `call_id` is one call. So the same conversation gives the same message and call counts in all three formats.
- **Tool names** are lowercased (`Bash` is `bash`), and codex's shell tools (`exec_command`, `shell`) are `bash`, so `--tool bash --match` works across harnesses. codex records how long a command took (`Wall time`) and its exit code, which give `durationMs` and the error flag; claude's calls are estimated from the gap.
- **`find` matches normalised text.** A user or assistant turn's text is conversation; claude's `isMeta` additions, codex's developer messages, the instructions codex adds as user messages (`<environment_context>`, `# AGENTS.md instructions`) and its `session_meta`, `turn_context` and `world_state` records count as the system prompt; codex's `event_msg` records and claude's queued prompts only repeat other lines, so they are skipped.
- **Metadata** for claude and codex comes from the meta record `museum hook` wrote (`<machine>/<harness>/meta/<session id>.json`; a claude subagent uses its parent's), so `--role`, `--agent` and `--workstream` and the project shown by `find` work as for pi.

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

`host` alone decides between an ssh and a local store. A local store's folder must already exist: `install.sh` creates it, and the sync only ever creates `<path>/<machine>/` inside it. So a forgotten `host` line fails with `store.path ... does not exist here (is store.host missing?)` instead of backing up to this machine. The file `install.sh` writes explains all of this in comments.

Both scripts check the file strictly: an unknown key, a relative `path` or `rsync_path`, or `ssh_mux_only` without a `host` is an error, which the pi footer shows. `install.sh` pins the first rsync 3 it finds as `rsync_path`, because a timer, the tmux server and harness hooks start the sync with a `PATH` of their own. A machine still on the old `KEY=value` `config` file is converted on its first sync, and the old file is kept as `config.old`.

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
| `MUSEUM_TIMER_KIND` | `launchd` on macOS, else `systemd` | Which timer `museum timer` installs |
| `MUSEUM_SYNC` | `bin/museum-sync` beside the extension | Script the extension runs |

The extension and the script read the environment of the pi process, so set these where pi starts. The timer and `#(museum tick)` read the environment of launchd, systemd or the tmux server; `museum timer install` copies `XDG_STATE_HOME` and `MUSEUM_STATE_DIR` into the timer when they are set, so its syncs share pi's state folder.

## Requirements

- pi with extensions enabled, or the timer, or both. Without the timer, the sync runs only while pi runs.
- Python 3.11 or later on each client, standard library only, for `tomllib`. The Command Line Tools' `/usr/bin/python3` on macOS is 3.9, so install one with `brew install python`, which also puts it first on PATH. The store host needs `python3` of any recent version for searching.
- rsync 3.x on the client. On macOS, `/usr/bin/rsync` is openrsync and lacks `--append-verify`, so install rsync with `brew install rsync`.
- rsync on the store host.
- Non-interactive ssh from each client to the store host (the scripts use `BatchMode=yes`). The sync runs in pi's environment, so a key held by your ssh agent works.
- `rg` on the store host is optional. `museum-search` uses it to pick matching files when it is there, and scans in Python when it is not.

## Back up the store

The store holds the only complete copy of every machine's sessions. Snapshot it separately, for example with restic or filesystem snapshots on the store host.

## Layout

```
bin/museum           tick, hook, sync and timer: what tmux, timers and harness hooks call
bin/museum-sync      the rsync backup, one at a time per node; the filter; the hook
pi/museum.ts          pi extension: triggers the sync, shows its warning in the footer
install.sh            per-machine setup: config, pi package, timer, first sync
package.json          pi package manifest (the extension and the skill)
skills/museum/       agent skill: where the store is and how to search it
  scripts/museum-search  search the store: ranked triage, then one session's conversation
docs/                 design notes
test/                 unittest suite for both scripts
```

## Develop

`bin/museum-sync` and `skills/museum/scripts/museum-search` are single-file, stdlib-only Python, so any system `python3` runs them with nothing installed. They are linted and formatted with ruff and type-checked with ty; `install.sh`, `bin/museum` and the git hook are shell, checked with shellcheck and shfmt. All four run through `uvx` at pinned versions, so you only need [uv](https://docs.astral.sh/uv/). Tests are stdlib `unittest` in `test/`, against a temporary local store. The same `make check` runs in CI, where the tests run again on Python 3.11, the oldest supported.

```sh
make check   # lint (shellcheck, ruff, ty), tests, format check (shfmt, ruff)
make test    # tests only; PYTHON=... picks the interpreter
make fmt     # shfmt -w, ruff format, ruff check --fix
make hooks   # run make check on every commit that touches the code
```

Part of [mu-crew](https://github.com/mu-crew). Written mostly by AI coding agents, with a human reviewing what ships, and built for running them.
