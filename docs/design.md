# Design notes

## Goal

The main goal is keeping every pi session safely backed up in one place. Search is secondary and rare, and an agent does it using a skill.

## Decisions

- **Store = plain folders under rsync.** Pi session files are append-only JSONL files with UUID names, so `rsync --append-verify` without `--delete` makes a correct, incremental, never-deleting copy.
- **Per-host folders instead of merging.** Each machine owns `<store>/<hostname>/`, so writes can never race. A project worked on from several machines appears under several host folders, and a glob such as `*/*project*` covers them all.
- **No index.** Around 1 GB and 1,600 sessions, `rg` over the store takes seconds, and the agent can rank and summarize results itself.
- **No daemon, no server-side binary.** The store host needs only ssh and rsync.

## Rejected or deferred

- **A central SQLite FTS index.** SQLite locking does not work across rsync copies or network filesystems. It would need `ssh host sessidx …`, which means a binary installed on the host.
- **Per-host index shards plus a merged cache on each client.** This works, but it adds complexity that occasional grep searches don't need.
- **Hashed file names or content-addressed chunks.** They aren't needed, because per-host folders already rule out collisions and readable paths make it easy to narrow a search.
- **Existing tools.** chronicle (git transport; rewrites session files) doesn't fit. adobe/pi-session-search (scans local files) is the fork candidate if raw `rg` and `jq` prove too clumsy for the agent.

## Possible later additions

- **Sidecar metadata** (git remote, branch, commit) recorded at session start, to group sessions from throwaway mu worktree paths under their real project.
- **A pi `session_shutdown` hook** that runs `museum-sync` right away, instead of waiting up to 10 minutes.
- **An FTS index built on the store host,** if grep gets slow. The raw files remain the source of truth, so an index can be built at any time.
