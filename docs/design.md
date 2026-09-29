# Design notes

## Goal

The main goal is keeping every pi session safely backed up in one place. Search is secondary and rare, and an agent does it using a skill.

## Decisions

- **Store = plain folders under rsync.** Pi session files are append-only JSONL files with UUID names, so `rsync --append-verify` without `--delete` makes a correct, incremental, never-deleting copy.
- **Per-host folders instead of merging.** Each machine owns `<store>/<hostname>/`, so writes can never race. A project worked on from several machines appears under several host folders, and a glob such as `*/*project*` covers them all.
- **No index.** Around 1 GB and 1,600 sessions, `rg` over the store takes seconds, and the agent can rank and summarize results itself.
- **No daemon, no server-side binary.** The store host needs only ssh and rsync.
- **pi triggers the sync.** Sessions only change while pi runs. A pi extension syncs at `session_start` (catch-up, if due), `agent_settled` (if due, 10 minutes) and `session_shutdown` (now). It runs in the pane's environment, so ssh agents and forwarded keys work. An idle agent is still a pi process, so overnight work syncs too. Failures show in pi's footer. Lost: the tail of a crashed session on a host that is reclaimed before pi runs there again.
- **Concurrency lives in `museum-sync`, not the extension.** A node can run 10+ agents. The script takes a non-blocking kernel lock and exits if another sync holds it; the due check happens under the lock. A flush (`--now`) first writes a `pending` mark, and the holder loops until no mark is left, re-checking after it unlocks. The lock is `flock` (Linux) or `lockf` (macOS) on an fd, released on death; ssh/rsync run with the fd closed so an orphan cannot keep it; rsync and ssh timeouts bound a hung connection.
- **No automatic retry over a fresh ssh connection.** `BatchMode=yes` blocks password and passphrase prompts, but not a security-key agent asking for a touch. A retry could cost the user a touch, so a failed sync waits for the next due one. Sessions stay on local disk, and `--append-verify` resumes, so waiting loses nothing. One ssh connection per sync: the remote `mkdir` rides rsync's connection through `--rsync-path`.
- **`SSH_MUX_ONLY=1` for store hosts that need a human to log in.** The sync runs only over an existing ControlMaster, checked with `ssh -O check`, which never connects. With no master, it records `no ssh master for <host>` and skips, so the footer asks the user to open one. All ssh auth methods are also disabled, so a master that dies between the check and rsync fails instead of prompting. Here a retry costs nothing, so a failure retries once after `MUSEUM_RETRY_DELAY` (30s) while the master is up. Opting in is explicit because the script cannot tell ahead of time whether a fresh connection would prompt.

## Rejected or deferred

- **A central SQLite FTS index.** SQLite locking does not work across rsync copies or network filesystems. It would need `ssh host sessidx …`, which means a binary installed on the host.
- **Per-host index shards plus a merged cache on each client.** This works, but it adds complexity that occasional grep searches don't need.
- **Hashed file names or content-addressed chunks.** They aren't needed, because per-host folders already rule out collisions and readable paths make it easy to narrow a search.
- **Existing tools.** chronicle (git transport; rewrites session files) doesn't fit. adobe/pi-session-search (scans local files) is the fork candidate if raw `rg` and `jq` prove too clumsy for the agent.

## Possible later additions

- **A failure log.** `state` holds only the last result, so a failure that a later sync fixes leaves no trace.

- **Sidecar metadata** (git remote, branch, commit) recorded at session start, to group sessions from throwaway mu worktree paths under their real project.
- **An FTS index built on the store host,** if grep gets slow. The raw files remain the source of truth, so an index can be built at any time.
