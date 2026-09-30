# museum

> "We talked about this before. What did we decide?"

That's easy to answer when one machine holds all your sessions. Across a fleet
of laptops, devservers and throwaway worktrees, the session you need is on a
host you've forgotten, or on one that no longer exists.

museum is a central, append-only backup of pi coding-agent sessions from every machine, searchable by agents with `bin/museum-search`.

## How it works

A pi extension runs `bin/museum-sync` from inside pi: when a session starts, at most every 10 minutes while agents work, and when a session ends. It uses rsync to copy `~/.pi/agent/sessions/*.jsonl` into the store:

```
<store>/<hostname>/<pi cwd folder>/<timestamp>_<session id>.jsonl
```

- **The store** is any ssh host plus folder (`host:/path`), or a local path.
- **Append-only:** the sync never passes `--delete`, so sessions you delete locally stay in the store.
- **No collisions:** each machine writes only its own `<hostname>/` folder, so nothing in the store needs locking or merging.
- **No daemon, no index:** sessions only change while pi runs, so pi triggers the sync, in the pane's own environment (ssh agent, PATH). Agents search the files directly with `bin/museum-search`, as the `museum` skill describes.
- **Repo metadata:** at session start the extension adds a `museum` custom entry to the session (not sent to the model) with the git repo, remote, branch, commit and mu workstream, so a session in a throwaway mu worktree still names its project.
- **One sync per node:** ten agents finishing at once start one rsync. A kernel lock (`flock(2)`) serialises them; a session that ends mid-sync makes the running sync go round once more; the lock dies with its holder.
- **Failures show up in pi:** the footer says `museum: backup failing: <error>` or `museum: no backup for 3d`, and says nothing while backups work.

## Install (per machine)

```sh
./install.sh devbox:/data/museum          # first time, ssh store: writes ~/.config/museum/config
./install.sh /data/museum                 # first time, local store (this machine is the store host)
./install.sh /Volumes/backup/museum       # local path on a mounted disk or network share
./install.sh devbox:/data/museum work-mbp # first time, with an explicit machine name
./install.sh                              # later runs: re-link the skill and the extension
```

The store uses rsync's syntax: `host:/path` goes over ssh, anything else is a local path. On the store host itself, use the local path rather than `localhost:`, which would need sshd and a key for your own account.

The install script:
1. writes `~/.config/museum/config` with `STORE=` and `NAME=`. `NAME` defaults to `hostname -s` and is fixed at install time, so a later hostname change keeps using the same folder. If `<store>/<NAME>` already exists, the install stops unless you pass `NAME` explicitly
2. symlinks `skills/museum` into `~/.agents/skills/`
3. symlinks `pi/museum.ts` into `~/.pi/agent/extensions/`. Running pi agents pick it up on restart or `/reload`
4. runs the first sync

## Check and sync by hand

```sh
cat ~/.cache/museum/state        # status=ok|error, time (epoch), message (last error)
bin/museum-sync                  # sync now, and print rsync or ssh errors
```

The pi footer shows the same state: nothing while backups work, `museum: backup failing: <error>` after a failed sync, `museum: no backup for 3d` when no sync has succeeded for over a day.

## Search

```sh
bin/museum-search find stalwart hail     # sessions mentioning TERM, in store folders matching *hail*
bin/museum-search show FILE stalwart     # that session's turns about TERM, with neighbours
```

For an ssh store it runs on the store host in one ssh call. The `museum` skill tells agents how to use it.

## Configuration

`~/.config/museum/config` holds `STORE=` and `NAME=`, and optionally `SSH_MUX_ONLY=1`.

`SSH_MUX_ONLY=1` is for an ssh store whose login needs a human, such as a security-key touch. The sync then runs only over an ssh ControlMaster that is already open, and never opens a new connection. If no master is running, it records `no ssh master for <host> (run: ssh -MNf <host>)` and skips, so the footer tells you what to run and nothing asks for your key. In this mode a failed sync retries once after 30 seconds, because a retry over the master costs no touch. Without the setting, a failed sync waits for the next one: a new connection might need a touch.

These environment variables are for tests and unusual layouts:

| Variable | Default | Effect |
| --- | --- | --- |
| `MUSEUM_INTERVAL` | `600` | Seconds between routine syncs |
| `MUSEUM_RETRY_DELAY` | `30` | Seconds before the one retry (`SSH_MUX_ONLY` only) |
| `MUSEUM_CONFIG` | `~/.config/museum/config` | Config file |
| `MUSEUM_STATE_DIR` | `~/.cache/museum` | Lock, `pending`, `last-start` and `state` |
| `PI_SESSIONS_DIR` | `~/.pi/agent/sessions` | What gets backed up |
| `MUSEUM_SYNC` | `bin/museum-sync` beside the extension | Script the extension runs |

The extension and the script read the environment of the pi process, so set these where pi starts.

## Requirements

- pi with extensions enabled. The sync runs only while pi runs.
- Python 3.9 or later on each client and on the store host, standard library only. macOS ships 3.9 with the Command Line Tools.
- rsync 3.x on the client. On macOS, `/usr/bin/rsync` is openrsync and lacks `--append-verify`, so install rsync with `brew install rsync`.
- rsync on the store host.
- Non-interactive ssh from each client to the store host (the scripts use `BatchMode=yes`). The sync runs in pi's environment, so a key held by your ssh agent works.
- `rg` on the store host is optional. `museum-search` uses it to pick matching files when it is there, and scans in Python when it is not.

## Back up the store

The store holds the only complete copy of every machine's sessions. Snapshot it separately, for example with restic or filesystem snapshots on the store host.

## Layout

```
bin/museum-sync      the rsync backup, one at a time per node
bin/museum-search    search the store: ranked triage, then one session's conversation
pi/museum.ts          pi extension: triggers the sync, warns in the footer
install.sh            per-machine setup
skills/museum/       agent skill: where the store is and how to search it
docs/                 design notes
test/                 unittest suite for bin/
```

## Develop

The `bin/` scripts are single-file, stdlib-only Python, so any system `python3` runs them with nothing installed. They are linted and formatted with ruff and type-checked with ty; `install.sh` and the git hook are shell, checked with shellcheck and shfmt. All four run through `uvx` at pinned versions, so you only need [uv](https://docs.astral.sh/uv/). Tests are stdlib `unittest` in `test/`, against a temporary local store. The same `make check` runs in CI, where the tests run again on Python 3.9.

```sh
make check   # lint (shellcheck, ruff, ty), tests, format check (shfmt, ruff)
make test    # tests only; PYTHON=... picks the interpreter
make fmt     # shfmt -w, ruff format, ruff check --fix
make hooks   # run make check on every commit that touches the code
```

Part of [mu-crew](https://github.com/mu-crew). Written mostly by AI coding agents, with a human reviewing what ships, and built for running them.
