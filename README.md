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
npm i -g @mu-crew/museum
museum init devbox:/data/museum
pi install npm:@mu-crew/museum   # pi: museum's extension and skill
```

The argument is the store: `host:/path` over ssh, or `/path` for a local
store (use that on the store host itself). A second argument names this
machine's folder in the store; it defaults to `hostname -s`.

`museum init` writes `~/.config/museum/config.toml`, adds `museum hook
<harness>` to the session start hook of Claude Code, Codex, cursor-agent and
Muse Code where each is set up (`--hooks LIST|all` for others, `--no-hooks`
for none), links the skill into each harness, runs the first sync and
installs a timer (launchd or systemd) that checks every 5 minutes and syncs
what changed. It touches only museum's own entries, and removes what older
museum installs left behind. pi's package list it leaves to you, as `mu
init` does: it says when pi needs a `pi install` or a `pi remove`. The pi
package's extension is pi's hook, and shows `museum: backup failing` or
`museum: backup stale` in pi's footer when backups stop. Muse Code's skill
goes in by hand: `muse skills`. `museum doctor` checks the whole setup and
says how to fix what is wrong.

Optionally, `tmux set -ag status-right '#(museum tick)'` syncs from the
status line too. On Linux, `loginctl enable-linger` keeps the timer running
while you are logged out.

**Update** with `npm update -g @mu-crew/museum`, then `museum init` again;
`pi update --extensions` updates pi's copy. `museum status` shows what every
machine runs.

**Remove** with `museum timer remove`, `pi remove npm:@mu-crew/museum` and
`npm rm -g @mu-crew/museum`, then delete the `museum` entries from the
harnesses' hooks and their skills folders. The store is never touched.

**From a checkout**, for working on museum: `./install.sh STORE` in the clone
runs its `museum init`, which links `~/.local/bin/museum`; `pi install .`
there loads the checkout in pi, in place of the npm package, so edits are
live.

## Use

Agents search with the `museum` skill. By hand:

```sh
S=$(npm root -g)/@mu-crew/museum/skills/museum/scripts/museum-search
$S find stalwart hail          # sessions mentioning a term, in folders matching *hail*
$S show FILE --users           # one session's arc: its user turns
$S show FILE stalwart          # its turns about the term, with neighbours
$S stats hail --since 2026-10-01 --by 6h   # model, tool and wait time by role
$S calls hail --tool bash --min-secs 30    # the tool calls behind it

museum sync       # sync now, and show any error
museum version    # this machine's museum
museum status     # this machine's last sync, and every machine's museum
```

For an ssh store, `museum-search` runs on the store host in one ssh call.

## How it works

- **Store layout.** Each machine writes only its own folder, so nothing needs
  locking: `<store>/<machine>/<harness>/<project>/…` for every harness, pi,
  claude, codex and cursor-agent, with each session's metadata in
  `<harness>/meta/`. A machine's first sync with 0.6 moves its pi sessions
  from the old layout, `<machine>/<pi cwd folder>/`, into `pi/`.
- **Append-only.** rsync without `--delete`: sessions deleted locally, or by
  claude's cleanup, stay in the store.
- **Triggers.** Only `museum tick`, from the timer or tmux, starts a sync,
  outside every harness sandbox: at most every 10 minutes, and only when a
  session changed since the last sync that worked, so an idle machine makes
  no connections. Harness hooks, pi's extension among them, only record each
  session's repo and mu identity at its start. One sync runs per machine at
  a time.
- **Filtered copies.** claude, codex and cursor-agent transcripts carry bulk
  (API request copies, encrypted reasoning, repeated compaction histories);
  museum syncs a filtered copy, with tool output cut at 64 KB.
- **Search index.** `find` reads user and assistant text from
  `.museum-turns.sqlite`, an FTS5 index at the store's root that each search
  and each local sync keeps current; it is safe to delete.
- **Versions.** Each sync leaves `<machine>/.museum/<version>.json`, which
  `museum status` reads.
- **Compression** is left to the filesystem: put the store on btrfs with
  `compress-force=zstd:3`, which takes it to about a third.

The store is the only complete copy of every session: snapshot it, for
example with restic.

Details, configuration and environment variables:
[docs/reference.md](docs/reference.md). Decisions: [docs/design.md](docs/design.md).

## Develop

The scripts are single-file, stdlib-only Python (`install.sh` is sh). `make check`
runs shellcheck, ruff, ty, the unittest suite and the format checks through
`uvx` at pinned versions, so you need only [uv](https://docs.astral.sh/uv/).
`make hooks` runs it on every commit. Releases: [RELEASING.md](RELEASING.md).

Part of [mu-crew](https://github.com/mu-crew). Written mostly by AI coding
agents, with a human reviewing what ships, and built for running them.
