# museum

> "We talked about this before. What did we decide?"

That's easy to answer when one machine holds all your sessions. Across a fleet
of laptops, devservers and throwaway worktrees, the session you need is on a
host you've forgotten, or on one that no longer exists.

museum is a central, append-only backup of agent sessions (pi, Claude Code,
Codex and cursor-agent) from every machine, searchable by agents with the
`museum` skill.

## Install

You need, on each machine: Python 3.11+ (`brew install python` on macOS),
rsync 3 (`brew install rsync`; macOS's own is openrsync), and non-interactive
ssh to the store host for an ssh store. The store host needs rsync and any
`python3`; `rg` there makes search faster.

```sh
pi install npm:@mu-crew/museum
~/.pi/agent/npm/node_modules/@mu-crew/museum/install.sh devbox:/data/museum
```

The argument is the store: `host:/path` over ssh, or `/path` for a local
store (use that on the store host itself). A second argument names this
machine's folder in the store; it defaults to `hostname -s`.

`install.sh` writes `~/.config/museum/config.toml`, links `museum` into
`~/.local/bin` and the skill into each harness's skills folder (claude, codex,
cursor), runs the first sync, and installs a timer (launchd or systemd) that
syncs every 5 minutes. pi itself loads the extension, which syncs as agents
work and shows `museum: backup failing: …` in the footer when a sync fails.
Run `museum setup` to redo these steps later.

For Claude Code, Codex, cursor-agent and Muse Code, add `museum hook
<harness>` to their session start and end hooks, so each session gets its mu
identity and its final turn is synced. In `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "museum hook claude" }] }],
    "SessionEnd": [{ "hooks": [{ "type": "command", "command": "museum hook claude" }] }]
  }
}
```

Codex: the same in `~/.codex/hooks.json` with `museum hook codex`.
cursor-agent: `museum hook cursor-agent` on `sessionStart` and `sessionEnd` in
`~/.cursor/hooks.json`. Muse Code: `museum hook muse`, and add the skill
with `muse skills`. Optionally, `tmux set -ag status-right '#(museum tick)'`
syncs from the status line too. On Linux, `loginctl enable-linger` keeps the
timer running while you are logged out.

**Update** with `pi update --extensions` (or `pi update
npm:@mu-crew/museum`). Paths stay the same across updates; the
[CHANGELOG](CHANGELOG.md) says when a release needs `museum setup` again.
`museum fleet` shows what every machine runs.

**Remove** with `museum timer remove` and `pi remove npm:@mu-crew/museum`,
then delete `~/.local/bin/museum` and the `museum` links in the harnesses'
skills folders. The store is never touched.

**From a checkout**, for working on museum: `./install.sh STORE` in the clone
makes pi load the checkout, so edits are live. Keep only one of the two;
`install.sh` warns when pi loads both.

## Use

Agents search with the `museum` skill. By hand:

```sh
S=~/.pi/agent/npm/node_modules/@mu-crew/museum/skills/museum/scripts/museum-search
$S find stalwart hail          # sessions mentioning a term, in folders matching *hail*
$S show FILE --users           # one session's arc: its user turns
$S show FILE stalwart          # its turns about the term, with neighbours
$S stats hail --since 2026-10-01 --by 6h   # model, tool and wait time by role
$S calls hail --tool bash --min-secs 30    # the tool calls behind it

museum sync       # sync now, and show any error
museum version    # this machine's museum
museum fleet      # every machine's museum version and newest session
cat ~/.local/state/museum/state.json   # the last sync's result
```

For an ssh store, `museum-search` runs on the store host in one ssh call.

## How it works

- **Store layout.** Each machine writes only its own folder, so nothing needs
  locking: `<store>/<machine>/<pi cwd folder>/<time>_<id>.jsonl` for pi, and
  `<store>/<machine>/{claude,codex,cursor-agent}/<project>/…` for the others.
- **Append-only.** rsync without `--delete`: sessions deleted locally, or by
  claude's cleanup, stay in the store.
- **Triggers.** pi's extension syncs at session start, at most every 10
  minutes while agents work, and at session end. Harness hooks only leave
  stamps, since they may run in a sandbox; the timer and `museum tick` start
  the sync outside it. One sync runs per machine at a time.
- **Filtered copies.** claude, codex and cursor-agent transcripts carry bulk
  (API request copies, encrypted reasoning, repeated compaction histories);
  museum syncs a filtered copy, with tool output cut at 64 KB.
- **Search index.** `find` reads user and assistant text from
  `.museum-turns.sqlite`, an FTS5 index at the store's root that each search
  and each local sync keeps current; it is safe to delete.
- **Versions.** Each sync leaves `<machine>/.museum/<version>.json`, which
  `museum fleet` reads.
- **Compression** is left to the filesystem: put the store on btrfs with
  `compress-force=zstd:3`, which takes it to about a third.

The store is the only complete copy of every session: snapshot it, for
example with restic.

Details, configuration and environment variables:
[docs/reference.md](docs/reference.md). Decisions: [docs/design.md](docs/design.md).

## Develop

The scripts are single-file, stdlib-only Python and POSIX shell. `make check`
runs shellcheck, ruff, ty, the unittest suite and the format checks through
`uvx` at pinned versions, so you need only [uv](https://docs.astral.sh/uv/).
`make hooks` runs it on every commit. Releases: [RELEASING.md](RELEASING.md).

Part of [mu-crew](https://github.com/mu-crew). Written mostly by AI coding
agents, with a human reviewing what ships, and built for running them.
