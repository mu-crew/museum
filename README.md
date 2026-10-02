# museum

> "We talked about this before. What did we decide?"

That's easy to answer when one machine holds all your sessions. Across a fleet
of laptops, devservers and throwaway worktrees, the session you need is on a
host you've forgotten, or on one that no longer exists.

museum is a central, append-only backup of pi coding-agent sessions from every machine, searchable by agents with the `museum` skill and its `museum-search` script.

## How it works

A pi extension runs `bin/museum-sync` from inside pi: when a session starts, at most every 10 minutes while agents work, and when a session ends. It uses rsync to copy `~/.pi/agent/sessions/*.jsonl` into the store:

```
<store>/<hostname>/<pi cwd folder>/<timestamp>_<session id>.jsonl
```

- **The store** is any ssh host plus folder (`host:/path`), or a local path.
- **Append-only:** the sync never passes `--delete`, so sessions you delete locally stay in the store.
- **No collisions:** each machine writes only its own `<hostname>/` folder, so nothing in the store needs locking or merging.
- **Compression is left to the filesystem:** session files are plain JSONL, and they compress to about a third of their size. Put the store on a filesystem that compresses, such as btrfs mounted with `compress-force=zstd:3`.
- **No daemon, no index:** sessions only change while pi runs, so pi triggers the sync, in the pane's own environment (ssh agent, PATH). Agents search the files directly with `skills/museum/scripts/museum-search`, as the `museum` skill describes.
- **Repo metadata:** at session start the extension adds a `museum` custom entry to the session (not sent to the model) with the git repo, remote, branch, commit and mu workstream, so a session in a throwaway mu worktree still names its project.
- **One sync per node:** ten agents finishing at once start one rsync. A kernel lock (`flock(2)`) serialises them; a session that ends mid-sync makes the running sync go round once more; the lock dies with its holder.
- **Failures show up in pi:** the footer says `museum: backup failing: <error>`, and says nothing while backups work. `museum-sync` writes the label into one `state.json` per node and every pi re-reads it every 5 seconds, so when one agent's sync fails or recovers, every agent's footer follows.

## Install (per machine)

```sh
./install.sh devbox:/data/museum          # first time, ssh store: writes ~/.config/museum/config.toml
./install.sh /data/museum                 # first time, local store (this machine is the store host)
./install.sh /Volumes/backup/museum       # local path on a mounted disk or network share
./install.sh devbox:/data/museum work-mbp # first time, with an explicit machine folder name
./install.sh                              # later runs: re-link the skill and the extension
```

The store uses rsync's syntax: `host:/path` goes over ssh, anything else is a local path. On the store host itself, use the local path rather than `localhost:`, which would need sshd and a key for your own account.

