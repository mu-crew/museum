# museum

Central, append-only backup of pi coding-agent sessions from every machine, searchable by agents through plain `rg` and `jq`.

## How it works

Each machine runs `bin/museum-sync` every 10 minutes. It uses rsync to copy `~/.pi/agent/sessions/*.jsonl` into the store:

```
<store>/<hostname>/<pi cwd folder>/<timestamp>_<session id>.jsonl
```

- **The store** is any ssh host plus folder (`host:/path`), or a local path.
- **Append-only:** the sync never passes `--delete`, so sessions you delete locally stay in the store.
- **No collisions:** each machine writes only its own `<hostname>/` folder, so there is nothing to lock or merge.
- **No daemon, no index:** agents search the files directly using the `museum` skill.

## Install (per machine)

```sh
./install.sh devbox:/data/museum   # first time: writes ~/.config/museum/config
./install.sh                        # later runs: re-link the skill and reload the schedule
```

The install script:
1. writes `~/.config/museum/config` (`STORE=`, and optionally `NAME=` to override the hostname)
2. symlinks `skills/museum` into `~/.agents/skills/`
3. installs the schedule: a launchd agent on macOS, a cron entry on Linux
4. runs the first sync

## Requirements

- rsync 3.x on the client. On macOS, `/usr/bin/rsync` is openrsync and lacks `--append-verify`, so install rsync with `brew install rsync`.
- rsync on the store host.
- Non-interactive ssh from each client to the store host (the scripts use `BatchMode=yes`).
- `rg` and `jq` for searching. The skill falls back to `grep` on hosts without `rg`.

## Back up the store

The store holds the only complete copy of every machine's sessions. Snapshot it separately, for example with restic or filesystem snapshots on the store host.

## Layout

```
bin/museum-sync      the rsync backup
install.sh            per-machine setup
launchd/              macOS schedule template
skills/museum/       agent skill: where the store is and how to search it
docs/                 design notes
```

Part of [mu-crew](https://github.com/mu-crew). Written mostly by AI coding agents, with a human reviewing what ships, and built for running them.
