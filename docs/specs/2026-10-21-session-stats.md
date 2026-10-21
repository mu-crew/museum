# `museum-search stats` and `calls`: where agent time goes

Status: approved design, not implemented.
Date: 2026-10-21

## Problem

Questions like "why is this crew getting slower?" need numbers that
`find` and `show` cannot give: how long tool calls take, how much time
goes to the model's turns versus tools, and how that changes over days
and by agent role. The swaywardlock gate analysis on 2026-10-21 took
throwaway Python scripts run over ssh on the store host, plus a lot of
guessing an agent's role from its first prompt. The same question about
mu will come up, and so will others.

## Approach

Two subcommands in `skills/museum/scripts/museum-search`, `stats` and
`calls`. They reuse
the config loading and the single-ssh-call remote path that `find` and
`show` already have, so it runs where the files are. Python standard
library only, like the rest of the script.

```
museum-search stats [PROJECT] [--since DATE] [--until DATE]
                    [CALL FILTERS] [--by day|6h|hour] [--slow N] [--json]
museum-search calls [PROJECT] [--since DATE] [--until DATE]
                    [CALL FILTERS] [--group-by session|agent|workstream|role]
                    [--sort secs|time] [--limit N] [--json]
```

`stats` gives the aggregate view, and `calls` drills into the calls
behind it. Both take the same call filters, so a filter tried in `calls`
can be pasted into `stats` unchanged.

- `PROJECT` narrows to store folders whose name contains it, as in
  `find`. The `museum` entry's `project` field also counts, so mu
  worktree folders are included.
- `CALL FILTERS` limit the tool-call figures to matching calls; see
  [Call filters](#call-filters).
- `--by` sets the time window for the trend table.
- `--slow N` lists the N slowest tool calls, with time, role, session
  file and the start of the command.
- `--json` prints the raw rows, so other analysis does not need new
  scripts.

### Call filters

These are the cuts the swaywardlock gate analysis had to write by hand
(`/tmp/gs3.py`). A call must pass every filter given.

| Filter | Selects | Why it is needed |
|---|---|---|
| `--match REGEX` | the command or arguments match | pick the calls in question, e.g. `contrib/gate\|cargo test` |
| `--exclude REGEX` | the command does **not** match | drop calls that only mention the term, e.g. `^\s*(cat\|sed\|grep\|rg\|head\|tail\|wc)\b` for gate scripts being read rather than run |
| `--tool NAME[,NAME]` | the tool name (`bash`, `codemode`, `read`, …) | separate shell work from file reads |
| `--role ROLE`, `--agent GLOB`, `--workstream GLOB` | the session's `museum` fields | e.g. workers only, or `swaywardlock-ut-*` |
| `--min-secs N`, `--max-secs N` | call duration | drop instant calls, or find the polls |
| `--result REGEX` | the tool result text matches | `gate: ok` vs `FAIL\|panicked`, to count passes and failures |
| `--error` | calls whose result is an error | failed tool calls only |
| `--background` / `--foreground` | the command backgrounds itself (`&` with a redirect) | a backgrounded gate returns at once, and its time shows up in later polls |

### `calls` output

With no `--group-by`, one row per call: start time, duration, tool, role,
agent, workstream, `ok`/`fail`/blank from `--result`, a background flag,
the session file, and the first 120 characters of the command. `--sort`
is by duration (the default) or by start time. `--limit` defaults to 50.

`--group-by` adds up the calls per session, agent, workstream or role:
count, total and median duration, passes/failures, and the first user
message of the session (for `session`). Grouping by session gives full
gate runs per task, because a worker or reviewer session covers one
task.

Each session row includes the file path, so the next step is
`museum-search show FILE TERM`.

## What it measures

Per session, from the timestamps of its entries:
- **model time:** from the previous entry to each assistant message
  (inference, including streaming)
- **tool time:** from each tool call to its matching tool result, per
  call
- **user wait:** from an assistant message that ended its turn to the
  next user message (excluded from the other two)
- the number of tool calls, and the calls the call filters selected,
  with their durations

## Output

Three plain-text tables:

1. **Totals by role:** sessions, tool calls, model hours, tool hours,
   share of the session that is tool time, and the count and hours of
   the calls the filters selected.
2. **Trend:** one row per window. Sessions, tool calls, matched calls,
   matched median and p90 duration, and the model-time / tool-time
   split.
3. **Slowest calls** (with `--slow`): the top rows of
   `calls --sort secs`, with the same columns, so `--slow` is a preview
   and `calls` does the drilling down.

The trend table also shows **max concurrent matched calls** for each
window: how many matched calls overlapped at their peak. Gates running
side by side on one machine slow each other down, so a rising duration
with a rising concurrency count tells a different story from a rising
duration alone.

## Decisions

- **Role comes from data, not guessing.** Use the `museum` entry's
  `agent` field (worker-N, reviewer-N, fixer-N, delegate-…) and
  `workstream` field. Sessions without one are grouped as `other`. The
  first-prompt heuristics from the throwaway scripts are not carried
  over.
- **Cheap enough for multi-GB stores.** Stream each file line by line.
  Look only at `session`, `custom/museum` and `message` entries, and
  keep only timestamps, roles, tool names and the first 300 characters
  of each command. Select files by folder and by start time (taken from
  the file name) before opening any.
- **A background run shows up as quick calls.** A gate started with `&`
  returns at once, and the time goes to the later calls that poll its
  log. `--match` catches those polls too when the pattern names the log,
  so this is documented rather than worked around.

## Testing

`test/test_search.py` style: small JSONL fixtures with known timestamps.
Check the model / tool / user-wait split, each call filter (including
`--exclude` against a read-only command that mentions the match term,
and `--background`), `calls --group-by`, the max concurrent count, the
window grouping, the role fallback, and that `--json` round-trips.

## Out of scope

Charts, an index or cache, and cost or token accounting (sessions do
carry `usage`, so token use could be added later).

## Rollout

Build it, then reproduce the swaywardlock numbers in
`mu/docs/specs/2026-10-21-fast-and-slow-gates.md` with one command per
table. The filter that makes `contrib/gate` count only real runs is long,
so it goes in a variable:

```
G='(distrobox enter|timeout [0-9]+) .*contrib/gate|(^|[;&(]) *contrib/gate'
R='^\s*(cat|sed|grep|rg|head|tail|wc)\b'
S=skills/museum/scripts/museum-search

# full gate runs and how long they take, per 6 hours
$S stats swayward --match "$G" --exclude "$R" --min-secs 30 --by 6h

# full gate runs per task, split by role
$S calls swayward --match "$G" --exclude "$R" --group-by session

# pass and fail counts
$S calls swayward --match "$G" --result 'gate: ok' --group-by role
$S calls swayward --match "$G" --result 'FAIL|panicked' --group-by role

# what workers spend polling background gates
$S calls swayward --role worker --match 'gate.*\.log' --group-by session
``` Any
mismatch is either a bug in the tool or a mistake in the spec's figures.
Then update the museum SKILL.md with a short "Stats" section.