The install script:
1. writes `~/.config/museum/config.toml` (see [Configuration](#configuration)). The machine name defaults to `hostname -s` and is fixed at install time, so a later hostname change keeps using the same folder. If `<store>/<machine>` already exists, the install stops unless you pass the name explicitly
2. symlinks `skills/museum` into `~/.agents/skills/`
3. symlinks `pi/museum.ts` into `~/.pi/agent/extensions/`. Running pi agents pick it up on restart or `/reload`
4. runs the first sync

## Check and sync by hand

```sh
cat ~/.local/state/museum/state.json  # status ok|error, time (epoch), message (last error), warning (footer label)
bin/museum-sync                  # sync now, and print rsync or ssh errors
```

The pi footer shows the `warning` field of `state.json`, which the sync writes: nothing while backups work, `museum: backup failing: <error>` after a failed sync. pi adds `museum: no backup yet` before the first sync finishes, and `museum: cannot run museum-sync` when it cannot start the script.

## Search

```sh
S=skills/museum/scripts/museum-search
$S find stalwart hail     # sessions mentioning TERM, in store folders matching *hail*
$S show FILE --users      # a session's arc: its user turns, one line each
$S show FILE stalwart     # that session's turns about TERM, with neighbours
```

For an ssh store it runs on the store host in one ssh call. The `museum` skill tells agents how to use it.

## Configuration

`~/.config/museum/config.toml`:

```toml
machine = "macmini"          # this machine's folder in the store: <path>/macmini/

[store]
host = "devbox"              # set: an ssh store on devbox. Left out: a local store
path = "/data/museum"        # absolute, on whichever host holds the store
ssh_mux_only = false         # ssh only, see below
```

`host` alone decides between an ssh and a local store. A local store's folder must already exist: `install.sh` creates it, and the sync only ever creates `<path>/<machine>/` inside it. So a forgotten `host` line fails with `store.path ... does not exist here (is store.host missing?)` instead of backing up to this machine. The file `install.sh` writes explains all of this in comments.

Both scripts check the file strictly: an unknown key, a relative `path` or `ssh_mux_only` without a `host` is an error, which the pi footer shows. A machine still on the old `KEY=value` `config` file is converted on its first sync, and the old file is kept as `config.old`.

`ssh_mux_only = true` is for an ssh store whose login needs a human, such as a security-key touch. The sync then runs only over an ssh ControlMaster that is already open, and never opens a new connection. If no master is running, it records `no ssh master for <host> (run: ssh -MNf <host>)` and skips, so the footer tells you what to run and nothing asks for your key. In this mode a failed sync retries once after 30 seconds, because a retry over the master costs no touch. Without the setting, a failed sync waits for the next one: a new connection might need a touch.

These environment variables are for tests and unusual layouts:

| Variable | Default | Effect |
| --- | --- | --- |
| `MUSEUM_INTERVAL` | `600` | Seconds between routine syncs |
| `MUSEUM_RETRY_DELAY` | `30` | Seconds before the one retry (`ssh_mux_only` only) |
| `MUSEUM_CONFIG` | `~/.config/museum/config.toml` | Config file |
| `MUSEUM_STATE_DIR` | `$XDG_STATE_HOME/museum`, else `~/.local/state/museum` | Lock, `pending`, `last-start` and `state.json` |
| `PI_SESSIONS_DIR` | `~/.pi/agent/sessions` | What gets backed up |
| `MUSEUM_SYNC` | `bin/museum-sync` beside the extension | Script the extension runs |

The extension and the script read the environment of the pi process, so set these where pi starts.

## Requirements

- pi with extensions enabled. The sync runs only while pi runs.
- Python 3.11 or later on each client, standard library only, for `tomllib`. The Command Line Tools' `/usr/bin/python3` on macOS is 3.9, so install one with `brew install python`, which also puts it first on PATH. The store host needs `python3` of any recent version for searching.
- rsync 3.x on the client. On macOS, `/usr/bin/rsync` is openrsync and lacks `--append-verify`, so install rsync with `brew install rsync`.
- rsync on the store host.
- Non-interactive ssh from each client to the store host (the scripts use `BatchMode=yes`). The sync runs in pi's environment, so a key held by your ssh agent works.
- `rg` on the store host is optional. `museum-search` uses it to pick matching files when it is there, and scans in Python when it is not.

## Back up the store

The store holds the only complete copy of every machine's sessions. Snapshot it separately, for example with restic or filesystem snapshots on the store host.

## Layout

```
bin/museum-sync      the rsync backup, one at a time per node
pi/museum.ts          pi extension: triggers the sync, shows its warning in the footer
install.sh            per-machine setup
skills/museum/       agent skill: where the store is and how to search it
  scripts/museum-search  search the store: ranked triage, then one session's conversation
docs/                 design notes
test/                 unittest suite for both scripts
```

## Develop

`bin/museum-sync` and `skills/museum/scripts/museum-search` are single-file, stdlib-only Python, so any system `python3` runs them with nothing installed. They are linted and formatted with ruff and type-checked with ty; `install.sh` and the git hook are shell, checked with shellcheck and shfmt. All four run through `uvx` at pinned versions, so you only need [uv](https://docs.astral.sh/uv/). Tests are stdlib `unittest` in `test/`, against a temporary local store. The same `make check` runs in CI, where the tests run again on Python 3.11, the oldest supported.

```sh
make check   # lint (shellcheck, ruff, ty), tests, format check (shfmt, ruff)
make test    # tests only; PYTHON=... picks the interpreter
make fmt     # shfmt -w, ruff format, ruff check --fix
make hooks   # run make check on every commit that touches the code
```

Part of [mu-crew](https://github.com/mu-crew). Written mostly by AI coding agents, with a human reviewing what ships, and built for running them.
